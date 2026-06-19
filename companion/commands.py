"""Signed companion commands for mobile code-development clients.

These commands intentionally do not expose raw shell execution. They provide a
small, fixed command surface for private mobile clients: status inspection,
workspace file inspection, and surgical exact-string file edits behind the
registered-key signed-command verifier.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MAX_LINES = 200
MAX_OUTPUT_CHARS = 20_000
MAX_FILE_ENTRIES = 200
MAX_CHECK_TARGETS = 20
DEFAULT_CHECK_TIMEOUT_SECONDS = 30
MAX_CHECK_TIMEOUT_SECONDS = 120
GIT_TIMEOUT_SECONDS = 5

READ_ONLY_COMMANDS = {
    "capabilities",
    "server_status",
    "workspace_status",
    "git_status",
    "list_files",
    "read_file",
    "run_check",
}
MUTATING_COMMANDS = {
    "edit_file",
}
ALLOWED_COMMANDS = READ_ONLY_COMMANDS | MUTATING_COMMANDS
ADMIN_WORKSPACE_COMMANDS = {"list_files", "read_file", "edit_file", "run_check"}
RUN_CHECKS = {"git_status", "git_diff", "py_compile", "pytest"}

COMMAND_DEFINITIONS = {
    "capabilities": {
        "description": "Return the fixed read-only command list and safety flags.",
        "mode": "read_only",
        "mutating": False,
        "args_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        },
    },
    "server_status": {
        "description": "Return server, version, owner, time, and platform status.",
        "mode": "read_only",
        "mutating": False,
        "args_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        },
    },
    "workspace_status": {
        "description": "Return current process workspace and git summary.",
        "mode": "read_only",
        "mutating": False,
        "args_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        },
    },
    "git_status": {
        "description": "Return the git summary only.",
        "mode": "read_only",
        "mutating": False,
        "args_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        },
    },
    "list_files": {
        "description": "List files and directories inside an explicit workspace path.",
        "mode": "workspace_read",
        "mutating": False,
        "requires_admin": True,
        "args_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["workspace"],
            "properties": {
                "workspace": {"type": "string"},
                "path": {"type": "string"},
                "max_entries": {"type": "integer", "minimum": 1, "maximum": MAX_FILE_ENTRIES},
            },
        },
    },
    "read_file": {
        "description": "Read a bounded UTF-8 text slice from inside an explicit workspace path.",
        "mode": "workspace_read",
        "mutating": False,
        "requires_admin": True,
        "args_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["workspace", "path"],
            "properties": {
                "workspace": {"type": "string"},
                "path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LINES},
            },
        },
    },
    "edit_file": {
        "description": "Apply an exact string replacement inside an explicit workspace path.",
        "mode": "workspace_edit",
        "mutating": True,
        "requires_admin": True,
        "args_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["workspace", "path", "old_string", "new_string"],
            "properties": {
                "workspace": {"type": "string"},
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean"},
            },
        },
    },
    "run_check": {
        "description": "Run a bounded allowlisted verification command inside an explicit workspace.",
        "mode": "workspace_exec",
        "mutating": False,
        "requires_admin": True,
        "raw_shell": False,
        "allowed_checks": sorted(RUN_CHECKS),
        "args_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["workspace", "check"],
            "properties": {
                "workspace": {"type": "string"},
                "check": {
                    "type": "string",
                    "enum": sorted(RUN_CHECKS),
                },
                "targets": {
                    "type": "array",
                    "maxItems": MAX_CHECK_TARGETS,
                    "items": {"type": "string"},
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_CHECK_TIMEOUT_SECONDS,
                },
            },
        },
    },
}


class CompanionCommandError(ValueError):
    """Raised when a signed companion command is unsupported or malformed."""


class CompanionCommandForbidden(PermissionError):
    """Raised when a verified companion command lacks owner privileges."""


def _limited_lines(value: str, *, max_lines: int = MAX_LINES) -> list[str]:
    return value[:MAX_OUTPUT_CHARS].splitlines()[:max_lines]


def _as_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _string_arg(
    args: dict[str, Any],
    name: str,
    *,
    required: bool = False,
    strip: bool = True,
) -> str:
    value = args.get(name)
    if value is None:
        if required:
            raise CompanionCommandError(f"Command arg is required: {name}")
        return ""
    if not isinstance(value, str):
        raise CompanionCommandError(f"Command arg must be a string: {name}")
    cleaned = value.strip() if strip else value
    if required and cleaned == "":
        raise CompanionCommandError(f"Command arg is required: {name}")
    return cleaned


def _int_arg(
    args: dict[str, Any],
    name: str,
    *,
    default: int,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    value = args.get(name)
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise CompanionCommandError(f"Command arg must be an integer: {name}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise CompanionCommandError(f"Command arg must be an integer: {name}") from exc
    if parsed < minimum:
        raise CompanionCommandError(f"Command arg is below minimum: {name}")
    if maximum is not None and parsed > maximum:
        return maximum
    return parsed


def _require_admin_owner(owner: str | None) -> None:
    from src.tool_security import owner_is_admin_or_single_user

    if not owner_is_admin_or_single_user(owner):
        raise CompanionCommandForbidden("Workspace control commands are admin-only")


def companion_workspace_roots() -> list[str]:
    """Configured roots a signed companion workspace command may target."""
    try:
        from src.tool_execution import _tool_path_roots

        roots = _tool_path_roots()
    except Exception:
        roots = []
    out: list[str] = []
    seen: set[str] = set()
    for root in roots:
        try:
            resolved = os.path.realpath(os.path.expanduser(str(root)))
        except OSError:
            continue
        if not resolved or resolved in seen:
            continue
        seen.add(resolved)
        out.append(resolved)
    return out


def _path_is_inside(path: Path, root: str) -> bool:
    path_value = os.path.normcase(str(path))
    root_value = os.path.normcase(root)
    if path_value == root_value:
        return True
    try:
        return os.path.commonpath([path_value, root_value]) == root_value
    except ValueError:
        return False


def _workspace_root(args: dict[str, Any]) -> Path:
    workspace = _string_arg(args, "workspace", required=True)
    root = Path(os.path.realpath(os.path.expanduser(workspace)))
    if not root.is_dir():
        raise CompanionCommandError("Command workspace must be an existing directory")
    if not any(_path_is_inside(root, allowed) for allowed in companion_workspace_roots()):
        raise CompanionCommandError("Command workspace is outside configured companion roots")
    return root


def _workspace_path(args: dict[str, Any], *, require_file_path: bool = True) -> Path:
    workspace = _workspace_root(args)
    raw_path = _string_arg(args, "path", required=require_file_path) or "."
    if "\x00" in raw_path:
        raise CompanionCommandError("Command path is invalid")

    candidate = Path(os.path.expanduser(raw_path.strip()))
    if not candidate.is_absolute():
        candidate = workspace / candidate
    resolved = Path(os.path.realpath(candidate))

    try:
        from src.tool_execution import _is_sensitive_path

        if _is_sensitive_path(str(resolved)):
            raise CompanionCommandError(
                "Command path is inside a sensitive directory or matches a sensitive filename"
            )
    except CompanionCommandError:
        raise
    except Exception:
        pass

    if not _path_is_inside(resolved, str(workspace)):
        raise CompanionCommandError("Command path is outside the workspace")
    return resolved


def _workspace_relative_arg(workspace: Path, raw_path: str, *, allow_selector: bool = False) -> str:
    if "\x00" in raw_path or "\n" in raw_path or "\r" in raw_path:
        raise CompanionCommandError("Command target path is invalid")
    path_part = raw_path
    selector = ""
    if allow_selector and "::" in raw_path:
        path_part, selector = raw_path.split("::", 1)
        selector = f"::{selector}"
    if not path_part.strip():
        raise CompanionCommandError("Command target path is required")
    path = _workspace_path({"workspace": str(workspace), "path": path_part})
    return f"{_relative_to_workspace(path, workspace)}{selector}"


def _string_list_arg(args: dict[str, Any], name: str) -> list[str]:
    value = args.get(name) or []
    if not isinstance(value, list):
        raise CompanionCommandError(f"Command arg must be a list: {name}")
    if len(value) > MAX_CHECK_TARGETS:
        raise CompanionCommandError(f"Command arg has too many entries: {name}")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise CompanionCommandError(f"Command arg entries must be strings: {name}")
        out.append(item.strip())
    return out


def _relative_to_workspace(path: Path, workspace: Path) -> str:
    try:
        return str(path.relative_to(workspace))
    except ValueError:
        return str(path)


def _list_files(args: dict[str, Any]) -> dict[str, Any]:
    workspace = _workspace_root(args)
    target = _workspace_path(args, require_file_path=False)
    max_entries = _int_arg(
        args,
        "max_entries",
        default=MAX_FILE_ENTRIES,
        minimum=1,
        maximum=MAX_FILE_ENTRIES,
    )
    if not target.is_dir():
        raise CompanionCommandError("Command path must be a directory")

    entries: list[dict[str, Any]] = []
    truncated = False
    try:
        with os.scandir(target) as it:
            for entry in it:
                try:
                    child = Path(entry.path)
                    rel = _relative_to_workspace(child, workspace)
                    # Re-run the shared workspace resolver for each displayed
                    # child so sensitive names are omitted from mobile listings.
                    _workspace_path({"workspace": str(workspace), "path": rel})
                    kind = "directory" if entry.is_dir(follow_symlinks=False) else "file"
                    stat = entry.stat(follow_symlinks=False)
                    item = {
                        "name": entry.name,
                        "path": rel,
                        "type": kind,
                        "size": stat.st_size if kind == "file" else None,
                    }
                except (OSError, ValueError, CompanionCommandError):
                    continue
                if len(entries) >= max_entries:
                    truncated = True
                    break
                entries.append(item)
    except OSError as exc:
        raise CompanionCommandError(f"Could not list directory: {exc}") from exc

    entries.sort(key=lambda item: (item["type"] != "directory", item["name"].lower()))
    return {
        "workspace": str(workspace),
        "path": _relative_to_workspace(target, workspace),
        "entries": entries,
        "truncated": truncated,
    }


def _read_file(args: dict[str, Any]) -> dict[str, Any]:
    from src.constants import MAX_READ_CHARS

    workspace = _workspace_root(args)
    path = _workspace_path(args)
    offset = _int_arg(args, "offset", default=0, minimum=1)
    limit = _int_arg(args, "limit", default=0, minimum=1, maximum=MAX_LINES)
    if path.is_dir():
        raise CompanionCommandError("Command path is a directory")

    truncated = False
    try:
        if offset > 0 or limit > 0:
            start = max(offset, 1)
            out: list[str] = []
            line_count = 0
            budget = MAX_READ_CHARS
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line_number, line in enumerate(fh, 1):
                    if line_number < start:
                        continue
                    if limit > 0 and line_count >= limit:
                        truncated = True
                        break
                    out.append(line)
                    line_count += 1
                    budget -= len(line)
                    if budget <= 0:
                        truncated = True
                        break
            content = "".join(out)
        else:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read(MAX_READ_CHARS + 1)
            if len(content) > MAX_READ_CHARS:
                content = content[:MAX_READ_CHARS]
                truncated = True
    except FileNotFoundError as exc:
        raise CompanionCommandError("Command path was not found") from exc
    except OSError as exc:
        raise CompanionCommandError(f"Could not read file: {exc}") from exc

    return {
        "workspace": str(workspace),
        "path": _relative_to_workspace(path, workspace),
        "content": content,
        "truncated": truncated,
    }


def _edit_file(args: dict[str, Any]) -> dict[str, Any]:
    from src.agent_tools.filesystem_tools import _unified_diff

    workspace = _workspace_root(args)
    path = _workspace_path(args)
    old = _string_arg(args, "old_string", required=True, strip=False)
    new = args.get("new_string")
    if not isinstance(new, str):
        raise CompanionCommandError("Command arg must be a string: new_string")
    replace_all = bool(args.get("replace_all", False))
    if old == new:
        raise CompanionCommandError("old_string and new_string are identical")
    if path.is_dir():
        raise CompanionCommandError("Command path is a directory")

    try:
        original = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CompanionCommandError("Command path was not found") from exc
    except UnicodeDecodeError as exc:
        raise CompanionCommandError("Command path is not an editable UTF-8 text file") from exc
    except OSError as exc:
        raise CompanionCommandError(f"Could not read file: {exc}") from exc

    count = original.count(old)
    if count == 0:
        raise CompanionCommandError("old_string not found in command path")
    if count > 1 and not replace_all:
        raise CompanionCommandError(
            f"old_string is not unique in command path ({count} matches)"
        )

    updated = original.replace(old, new) if replace_all else original.replace(old, new, 1)
    try:
        path.write_text(updated, encoding="utf-8")
    except OSError as exc:
        raise CompanionCommandError(f"Could not write file: {exc}") from exc

    diff = _unified_diff(original, updated, str(path))
    return {
        "workspace": str(workspace),
        "path": _relative_to_workspace(path, workspace),
        "replacements": count if replace_all else 1,
        "diff": diff,
    }


def _run_check(args: dict[str, Any]) -> dict[str, Any]:
    workspace = _workspace_root(args)
    check = _string_arg(args, "check", required=True)
    if check not in RUN_CHECKS:
        raise CompanionCommandError("Companion check is not allowed")
    timeout = _int_arg(
        args,
        "timeout_seconds",
        default=DEFAULT_CHECK_TIMEOUT_SECONDS,
        minimum=1,
        maximum=MAX_CHECK_TIMEOUT_SECONDS,
    )
    raw_targets = _string_list_arg(args, "targets")
    targets = [
        _workspace_relative_arg(
            workspace,
            target,
            allow_selector=check == "pytest",
        )
        for target in raw_targets
    ]

    if check == "git_status":
        if targets:
            raise CompanionCommandError("git_status does not accept targets")
        argv = ["git", "-c", "core.fsmonitor=false", "status", "--short", "--branch"]
    elif check == "git_diff":
        argv = [
            "git",
            "-c",
            "core.fsmonitor=false",
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--",
            *targets,
        ]
    elif check == "py_compile":
        if not targets:
            raise CompanionCommandError("py_compile requires at least one target")
        argv = [sys.executable or "python", "-m", "py_compile", *targets]
    else:  # pytest
        if not targets:
            raise CompanionCommandError("pytest requires at least one target")
        argv = [sys.executable or "python", "-m", "pytest", *targets]

    env = {
        **os.environ,
        "TERM": "xterm-256color",
        "COLUMNS": "120",
        "LINES": "40",
    }
    try:
        proc = subprocess.run(
            argv,
            cwd=str(workspace),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        timed_out = False
        exit_code = proc.returncode
        stdout = proc.stdout
        stderr = proc.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = 124
        stdout = _as_text(exc.stdout)
        stderr = _as_text(exc.stderr) + f"\nCommand timed out after {timeout}s"
    except FileNotFoundError as exc:
        raise CompanionCommandError(f"Check executable not found: {argv[0]}") from exc

    return {
        "workspace": str(workspace),
        "check": check,
        "argv": argv,
        "targets": targets,
        "timeout_seconds": timeout,
        "timed_out": timed_out,
        "exit_code": exit_code,
        "stdout": _limited_lines(_as_text(stdout)),
        "stderr": _limited_lines(_as_text(stderr)),
    }


def _run_git(args: list[str], *, cwd: Path) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        return {"available": False, "stdout": [], "stderr": ["git not found"], "exit_code": 127}
    except subprocess.TimeoutExpired:
        return {
            "available": True,
            "stdout": [],
            "stderr": [f"git command timed out after {GIT_TIMEOUT_SECONDS}s"],
            "exit_code": -1,
        }
    return {
        "available": True,
        "stdout": _limited_lines(proc.stdout),
        "stderr": _limited_lines(proc.stderr),
        "exit_code": proc.returncode,
    }


def _git_value(args: list[str], *, cwd: Path) -> str | None:
    result = _run_git(args, cwd=cwd)
    if result["exit_code"] != 0:
        return None
    return "\n".join(result["stdout"]).strip() or None


def _git_workspace(cwd: Path) -> dict[str, Any]:
    root = _git_value(["rev-parse", "--show-toplevel"], cwd=cwd)
    branch = _git_value(["branch", "--show-current"], cwd=cwd)
    status = _run_git(["status", "--short", "--branch"], cwd=cwd)
    status_lines = status["stdout"] if status["exit_code"] == 0 else []
    return {
        "is_git_repo": bool(root),
        "root": root,
        "branch": branch,
        "dirty": any(line and not line.startswith("## ") for line in status_lines),
        "status": status_lines,
        "git": {
            "available": status["available"],
            "exit_code": status["exit_code"],
            "stderr": status["stderr"],
        },
    }


def command_capabilities() -> dict[str, Any]:
    return {
        "mode": "workspace_control",
        "allowed_commands": sorted(ALLOWED_COMMANDS),
        "commands": command_definitions(),
        "mutating_commands_enabled": True,
        "mutating_commands": sorted(MUTATING_COMMANDS),
        "workspace_exec_enabled": True,
        "allowed_checks": sorted(RUN_CHECKS),
        "allowed_workspace_roots": companion_workspace_roots(),
        "raw_shell_enabled": False,
    }


def command_definitions() -> list[dict[str, Any]]:
    """Stable mobile-client command catalogue."""
    return [
        {"name": name, **COMMAND_DEFINITIONS[name]}
        for name in sorted(COMMAND_DEFINITIONS)
    ]


def validate_command_args(command: str, args: dict[str, Any]) -> None:
    schema = COMMAND_DEFINITIONS[command]["args_schema"]
    allowed = set((schema.get("properties") or {}).keys())
    if not schema.get("additionalProperties", True):
        unexpected = sorted(set(args) - allowed)
        if unexpected:
            raise CompanionCommandError(
                f"Command args not allowed: {', '.join(unexpected)}"
            )
    missing = [
        name
        for name in schema.get("required", [])
        if args.get(name) in (None, "")
    ]
    if missing:
        raise CompanionCommandError(
            f"Command args required: {', '.join(sorted(missing))}"
        )


def execute_companion_command(*, owner: str, body: dict[str, Any]) -> dict[str, Any]:
    """Execute one signed command from a verified companion request."""
    if not isinstance(body, dict):
        raise CompanionCommandError("Command body must be a JSON object")
    command = str(body.get("command") or "").strip()
    if not command:
        raise CompanionCommandError("Command is required")
    if command not in ALLOWED_COMMANDS:
        raise CompanionCommandError("Companion command is not allowed")
    args = body.get("args") or {}
    if not isinstance(args, dict):
        raise CompanionCommandError("Command args must be a JSON object")
    validate_command_args(command, args)
    if command in ADMIN_WORKSPACE_COMMANDS:
        _require_admin_owner(owner)

    cwd = Path(os.getcwd()).resolve()
    if command == "capabilities":
        payload = command_capabilities()
    elif command == "server_status":
        from core.constants import APP_VERSION

        payload = {
            "name": "odysseus",
            "version": APP_VERSION,
            "owner": owner,
            "server_time": datetime.now(timezone.utc).isoformat(),
            "platform": {
                "system": platform.system(),
                "machine": platform.machine(),
                "python": platform.python_version(),
            },
        }
    elif command == "workspace_status":
        payload = {
            "cwd": str(cwd),
            "git": _git_workspace(cwd),
            "commands": command_capabilities(),
        }
    elif command == "git_status":
        payload = _git_workspace(cwd)
    elif command == "list_files":
        payload = _list_files(args)
    elif command == "read_file":
        payload = _read_file(args)
    elif command == "edit_file":
        payload = _edit_file(args)
    else:  # run_check
        payload = _run_check(args)

    return {
        "command": command,
        "mode": COMMAND_DEFINITIONS[command]["mode"],
        "mutating": bool(COMMAND_DEFINITIONS[command]["mutating"]),
        "result": payload,
    }
