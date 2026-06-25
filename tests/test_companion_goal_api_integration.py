"""HTTP-level integration tests for companion server-owned goal runs."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request

from companion.goal_runs import GoalRunManager
from companion.routes import setup_companion_routes


class _FakeSessionManager:
    def __init__(self):
        self.session = SimpleNamespace(
            id="session-1",
            name="Phone goal",
            model="model-a",
            endpoint_url="http://llm.test/v1/chat/completions",
            rag=False,
            archived=False,
            message_count=0,
            owner="alice",
        )

    def get_session(self, session_id):
        if session_id != self.session.id:
            raise KeyError(session_id)
        return self.session


class _ScriptedGoalRunManager(GoalRunManager):
    def __init__(self):
        self.responses = [
            "I inspected the current state and one step remains.\nGOAL_STATUS: continue",
            "The requested goal is fully complete and verified.\nGOAL_STATUS: complete",
        ]
        super().__init__()

    async def _run_agent_turn(self, run_id, *, run, turn, prompt, deps, request_context):
        return self.responses.pop(0), {"scripted": True, "round": turn["round"]}, None


def _app(monkeypatch, tmp_path):
    import companion.goal_runs as goal_runs
    import companion.routes as companion_routes

    monkeypatch.setattr(goal_runs, "GOAL_RUNS_FILE", str(tmp_path / "goal_runs.json"))
    manager = _ScriptedGoalRunManager()
    sessions = _FakeSessionManager()
    monkeypatch.setattr(goal_runs, "get_goal_run_manager", lambda: manager)
    monkeypatch.setattr(companion_routes, "_companion_session_manager", lambda: sessions)

    app = FastAPI()

    @app.middleware("http")
    async def _stamp_companion_token(request: Request, call_next):
        auth = request.headers.get("authorization", "")
        if auth == "Bearer ody_test_goal_token":
            request.state.api_token = True
            request.state.api_token_owner = "alice"
            request.state.api_token_scopes = ["chat"]
            request.state.current_user = "api"
        return await call_next(request)

    app.state.session_manager = sessions
    app.state.chat_handler = object()
    app.state.chat_processor = object()
    app.state.memory_manager = object()
    app.include_router(setup_companion_routes())
    return app


async def _wait_for_status(client, run_id, status, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    headers = {"Authorization": "Bearer ody_test_goal_token"}
    last = None
    while loop.time() < deadline:
        response = await client.get(f"/api/companion/goals/{run_id}", headers=headers)
        response.raise_for_status()
        last = response.json()["run"]
        if last["status"] == status:
            return last
        await asyncio.sleep(0.02)
    raise AssertionError(f"goal run did not reach {status}: {last}")


@pytest.mark.asyncio
async def test_companion_goal_run_http_api_loops_to_completion(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    headers = {"Authorization": "Bearer ody_test_goal_token"}
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        started = await client.post(
            "/api/companion/goals",
            headers=headers,
            json={
                "session_id": "session-1",
                "goal": "Finish a scripted mobile goal",
                "use_web": False,
                "allow_bash": False,
            },
        )

        assert started.status_code == 200
        run = started.json()["run"]
        assert run["goal"] == "Finish a scripted mobile goal"
        assert run["session_id"] == "session-1"
        assert run["status"] in {"queued", "running", "continuing", "complete"}

        completed = await _wait_for_status(client, run["id"], "complete")

        assert completed["round"] == 2
        assert completed["last_metrics"] == {"scripted": True, "round": 2}
        assert [turn["status"] for turn in completed["transcript"]] == [
            "continue",
            "complete",
        ]
        assert completed["transcript"][0]["response"].endswith("GOAL_STATUS: continue")
        assert completed["transcript"][1]["response"].endswith("GOAL_STATUS: complete")

        listed = await client.get("/api/companion/goals", headers=headers)
        listed.raise_for_status()
        assert [item["id"] for item in listed.json()["runs"]] == [run["id"]]
