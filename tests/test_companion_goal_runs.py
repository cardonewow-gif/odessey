"""Tests for server-owned companion goal runs."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from companion.goal_runs import (
    GoalRunDependencies,
    GoalRunManager,
    build_continue_goal_prompt,
    build_initial_goal_prompt,
    goal_session_name,
    parse_goal_status,
)


class _ScriptedGoalRunManager(GoalRunManager):
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []
        super().__init__()

    async def _run_agent_turn(self, run_id, *, run, turn, prompt, deps, request_context):
        self.prompts.append(prompt)
        return self.responses.pop(0), {"prompt_count": len(self.prompts)}, None


async def _wait_for_status(manager, owner, run_id, statuses, timeout=1.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        run = manager.get_run(owner, run_id)
        if run and run.get("status") in statuses:
            return run
        await asyncio.sleep(0.01)
    raise AssertionError(f"goal run did not reach {statuses}: {manager.get_run(owner, run_id)}")


def _deps():
    return GoalRunDependencies(
        session_manager=object(),
        chat_handler=object(),
        chat_processor=object(),
        memory_manager=object(),
    )


def _request_context():
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))


def test_goal_status_parser_requires_line_marker():
    assert parse_goal_status("done\nGOAL_STATUS: complete") == "complete"
    assert parse_goal_status("GOAL_STATUS: continue\nmore") == "continue"
    assert parse_goal_status("notes\nGOAL_STATUS: blocked") == "blocked"
    assert parse_goal_status("This is not a marker: GOAL_STATUS: complete") is None
    assert parse_goal_status("GOAL_STATUS: maybe") is None


def test_goal_prompts_pin_autonomous_loop_contract():
    initial = build_initial_goal_prompt("  Ship TestFlight build  ")
    followup = build_continue_goal_prompt(
        goal="Ship TestFlight build",
        round=3,
        previous_status_missing=True,
    )

    assert "until it is 100% complete" in initial
    assert "Goal: Ship TestFlight build" in initial
    assert "GOAL_STATUS: complete" in initial
    assert "server goal loop turn 3" in followup
    assert "previous turn did not include" in followup
    assert "GOAL_STATUS: blocked" in followup


def test_goal_session_name_compacts_long_goals():
    title = goal_session_name("  " + ("ship " * 40))

    assert title.startswith("Goal: ship ship")
    assert len(title) <= 78


@pytest.mark.asyncio
async def test_goal_manager_loops_until_complete(monkeypatch, tmp_path):
    import companion.goal_runs as goal_runs

    store_path = tmp_path / "goal_runs.json"
    monkeypatch.setattr(goal_runs, "GOAL_RUNS_FILE", str(store_path))
    manager = _ScriptedGoalRunManager(
        [
            "First pass made progress.\nGOAL_STATUS: continue",
            "Final state verified.\nGOAL_STATUS: complete",
        ]
    )

    started = await manager.start(
        owner="alice",
        goal="Ship the mobile pursue-goal flow",
        session_id="session-1",
        use_web=False,
        allow_bash=False,
        max_turns=0,
        deps=_deps(),
        request_context=_request_context(),
    )
    completed = await _wait_for_status(manager, "alice", started["id"], {"complete"})

    assert completed["status"] == "complete"
    assert completed["round"] == 2
    assert [turn["status"] for turn in completed["transcript"]] == ["continue", "complete"]
    assert "100% complete" in manager.prompts[0]
    assert "server goal loop turn 2" in manager.prompts[1]

    stored = json.loads(store_path.read_text(encoding="utf-8"))
    assert stored[started["id"]]["status"] == "complete"
    assert stored[started["id"]]["last_metrics"] == {"prompt_count": 2}


@pytest.mark.asyncio
async def test_goal_manager_resume_extends_turn_budget(monkeypatch, tmp_path):
    import companion.goal_runs as goal_runs

    monkeypatch.setattr(goal_runs, "GOAL_RUNS_FILE", str(tmp_path / "goal_runs.json"))
    manager = _ScriptedGoalRunManager(
        [
            "Need one more turn.\nGOAL_STATUS: continue",
            "Done after resume.\nGOAL_STATUS: complete",
        ]
    )

    started = await manager.start(
        owner="alice",
        goal="Finish after a bounded turn chunk",
        session_id="session-1",
        use_web=False,
        allow_bash=False,
        max_turns=1,
        deps=_deps(),
        request_context=_request_context(),
    )
    paused = await _wait_for_status(manager, "alice", started["id"], {"paused"})

    assert paused["round"] == 1
    assert paused["status"] == "paused"
    assert "Paused after 1 server goal turns" in paused["error"]

    await manager.resume(
        owner="alice",
        run_id=started["id"],
        deps=_deps(),
        request_context=_request_context(),
    )
    completed = await _wait_for_status(manager, "alice", started["id"], {"complete"})

    assert completed["round"] == 2
    assert completed["status"] == "complete"
    assert completed["max_turns"] == 2
    assert [turn["status"] for turn in completed["transcript"]] == ["continue", "complete"]
