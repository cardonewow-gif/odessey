"""Optional sandboxed code execution.

bash/python tool calls run on the host by default (unchanged). When
ODYSSEUS_SANDBOX=1 and a container runtime exists, they run inside a hardened
throwaway container instead. When sandboxing is required but no runtime is
available, code execution FAILS CLOSED rather than silently running on the host.

These tests patch the module-level sandbox flags directly (rather than reloading
the module) so they are order-independent in the full suite.
"""

import asyncio

import pytest

import src.tool_execution as te


def _set(monkeypatch, *, enabled, fallback_host=False, runtime=None):
    """Configure the sandbox flags and the runtime probe for one test.

    `runtime` is the value `shutil.which` should return for podman/docker:
    None = nothing on PATH; a callable = used as-is; a str = that path for any.
    """
    monkeypatch.setattr(te, "SANDBOX_ENABLED", enabled)
    monkeypatch.setattr(te, "SANDBOX_FALLBACK_HOST", fallback_host)
    if callable(runtime):
        which = runtime
    else:
        which = lambda x: runtime
    monkeypatch.setattr(te._shutil, "which", which)


def test_sandbox_off_by_default(monkeypatch):
    """Disabled -> None, even if podman is on PATH (opt-in only)."""
    _set(monkeypatch, enabled=False, runtime="/usr/bin/podman")
    assert te._sandbox_runtime() is None


def test_sandbox_fails_closed_without_runtime(monkeypatch):
    """Enabled but no podman/docker and no host-fallback opt-in -> FAIL CLOSED:
    raise SandboxUnavailable rather than silently downgrade to host execution."""
    _set(monkeypatch, enabled=True, fallback_host=False, runtime=None)
    with pytest.raises(te.SandboxUnavailable):
        te._sandbox_runtime()


def test_sandbox_host_fallback_is_opt_in(monkeypatch):
    """ODYSSEUS_SANDBOX_FALLBACK=host re-enables host execution explicitly
    (the only way enabling the sandbox is allowed to run on the host)."""
    _set(monkeypatch, enabled=True, fallback_host=True, runtime=None)
    assert te._sandbox_runtime() is None


def test_sandbox_picks_podman_then_docker(monkeypatch):
    _set(monkeypatch, enabled=True,
         runtime=lambda x: "/usr/bin/" + x if x == "podman" else None)
    assert te._sandbox_runtime() == "podman"
    monkeypatch.setattr(te._shutil, "which",
                        lambda x: "/usr/bin/" + x if x == "docker" else None)
    assert te._sandbox_runtime() == "docker"


def test_wrapper_has_hardening_flags():
    argv = te._wrap_in_sandbox("podman", ["python", "-I", "-c", "print(1)"])
    joined = " ".join(argv)
    assert argv[:3] == ["podman", "run", "--rm"]
    assert "--network none" in joined          # no network by default
    assert "--read-only" in joined             # read-only root fs
    assert "--cap-drop ALL" in joined          # all capabilities dropped
    assert "no-new-privileges" in joined       # no privilege escalation
    assert "--memory" in argv and "--cpus" in argv and "--pids-limit" in argv
    assert "65534:65534" in joined             # non-root (nobody)
    # the user's code argv is preserved at the end
    assert argv[-4:] == ["python", "-I", "-c", "print(1)"]


def test_wrapper_optional_network():
    off = " ".join(te._wrap_in_sandbox("podman", ["bash", "-lc", "x"]))
    on = " ".join(te._wrap_in_sandbox("podman", ["bash", "-lc", "x"], network=True))
    assert "--network none" in off
    assert "--network bridge" in on


def test_blocked_sandbox_never_runs_code_on_host(monkeypatch):
    """The core guarantee pewds asked for: ODYSSEUS_SANDBOX=1 + no runtime =
    NO host execution. The bash/python tool path returns a fail-closed error
    (exit 126) and never spawns a host process."""
    _set(monkeypatch, enabled=True, fallback_host=False, runtime=None)

    async def _boom(*a, **k):
        raise AssertionError("host subprocess spawned despite required-but-unavailable sandbox")

    monkeypatch.setattr(te.asyncio, "create_subprocess_shell", _boom)
    monkeypatch.setattr(te.asyncio, "create_subprocess_exec", _boom)

    for tool in ("bash", "python"):
        res = asyncio.run(te._direct_fallback(tool, "echo this-must-not-run"))
        assert res["exit_code"] == 126, res
        assert "fail closed" in res["error"].lower()


def test_non_code_tools_unaffected_when_sandbox_unavailable(monkeypatch, tmp_path):
    """Sandboxing covers code execution only — read_file/write_file must still
    work when the sandbox is required but unavailable (they don't run code)."""
    _set(monkeypatch, enabled=True, fallback_host=False, runtime=None)
    p = tmp_path / "note.txt"
    res = asyncio.run(te._direct_fallback("write_file", f"{p}\nhello"))
    assert res["exit_code"] == 0, res
    res = asyncio.run(te._direct_fallback("read_file", str(p)))
    assert "hello" in res["output"]
