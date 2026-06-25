"""Tests for companion mobile session list/create routes."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from companion.routes import setup_companion_routes


def _request(scopes=None):
    return SimpleNamespace(
        state=SimpleNamespace(
            api_token=True,
            api_token_owner="alice",
            api_token_scopes=scopes or ["chat"],
            current_user="api",
        ),
        app=SimpleNamespace(
            state=SimpleNamespace(
                chat_handler=object(),
                chat_processor=object(),
                memory_manager=object(),
            )
        ),
        headers={},
    )


def _route(path, method):
    for route in setup_companion_routes().routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"{method} {path} route not found")


class _FakeManager:
    def __init__(self):
        self.created = []
        self.sessions_by_owner = {
            "alice": {
                "live": SimpleNamespace(
                    id="live",
                    name="Live",
                    model="model-a",
                    endpoint_url="http://llm/v1/chat/completions",
                    rag=False,
                    archived=False,
                    message_count=2,
                ),
                "archived": SimpleNamespace(
                    id="archived",
                    name="Archived",
                    model="model-a",
                    endpoint_url="http://llm/v1/chat/completions",
                    rag=False,
                    archived=True,
                    message_count=3,
                ),
            }
        }

    def get_sessions_for_user(self, owner):
        return self.sessions_by_owner.get(owner, {})

    def get_session(self, session_id):
        for sessions in self.sessions_by_owner.values():
            if session_id in sessions:
                session = sessions[session_id]
                session.owner = "alice"
                return session
        raise KeyError(session_id)

    def create_session(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(
            id=kwargs["session_id"],
            name=kwargs["name"],
            model=kwargs["model"],
            endpoint_url=kwargs["endpoint_url"],
            rag=kwargs["rag"],
            archived=False,
            message_count=0,
            headers={},
        )


def test_companion_sessions_list_uses_token_owner_and_hides_archived(monkeypatch):
    import companion.routes as cr

    manager = _FakeManager()
    monkeypatch.setattr(cr, "_companion_session_manager", lambda: manager)

    response = _route("/api/companion/sessions", "GET")(_request())

    assert response["sessions"] == [
        {
            "id": "live",
            "name": "Live",
            "model": "model-a",
            "endpoint_url": "http://llm/v1/chat/completions",
            "rag": False,
            "archived": False,
            "message_count": 2,
        }
    ]


def test_companion_session_create_uses_saved_endpoint_selection(monkeypatch):
    import companion.routes as cr

    manager = _FakeManager()
    monkeypatch.setattr(cr, "_companion_session_manager", lambda: manager)
    monkeypatch.setattr(
        cr,
        "_pick_companion_session_endpoint",
        lambda **kwargs: {
            "endpoint_id": "ep-1",
            "endpoint_url": "http://llm/v1/chat/completions",
            "endpoint_base_url": "http://llm/v1",
            "model": "model-a",
            "api_key": "",
        },
    )

    response = _route("/api/companion/sessions", "POST")(
        _request(),
        body={
            "name": "Phone work",
            "endpoint_id": "ep-1",
            "model": "model-a",
            "endpoint_url": "http://169.254.169.254/latest/meta-data",
            "rag": True,
        },
    )

    created = manager.created[0]
    assert created["owner"] == "alice"
    assert created["name"] == "Phone work"
    assert created["endpoint_url"] == "http://llm/v1/chat/completions"
    assert created["model"] == "model-a"
    assert created["rag"] is True
    assert response["endpoint_id"] == "ep-1"
    assert response["session"]["id"] == created["session_id"]


@pytest.mark.asyncio
async def test_companion_goal_start_uses_existing_owned_session(monkeypatch):
    import companion.routes as cr
    import companion.goal_runs as goal_runs

    manager = _FakeManager()
    monkeypatch.setattr(cr, "_companion_session_manager", lambda: manager)

    captured = {}

    class _GoalManager:
        async def start(self, **kwargs):
            captured.update(kwargs)
            return {
                "id": "run-1",
                "goal": kwargs["goal"],
                "session_id": kwargs["session_id"],
                "status": "queued",
            }

    monkeypatch.setattr(goal_runs, "get_goal_run_manager", lambda: _GoalManager())

    route = _route("/api/companion/goals", "POST")
    response = await route(
        _request(),
        body={
            "goal": "Ship the mobile app",
            "session_id": "live",
            "use_web": True,
            "allow_bash": False,
        },
    )

    assert response["run"] == {
        "id": "run-1",
        "goal": "Ship the mobile app",
        "session_id": "live",
        "status": "queued",
    }
    assert captured["owner"] == "alice"
    assert captured["goal"] == "Ship the mobile app"
    assert captured["session_id"] == "live"
    assert captured["use_web"] is True
    assert captured["allow_bash"] is False
    assert captured["request_context"].state.api_token_owner == "alice"


@pytest.mark.asyncio
async def test_companion_goal_start_requires_remote_development_for_bash(monkeypatch):
    import companion.routes as cr

    manager = _FakeManager()
    monkeypatch.setattr(cr, "_companion_session_manager", lambda: manager)

    with pytest.raises(HTTPException) as exc:
        await _route("/api/companion/goals", "POST")(
            _request(scopes=["chat"]),
            body={
                "goal": "Run checks until done",
                "session_id": "live",
                "allow_bash": True,
            },
        )

    assert exc.value.status_code == 403
