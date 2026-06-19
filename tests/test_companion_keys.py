"""Tests for the persistent companion signed-command key registry."""

from __future__ import annotations

import base64
import contextlib
import os
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from companion.keys import (
    delete_owner_device_keys,
    get_active_device_key,
    list_device_keys,
    prune_expired_nonces,
    register_device_key,
    revoke_device_key,
    serialize_device_key,
    verify_registered_signed_command,
)
from companion.routes import setup_companion_routes
from companion.signing import (
    SIGNED_COMMAND_VERSION,
    SignedCommandEnvelope,
    SignedCommandError,
    signing_payload,
)
from core.database import Base, CompanionCommandNonce, CompanionDeviceKey


NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)
TIMESTAMP = "2026-06-08T12:00:00Z"


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


def _private_key():
    return Ed25519PrivateKey.generate()


def _public_key_b64(private_key):
    raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def _signed_envelope(private_key, *, body=None, method="POST", path="/api/companion/control"):
    envelope = SignedCommandEnvelope(
        key_id="phone-1",
        timestamp=TIMESTAMP,
        nonce="nonce-1",
        signature="",
        version=SIGNED_COMMAND_VERSION,
    )
    signature = private_key.sign(signing_payload(method, path, body or {}, envelope))
    return SignedCommandEnvelope(
        key_id=envelope.key_id,
        timestamp=envelope.timestamp,
        nonce=envelope.nonce,
        signature=base64.b64encode(signature).decode("ascii"),
        version=envelope.version,
    )


def _request(**state):
    return SimpleNamespace(state=SimpleNamespace(**state))


def _route(method, path):
    for route in setup_companion_routes().routes:
        if getattr(route, "path", "") == path and method.upper() in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"{method} {path} route not found")


class _DBProxy:
    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def close(self):
        pass


def test_register_device_key_persists_owner_scoped_active_key(db_session):
    private_key = _private_key()

    row = register_device_key(
        db_session,
        owner="alice",
        key_id="phone-1",
        label="Alice phone",
        public_key_b64=_public_key_b64(private_key),
        scopes=["remote_development", "status"],
    )
    db_session.commit()

    persisted = (
        db_session.query(CompanionDeviceKey)
        .filter_by(owner="alice", key_id="phone-1")
        .one()
    )
    assert persisted.id == row.id
    assert persisted.owner == "alice"
    assert persisted.label == "Alice phone"
    assert persisted.algorithm == "ed25519"
    assert persisted.protocol_version == 1
    assert persisted.scopes == "remote_development,status"
    assert persisted.is_active is True
    assert get_active_device_key(db_session, owner="alice", key_id="phone-1").id == row.id

    with pytest.raises(SignedCommandError) as exc:
        get_active_device_key(db_session, owner="bob", key_id="phone-1")
    assert "approved" in str(exc.value)


def test_device_key_ids_are_unique_per_owner_not_global(db_session):
    register_device_key(
        db_session,
        owner="alice",
        key_id="phone-1",
        public_key_b64=_public_key_b64(_private_key()),
    )
    register_device_key(
        db_session,
        owner="bob",
        key_id="phone-1",
        public_key_b64=_public_key_b64(_private_key()),
    )
    db_session.commit()

    assert db_session.query(CompanionDeviceKey).count() == 2
    assert get_active_device_key(db_session, owner="alice", key_id="phone-1").owner == "alice"
    assert get_active_device_key(db_session, owner="bob", key_id="phone-1").owner == "bob"

    with pytest.raises(SignedCommandError) as exc:
        register_device_key(
            db_session,
            owner="alice",
            key_id="phone-1",
            public_key_b64=_public_key_b64(_private_key()),
        )
    assert "this owner" in str(exc.value)


def test_register_device_key_rejects_invalid_public_key(db_session):
    with pytest.raises(SignedCommandError) as exc:
        register_device_key(
            db_session,
            owner="alice",
            key_id="phone-1",
            public_key_b64="not-base64",
        )

    assert "base64" in str(exc.value)
    assert db_session.query(CompanionDeviceKey).count() == 0


def test_device_key_serialization_hides_public_key_blob(db_session):
    private_key = _private_key()
    public_key_b64 = _public_key_b64(private_key)
    row = register_device_key(
        db_session,
        owner="alice",
        key_id="phone-1",
        public_key_b64=public_key_b64,
    )
    db_session.commit()

    payload = serialize_device_key(row)

    assert payload["key_id"] == "phone-1"
    assert payload["public_key_sha256"]
    assert payload["scopes"] == ["remote_development"]
    assert "public_key_b64" not in payload
    assert public_key_b64 not in repr(payload)


def test_list_device_keys_returns_only_active_owner_keys(db_session):
    alice_key = _private_key()
    bob_key = _private_key()
    active = register_device_key(
        db_session,
        owner="alice",
        key_id="alice-active",
        public_key_b64=_public_key_b64(alice_key),
    )
    revoked = register_device_key(
        db_session,
        owner="alice",
        key_id="alice-revoked",
        public_key_b64=_public_key_b64(_private_key()),
    )
    register_device_key(
        db_session,
        owner="bob",
        key_id="bob-active",
        public_key_b64=_public_key_b64(bob_key),
    )
    revoked.is_active = False
    revoked.revoked_at = NOW.replace(tzinfo=None)
    db_session.commit()

    rows = list_device_keys(db_session, owner="alice")

    assert [row.key_id for row in rows] == [active.key_id]


def test_revoke_device_key_marks_inactive_and_removes_nonces(db_session):
    private_key = _private_key()
    register_device_key(
        db_session,
        owner="alice",
        key_id="phone-1",
        public_key_b64=_public_key_b64(private_key),
    )
    db_session.add(CompanionCommandNonce(
        owner="alice",
        key_id="phone-1",
        nonce="nonce-1",
        expires_at=(NOW + timedelta(minutes=5)).replace(tzinfo=None),
        created_at=NOW.replace(tzinfo=None),
    ))
    db_session.commit()

    row, removed_nonces = revoke_device_key(db_session, owner="alice", key_id="phone-1")
    db_session.commit()

    assert removed_nonces == 1
    assert row.is_active is False
    assert row.revoked_at is not None
    assert db_session.query(CompanionCommandNonce).count() == 0
    assert list_device_keys(db_session, owner="alice") == []


def test_verify_registered_signed_command_records_nonce_and_last_use(db_session):
    private_key = _private_key()
    body = {"command": "status", "args": {"tail": 20}}
    register_device_key(
        db_session,
        owner="alice",
        key_id="phone-1",
        label="Alice phone",
        public_key_b64=_public_key_b64(private_key),
    )
    db_session.commit()
    envelope = _signed_envelope(private_key, body=body)

    result = verify_registered_signed_command(
        db_session,
        owner="alice",
        method="POST",
        path="/api/companion/control",
        body=body,
        envelope=envelope,
        now=NOW,
    )
    db_session.commit()

    assert result.key_id == "phone-1"
    assert result.nonce == "nonce-1"
    stored_nonce = db_session.query(CompanionCommandNonce).one()
    assert stored_nonce.owner == "alice"
    assert stored_nonce.key_id == "phone-1"
    key = get_active_device_key(db_session, owner="alice", key_id="phone-1")
    assert key.last_used_at == NOW.replace(tzinfo=None)

    with pytest.raises(SignedCommandError) as exc:
        verify_registered_signed_command(
            db_session,
            owner="alice",
            method="POST",
            path="/api/companion/control",
            body=body,
            envelope=envelope,
            now=NOW,
        )
    assert "nonce" in str(exc.value)


def test_registered_command_nonces_are_owner_scoped(db_session):
    alice_key = _private_key()
    bob_key = _private_key()
    body = {"command": "status", "args": {"tail": 20}}
    register_device_key(
        db_session,
        owner="alice",
        key_id="phone-1",
        public_key_b64=_public_key_b64(alice_key),
    )
    register_device_key(
        db_session,
        owner="bob",
        key_id="phone-1",
        public_key_b64=_public_key_b64(bob_key),
    )
    db_session.commit()

    alice_envelope = _signed_envelope(alice_key, body=body)
    bob_envelope = _signed_envelope(bob_key, body=body)

    verify_registered_signed_command(
        db_session,
        owner="alice",
        method="POST",
        path="/api/companion/control",
        body=body,
        envelope=alice_envelope,
        now=NOW,
    )
    verify_registered_signed_command(
        db_session,
        owner="bob",
        method="POST",
        path="/api/companion/control",
        body=body,
        envelope=bob_envelope,
        now=NOW,
    )
    db_session.commit()

    rows = db_session.query(CompanionCommandNonce).order_by(CompanionCommandNonce.owner).all()
    assert [(row.owner, row.key_id, row.nonce) for row in rows] == [
        ("alice", "phone-1", "nonce-1"),
        ("bob", "phone-1", "nonce-1"),
    ]

    with pytest.raises(SignedCommandError) as exc:
        verify_registered_signed_command(
            db_session,
            owner="alice",
            method="POST",
            path="/api/companion/control",
            body=body,
            envelope=alice_envelope,
            now=NOW,
        )
    assert "nonce" in str(exc.value)


def test_replayed_nonce_does_not_rollback_caller_transaction(db_session):
    private_key = _private_key()
    body = {"command": "status", "args": {"tail": 20}}
    register_device_key(
        db_session,
        owner="alice",
        key_id="phone-1",
        label="Alice phone",
        public_key_b64=_public_key_b64(private_key),
    )
    db_session.commit()
    envelope = _signed_envelope(private_key, body=body)

    verify_registered_signed_command(
        db_session,
        owner="alice",
        method="POST",
        path="/api/companion/control",
        body=body,
        envelope=envelope,
        now=NOW,
    )
    db_session.commit()

    key = get_active_device_key(db_session, owner="alice", key_id="phone-1")
    key.label = "Updated label"
    with pytest.raises(SignedCommandError) as exc:
        verify_registered_signed_command(
            db_session,
            owner="alice",
            method="POST",
            path="/api/companion/control",
            body=body,
            envelope=envelope,
            now=NOW,
        )
    assert "nonce" in str(exc.value)
    db_session.commit()

    assert get_active_device_key(db_session, owner="alice", key_id="phone-1").label == "Updated label"


def test_verify_registered_signed_command_rejects_revoked_key_without_nonce(db_session):
    private_key = _private_key()
    row = register_device_key(
        db_session,
        owner="alice",
        key_id="phone-1",
        public_key_b64=_public_key_b64(private_key),
    )
    row.is_active = False
    row.revoked_at = NOW.replace(tzinfo=None)
    db_session.commit()

    with pytest.raises(SignedCommandError) as exc:
        verify_registered_signed_command(
            db_session,
            owner="alice",
            method="POST",
            path="/api/companion/control",
            body={},
            envelope=_signed_envelope(private_key),
            now=NOW,
        )

    assert "revoked" in str(exc.value)
    assert db_session.query(CompanionCommandNonce).count() == 0


def test_prune_expired_nonces_removes_only_old_rows(db_session):
    db_session.add(CompanionCommandNonce(
        owner="alice",
        key_id="phone-1",
        nonce="old",
        expires_at=(NOW - timedelta(seconds=1)).replace(tzinfo=None),
        created_at=(NOW - timedelta(minutes=10)).replace(tzinfo=None),
    ))
    db_session.add(CompanionCommandNonce(
        owner="alice",
        key_id="phone-1",
        nonce="fresh",
        expires_at=(NOW + timedelta(minutes=5)).replace(tzinfo=None),
        created_at=NOW.replace(tzinfo=None),
    ))
    db_session.commit()

    assert prune_expired_nonces(db_session, now=NOW) == 1
    db_session.commit()

    remaining = db_session.query(CompanionCommandNonce).one()
    assert remaining.nonce == "fresh"


def test_delete_owner_device_keys_removes_owner_keys_and_nonces_only(db_session):
    alice_key = _private_key()
    bob_key = _private_key()
    register_device_key(
        db_session,
        owner="alice",
        key_id="alice-phone",
        public_key_b64=_public_key_b64(alice_key),
    )
    register_device_key(
        db_session,
        owner="bob",
        key_id="bob-phone",
        public_key_b64=_public_key_b64(bob_key),
    )
    db_session.add(CompanionCommandNonce(
        owner="alice",
        key_id="alice-phone",
        nonce="alice-nonce",
        expires_at=(NOW + timedelta(minutes=5)).replace(tzinfo=None),
        created_at=NOW.replace(tzinfo=None),
    ))
    db_session.add(CompanionCommandNonce(
        owner="bob",
        key_id="bob-phone",
        nonce="bob-nonce",
        expires_at=(NOW + timedelta(minutes=5)).replace(tzinfo=None),
        created_at=NOW.replace(tzinfo=None),
    ))
    db_session.commit()

    assert delete_owner_device_keys(db_session, "alice") == (1, 1)
    db_session.commit()

    remaining_key = db_session.query(CompanionDeviceKey).one()
    remaining_nonce = db_session.query(CompanionCommandNonce).one()
    assert remaining_key.owner == "bob"
    assert remaining_nonce.owner == "bob"
    assert remaining_nonce.key_id == "bob-phone"


def test_auth_manager_delete_user_revokes_companion_device_keys(
    db_session,
    monkeypatch,
    tmp_path,
):
    import core.auth as auth_mod
    import core.database as cdb

    monkeypatch.setattr(auth_mod, "_hash_password", lambda password: f"hash:{password}")
    monkeypatch.setattr(
        auth_mod,
        "_verify_password",
        lambda password, hashed: hashed == f"hash:{password}",
    )

    @contextlib.contextmanager
    def _db_ctx():
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    monkeypatch.setattr(cdb, "get_db_session", _db_ctx)

    manager = auth_mod.AuthManager(str(tmp_path / "auth.json"))
    assert manager.create_user("admin", "secret-admin-pw", is_admin=True)
    assert manager.create_user("bob", "secret-bob-pw", is_admin=False)

    private_key = _private_key()
    register_device_key(
        db_session,
        owner="bob",
        key_id="bob-phone",
        public_key_b64=_public_key_b64(private_key),
    )
    db_session.add(CompanionCommandNonce(
        owner="bob",
        key_id="bob-phone",
        nonce="bob-nonce",
        expires_at=(NOW + timedelta(minutes=5)).replace(tzinfo=None),
        created_at=NOW.replace(tzinfo=None),
    ))
    db_session.commit()

    assert manager.delete_user("bob", "admin") is True

    assert db_session.query(CompanionDeviceKey).count() == 0
    assert db_session.query(CompanionCommandNonce).count() == 0


def test_companion_key_routes_register_list_and_revoke_for_token_owner(
    db_session,
    monkeypatch,
):
    import core.database as cdb

    monkeypatch.setattr(cdb, "SessionLocal", lambda: _DBProxy(db_session))
    private_key = _private_key()
    public_key_b64 = _public_key_b64(private_key)
    req = _request(
        api_token=True,
        api_token_owner="alice",
        api_token_scopes=["chat", "remote_development"],
        current_user="api",
    )

    register = _route("POST", "/api/companion/keys")
    created = register(
        req,
        body={
            "key_id": "phone-1",
            "label": "Alice phone",
            "public_key_b64": public_key_b64,
        },
    )

    assert created["key"]["key_id"] == "phone-1"
    assert created["key"]["label"] == "Alice phone"
    assert created["key"]["is_active"] is True
    assert "public_key_b64" not in created["key"]
    assert public_key_b64 not in repr(created)

    listed = _route("GET", "/api/companion/keys")(req)
    assert [item["key_id"] for item in listed["keys"]] == ["phone-1"]

    revoked = _route("DELETE", "/api/companion/keys/{key_id}")(req, "phone-1")
    assert revoked["status"] == "revoked"
    assert revoked["key"]["is_active"] is False

    listed_after_revoke = _route("GET", "/api/companion/keys")(req)
    assert listed_after_revoke["keys"] == []


def test_companion_key_routes_require_chat_scope(db_session, monkeypatch):
    import core.database as cdb

    monkeypatch.setattr(cdb, "SessionLocal", lambda: _DBProxy(db_session))
    req = _request(
        api_token=True,
        api_token_owner="alice",
        api_token_scopes=["memory:read"],
        current_user="api",
    )

    with pytest.raises(HTTPException) as exc:
        _route("POST", "/api/companion/keys")(
            req,
            body={"public_key_b64": _public_key_b64(_private_key())},
        )

    assert exc.value.status_code == 403


def test_companion_key_routes_require_remote_development_scope(db_session, monkeypatch):
    import core.database as cdb

    monkeypatch.setattr(cdb, "SessionLocal", lambda: _DBProxy(db_session))
    req = _request(
        api_token=True,
        api_token_owner="alice",
        api_token_scopes=["chat"],
        current_user="api",
    )

    with pytest.raises(HTTPException) as exc:
        _route("POST", "/api/companion/keys")(
            req,
            body={"public_key_b64": _public_key_b64(_private_key())},
        )

    assert exc.value.status_code == 403
    assert "remote_development" in exc.value.detail


def test_companion_key_routes_require_resolved_owner(db_session, monkeypatch):
    import core.database as cdb

    monkeypatch.setattr(cdb, "SessionLocal", lambda: _DBProxy(db_session))
    req = _request(
        api_token=True,
        api_token_owner=None,
        api_token_scopes=["chat", "remote_development"],
        current_user="api",
    )

    with pytest.raises(HTTPException) as exc:
        _route("GET", "/api/companion/keys")(req)

    assert exc.value.status_code == 401
