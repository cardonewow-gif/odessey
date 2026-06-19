"""Server-owned companion goal runs.

A mobile client can start one goal run and disconnect. The server keeps taking
agent-mode turns in the associated chat session until the assistant marks the
goal complete, blocked, stopped, or the configured safety turn limit is reached.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

from core.atomic_io import atomic_write_json
from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

GOAL_RUNS_FILE = str(Path(DATA_DIR) / "companion_goal_runs.json")
DEFAULT_MAX_TURNS = 0
HARD_MAX_TURNS = 200

TERMINAL_STATUSES = {"complete", "blocked", "error", "stopped"}
ACTIVE_STATUSES = {"queued", "running", "continuing"}


def now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_goal_status(text: str) -> Optional[str]:
    import re

    match = re.search(r"^GOAL_STATUS:\s*(complete|continue|blocked)\b", text or "", re.I | re.M)
    return match.group(1).lower() if match else None


def goal_session_name(goal: str) -> str:
    compact = " ".join((goal or "").split()).strip()
    if not compact:
        return "Goal"
    if len(compact) > 71:
        compact = compact[:68].rstrip() + "..."
    return f"Goal: {compact}"


def build_initial_goal_prompt(goal: str) -> str:
    return "\n".join(
        [
            "Pursue this goal autonomously until it is 100% complete.",
            "",
            f"Goal: {(goal or '').strip()}",
            "",
            "Work in a loop. Make a concrete plan, take the next action, inspect results, and keep going.",
            "Use available tools when they are needed. Verify the final state before claiming completion.",
            "Do not stop early because the work is lengthy. If more work remains, say so with the continuation marker.",
            "If you are blocked by missing credentials, permissions, destructive-risk confirmation, or required user input, stop and mark the goal blocked.",
            "",
            "End every turn with exactly one of these lines:",
            "GOAL_STATUS: complete",
            "GOAL_STATUS: continue",
            "GOAL_STATUS: blocked",
            "",
            "Only use GOAL_STATUS: complete when the goal is fully done and verified.",
        ]
    )


def build_continue_goal_prompt(
    *,
    goal: str,
    round: int,
    previous_status_missing: bool = False,
) -> str:
    return "\n".join(
        [
            f"Continue pursuing the same goal. This is server goal loop turn {round}.",
            "",
            f"Goal: {(goal or '').strip()}",
            "",
            (
                "Your previous turn did not include the required GOAL_STATUS line. Continue the actual work and include the marker this time."
                if previous_status_missing
                else "Continue from the current state. Do not restart from scratch unless the current state proves that is necessary."
            ),
            "Take the next concrete action, verify results, and keep going until the goal is complete or genuinely blocked.",
            "",
            "End this turn with exactly one of:",
            "GOAL_STATUS: complete",
            "GOAL_STATUS: continue",
            "GOAL_STATUS: blocked",
        ]
    )


def _load_store() -> Dict[str, dict]:
    try:
        path = Path(GOAL_RUNS_FILE)
        if not path.exists():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            return {}
        runs = {
            str(run_id): dict(run)
            for run_id, run in raw.items()
            if isinstance(run, dict) and isinstance(run.get("id"), str)
        }
    except Exception:
        return {}

    # A process restart cannot resume an in-flight coroutine. Preserve state but
    # make the interruption explicit so a client can resume intentionally.
    changed = False
    for run in runs.values():
        if run.get("status") in ACTIVE_STATUSES:
            run["status"] = "paused"
            run["error"] = "Goal run paused because the Odysseus server restarted."
            run["updated_at"] = now_iso()
            changed = True
    if changed:
        _save_store(runs)
    return runs


def _save_store(runs: Dict[str, dict]) -> None:
    atomic_write_json(GOAL_RUNS_FILE, runs, indent=2)


def _public_run(run: dict) -> dict:
    out = dict(run)
    out.pop("owner", None)
    return out


@dataclass
class GoalRunDependencies:
    session_manager: Any
    chat_handler: Any
    chat_processor: Any
    memory_manager: Any
    memory_vector: Any = None
    webhook_manager: Any = None
    skills_manager: Any = None


class GoalRunManager:
    def __init__(self) -> None:
        self._runs: Dict[str, dict] = _load_store()
        self._tasks: Dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    def list_runs(self, owner: str) -> list[dict]:
        return [
            _public_run(run)
            for run in sorted(
                self._runs.values(),
                key=lambda item: str(item.get("updated_at") or item.get("started_at") or ""),
                reverse=True,
            )
            if run.get("owner") == owner
        ]

    def get_run(self, owner: str, run_id: str) -> Optional[dict]:
        run = self._runs.get(run_id)
        if not run or run.get("owner") != owner:
            return None
        return _public_run(run)

    async def start(
        self,
        *,
        owner: str,
        goal: str,
        session_id: str,
        use_web: bool,
        allow_bash: bool,
        max_turns: int = DEFAULT_MAX_TURNS,
        deps: GoalRunDependencies,
        request_context: Any,
    ) -> dict:
        clean_goal = " ".join((goal or "").split()).strip()
        if not clean_goal:
            raise ValueError("Goal is required")

        run_id = uuid.uuid4().hex
        timestamp = now_iso()
        run = {
            "version": 1,
            "id": run_id,
            "owner": owner,
            "goal": clean_goal,
            "session_id": session_id,
            "status": "queued",
            "round": 0,
            "started_at": timestamp,
            "updated_at": timestamp,
            "completed_at": None,
            "error": None,
            "use_web": bool(use_web),
            "allow_bash": bool(allow_bash),
            "max_turns": max(0, min(int(max_turns or 0), HARD_MAX_TURNS)),
            "turn_budget": max(0, min(int(max_turns or 0), HARD_MAX_TURNS)),
            "transcript": [],
            "live_text": "",
        }
        async with self._lock:
            self._runs[run_id] = run
            _save_store(self._runs)
            self._tasks[run_id] = asyncio.create_task(
                self._run_loop(run_id, deps=deps, request_context=request_context)
            )
        return _public_run(run)

    async def resume(
        self,
        *,
        owner: str,
        run_id: str,
        deps: GoalRunDependencies,
        request_context: Any,
    ) -> dict:
        async with self._lock:
            run = self._runs.get(run_id)
            if not run or run.get("owner") != owner:
                raise KeyError(run_id)
            if run.get("status") == "complete":
                return _public_run(run)
            task = self._tasks.get(run_id)
            if task and not task.done():
                return _public_run(run)
            turn_budget = int(run.get("turn_budget") or run.get("max_turns") or 0)
            if turn_budget and int(run.get("round") or 0) >= int(run.get("max_turns") or 0):
                run["max_turns"] = min(int(run.get("round") or 0) + turn_budget, HARD_MAX_TURNS)
            run["status"] = "continuing"
            run["error"] = None
            run["updated_at"] = now_iso()
            _save_store(self._runs)
            self._tasks[run_id] = asyncio.create_task(
                self._run_loop(run_id, deps=deps, request_context=request_context)
            )
            return _public_run(run)

    async def stop(self, *, owner: str, run_id: str) -> dict:
        async with self._lock:
            run = self._runs.get(run_id)
            if not run or run.get("owner") != owner:
                raise KeyError(run_id)
            task = self._tasks.get(run_id)
            if task and not task.done():
                task.cancel()
            self._patch(run_id, status="stopped", completed_at=now_iso())
            return _public_run(self._runs[run_id])

    def _patch(self, run_id: str, **fields: Any) -> None:
        run = self._runs.get(run_id)
        if not run:
            return
        run.update(fields)
        run["updated_at"] = now_iso()
        _save_store(self._runs)

    def _append_or_update_turn(self, run_id: str, turn: dict) -> None:
        run = self._runs.get(run_id)
        if not run:
            return
        transcript = list(run.get("transcript") or [])
        for index, existing in enumerate(transcript):
            if existing.get("id") == turn.get("id"):
                transcript[index] = turn
                break
        else:
            transcript.append(turn)
        run["transcript"] = transcript
        run["live_text"] = turn.get("response", "")
        run["updated_at"] = now_iso()
        _save_store(self._runs)

    async def _run_loop(
        self,
        run_id: str,
        *,
        deps: GoalRunDependencies,
        request_context: Any,
    ) -> None:
        try:
            for _ in range(HARD_MAX_TURNS):
                run = self._runs.get(run_id)
                if not run or run.get("status") in TERMINAL_STATUSES:
                    return
                configured_limit = int(run.get("max_turns") or 0)
                if configured_limit and int(run.get("round") or 0) >= configured_limit:
                    self._patch(
                        run_id,
                        status="paused",
                        error=f"Paused after {configured_limit} server goal turns. Resume to continue.",
                        live_text="",
                    )
                    return

                round_number = int(run.get("round") or 0) + 1
                previous_turn = (run.get("transcript") or [])[-1] if run.get("transcript") else None
                prompt = (
                    build_initial_goal_prompt(run["goal"])
                    if round_number == 1
                    else build_continue_goal_prompt(
                        goal=run["goal"],
                        round=round_number,
                        previous_status_missing=not bool(previous_turn and previous_turn.get("status")),
                    )
                )
                turn = {
                    "id": f"goal-turn-{uuid.uuid4().hex[:12]}",
                    "round": round_number,
                    "prompt": prompt,
                    "response": "",
                    "status": None,
                    "started_at": now_iso(),
                    "completed_at": None,
                }
                self._patch(
                    run_id,
                    status="running" if round_number == 1 else "continuing",
                    round=round_number,
                    error=None,
                    live_text="",
                )
                self._append_or_update_turn(run_id, turn)

                response, metrics, context = await self._run_agent_turn(
                    run_id,
                    run=dict(self._runs[run_id]),
                    turn=turn,
                    prompt=prompt,
                    deps=deps,
                    request_context=request_context,
                )

                status = parse_goal_status(response)
                completed = dict(turn)
                completed.update(
                    {
                        "response": response,
                        "status": status,
                        "completed_at": now_iso(),
                    }
                )
                self._append_or_update_turn(run_id, completed)

                next_status = "complete" if status == "complete" else "blocked" if status == "blocked" else "continuing"
                patch: dict[str, Any] = {
                    "status": next_status,
                    "live_text": "",
                    "last_metrics": metrics or {},
                }
                if next_status in {"complete", "blocked"}:
                    patch["completed_at"] = now_iso()
                self._patch(run_id, **patch)

                if status in {"complete", "blocked"}:
                    return
                if not status:
                    logger.info("[goal-run] %s turn %s missing GOAL_STATUS; continuing", run_id, round_number)
                await asyncio.sleep(0)

            self._patch(
                run_id,
                status="paused",
                error=f"Paused after the hard safety limit of {HARD_MAX_TURNS} goal turns.",
                live_text="",
            )
        except asyncio.CancelledError:
            self._patch(run_id, status="stopped", completed_at=now_iso(), live_text="")
            raise
        except Exception as exc:
            logger.error("[goal-run] %s failed: %s", run_id, exc, exc_info=True)
            self._patch(run_id, status="error", error=str(exc), completed_at=now_iso(), live_text="")
        finally:
            self._tasks.pop(run_id, None)

    async def _run_agent_turn(
        self,
        run_id: str,
        *,
        run: dict,
        turn: dict,
        prompt: str,
        deps: GoalRunDependencies,
        request_context: Any,
    ) -> tuple[str, dict | None, Any]:
        from routes.chat_helpers import (
            _enforce_chat_privileges,
            build_chat_context,
            resolve_session_auth,
            run_post_response_tasks,
            save_assistant_response,
        )
        from src.agent_loop import stream_agent_loop
        from src.endpoint_resolver import resolve_chat_fallback_candidates
        from src.settings import get_setting
        from src.agent_tools import MAX_AGENT_ROUNDS
        from src.tool_policy import build_effective_tool_policy

        session_id = run["session_id"]
        sess = deps.session_manager.get_session(session_id)
        owner = run["owner"]
        resolve_session_auth(sess, session_id, owner=owner)
        _enforce_chat_privileges(request_context, sess)

        disabled_tools = set()
        if not run.get("allow_bash"):
            disabled_tools.update({"bash", "python", "read_file", "write_file"})
        if not run.get("use_web"):
            disabled_tools.update({"web_search", "web_fetch"})
        try:
            privs = request_context.app.state.auth_manager.get_privileges(owner) or {}
        except Exception:
            privs = {}
        if privs:
            if not privs.get("can_use_agent", True):
                raise PermissionError("Your account is not allowed to use agent mode.")
            if not privs.get("can_use_bash", True):
                disabled_tools.update({"bash", "python", "read_file", "write_file"})
            if not privs.get("can_use_browser", True):
                disabled_tools.add("builtin_browser")
            if not privs.get("can_use_documents", True):
                disabled_tools.update({"create_document", "edit_document", "update_document", "suggest_document"})
            if not privs.get("can_generate_images", True):
                disabled_tools.add("generate_image")
            if not privs.get("can_manage_memory", True):
                disabled_tools.update({"manage_memory", "manage_skills"})
        global_disabled = get_setting("disabled_tools", [])
        if isinstance(global_disabled, list):
            disabled_tools.update(str(item) for item in global_disabled)

        tool_policy = build_effective_tool_policy(
            disabled_tools=disabled_tools,
            last_user_message=prompt,
        )

        ctx = await build_chat_context(
            sess,
            request_context,
            deps.chat_handler,
            deps.chat_processor,
            message=prompt,
            session_id=session_id,
            use_web=run.get("use_web"),
            allow_tool_preprocessing=not tool_policy.block_all_tool_calls,
            agent_mode=True,
        )

        try:
            fallback_candidates = resolve_chat_fallback_candidates(owner=owner)
        except Exception:
            fallback_candidates = []

        try:
            max_rounds = int(get_setting("agent_max_rounds", MAX_AGENT_ROUNDS) or MAX_AGENT_ROUNDS)
        except (TypeError, ValueError):
            max_rounds = MAX_AGENT_ROUNDS
        max_rounds = max(1, min(max_rounds, 200))
        try:
            max_tool_calls = int(get_setting("agent_max_tool_calls", 0) or 0)
        except (TypeError, ValueError):
            max_tool_calls = 0

        full_response = ""
        last_metrics: dict | None = None
        agent_rounds = 0
        agent_tool_calls = 0

        async for chunk in stream_agent_loop(
            sess.endpoint_url,
            sess.model,
            ctx.messages,
            headers=sess.headers,
            temperature=ctx.preset.temperature,
            max_tokens=ctx.preset.max_tokens,
            prompt_type=None,
            max_tool_calls=max_tool_calls,
            max_rounds=max_rounds,
            context_length=ctx.context_length,
            session_id=session_id,
            disabled_tools=tool_policy.all_disabled_names(),
            tool_policy=tool_policy,
            owner=owner,
            fallbacks=fallback_candidates,
            plan_mode=False,
        ):
            if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
                try:
                    data = json.loads(chunk[6:])
                except json.JSONDecodeError:
                    continue
                if "delta" in data and not data.get("thinking"):
                    full_response += str(data["delta"])
                    live_turn = dict(turn)
                    live_turn["response"] = full_response
                    self._append_or_update_turn(run_id, live_turn)
                elif data.get("type") == "metrics" and isinstance(data.get("data"), dict):
                    last_metrics = data["data"]
                elif data.get("type") == "agent_step":
                    try:
                        agent_rounds = max(agent_rounds, int(data.get("round") or 1))
                    except (TypeError, ValueError):
                        agent_rounds = max(agent_rounds, 1)
                elif data.get("type") == "tool_start":
                    agent_tool_calls += 1
            elif chunk.startswith("event: error"):
                raise RuntimeError("Agent stream failed during goal run")

        if full_response:
            save_assistant_response(
                sess,
                deps.session_manager,
                session_id,
                full_response,
                last_metrics,
                character_name=ctx.preset.character_name,
                web_sources=ctx.web_sources,
                rag_sources=ctx.rag_sources,
                used_memories=ctx.used_memories,
            )
            run_post_response_tasks(
                sess,
                deps.session_manager,
                session_id,
                prompt,
                full_response,
                last_metrics,
                ctx.uprefs,
                deps.memory_manager,
                deps.memory_vector,
                deps.webhook_manager,
                character_name=ctx.preset.character_name,
                agent_rounds=agent_rounds,
                agent_tool_calls=agent_tool_calls,
                skills_manager=deps.skills_manager,
                owner=owner,
                extract_skills=True,
                allow_background_extraction=not tool_policy.block_all_tool_calls,
            )

        return full_response, last_metrics, ctx


_MANAGER = GoalRunManager()


def get_goal_run_manager() -> GoalRunManager:
    return _MANAGER


def request_context_from_companion_request(request: Any, owner: str) -> Any:
    state = SimpleNamespace(
        api_token=getattr(request.state, "api_token", False),
        api_token_owner=owner,
        api_token_scopes=list(getattr(request.state, "api_token_scopes", []) or []),
        current_user=owner,
    )
    return SimpleNamespace(
        app=request.app,
        headers=getattr(request, "headers", {}),
        state=state,
    )
