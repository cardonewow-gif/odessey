"""Owner-scope tests for the read-only companion bridge.

Mirrors the direct-helper style of tests/test_null_owner_gates.py: exercise the
small pure helpers against mock request state and owner values, so the scoping
rule can't silently regress. A bearer token for owner A must never see owner B's
rows, and legacy null-owner rows must not widen a token's access.
"""

import os
import sys
import types
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# core.database instantiates SQLAlchemy declarative classes at import time, which
# blows up under conftest's sqlalchemy MagicMock stubs. companion.routes imports
# core.middleware, which imports core/__init__ and can ask for extra ORM names
# during collection, so expose a resilient stub and pin only the model pieces
# this file asserts on.
if "core.database" not in sys.modules:
    class _DBStub(types.ModuleType):
        def __getattr__(self, name):  # noqa: D401
            if name.startswith("__"):
                raise AttributeError(name)
            return MagicMock()

    _db = _DBStub("core.database")
    _db.SessionLocal = MagicMock()
    _db.ModelEndpoint = MagicMock()
    sys.modules["core.database"] = _db

import companion.routes as companion_routes
from companion.routes import (
    companion_manifest,
    COMPANION_CONTRACT_VERSION,
    owner_can_see,
    require_companion_scope,
    setup_companion_routes,
    token_owner,
)


def _request(**state):
    return SimpleNamespace(state=SimpleNamespace(**state))


class _Predicate:
    def __init__(self, check):
        self._check = check

    def __call__(self, row):
        return self._check(row)

    def __or__(self, other):
        return _Predicate(lambda row: self(row) or other(row))


class _Column:
    def __init__(self, name):
        self.name = name

    def __eq__(self, value):  # noqa: D401
        return _Predicate(lambda row: getattr(row, self.name) == value)


class _ModelEndpoint:
    is_enabled = _Column("is_enabled")
    model_type = _Column("model_type")
    owner = _Column("owner")


class _Query:
    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, *predicates):
        self._rows = [
            row for row in self._rows
            if all(predicate(row) for predicate in predicates)
        ]
        return self

    def all(self):
        return list(self._rows)


class _DB:
    def __init__(self, rows):
        self._rows = rows
        self.closed = False

    def query(self, model):
        assert model is _ModelEndpoint
        return _Query(self._rows)

    def close(self):
        self.closed = True


def _ep(
    id,
    name,
    owner,
    *,
    is_enabled=True,
    model_type="llm",
    base_url=None,
    cached_models=None,
    hidden_models=None,
    supports_tools=False,
    api_key="secret-key",
):
    return SimpleNamespace(
        id=id,
        name=name,
        owner=owner,
        is_enabled=is_enabled,
        model_type=model_type,
        base_url=base_url or f"https://{name}.example/v1",
        cached_models=json.dumps(cached_models or [f"{name}-model"]),
        hidden_models=json.dumps(hidden_models or []),
        supports_tools=supports_tools,
        api_key=api_key,
        headers={"Authorization": "Bearer secret-header"},
    )


def _models_route():
    for route in setup_companion_routes().routes:
        if getattr(route, "path", "") == "/api/companion/models":
            assert "GET" in getattr(route, "methods", set())
            return route.endpoint
    raise AssertionError("GET /api/companion/models route not found")


def _route(path):
    for route in setup_companion_routes().routes:
        if getattr(route, "path", "") == path and "GET" in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"GET {path} route not found")


def _call_models_route(monkeypatch, rows, request):
    db = _DB(rows)
    db_mod = sys.modules["core.database"]
    monkeypatch.setattr(db_mod, "SessionLocal", lambda: db)
    monkeypatch.setattr(db_mod, "ModelEndpoint", _ModelEndpoint)

    endpoint_mod = sys.modules.get("src.endpoint_resolver")
    if endpoint_mod is None:
        endpoint_mod = types.ModuleType("src.endpoint_resolver")
        sys.modules["src.endpoint_resolver"] = endpoint_mod
    monkeypatch.setattr(
        endpoint_mod,
        "build_chat_url",
        lambda base_url: f"{base_url.rstrip('/')}/chat/completions",
        raising=False,
    )

    response = _models_route()(request)
    assert db.closed is True
    return response["endpoints"]


def _endpoint_names(endpoints):
    return [endpoint["name"] for endpoint in endpoints]


# --- token_owner: who a request is attributed to ---------------------------

def test_companion_scope_allows_cookie_sessions():
    # Cookie sessions are already app-authenticated; only bearer tokens need
    # integration-scope checks here.
    req = _request(api_token=False, current_user="alice")
    require_companion_scope(req)


def test_companion_scope_allows_chat_scoped_bearer_token():
    req = _request(api_token=True, api_token_scopes=["chat"], current_user="api")
    require_companion_scope(req)


def test_companion_scope_rejects_non_chat_bearer_token():
    req = _request(
        api_token=True,
        api_token_scopes=["todos:read", "documents:read"],
        current_user="api",
    )
    with pytest.raises(HTTPException) as exc:
        require_companion_scope(req)

    assert exc.value.status_code == 403
    assert "chat" in exc.value.detail


def test_companion_scope_rejects_unscoped_bearer_token():
    req = _request(api_token=True, current_user="api")
    with pytest.raises(HTTPException) as exc:
        require_companion_scope(req)

    assert exc.value.status_code == 403
    assert "chat" in exc.value.detail


def test_ping_route_requires_chat_scope_for_bearer_token():
    req = _request(api_token=True, api_token_scopes=["email:read"], current_user="api")

    with pytest.raises(HTTPException) as exc:
        _route("/api/companion/ping")(req)

    assert exc.value.status_code == 403


def test_info_route_requires_chat_scope_for_bearer_token():
    req = _request(api_token=True, api_token_scopes=["documents:read"], current_user="api")

    with pytest.raises(HTTPException) as exc:
        _route("/api/companion/info")(req)

    assert exc.value.status_code == 403


def test_info_route_advertises_manifest_contract(monkeypatch):
    monkeypatch.setattr(companion_routes, "get_current_user", lambda request: "alice")
    req = _request(api_token=False, current_user="alice")

    response = _route("/api/companion/info")(req)

    assert response["owner"] == "alice"
    assert response["client_contract"] == {
        "version": COMPANION_CONTRACT_VERSION,
        "manifest": "/api/companion/manifest",
    }


def test_companion_manifest_describes_private_mobile_contract(monkeypatch):
    import companion.commands as companion_commands

    monkeypatch.setattr(companion_routes, "get_current_user", lambda request: "api")
    monkeypatch.setattr(
        companion_commands,
        "companion_workspace_roots",
        lambda: ["/workspace/alpha", "/workspace/beta"],
    )
    req = _request(
        api_token=True,
        api_token_owner="alice",
        api_token_scopes=["chat"],
        current_user="api",
    )

    response = companion_manifest(req)

    assert response["name"] == "odysseus"
    assert response["contract_version"] == COMPANION_CONTRACT_VERSION
    assert response["owner"] == "alice"
    assert response["auth"]["mode"] == "token"
    assert response["auth"]["required_bearer_scope"] == "chat"
    assert response["auth"]["required_command_scope"] == "remote_development"
    assert response["auth"]["token_scopes"] == ["chat"]
    assert response["auth"]["pairing"] == {
        "method": "admin_cookie_post",
        "path": "/api/companion/pair",
        "payload_version": 1,
        "scopes": ["chat", "remote_development"],
    }
    assert response["transport"]["private_network_required"] is True
    assert response["transport"]["public_internet_supported"] is False
    assert response["transport"]["base_url"] is None
    assert "wireguard" in response["transport"]["recommended"]
    assert response["endpoints"]["manifest"] == {
        "method": "GET",
        "path": "/api/companion/manifest",
    }
    assert response["endpoints"]["commands"] == {
        "method": "POST",
        "path": "/api/companion/commands",
    }
    assert response["endpoints"]["sessions"] == {
        "method": "GET",
        "path": "/api/companion/sessions",
    }
    assert response["endpoints"]["create_session"] == {
        "method": "POST",
        "path": "/api/companion/sessions",
    }
    assert response["endpoints"]["chat_stream"] == {
        "method": "POST",
        "path": "/api/chat_stream",
    }
    assert response["endpoints"]["chat_resume"] == {
        "method": "GET",
        "path": "/api/chat/resume/{session_id}",
    }
    assert response["endpoints"]["chat_stop"] == {
        "method": "POST",
        "path": "/api/chat/stop/{session_id}",
    }
    assert response["endpoints"]["chat_stream_status"] == {
        "method": "GET",
        "path": "/api/chat/stream_status/{session_id}",
    }
    assert response["endpoints"]["start_goal"] == {
        "method": "POST",
        "path": "/api/companion/goals",
    }
    assert response["endpoints"]["goal_status"] == {
        "method": "GET",
        "path": "/api/companion/goals/{run_id}",
    }
    assert response["features"]["chat"] == {
        "available": True,
        "streaming": True,
        "stream_path": "/api/chat_stream",
        "resume_path": "/api/chat/resume/{session_id}",
        "stop_path": "/api/chat/stop/{session_id}",
        "status_path": "/api/chat/stream_status/{session_id}",
        "request_body": "multipart_form_data",
        "required_bearer_scope": "chat",
        "agent_bash_requires_remote_development": True,
    }
    assert response["features"]["sessions"] == {"list": True, "create": True}
    signed_commands = response["features"]["signed_commands"]
    assert signed_commands["status"] == "workspace_file_control_ready"
    assert signed_commands["enabled_routes"] == ["/api/companion/commands"]
    assert signed_commands["required_bearer_scope"] == "remote_development"
    assert signed_commands["protocol_version"] == 1
    assert signed_commands["algorithm"] == "ed25519"
    assert signed_commands["clock_skew_seconds"] == 300
    assert signed_commands["canonical_payload"] == "json_body_sha256_v1"
    assert signed_commands["headers"]["key_id"] == "X-Odysseus-Command-Key-Id"
    assert "workspace_status" in signed_commands["allowed_commands"]
    assert "edit_file" in signed_commands["allowed_commands"]
    assert "run_check" in signed_commands["allowed_commands"]
    command_catalog = {item["name"]: item for item in signed_commands["commands"]}
    assert set(command_catalog) == set(signed_commands["allowed_commands"])
    assert command_catalog["workspace_status"]["mode"] == "read_only"
    assert command_catalog["workspace_status"]["mutating"] is False
    assert command_catalog["workspace_status"]["args_schema"] == {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
    }
    assert command_catalog["read_file"]["mode"] == "workspace_read"
    assert command_catalog["read_file"]["requires_admin"] is True
    assert command_catalog["edit_file"]["mode"] == "workspace_edit"
    assert command_catalog["edit_file"]["mutating"] is True
    assert command_catalog["edit_file"]["requires_admin"] is True
    assert command_catalog["run_check"]["mode"] == "workspace_exec"
    assert command_catalog["run_check"]["mutating"] is False
    assert command_catalog["run_check"]["raw_shell"] is False
    assert command_catalog["run_check"]["allowed_checks"] == [
        "git_diff",
        "git_status",
        "py_compile",
        "pytest",
    ]
    assert signed_commands["raw_shell_enabled"] is False
    assert signed_commands["allowed_workspace_roots"] == [
        "/workspace/alpha",
        "/workspace/beta",
    ]
    assert signed_commands["mutating_commands_enabled"] is True
    assert signed_commands["mutating_commands"] == ["edit_file"]
    assert signed_commands["workspace_exec_enabled"] is True
    assert signed_commands["allowed_checks"] == [
        "git_diff",
        "git_status",
        "py_compile",
        "pytest",
    ]
    assert signed_commands["key_registry"] == {
        "status": "enrollment_ready",
        "approved_keys_required": True,
        "required_bearer_scope": "remote_development",
        "list_path": "/api/companion/keys",
        "register_path": "/api/companion/keys",
        "revoke_path": "/api/companion/keys/{key_id}",
        "public_key_table": "companion_device_keys",
        "nonce_table": "companion_command_nonces",
    }
    remote_dev = response["features"]["remote_development"]
    assert remote_dev["status"] == "signed_workspace_file_control_ready"
    assert remote_dev["host_control_enabled"] is False
    assert remote_dev["workspace_file_control_enabled"] is True
    assert remote_dev["read_only_commands_enabled"] is True
    assert remote_dev["mutating_commands_enabled"] is True
    assert remote_dev["workspace_exec_enabled"] is True
    assert remote_dev["allowed_workspace_roots"] == [
        "/workspace/alpha",
        "/workspace/beta",
    ]
    assert remote_dev["raw_shell_enabled"] is False
    assert remote_dev["agent_bash_enabled"] is True
    assert remote_dev["agent_bash_requires_remote_development"] is True
    assert remote_dev["chat_stream_path"] == "/api/chat_stream"
    assert remote_dev["command_path"] == "/api/companion/commands"
    assert remote_dev["required_bearer_scope"] == "remote_development"
    assert remote_dev["requires_signed_commands"] is True
    assert remote_dev["requires_replay_protection"] is True
    assert remote_dev["requires_admin_for_workspace_files"] is True
    assert "react_native" in remote_dev["intended_clients"]
    goal_runs = response["features"]["goal_runs"]
    assert goal_runs["status"] == "server_owned_loop_ready"
    assert goal_runs["available"] is True
    assert goal_runs["required_bearer_scope"] == "chat"
    assert goal_runs["requires_session_id"] is True
    assert goal_runs["allow_bash_requires_remote_development"] is True
    assert goal_runs["start_path"] == "/api/companion/goals"
    assert goal_runs["resume_path"] == "/api/companion/goals/{run_id}/resume"
    assert "GOAL_STATUS: complete" in goal_runs["completion_markers"]
    assert response["safety"]["host_control"] == "signed_workspace_file_control_enabled_raw_shell_disabled"
    assert response["safety"]["credential_authority"] == (
        "revocable_chat_and_remote_development_scoped_token"
    )


def test_manifest_route_allows_cookie_sessions(monkeypatch):
    monkeypatch.setattr(companion_routes, "get_current_user", lambda request: "alice")
    req = _request(api_token=False, current_user="alice")

    response = _route("/api/companion/manifest")(req)

    assert response["owner"] == "alice"
    assert response["auth"]["mode"] == "session"
    assert response["auth"]["token_scopes"] == []


def test_manifest_route_requires_chat_scope_for_bearer_token():
    req = _request(api_token=True, api_token_scopes=["memory:read"], current_user="api")

    with pytest.raises(HTTPException) as exc:
        _route("/api/companion/manifest")(req)

    assert exc.value.status_code == 403


def test_token_owner_bearer_resolves_to_token_owner():
    # A paired bearer caller runs as the "api" pseudo-user, but must attribute
    # to the token's real owner.
    req = _request(api_token=True, api_token_owner="alice", current_user="api")
    assert token_owner(req) == "alice"


def test_token_owner_cookie_uses_logged_in_user():
    req = _request(api_token=False, current_user="alice")
    assert token_owner(req) == "alice"


def test_token_owner_none_when_unresolved():
    req = _request(api_token=True, api_token_owner=None, current_user="api")
    assert token_owner(req) is None


# --- owner_can_see: the read-scope rule ------------------------------------

def test_owner_sees_their_own_rows():
    assert owner_can_see("alice", "alice") is True


def test_null_owner_shared_rows_are_visible():
    # Legacy shared rows (owner is None) are visible to everyone by design...
    assert owner_can_see(None, "alice") is True


def test_null_owner_does_not_widen_access_to_others_rows():
    # ...but a null-owner row must not be a backdoor to another OWNER's rows.
    assert owner_can_see("bob", "alice") is False


def test_cross_owner_is_blocked():
    assert owner_can_see("bob", "alice") is False
    assert owner_can_see("alice", "bob") is False


def test_unauthenticated_owner_sees_only_shared_rows():
    # owner=None (no resolved caller): only null-owner shared rows are visible,
    # never any owned row.
    assert owner_can_see(None, None) is True
    assert owner_can_see("alice", None) is False


# --- GET /api/companion/models: route-level scoping -----------------------

def test_models_route_scopes_cookie_user_to_owned_and_shared_rows(monkeypatch):
    rows = [
        _ep(1, "alice-endpoint", "alice"),
        _ep(2, "shared-endpoint", None),
        _ep(3, "bob-endpoint", "bob"),
    ]
    monkeypatch.setattr(companion_routes, "get_current_user", lambda request: "alice")

    endpoints = _call_models_route(
        monkeypatch,
        rows,
        _request(api_token=False, current_user="ignored"),
    )

    assert _endpoint_names(endpoints) == ["alice-endpoint", "shared-endpoint"]


def test_models_route_scopes_api_token_to_token_owner(monkeypatch):
    rows = [
        _ep(1, "alice-endpoint", "alice"),
        _ep(2, "shared-endpoint", None),
        _ep(3, "bob-endpoint", "bob"),
    ]
    monkeypatch.setattr(companion_routes, "get_current_user", lambda request: "api")

    endpoints = _call_models_route(
        monkeypatch,
        rows,
        _request(
            api_token=True,
            api_token_owner="alice",
            api_token_scopes=["chat"],
            current_user="api",
        ),
    )

    assert _endpoint_names(endpoints) == ["alice-endpoint", "shared-endpoint"]


def test_models_route_rejects_api_token_without_chat_scope(monkeypatch):
    monkeypatch.setattr(companion_routes, "get_current_user", lambda request: "api")

    with pytest.raises(HTTPException) as exc:
        _models_route()(
            _request(
                api_token=True,
                api_token_owner="alice",
                api_token_scopes=["todos:read"],
                current_user="api",
            )
        )

    assert exc.value.status_code == 403
    assert "chat scope" in exc.value.detail


def test_models_route_unresolved_owner_returns_only_shared_rows(monkeypatch):
    rows = [
        _ep(1, "alice-endpoint", "alice"),
        _ep(2, "shared-endpoint", None),
        _ep(3, "bob-endpoint", "bob"),
    ]
    monkeypatch.setattr(companion_routes, "get_current_user", lambda request: None)

    endpoints = _call_models_route(
        monkeypatch,
        rows,
        _request(
            api_token=True,
            api_token_owner=None,
            api_token_scopes=["chat"],
            current_user="api",
        ),
    )

    assert _endpoint_names(endpoints) == ["shared-endpoint"]


def test_models_route_requires_chat_scope_for_bearer_token(monkeypatch):
    rows = [
        _ep(1, "alice-endpoint", "alice"),
        _ep(2, "shared-endpoint", None),
    ]
    monkeypatch.setattr(companion_routes, "get_current_user", lambda request: "api")

    with pytest.raises(HTTPException) as exc:
        _call_models_route(
            monkeypatch,
            rows,
            _request(
                api_token=True,
                api_token_owner="alice",
                api_token_scopes=["todos:read"],
                current_user="api",
            ),
        )

    assert exc.value.status_code == 403


def test_models_route_filters_hidden_models_and_secret_fields(monkeypatch):
    rows = [
        _ep(
            1,
            "alice-endpoint",
            "alice",
            base_url="https://alice.example/v1",
            cached_models=["visible-model", "hidden-model"],
            hidden_models=["hidden-model"],
            supports_tools=True,
            api_key="super-secret",
        ),
    ]
    monkeypatch.setattr(companion_routes, "get_current_user", lambda request: "alice")

    endpoints = _call_models_route(
        monkeypatch,
        rows,
        _request(api_token=False, current_user="alice"),
    )

    assert endpoints == [{
        "endpoint_id": 1,
        "name": "alice-endpoint",
        "endpoint_url": "https://alice.example/v1/chat/completions",
        "models": ["visible-model"],
        "supports_tools": True,
    }]
    returned = endpoints[0]
    assert "hidden-model" not in returned["models"]
    assert set(returned) == {
        "endpoint_id",
        "name",
        "endpoint_url",
        "models",
        "supports_tools",
    }
    assert "api_key" not in returned
    assert "headers" not in returned
    assert "base_url" not in returned
    assert "super-secret" not in repr(returned)


def test_models_route_tolerates_invalid_cached_models_json(monkeypatch):
    endpoint = _ep(1, "alice-endpoint", "alice")
    endpoint.cached_models = "{not-json"
    rows = [endpoint]
    monkeypatch.setattr(companion_routes, "get_current_user", lambda request: "alice")

    endpoints = _call_models_route(
        monkeypatch,
        rows,
        _request(api_token=False, current_user="alice"),
    )

    assert len(endpoints) == 1
    returned = endpoints[0]
    assert returned["name"] == "alice-endpoint"
    assert returned["models"] == []
    assert "api_key" not in returned
    assert "headers" not in returned
    assert "base_url" not in returned


def test_models_route_tolerates_invalid_hidden_models_json(monkeypatch):
    endpoint = _ep(
        1,
        "alice-endpoint",
        "alice",
        cached_models=["visible-model"],
    )
    endpoint.hidden_models = "{not-json"
    rows = [endpoint]
    monkeypatch.setattr(companion_routes, "get_current_user", lambda request: "alice")

    endpoints = _call_models_route(
        monkeypatch,
        rows,
        _request(api_token=False, current_user="alice"),
    )

    assert len(endpoints) == 1
    returned = endpoints[0]
    assert returned["name"] == "alice-endpoint"
    assert returned["models"] == ["visible-model"]
    assert "api_key" not in returned
    assert "headers" not in returned
    assert "base_url" not in returned


def test_models_route_filters_disabled_and_non_llm_endpoints(monkeypatch):
    rows = [
        _ep(1, "enabled-llm", "alice", is_enabled=True, model_type="llm"),
        _ep(2, "legacy-null-type", "alice", is_enabled=True, model_type=None),
        _ep(3, "disabled-llm", "alice", is_enabled=False, model_type="llm"),
        _ep(4, "image-endpoint", "alice", is_enabled=True, model_type="image"),
    ]
    monkeypatch.setattr(companion_routes, "get_current_user", lambda request: "alice")

    endpoints = _call_models_route(
        monkeypatch,
        rows,
        _request(api_token=False, current_user="alice"),
    )

    assert _endpoint_names(endpoints) == ["enabled-llm", "legacy-null-type"]


def test_models_route_returns_built_chat_url(monkeypatch):
    rows = [
        _ep(1, "alice-endpoint", "alice", base_url="https://raw.example/v1"),
    ]
    monkeypatch.setattr(companion_routes, "get_current_user", lambda request: "alice")

    endpoints = _call_models_route(
        monkeypatch,
        rows,
        _request(api_token=False, current_user="alice"),
    )

    assert endpoints[0]["endpoint_url"] == "https://raw.example/v1/chat/completions"
    assert endpoints[0]["endpoint_url"] != "https://raw.example/v1"
