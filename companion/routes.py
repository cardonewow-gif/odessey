"""Companion bridge — /api/companion/*.

A thin, additive layer so a LAN client (e.g. a phone) can discover what a server
offers and pair to it, without duplicating any LLM logic.

API token. The read endpoints (ping/info/models) accept either; the pairing
endpoints are admin-cookie only. Bearer-token callers must carry the `chat`
scope for chat/session/model work, and `remote_development` for key enrollment
or signed commands. Companion pairing mints both scopes by default, and
unrelated integration tokens (todos/email/documents/etc.) must not be accepted
just because they authenticate as an owner.

Pairing CSRF posture: minting happens ONLY on POST. The session cookie is
SameSite=Lax (routes/auth_routes.py), which a browser does not send on a
cross-site POST, so an admin's cookie can't be used by a malicious page to mint
a token -- the same protection the existing POST /api/tokens relies on. Minting
on a GET would be unsafe (Lax cookies ride top-level GET navigations), so GET
/pair only renders a form.
"""

import html
import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import HTMLResponse

from core.middleware import require_admin
from src.auth_helpers import get_current_user

from companion import pairing as _pairing
from companion.signing import (
    MAX_CLOCK_SKEW_SECONDS,
    SIGNED_COMMAND_ALGORITHM,
    SIGNED_COMMAND_HEADERS,
    SIGNED_COMMAND_VERSION,
)

COMPANION_CONTRACT_VERSION = 1


def require_companion_scope(request: Request) -> None:
    """Require the companion access scope for bearer-token callers.

    Cookie-session users are already authenticated by the normal app session and
    can use read endpoints as before. Bearer tokens are integration credentials,
    so companion routes should only accept tokens minted for chat/companion use.
    """
    if not getattr(request.state, "api_token", False):
        return
    scopes = set(getattr(request.state, "api_token_scopes", []) or [])
    if "chat" not in scopes:
        raise HTTPException(403, "API token missing required chat scope")


def require_remote_development_scope(request: Request) -> None:
    """Require explicit approval for signed command/key enrollment routes."""
    require_companion_scope(request)
    if not getattr(request.state, "api_token", False):
        require_admin(request)
        return
    scopes = set(getattr(request.state, "api_token_scopes", []) or [])
    required = _pairing.REMOTE_DEVELOPMENT_SCOPE
    if required not in scopes:
        raise HTTPException(
            403,
            f"API token missing required scope: {required}",
        )


def companion_manifest(request: Request) -> dict:
    """Stable discovery contract for thin mobile/PWA companion clients.

    The manifest is intentionally conservative: it advertises the private
    network + scoped-token + key-enrollment foundation that exists today, and
    it explicitly marks raw shell control as disabled while owner-approved
    signed workspace file commands provide the first code-development control
    surface.
    """
    from core.constants import APP_VERSION
    from companion.commands import command_definitions, companion_workspace_roots

    api_token = bool(getattr(request.state, "api_token", False))
    token_scopes = sorted(set(getattr(request.state, "api_token_scopes", []) or []))
    command_catalog = command_definitions()
    workspace_roots = companion_workspace_roots()
    return {
        "name": "odysseus",
        "version": APP_VERSION,
        "contract_version": COMPANION_CONTRACT_VERSION,
        "owner": token_owner(request),
        "auth": {
            "mode": "token" if api_token else "session",
            "required_bearer_scope": "chat",
            "required_command_scope": _pairing.REMOTE_DEVELOPMENT_SCOPE,
            "token_scopes": token_scopes if api_token else [],
            "pairing": {
                "method": "admin_cookie_post",
                "path": "/api/companion/pair",
                "payload_version": _pairing.PAIRING_VERSION,
                "scopes": list(_pairing.COMPANION_SCOPES),
            },
        },
        "transport": {
            "private_network_required": True,
            "public_internet_supported": False,
            "base_url": _pairing.configured_base_url(),
            "recommended": [
                "lan",
                "tailscale",
                "wireguard",
                "https_reverse_proxy",
            ],
        },
        "endpoints": {
            "ping": {"method": "GET", "path": "/api/companion/ping"},
            "info": {"method": "GET", "path": "/api/companion/info"},
            "manifest": {"method": "GET", "path": "/api/companion/manifest"},
            "models": {"method": "GET", "path": "/api/companion/models"},
            "sessions": {"method": "GET", "path": "/api/companion/sessions"},
            "create_session": {"method": "POST", "path": "/api/companion/sessions"},
            "chat_stream": {"method": "POST", "path": "/api/chat_stream"},
            "chat_resume": {"method": "GET", "path": "/api/chat/resume/{session_id}"},
            "chat_stop": {"method": "POST", "path": "/api/chat/stop/{session_id}"},
            "chat_stream_status": {
                "method": "GET",
                "path": "/api/chat/stream_status/{session_id}",
            },
            "keys": {"method": "GET", "path": "/api/companion/keys"},
            "register_key": {"method": "POST", "path": "/api/companion/keys"},
            "revoke_key": {"method": "DELETE", "path": "/api/companion/keys/{key_id}"},
            "commands": {"method": "POST", "path": "/api/companion/commands"},
            "goals": {"method": "GET", "path": "/api/companion/goals"},
            "start_goal": {"method": "POST", "path": "/api/companion/goals"},
            "goal_status": {
                "method": "GET",
                "path": "/api/companion/goals/{run_id}",
            },
            "resume_goal": {
                "method": "POST",
                "path": "/api/companion/goals/{run_id}/resume",
            },
            "stop_goal": {
                "method": "POST",
                "path": "/api/companion/goals/{run_id}/stop",
            },
        },
        "features": {
            "chat": {
                "available": True,
                "streaming": True,
                "stream_path": "/api/chat_stream",
                "resume_path": "/api/chat/resume/{session_id}",
                "stop_path": "/api/chat/stop/{session_id}",
                "status_path": "/api/chat/stream_status/{session_id}",
                "request_body": "multipart_form_data",
                "required_bearer_scope": "chat",
                "agent_bash_requires_remote_development": True,
            },
            "sessions": {"list": True, "create": True},
            "models": {"read": True},
            "signed_commands": {
                "status": "workspace_file_control_ready",
                "enabled_routes": ["/api/companion/commands"],
                "required_bearer_scope": _pairing.REMOTE_DEVELOPMENT_SCOPE,
                "protocol_version": SIGNED_COMMAND_VERSION,
                "algorithm": SIGNED_COMMAND_ALGORITHM,
                "headers": SIGNED_COMMAND_HEADERS,
                "clock_skew_seconds": MAX_CLOCK_SKEW_SECONDS,
                "canonical_payload": "json_body_sha256_v1",
                "allowed_commands": [item["name"] for item in command_catalog],
                "commands": command_catalog,
                "allowed_workspace_roots": workspace_roots,
                "raw_shell_enabled": False,
                "mutating_commands_enabled": any(
                    bool(item.get("mutating")) for item in command_catalog
                ),
                "mutating_commands": [
                    item["name"] for item in command_catalog if item.get("mutating")
                ],
                "workspace_exec_enabled": any(
                    item.get("mode") == "workspace_exec" for item in command_catalog
                ),
                "allowed_checks": next(
                    (
                        item.get("allowed_checks", [])
                        for item in command_catalog
                        if item.get("name") == "run_check"
                    ),
                    [],
                ),
                "key_registry": {
                    "status": "enrollment_ready",
                    "approved_keys_required": True,
                    "required_bearer_scope": _pairing.REMOTE_DEVELOPMENT_SCOPE,
                    "list_path": "/api/companion/keys",
                    "register_path": "/api/companion/keys",
                    "revoke_path": "/api/companion/keys/{key_id}",
                    "public_key_table": "companion_device_keys",
                    "nonce_table": "companion_command_nonces",
                },
            },
            "remote_development": {
                "status": "signed_workspace_file_control_ready",
                "host_control_enabled": False,
                "workspace_file_control_enabled": True,
                "read_only_commands_enabled": True,
                "mutating_commands_enabled": any(
                    bool(item.get("mutating")) for item in command_catalog
                ),
                "workspace_exec_enabled": any(
                    item.get("mode") == "workspace_exec" for item in command_catalog
                ),
                "allowed_workspace_roots": workspace_roots,
                "raw_shell_enabled": False,
                "agent_bash_enabled": True,
                "agent_bash_requires_remote_development": True,
                "chat_stream_path": "/api/chat_stream",
                "command_path": "/api/companion/commands",
                "required_bearer_scope": _pairing.REMOTE_DEVELOPMENT_SCOPE,
                "requires_signed_commands": True,
                "requires_replay_protection": True,
                "requires_admin_for_workspace_files": True,
                "intended_clients": ["pwa", "react_native"],
            },
            "goal_runs": {
                "status": "server_owned_loop_ready",
                "available": True,
                "required_bearer_scope": "chat",
                "requires_session_id": True,
                "allow_bash_requires_remote_development": True,
                "start_path": "/api/companion/goals",
                "list_path": "/api/companion/goals",
                "status_path": "/api/companion/goals/{run_id}",
                "resume_path": "/api/companion/goals/{run_id}/resume",
                "stop_path": "/api/companion/goals/{run_id}/stop",
                "completion_markers": [
                    "GOAL_STATUS: complete",
                    "GOAL_STATUS: continue",
                    "GOAL_STATUS: blocked",
                ],
            },
        },
        "safety": {
            "host_control": "signed_workspace_file_control_enabled_raw_shell_disabled",
            "credential_authority": "revocable_chat_and_remote_development_scoped_token",
            "pairing_shows_secret_once": True,
        },
    }


def token_owner(request: Request) -> str | None:
    """The real owner to attribute a request to, for read-scoping.

    Cookie sessions resolve to the logged-in username via get_current_user.
    Bearer-token callers come through as the sandboxed pseudo-user "api"; their
    real owner is stamped on request.state.api_token_owner by the auth
    middleware. Returns None when no owner can be resolved.
    """
    if getattr(request.state, "api_token", False):
        return getattr(request.state, "api_token_owner", None)
    return get_current_user(request)


def require_companion_owner(request: Request) -> str:
    """Require a resolved human owner for companion read/chat actions."""
    require_companion_scope(request)
    owner = token_owner(request)
    if not owner:
        raise HTTPException(401, "Companion owner could not be resolved")
    return owner


def require_remote_development_owner(request: Request) -> str:
    """Require explicit remote-development approval and a resolved owner."""
    require_remote_development_scope(request)
    owner = token_owner(request)
    if not owner:
        raise HTTPException(401, "Companion owner could not be resolved")
    return owner


def owner_can_see(row_owner, owner) -> bool:
    """Owner-scope rule for read endpoints.

    A caller sees a row when it is their own, or when it is a legacy null-owner
    ("shared") row. A caller must NEVER see another owner's row. Mirrors the
    `owner_filter` rule used elsewhere, expressed as a pure predicate so it can
    be tested directly and used as a defensive in-Python check alongside the
    SQL filter.
    """
    return row_owner is None or row_owner == owner


def _companion_session_manager():
    from core import models as core_models

    manager = getattr(core_models, "_session_manager", None)
    if manager is None:
        raise HTTPException(503, "Session manager is not ready")
    return manager


def _json_list(value) -> list:
    if isinstance(value, list):
        return [str(item) for item in value if isinstance(item, str) and item.strip()]
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if isinstance(item, str) and item.strip()]


def _endpoint_model_ids(endpoint) -> list[str]:
    hidden = set(_json_list(getattr(endpoint, "hidden_models", None)))
    pinned = _json_list(getattr(endpoint, "pinned_models", None))
    cached = _json_list(getattr(endpoint, "cached_models", None))
    out: list[str] = []
    for model_id in [*pinned, *cached]:
        if model_id in hidden or model_id in out:
            continue
        out.append(model_id)
    return out


def _session_summary(session) -> dict:
    return {
        "id": session.id,
        "name": session.name,
        "model": session.model,
        "endpoint_url": session.endpoint_url,
        "rag": bool(session.rag),
        "archived": bool(session.archived),
        "message_count": int(getattr(session, "message_count", 0) or 0),
    }


def _persist_companion_session_headers(session_id: str, headers: dict | None) -> None:
    from core.database import Session as DbSession, SessionLocal

    db = SessionLocal()
    try:
        row = db.query(DbSession).filter(DbSession.id == session_id).first()
        if row:
            row.headers = headers or {}
            row.updated_at = datetime.utcnow()
            db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _pick_companion_session_endpoint(*, owner: str, endpoint_id: str, model: str):
    from core.database import ModelEndpoint, SessionLocal
    from src.endpoint_resolver import build_chat_url, normalize_base

    db = SessionLocal()
    try:
        q = db.query(ModelEndpoint).filter(
            ModelEndpoint.is_enabled == True,  # noqa: E712
            (ModelEndpoint.model_type == "llm") | (ModelEndpoint.model_type == None),  # noqa: E711
        )
        if owner:
            q = q.filter((ModelEndpoint.owner == owner) | (ModelEndpoint.owner == None))  # noqa: E711
        if endpoint_id:
            q = q.filter(ModelEndpoint.id == endpoint_id)
        for endpoint in q.all():
            if not owner_can_see(getattr(endpoint, "owner", None), owner):
                continue
            models = _endpoint_model_ids(endpoint)
            selected_model = model.strip() if model else ""
            if selected_model:
                if models and selected_model not in models:
                    continue
            elif models:
                selected_model = models[0]
            else:
                continue
            base_url = normalize_base(endpoint.base_url or "")
            return {
                "endpoint_id": endpoint.id,
                "endpoint_url": build_chat_url(base_url),
                "endpoint_base_url": base_url,
                "model": selected_model,
                "api_key": endpoint.api_key or "",
            }
    finally:
        db.close()
    return None
    

def mint_pairing_token(owner: str, invalidate=None) -> tuple[str, str]:
    """Mint a pairing token AND invalidate the auth middleware's in-memory token
    cache, so the new token is accepted on the very next request without a server
    restart. Returns (token_id, raw_token); the raw token is shown once.

    `invalidate` is the app's request.app.state.invalidate_token_cache callable
    (passed in so this stays a pure, testable unit).
    """
    token_id, raw_token = _pairing.mint_token(owner)
    if callable(invalidate):
        invalidate()
    return token_id, raw_token


def _companion_session_for_owner(owner: str, session_id: str):
    manager = _companion_session_manager()
    try:
        session = manager.get_session(session_id)
    except Exception:
        raise HTTPException(404, "Companion session was not found")
    if not owner_can_see(getattr(session, "owner", None), owner):
        raise HTTPException(404, "Companion session was not found")
    return session


def setup_companion_routes(
    *,
    chat_handler=None,
    chat_processor=None,
    memory_manager=None,
    memory_vector=None,
    webhook_manager=None,
    skills_manager=None,
) -> APIRouter:
    router = APIRouter(prefix="/api/companion", tags=["companion"])

    def _goal_dependencies(request: Request):
        from companion.goal_runs import GoalRunDependencies

        app_state = getattr(getattr(request, "app", None), "state", None)
        resolved_session_manager = (
            getattr(app_state, "session_manager", None)
            if app_state is not None
            else None
        ) or _companion_session_manager()
        resolved = GoalRunDependencies(
            session_manager=resolved_session_manager,
            chat_handler=chat_handler or getattr(app_state, "chat_handler", None),
            chat_processor=chat_processor or getattr(app_state, "chat_processor", None),
            memory_manager=memory_manager or getattr(app_state, "memory_manager", None),
            memory_vector=memory_vector if memory_vector is not None else getattr(app_state, "memory_vector", None),
            webhook_manager=webhook_manager if webhook_manager is not None else getattr(app_state, "webhook_manager", None),
            skills_manager=skills_manager if skills_manager is not None else getattr(app_state, "skills_manager", None),
        )
        missing = [
            name
            for name in ("chat_handler", "chat_processor", "memory_manager")
            if getattr(resolved, name) is None
        ]
        if missing:
            raise HTTPException(503, f"Goal runner dependencies are not ready: {', '.join(missing)}")
        return resolved

    @router.get("/ping")
    def ping(request: Request):
        """Cheap, auth-validated health check. A 200 with ok=true confirms the
        host/port and credential are valid; middleware returns 401 otherwise."""
        require_companion_scope(request)
        from core.constants import APP_VERSION
        return {
            "ok": True,
            "name": "odysseus",
            "version": APP_VERSION,
            "auth": "token" if getattr(request.state, "api_token", False) else "session",
        }

    @router.get("/info")
    def info(request: Request):
        """Server identity + coarse capability flags. `owner` is the caller's own
        identity (the token's owner for bearer callers)."""
        require_companion_scope(request)
        from core.constants import APP_VERSION
        return {
            "name": "odysseus",
            "version": APP_VERSION,
            "owner": token_owner(request),
            "capabilities": {"chat": True, "streaming": True},
            "client_contract": {
                "version": COMPANION_CONTRACT_VERSION,
                "manifest": "/api/companion/manifest",
            },
        }

    @router.get("/manifest")
    def manifest(request: Request):
        """Machine-readable contract for private mobile/PWA companion clients."""
        require_companion_scope(request)
        return companion_manifest(request)

    @router.get("/models")
    def models(request: Request):
        """LLM model endpoints the CALLER can use.

        The stock /api/models route scopes to get_current_user, which for a
        bearer token is the sandboxed pseudo-user "api" (owns nothing). Here we
        scope to the token's real owner instead, plus legacy null-owner shared
        rows -- the same rule as owner_filter. Read-only; never returns api_key
        material.
        """
        require_companion_scope(request)
        import json as _json

        from core.database import SessionLocal, ModelEndpoint
        from src.endpoint_resolver import build_chat_url

        owner = token_owner(request)
        out = []
        db = SessionLocal()
        try:
            q = db.query(ModelEndpoint).filter(
                ModelEndpoint.is_enabled == True,  # noqa: E712
                (ModelEndpoint.model_type == "llm") | (ModelEndpoint.model_type == None),  # noqa: E711
            )
            if owner:
                q = q.filter((ModelEndpoint.owner == owner) | (ModelEndpoint.owner == None))  # noqa: E711
            for ep in q.all():
                if not owner_can_see(ep.owner, owner):
                    continue
                try:
                    model_ids = _json.loads(ep.cached_models) if ep.cached_models else []
                except (ValueError, TypeError):
                    model_ids = []
                try:
                    hidden = set(_json.loads(ep.hidden_models)) if ep.hidden_models else set()
                except (ValueError, TypeError):
                    hidden = set()
                model_ids = [m for m in model_ids if m not in hidden]
                try:
                    chat_url = build_chat_url(ep.base_url)
                except Exception:
                    chat_url = ep.base_url
                out.append({
                    "endpoint_id": ep.id,
                    "name": ep.name,
                    "endpoint_url": chat_url,
                    "models": model_ids,
                    "supports_tools": ep.supports_tools,
                })
        finally:
            db.close()
        return {"endpoints": out}

    @router.get("/sessions")
    def list_sessions(request: Request):
        """List the caller's mobile-visible chat sessions."""
        owner = require_companion_owner(request)
        manager = _companion_session_manager()
        sessions = manager.get_sessions_for_user(owner)
        return {
            "sessions": [
                _session_summary(session)
                for session in sessions.values()
                if not getattr(session, "archived", False)
            ]
        }

    @router.post("/sessions")
    def create_session(request: Request, body: dict = Body(default_factory=dict)):
        """Create a chat session for a mobile/PWA companion client.

        Mobile callers may name a saved `endpoint_id` and model from
        `/api/companion/models`. If omitted, the first owner-visible enabled LLM
        endpoint with cached/pinned models is selected. Raw endpoint URLs are
        intentionally not accepted here.
        """
        owner = require_companion_owner(request)
        manager = _companion_session_manager()
        body = body or {}
        name = str(body.get("name") or "Companion").strip()[:120] or "Companion"
        endpoint_id = str(body.get("endpoint_id") or "").strip()
        model = str(body.get("model") or "").strip()
        rag = bool(body.get("rag", False))
        selected = _pick_companion_session_endpoint(
            owner=owner,
            endpoint_id=endpoint_id,
            model=model,
        )
        if not selected:
            raise HTTPException(400, "No owner-visible companion model endpoint found")

        sid = str(uuid.uuid4())
        session = manager.create_session(
            session_id=sid,
            name=name,
            endpoint_url=selected["endpoint_url"],
            model=selected["model"],
            rag=rag,
            owner=owner,
        )
        if selected["api_key"]:
            from src.endpoint_resolver import build_headers

            session.headers = build_headers(
                selected["api_key"],
                selected["endpoint_base_url"],
            )
            _persist_companion_session_headers(sid, session.headers)

        return {
            "session": _session_summary(session),
            "endpoint_id": selected["endpoint_id"],
        }

    @router.get("/goals")
    def list_goal_runs(request: Request):
        """List server-owned goal runs started by this companion owner."""
        owner = require_companion_owner(request)
        from companion.goal_runs import get_goal_run_manager

        return {"runs": get_goal_run_manager().list_runs(owner)}

    @router.post("/goals")
    async def start_goal_run(request: Request, body: dict = Body(default_factory=dict)):
        """Start a server-owned autonomous goal loop for an existing chat session."""
        owner = require_companion_owner(request)
        body = body or {}
        goal = str(body.get("goal") or "").strip()
        session_id = str(body.get("session_id") or "").strip()
        if not goal:
            raise HTTPException(400, "Goal is required")
        if not session_id:
            raise HTTPException(400, "session_id is required")
        _companion_session_for_owner(owner, session_id)

        allow_bash = bool(body.get("allow_bash", False))
        if allow_bash:
            require_remote_development_scope(request)

        from companion.goal_runs import (
            get_goal_run_manager,
            request_context_from_companion_request,
        )

        try:
            max_turns = int(body.get("max_turns") or 0)
        except (TypeError, ValueError):
            raise HTTPException(400, "max_turns must be an integer")

        try:
            run = await get_goal_run_manager().start(
                owner=owner,
                goal=goal,
                session_id=session_id,
                use_web=bool(body.get("use_web", False)),
                allow_bash=allow_bash,
                max_turns=max_turns,
                deps=_goal_dependencies(request),
                request_context=request_context_from_companion_request(request, owner),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"run": run}

    @router.get("/goals/{run_id}")
    def get_goal_run(request: Request, run_id: str):
        """Return one server-owned goal run."""
        owner = require_companion_owner(request)
        from companion.goal_runs import get_goal_run_manager

        run = get_goal_run_manager().get_run(owner, run_id)
        if not run:
            raise HTTPException(404, "Goal run was not found")
        return {"run": run}

    @router.post("/goals/{run_id}/resume")
    async def resume_goal_run(request: Request, run_id: str):
        """Resume a paused server-owned goal run."""
        owner = require_companion_owner(request)
        from companion.goal_runs import (
            get_goal_run_manager,
            request_context_from_companion_request,
        )

        try:
            run = await get_goal_run_manager().resume(
                owner=owner,
                run_id=run_id,
                deps=_goal_dependencies(request),
                request_context=request_context_from_companion_request(request, owner),
            )
        except KeyError as exc:
            raise HTTPException(404, "Goal run was not found") from exc
        return {"run": run}

    @router.post("/goals/{run_id}/stop")
    async def stop_goal_run(request: Request, run_id: str):
        """Stop a server-owned goal run."""
        owner = require_companion_owner(request)
        from companion.goal_runs import get_goal_run_manager

        try:
            run = await get_goal_run_manager().stop(owner=owner, run_id=run_id)
        except KeyError as exc:
            raise HTTPException(404, "Goal run was not found") from exc
        return {"run": run}

    @router.get("/keys")
    def list_keys(request: Request):
        """List the caller's active approved command keys.

        Returns metadata and a public-key fingerprint only. Public key blobs are
        not needed for normal mobile UX, and private keys are never server-side.
        """
        owner = require_remote_development_owner(request)
        from companion.keys import list_device_keys, serialize_device_key
        from core.database import SessionLocal

        db = SessionLocal()
        try:
            return {
                "keys": [
                    serialize_device_key(row)
                    for row in list_device_keys(db, owner=owner)
                ]
            }
        finally:
            db.close()

    @router.post("/keys")
    def register_key(request: Request, body: dict = Body(default_factory=dict)):
        """Register a mobile/PWA public key for future signed commands.

        A paired mobile client should generate an Ed25519 key pair locally, keep
        the private key on-device, and POST only the base64 public key here.
        """
        owner = require_remote_development_owner(request)
        body = body or {}
        public_key_b64 = str(body.get("public_key_b64") or "").strip()
        label = str(body.get("label") or "Companion device").strip()
        key_id = body.get("key_id")

        from companion.keys import register_device_key, serialize_device_key
        from companion.signing import SignedCommandError
        from core.database import SessionLocal

        db = SessionLocal()
        try:
            row = register_device_key(
                db,
                owner=owner,
                public_key_b64=public_key_b64,
                label=label,
                key_id=str(key_id).strip() if key_id is not None else None,
            )
            db.commit()
            return {"key": serialize_device_key(row)}
        except SignedCommandError as exc:
            db.rollback()
            raise HTTPException(400, str(exc)) from exc
        finally:
            db.close()

    @router.delete("/keys/{key_id}")
    def revoke_key(request: Request, key_id: str):
        """Revoke one of the caller's approved command keys."""
        owner = require_remote_development_owner(request)
        from companion.keys import revoke_device_key, serialize_device_key
        from companion.signing import SignedCommandError
        from core.database import SessionLocal

        db = SessionLocal()
        try:
            row, removed_nonces = revoke_device_key(db, owner=owner, key_id=key_id)
            db.commit()
            return {
                "status": "revoked",
                "removed_nonces": removed_nonces,
                "key": serialize_device_key(row),
            }
        except SignedCommandError as exc:
            db.rollback()
            raise HTTPException(404, str(exc)) from exc
        finally:
            db.close()

    @router.post("/commands")
    def run_command(request: Request, body: dict = Body(default_factory=dict)):
        """Run one fixed companion command after verifying a registered signature."""
        owner = require_remote_development_owner(request)
        body = body or {}
        from companion.commands import (
            CompanionCommandError,
            CompanionCommandForbidden,
            execute_companion_command,
        )
        from companion.signing import SignedCommandError, envelope_from_headers
        from companion.keys import verify_registered_signed_command
        from core.database import SessionLocal

        method = getattr(request, "method", "POST") or "POST"
        path = getattr(getattr(request, "url", None), "path", "/api/companion/commands")
        db = SessionLocal()
        try:
            envelope = envelope_from_headers(request.headers)
            verification = verify_registered_signed_command(
                db,
                owner=owner,
                method=method,
                path=path,
                body=body,
                envelope=envelope,
            )
            db.commit()
        except SignedCommandError as exc:
            db.rollback()
            raise HTTPException(401, str(exc)) from exc
        finally:
            db.close()

        try:
            command_result = execute_companion_command(owner=owner, body=body)
        except CompanionCommandForbidden as exc:
            raise HTTPException(403, str(exc)) from exc
        except CompanionCommandError as exc:
            raise HTTPException(400, str(exc)) from exc

        return {
            "ok": True,
            "verified": {
                "key_id": verification.key_id,
                "nonce": verification.nonce,
                "timestamp": verification.timestamp.isoformat(),
                "body_sha256": verification.body_sha256,
                "protocol_version": verification.protocol_version,
            },
            "command": command_result,
        }

    @router.get("/pair")
    def pair_page(request: Request):
        """Admin-only pairing page. Renders a form that POSTs to mint a code.

        A GET never mints a credential: SameSite=Lax session cookies ride
        top-level GET navigations, so minting on GET would be triggerable by a
        link or <img> (CSRF). The actual mint is the POST handler below.
        """
        require_admin(request)
        page = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pair a device</title>
<style>
  body{font-family:-apple-system,system-ui,sans-serif;max-width:520px;margin:48px auto;padding:0 20px;color:#e8e8e8;background:#16161a}
  .card{background:#1f1f25;border:1px solid #2c2c35;border-radius:14px;padding:28px;text-align:center}
  button{background:#7c9cff;color:#0e0e12;border:none;border-radius:10px;padding:12px 20px;font-size:15px;font-weight:600;cursor:pointer}
</style></head>
<body><div class="card">
  <h2>Pair a device</h2>
  <p>Generate a one-time pairing code (chat + remote-development scopes) for a private companion client.</p>
  <form method="POST" action="/api/companion/pair">
    <button type="submit">Generate pairing code</button>
  </form>
  <p style="color:#8a8a96;font-size:12px;margin-top:18px">Admin only. Each code mints a new token, shown once. Manage or revoke under Settings &rarr; API tokens.</p>
</div></body></html>"""
        return HTMLResponse(page)

    @router.post("/pair")
    def pair_create(request: Request):
        """Mint a pairing code. Admin-cookie only; CSRF-safe because the
        SameSite=Lax session cookie is not sent on a cross-site POST (same
        protection as POST /api/tokens). Minting invalidates the token cache so
        the code works immediately, no restart. `?format=json` returns the
        payload for an in-app pairing screen."""
        require_admin(request)
        owner = get_current_user(request)
        invalidate = getattr(request.app.state, "invalidate_token_cache", None)
        token_id, raw_token = mint_pairing_token(owner, invalidate)

        hosts = _pairing.lan_ip_candidates()
        host = hosts[0] if hosts else "127.0.0.1"
        port = request.url.port or _pairing.default_port()
        base_url = _pairing.configured_base_url()
        payload = _pairing.pairing_payload(host, port, raw_token, base_url=base_url)
        qr = _pairing.pairing_qr_png_data_uri(payload)
        qr_ok = bool(qr and qr.startswith("data:image/png;base64,"))

        if (request.query_params.get("format") or "").lower() == "json":
            response = {
                "host": host,
                "port": port,
                "token": raw_token,
                "token_id": token_id,
                "hosts": hosts,
                "payload": payload,
                "qr": qr if qr_ok else None,
            }
            if base_url:
                response["base_url"] = base_url
            return response

        import json as _json
        payload_json = _json.dumps(payload, separators=(",", ":"))
        # Only ever emit a known PNG data-URI into the src; every other value is
        # html.escaped.
        qr_block = (
            f'<img src="{html.escape(qr)}" alt="Pairing QR" width="260" height="260">'
            if qr_ok else "<p><em>QR rendering unavailable -- enter the details manually.</em></p>"
        )
        base_url_row = (
            f'<div class="row"><strong>Base URL:</strong> <code>{html.escape(base_url)}</code></div>'
            if base_url else ""
        )
        reachability_note = (
            "The device must be able to reach the configured private Base URL."
            if base_url else
            "The device must be on the same network, and the server must bind to your LAN."
        )
        page = f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pairing code</title>
<style>
  body{{font-family:-apple-system,system-ui,sans-serif;max-width:520px;margin:40px auto;padding:0 20px;color:#e8e8e8;background:#16161a}}
  .card{{background:#1f1f25;border:1px solid #2c2c35;border-radius:14px;padding:24px;text-align:center}}
  code{{background:#0e0e12;padding:2px 6px;border-radius:6px;word-break:break-all}}
  .row{{text-align:left;margin:10px 0;font-size:14px;color:#bdbdc7}}
  .warn{{color:#e0a85e;font-size:13px;margin-top:18px}}
</style></head>
<body><div class="card">
  <h2>Pairing code</h2>
  {qr_block}
  {base_url_row}
  <div class="row"><strong>Host:</strong> <code>{html.escape(host)}</code></div>
  <div class="row"><strong>Port:</strong> <code>{html.escape(str(port))}</code></div>
  <div class="row"><strong>Token:</strong> <code>{html.escape(raw_token)}</code></div>
  <div class="row"><strong>Payload:</strong> <code>{html.escape(payload_json)}</code></div>
  <p class="warn">Shown once. This grants chat and signed command access to your Odysseus; revoke it
  in Settings &rarr; API tokens (id <code>{html.escape(token_id)}</code>). {html.escape(reachability_note)}</p>
</div></body></html>"""
        return HTMLResponse(page)

    return router
