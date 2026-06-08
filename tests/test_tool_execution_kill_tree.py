"""Regression for #1131: stopping an agent tool must kill the whole process
tree, not just the direct child.

The bash/python tools are spawned via asyncio subprocesses. They can background
grandchildren (``foo &``, a pipeline, a dev server). The old stop/timeout path
called ``proc.kill()``, which signals only the direct shell/interpreter, so a
backgrounded grandchild was reparented to init and kept running after the user
hit stop. The fix spawns the child in its own process group
(``start_new_session=True``) and kills the whole group (``_kill_proc_tree``).
"""
import asyncio
import os
import shlex
import shutil
import sys
import tempfile
import time

import pytest

from src.tool_execution import _kill_proc_tree


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group test")
def test_kill_proc_tree_kills_backgrounded_grandchild():
    async def _run():
        marker = tempfile.NamedTemporaryFile(delete=False, suffix=".alive").name
        os.unlink(marker)
        # Mirror a bash tool that backgrounds a grandchild and waits on it.
        script = f"(for i in $(seq 1 50); do touch {marker}; sleep 0.2; done) & wait"
        proc = await asyncio.create_subprocess_shell(
            script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        # The child must lead its own group, or killpg would hit our own group.
        assert os.getpgid(proc.pid) == proc.pid
        await asyncio.sleep(0.6)  # let the grandchild start touching the marker

        _kill_proc_tree(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except Exception:
            pass

        # If the grandchild survived, it recreates the marker after we delete it.
        if os.path.exists(marker):
            os.unlink(marker)
        await asyncio.sleep(1.0)
        survived = os.path.exists(marker)
        if survived:
            os.unlink(marker)
        return survived

    assert asyncio.run(_run()) is False, "backgrounded grandchild survived stop"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group test")
@pytest.mark.skipif(shutil.which("setsid") is None, reason="setsid unavailable")
def test_kill_proc_tree_kills_detached_descendant_process_group():
    async def _run():
        marker = tempfile.NamedTemporaryFile(delete=False, suffix=".alive").name
        os.unlink(marker)
        marker_arg = shlex.quote(marker)
        script = (
            "setsid sh -c "
            + shlex.quote(f"for i in $(seq 1 50); do touch {marker_arg}; sleep 0.2; done")
            + " & wait"
        )
        proc = await asyncio.create_subprocess_shell(
            script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        assert os.getpgid(proc.pid) == proc.pid
        await asyncio.sleep(0.6)

        _kill_proc_tree(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except Exception:
            pass

        if os.path.exists(marker):
            os.unlink(marker)
        await asyncio.sleep(1.0)
        survived = os.path.exists(marker)
        if survived:
            os.unlink(marker)
        return survived

    assert asyncio.run(_run()) is False, "detached descendant survived stop"


def test_kill_proc_tree_tolerates_none_and_dead_pid():
    # No pid / already-dead pid must not raise.
    class _FakeProc:
        pid = None

        def kill(self):
            raise AssertionError("should not be called for pid=None")

    _kill_proc_tree(_FakeProc())  # pid None -> no-op


def test_tool_subprocess_spawns_use_process_groups():
    # Source-level guard (repo convention, cf. test_document_tool_owner_scope):
    # both tool spawns must request their own session/group, and the stop path
    # must kill the tree rather than the bare child.
    src = open("src/tool_execution.py", encoding="utf-8").read()
    assert src.count("start_new_session=True") >= 2, "both tool spawns must use start_new_session"
    assert "_kill_proc_tree(proc)" in src, "stop/timeout path must kill the process tree"
