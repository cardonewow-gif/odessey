from __future__ import annotations

import os
import pathlib
import subprocess
import textwrap


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "setup-companion-tailscale.sh"


def _run_script(tmp_path: pathlib.Path, *args: str, extra_env: dict[str, str] | None = None):
    env = os.environ.copy()
    env["ODYSSEUS_REPO_DIR"] = str(tmp_path)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        text=True,
        capture_output=True,
        env=env,
        timeout=60,
    )


def test_setup_companion_tailscale_derives_dns_and_writes_env(tmp_path: pathlib.Path):
    (tmp_path / ".env").write_text("APP_PORT=8123\nAPP_BIND=127.0.0.1\n", encoding="utf-8")
    log_path = tmp_path / "tailscale.log"
    tailscale_path = tmp_path / "tailscale"
    tailscale_path.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            set -eu
            if [ "${{1#--socket=}}" != "$1" ]; then
              shift
            fi
            if [ "$1" = "status" ] && [ "$2" = "--json" ]; then
              printf '%s' '{{"TUN":true,"Self":{{"DNSName":"odysseus-mac.taildc85bf.ts.net."}}}}'
              exit 0
            fi
            if [ "$1" = "serve" ]; then
              printf '%s\\n' "$@" >> "{log_path}"
              exit 0
            fi
            exit 1
            """
        ),
        encoding="utf-8",
    )
    tailscale_path.chmod(0o755)

    proc = _run_script(
        tmp_path,
        extra_env={"TAILSCALE_BIN": str(tailscale_path)},
    )

    assert proc.returncode == 0, proc.stderr
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "COMPANION_BASE_URL=https://odysseus-mac.taildc85bf.ts.net\n" in env_text
    assert "Local pair page: http://127.0.0.1:8123/api/companion/pair" in proc.stdout
    assert "Tailscale URL: https://odysseus-mac.taildc85bf.ts.net" in proc.stdout
    assert log_path.read_text(encoding="utf-8").splitlines() == [
        "serve",
        "--bg",
        "--yes",
        "http://127.0.0.1:8123",
    ]


def test_setup_companion_tailscale_warns_for_rootless_userspace_mode(tmp_path: pathlib.Path):
    (tmp_path / ".env").write_text("APP_PORT=7860\n", encoding="utf-8")
    tailscale_path = tmp_path / "tailscale"
    tailscale_path.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            set -eu
            if [ "${1#--socket=}" != "$1" ]; then
              shift
            fi
            if [ "$1" = "status" ] && [ "$2" = "--json" ]; then
              printf '%s' '{"TUN":false,"Self":{"DNSName":"odysseus-mac.taildc85bf.ts.net."}}'
              exit 0
            fi
            if [ "$1" = "serve" ]; then
              exit 0
            fi
            exit 1
            """
        ),
        encoding="utf-8",
    )
    tailscale_path.chmod(0o755)

    proc = _run_script(
        tmp_path,
        extra_env={"TAILSCALE_BIN": str(tailscale_path)},
    )

    assert proc.returncode == 0, proc.stderr
    assert "Warning: detected rootless/userspace Tailscale (TUN=false)." in proc.stdout


def test_setup_companion_tailscale_prefers_default_daemon_over_custom_socket(tmp_path: pathlib.Path):
    (tmp_path / ".env").write_text("APP_PORT=7860\n", encoding="utf-8")
    log_path = tmp_path / "tailscale.log"
    tailscale_path = tmp_path / "tailscale"
    tailscale_path.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            set -eu
            if [ "$1" = "status" ] && [ "$2" = "--json" ]; then
              printf '%s' '{{"TUN":true,"Self":{{"DNSName":"odysseus-mac.taildc85bf.ts.net."}}}}'
              exit 0
            fi
            if [ "${{1#--socket=}}" != "$1" ]; then
              echo "unexpected socket usage" >> "{log_path}"
              exit 1
            fi
            if [ "$1" = "serve" ]; then
              printf '%s\\n' "$@" >> "{log_path}"
              exit 0
            fi
            exit 1
            """
        ),
        encoding="utf-8",
    )
    tailscale_path.chmod(0o755)

    proc = _run_script(
        tmp_path,
        extra_env={
            "TAILSCALE_BIN": str(tailscale_path),
            "TAILSCALE_SOCKET": str(tmp_path / "should-not-be-used.sock"),
        },
    )

    assert proc.returncode == 0, proc.stderr
    assert "unexpected socket usage" not in log_path.read_text(encoding="utf-8")


def test_setup_companion_tailscale_accepts_manual_base_url_without_cli(tmp_path: pathlib.Path):
    (tmp_path / ".env").write_text("APP_PORT=7860\n", encoding="utf-8")

    proc = _run_script(
        tmp_path,
        "--base-url",
        "https://custom.example.ts.net/",
        "--no-serve",
    )

    assert proc.returncode == 0, proc.stderr
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "COMPANION_BASE_URL=https://custom.example.ts.net\n" in env_text
    assert "Tailscale Serve target" not in proc.stdout
