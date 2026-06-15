"""
event_bus.py

Lightweight event bus for triggering automation tasks based on events
like session creation, message sends, etc.
"""

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Optional

from src.constants import AUTH_FILE

logger = logging.getLogger(__name__)

_task_scheduler = None


def set_task_scheduler(scheduler):
    """Wire up the scheduler reference (called from app.py on startup)."""
    global _task_scheduler
    _task_scheduler = scheduler


def get_task_scheduler():
    """Return the current task scheduler instance."""
    return _task_scheduler


def fire_event(event_name: str, owner: Optional[str] = None, meta: Optional[dict] = None):
    """Fire an event — increments counters and triggers tasks that hit threshold.

    `meta` carries optional event context used to scope which subscribed tasks
    actually count the event. Today only "email_received" uses it (keys:
    "account_id", "folder") so a task can watch a specific mailbox/folder.

    Safe to call from both sync and async contexts.
    """
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_handle_event(event_name, owner, meta))
    except RuntimeError:
        # No running loop — run in a new one (shouldn't happen in FastAPI)
        asyncio.run(_handle_event(event_name, owner, meta))


def _resolve_event_owner(owner: Optional[str]) -> Optional[str]:
    """Resolve ownerless app events to the primary configured user.

    Some event sources run from localhost/internal code paths where request
    middleware is not present, so they cannot pass a username. Treating that as
    "all owners" made built-in tasks run once per account. Instead, route those
    events to the first admin account, matching the legacy-owner migration.
    """
    owner = (owner or "").strip()
    if owner:
        return owner

    try:
        auth_path = AUTH_FILE
        with open(auth_path, "r", encoding="utf-8") as f:
            users = (json.load(f).get("users") or {})
        for username, data in users.items():
            if data.get("is_admin") is True:
                return username
        if users:
            return next(iter(users))
    except Exception:
        logger.debug("Could not resolve ownerless event owner", exc_info=True)
    return None


def _event_matches_task(task, event_name: str, meta: Optional[dict]) -> bool:
    """Return True if this event should count toward `task`'s threshold.

    Only "email_received" applies optional per-task scoping. A task may pin:
      - trigger_event_folder: IMAP folder (null/empty ⇒ INBOX, the legacy
        behavior — so existing inbox tasks keep firing on INBOX only).
      - trigger_event_account: EmailAccount.id (null/empty ⇒ any account).
    When `meta` is absent (e.g. an event fired by older code), nothing is
    scoped and every subscribed task matches — preserving prior behavior.
    """
    if event_name != "email_received" or not meta:
        return True

    want_folder = (getattr(task, "trigger_event_folder", None) or "INBOX").strip()
    got_folder = (meta.get("folder") or "INBOX").strip()
    if want_folder and got_folder and want_folder.upper() != got_folder.upper():
        return False

    want_account = (getattr(task, "trigger_event_account", None) or "").strip()
    if want_account and want_account != (meta.get("account_id") or "").strip():
        return False

    return True


async def _handle_event(event_name: str, owner: Optional[str] = None, meta: Optional[dict] = None):
    """Process an event: increment counters, fire tasks that hit their threshold."""
    from core.database import SessionLocal, ScheduledTask

    resolved_owner = _resolve_event_owner(owner)
    db = SessionLocal()
    try:
        filters = [
            ScheduledTask.trigger_type == "event",
            ScheduledTask.trigger_event == event_name,
            ScheduledTask.status == "active",
        ]
        if resolved_owner:
            filters.append(ScheduledTask.owner == resolved_owner)
        else:
            filters.append(ScheduledTask.owner == None)  # noqa: E711

        tasks = db.query(ScheduledTask).filter(*filters).all()
        if not tasks:
            return

        for task in tasks:
            # Skip tasks whose account/folder scope doesn't match this event so
            # their counter never advances on mail they don't care about.
            if not _event_matches_task(task, event_name, meta):
                continue
            threshold = task.trigger_count or 1
            task.trigger_counter = (task.trigger_counter or 0) + 1

            if task.trigger_counter >= threshold:
                task.trigger_counter = 0
                # Persist the trigger before handing off to the in-memory
                # scheduler. If the process restarts while the task is queued
                # behind a model call, `next_run <= now` makes the trigger
                # survive reboot instead of losing the event after the counter
                # has already reset.
                task.next_run = datetime.utcnow()
                db.commit()
                # Fire the task
                if _task_scheduler:
                    logger.info(f"Event '{event_name}' triggered task '{task.name}' (every {threshold})")
                    await _task_scheduler.run_task_now(task.id)
                else:
                    logger.warning(f"Event triggered task '{task.name}' but no scheduler available")
            else:
                db.commit()
                logger.debug(f"Event '{event_name}': task '{task.name}' counter {task.trigger_counter}/{threshold}")

    except Exception:
        logger.exception(f"Error handling event '{event_name}'")
    finally:
        db.close()
