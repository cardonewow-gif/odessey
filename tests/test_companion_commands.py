"""Tests for signed read-only companion command routes."""

from __future__ import annotations

import base64
import os
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from companion.commands import (
    CompanionCommandError,
    CompanionCommandForbidden,
    command_definitions,
    execute_companion_command,
)
from companion.keys import register_device_key
from companion.routes import setup_companion_routes
from companion.signing import (
    SIGNED_COMMAND_HEADERS,
    SIGNED_COMMAND_VERSION,
    SignedCommandEnvelope,
    signing_payload,
)
from core.database import Base, CompanionCommandNonce


COMMAND_PATH = "/api/companion/commands"


@pytest.fixture
def db_session():
    tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    engine = create_engine(
        f"sqlite:///{tmpfile.name}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()
        tmpfile.close()
        try:
            os.unlink(tmpfile.name)
        except OSError:
            pass


class _DBProxy:
    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def close(self):
        pass


def _private_key():
    return Ed25519PrivateKey.generate()


def _public_key_b64(private_key):
    raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def _headers(private_key, *, body, nonce="nonce-1", key_id="phone-1"):
    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    envelope = SignedCommandEnvelope(
        key_id=key_id,
        timestamp=timestamp,
        nonce=nonce,
        signature="",
        version=SIGNED_COMMAND_VERSION,
    )
    signature = private_key.sign(signing_payload("POST", COMMAND_PATH, body, envelope))
    return {
        SIGNED_COMMAND_HEADERS["version"]: str(SIGNED_COMMAND_VERSION),
        SIGNED_COMMAND_HEADERS["key_id"]: key_id,
        SIGNED_COMMAND_HEADERS["timestamp"]: timestamp,
        SIGNED_COMMAND_HEADERS["nonce"]: nonce,
        SIGNED_COMMAND_HEADERS["signature"]: base64.b64encode(signature).decode("ascii"),
    }


def _request(headers):
    return SimpleNamespace(
        state=SimpleNamespace(
            api_token=True,
            api_token_owner="alice",
            api_token_scopes=["chat", "remote_development"],
            current_user="api",
        ),
        headers=headers,
        method="POST",
        url=SimpleNamespace(path=COMMAND_PATH),
    )


def _route():
    for route in setup_companion_routes().routes:
        if getattr(route, "path", "") == COMMAND_PATH and "POST" in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError("POST /api/companion/commands route not found")


def test_execute_companion_command_capabilities_reports_workspace_control():
    result = execute_companion_command(owner="alice", body={"command": "capabilities"})

    assert result["command"] == "capabilities"
    assert result["mode"] == "read_only"
    assert result["mutating"] is False
    assert result["result"]["raw_shell_enabled"] is False
    assert result["result"]["mutating_commands_enabled"] is True
    assert "workspace_status" in result["result"]["allowed_commands"]
    assert "edit_file" in result["result"]["mutating_commands"]
    assert result["result"]["workspace_exec_enabled"] is True
    assert "py_compile" in result["result"]["allowed_checks"]
    command_catalog = {item["name"]: item for item in result["result"]["commands"]}
    assert command_catalog["workspace_status"]["args_schema"] == {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
    }


def test_command_definitions_publish_workspace_file_controls():
    definitions = command_definitions()
    by_name = {item["name"]: item for item in definitions}

    assert set(by_name) == {
        "capabilities",
        "edit_file",
        "git_status",
        "list_files",
        "read_file",
        "run_check",
        "server_status",
        "workspace_status",
    }
    assert by_name["read_file"]["mode"] == "workspace_read"
    assert by_name["read_file"]["requires_admin"] is True
    assert by_name["edit_file"]["mode"] == "workspace_edit"
    assert by_name["edit_file"]["mutating"] is True
    assert by_name["edit_file"]["requires_admin"] is True
    assert by_name["edit_file"]["args_schema"]["required"] == [
        "workspace",
        "path",
        "old_string",
        "new_string",
    ]
    assert by_name["run_check"]["mode"] == "workspace_exec"
    assert by_name["run_check"]["mutating"] is False
    assert by_name["run_check"]["requires_admin"] is True
    assert by_name["run_check"]["raw_shell"] is False
    assert by_name["run_check"]["allowed_checks"] == [
        "git_diff",
        "git_status",
        "py_compile",
        "pytest",
    ]


def test_execute_companion_command_rejects_unknown_command():
    with pytest.raises(CompanionCommandError) as exc:
        execute_companion_command(owner="alice", body={"command": "shell", "args": {}})

    assert "not allowed" in str(exc.value)


def test_execute_companion_command_rejects_unexpected_args():
    with pytest.raises(CompanionCommandError) as exc:
        execute_companion_command(
            owner="alice",
            body={"command": "workspace_status", "args": {"shell": "pwd"}},
        )

    assert "args" in str(exc.value)


def test_workspace_file_commands_require_admin(monkeypatch, tmp_path):
    import src.tool_security as tool_security

    monkeypatch.setattr(tool_security, "owner_is_admin_or_single_user", lambda owner: False)

    with pytest.raises(CompanionCommandForbidden):
        execute_companion_command(
            owner="alice",
            body={
                "command": "read_file",
                "args": {"workspace": str(tmp_path), "path": "note.txt"},
            },
        )


def test_workspace_file_commands_are_confined_and_editable(monkeypatch, tmp_path):
    import src.tool_security as tool_security

    monkeypatch.setattr(tool_security, "owner_is_admin_or_single_user", lambda owner: True)
    (tmp_path / "src").mkdir()
    note = tmp_path / "src" / "note.txt"
    note.write_text("hello mobile\n", encoding="utf-8")
    (tmp_path / ".env").write_text("secret=1", encoding="utf-8")

    listed = execute_companion_command(
        owner="alice",
        body={
            "command": "list_files",
            "args": {"workspace": str(tmp_path), "path": ".", "max_entries": 20},
        },
    )
    names = {entry["name"] for entry in listed["result"]["entries"]}
    assert "src" in names
    assert ".env" not in names

    read = execute_companion_command(
        owner="alice",
        body={
            "command": "read_file",
            "args": {"workspace": str(tmp_path), "path": "src/note.txt"},
        },
    )
    assert read["mode"] == "workspace_read"
    assert read["result"]["content"] == "hello mobile\n"
    assert read["result"]["path"] == os.path.join("src", "note.txt")

    edited = execute_companion_command(
        owner="alice",
        body={
            "command": "edit_file",
            "args": {
                "workspace": str(tmp_path),
                "path": "src/note.txt",
                "old_string": "hello",
                "new_string": "hi",
            },
        },
    )
    assert edited["mode"] == "workspace_edit"
    assert edited["mutating"] is True
    assert edited["result"]["replacements"] == 1
    assert edited["result"]["diff"]["added"] == 1
    assert note.read_text(encoding="utf-8") == "hi mobile\n"


def test_workspace_file_commands_reject_escape(monkeypatch, tmp_path):
    import src.tool_security as tool_security

    monkeypatch.setattr(tool_security, "owner_is_admin_or_single_user", lambda owner: True)
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("nope", encoding="utf-8")
    try:
        with pytest.raises(CompanionCommandError) as exc:
            execute_companion_command(
                owner="alice",
                body={
                    "command": "read_file",
                    "args": {"workspace": str(tmp_path), "path": str(outside)},
                },
            )
        assert "outside the workspace" in str(exc.value)
    finally:
        outside.unlink(missing_ok=True)


def test_workspace_file_commands_reject_unconfigured_workspace(monkeypatch, tmp_path):
    import companion.commands as command_mod
    import src.tool_security as tool_security

    allowed = tmp_path / "allowed"
    blocked = tmp_path / "blocked"
    allowed.mkdir()
    blocked.mkdir()
    (blocked / "note.txt").write_text("nope\n", encoding="utf-8")
    monkeypatch.setattr(tool_security, "owner_is_admin_or_single_user", lambda owner: True)
    monkeypatch.setattr(command_mod, "companion_workspace_roots", lambda: [str(allowed)])

    with pytest.raises(CompanionCommandError) as exc:
        execute_companion_command(
            owner="alice",
            body={
                "command": "read_file",
                "args": {"workspace": str(blocked), "path": "note.txt"},
            },
        )

    assert "configured companion roots" in str(exc.value)


def test_run_check_requires_admin(monkeypatch, tmp_path):
    import src.tool_security as tool_security

    monkeypatch.setattr(tool_security, "owner_is_admin_or_single_user", lambda owner: False)

    with pytest.raises(CompanionCommandForbidden):
        execute_companion_command(
            owner="alice",
            body={
                "command": "run_check",
                "args": {
                    "workspace": str(tmp_path),
                    "check": "git_status",
                },
            },
        )


def test_run_check_executes_allowlisted_py_compile(monkeypatch, tmp_path):
    import src.tool_security as tool_security

    monkeypatch.setattr(tool_security, "owner_is_admin_or_single_user", lambda owner: True)
    module_path = tmp_path / "ok.py"
    module_path.write_text("VALUE = 1\n", encoding="utf-8")

    result = execute_companion_command(
        owner="alice",
        body={
            "command": "run_check",
            "args": {
                "workspace": str(tmp_path),
                "check": "py_compile",
                "targets": ["ok.py"],
                "timeout_seconds": 10,
            },
        },
    )

    assert result["mode"] == "workspace_exec"
    assert result["mutating"] is False
    assert result["result"]["check"] == "py_compile"
    assert result["result"]["targets"] == ["ok.py"]
    assert result["result"]["exit_code"] == 0
    assert result["result"]["timed_out"] is False
    assert result["result"]["argv"][1:3] == ["-m", "py_compile"]


def test_run_check_rejects_unlisted_check_and_target_escape(monkeypatch, tmp_path):
    import src.tool_security as tool_security

    monkeypatch.setattr(tool_security, "owner_is_admin_or_single_user", lambda owner: True)
    outside = tmp_path.parent / f"{tmp_path.name}-outside.py"
    outside.write_text("VALUE = 1\n", encoding="utf-8")
    try:
        with pytest.raises(CompanionCommandError) as check_exc:
            execute_companion_command(
                owner="alice",
                body={
                    "command": "run_check",
                    "args": {
                        "workspace": str(tmp_path),
                        "check": "shell",
                    },
                },
            )
        assert "not allowed" in str(check_exc.value)

        with pytest.raises(CompanionCommandError) as escape_exc:
            execute_companion_command(
                owner="alice",
                body={
                    "command": "run_check",
                    "args": {
                        "workspace": str(tmp_path),
                        "check": "py_compile",
                        "targets": [str(outside)],
                    },
                },
            )
        assert "outside the workspace" in str(escape_exc.value)
    finally:
        outside.unlink(missing_ok=True)


def test_signed_companion_command_route_verifies_registered_key_and_records_nonce(
    db_session,
    monkeypatch,
):
    import core.database as cdb

    monkeypatch.setattr(cdb, "SessionLocal", lambda: _DBProxy(db_session))
    private_key = _private_key()
    register_device_key(
        db_session,
        owner="alice",
        key_id="phone-1",
        public_key_b64=_public_key_b64(private_key),
    )
    db_session.commit()
    body = {"command": "capabilities", "args": {}}

    response = _route()(_request(_headers(private_key, body=body)), body=body)

    assert response["ok"] is True
    assert response["verified"]["key_id"] == "phone-1"
    assert response["verified"]["body_sha256"]
    assert response["command"]["command"] == "capabilities"
    assert response["command"]["mutating"] is False
    assert db_session.query(CompanionCommandNonce).count() == 1


def test_signed_companion_command_route_rejects_replayed_nonce(db_session, monkeypatch):
    import core.database as cdb

    monkeypatch.setattr(cdb, "SessionLocal", lambda: _DBProxy(db_session))
    private_key = _private_key()
    register_device_key(
        db_session,
        owner="alice",
        key_id="phone-1",
        public_key_b64=_public_key_b64(private_key),
    )
    db_session.commit()
    body = {"command": "capabilities", "args": {}}
    headers = _headers(private_key, body=body)
    route = _route()

    route(_request(headers), body=body)
    with pytest.raises(HTTPException) as exc:
        route(_request(headers), body=body)

    assert exc.value.status_code == 401
    assert "nonce" in exc.value.detail


def test_signed_companion_command_route_rejects_unsigned_request(db_session, monkeypatch):
    import core.database as cdb

    monkeypatch.setattr(cdb, "SessionLocal", lambda: _DBProxy(db_session))
    body = {"command": "capabilities", "args": {}}

    with pytest.raises(HTTPException) as exc:
        _route()(_request({}), body=body)

    assert exc.value.status_code == 401


def test_signed_companion_command_route_requires_remote_development_scope(
    db_session,
    monkeypatch,
):
    import core.database as cdb

    monkeypatch.setattr(cdb, "SessionLocal", lambda: _DBProxy(db_session))
    private_key = _private_key()
    register_device_key(
        db_session,
        owner="alice",
        key_id="phone-1",
        public_key_b64=_public_key_b64(private_key),
    )
    db_session.commit()
    body = {"command": "capabilities", "args": {}}
    req = _request(_headers(private_key, body=body))
    req.state.api_token_scopes = ["chat"]

    with pytest.raises(HTTPException) as exc:
        _route()(req, body=body)

    assert exc.value.status_code == 403
    assert "remote_development" in exc.value.detail


def test_signed_companion_command_route_rejects_non_admin_workspace_file_command(
    db_session,
    monkeypatch,
    tmp_path,
):
    import core.database as cdb
    import src.tool_security as tool_security

    monkeypatch.setattr(cdb, "SessionLocal", lambda: _DBProxy(db_session))
    monkeypatch.setattr(tool_security, "owner_is_admin_or_single_user", lambda owner: False)
    private_key = _private_key()
    register_device_key(
        db_session,
        owner="alice",
        key_id="phone-1",
        public_key_b64=_public_key_b64(private_key),
    )
    db_session.commit()
    body = {
        "command": "list_files",
        "args": {"workspace": str(tmp_path), "path": "."},
    }

    with pytest.raises(HTTPException) as exc:
        _route()(_request(_headers(private_key, body=body)), body=body)

    assert exc.value.status_code == 403
    assert "admin" in exc.value.detail.lower()
