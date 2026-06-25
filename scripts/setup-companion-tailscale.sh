#!/bin/bash
set -euo pipefail

REPO_DIR="${ODYSSEUS_REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENV_FILE="$REPO_DIR/.env"
PYTHON_BIN="${PYTHON_BIN:-}"
if [ -z "$PYTHON_BIN" ] && [ -x "$REPO_DIR/venv/bin/python3" ]; then
    PYTHON_BIN="$REPO_DIR/venv/bin/python3"
fi
if [ -z "$PYTHON_BIN" ]; then
    PYTHON_BIN="$(command -v python3 || true)"
fi
TAILSCALE_SOCKET_DEFAULT="$HOME/.local/share/tailscale/tailscaled.socket"

if [ -z "$PYTHON_BIN" ]; then
    echo "python3 is required to validate the Tailscale base URL."
    exit 1
fi

usage() {
    cat <<'EOF'
Usage: scripts/setup-companion-tailscale.sh [--base-url https://host.ts.net] [--no-serve] [--port 7860]

Configures Odysseus companion pairing for a private Tailscale HTTPS origin:
  - writes COMPANION_BASE_URL into .env
  - optionally runs `tailscale serve` to proxy HTTPS to the local app port

Examples:
  ./scripts/setup-companion-tailscale.sh
  ./scripts/setup-companion-tailscale.sh --port 7860
  ./scripts/setup-companion-tailscale.sh --base-url https://odysseus-mac.example.ts.net --no-serve
EOF
}

load_env_file() {
    [ -f "$ENV_FILE" ] || return 0
    while IFS='=' read -r key value; do
        [[ "$key" =~ ^[[:space:]]*# ]] && continue
        [[ -z "${key// }" ]] && continue
        value="${value%%#*}"
        value="${value#"${value%%[![:space:]]*}"}"
        value="${value%"${value##*[![:space:]]}"}"
        [ -n "$key" ] && [ -z "${!key+x}" ] && export "$key=$value"
    done < "$ENV_FILE"
}

detect_tailscale_socket() {
    if [ -n "${TAILSCALE_SOCKET:-}" ]; then
        printf '%s\n' "$TAILSCALE_SOCKET"
        return 0
    fi
    if [ -S "$TAILSCALE_SOCKET_DEFAULT" ]; then
        printf '%s\n' "$TAILSCALE_SOCKET_DEFAULT"
        return 0
    fi
    return 1
}

find_tailscale_bin() {
    if [ -n "${TAILSCALE_BIN:-}" ] && [ -x "${TAILSCALE_BIN}" ]; then
        printf '%s\n' "$TAILSCALE_BIN"
        return 0
    fi
    for app in /Applications/Tailscale.app "$HOME/Applications/Tailscale.app"; do
        [ -d "$app/Contents/MacOS" ] || continue
        for bin in "$app/Contents/MacOS/tailscale" "$app/Contents/MacOS/Tailscale"; do
            [ -x "$bin" ] || continue
            printf '%s\n' "$bin"
            return 0
        done
        found="$(find "$app/Contents/MacOS" -maxdepth 1 -type f -perm -111 2>/dev/null | head -n 1 || true)"
        if [ -n "$found" ]; then
            printf '%s\n' "$found"
            return 0
        fi
    done
    if command -v tailscale >/dev/null 2>&1; then
        command -v tailscale
        return 0
    fi
    return 1
}

normalize_origin() {
    "$PYTHON_BIN" - "$1" <<'PY'
import sys
from urllib.parse import urlsplit, urlunsplit

raw = (sys.argv[1] or "").strip()
if not raw or any(ch.isspace() for ch in raw):
    raise SystemExit(1)

try:
    parsed = urlsplit(raw)
except ValueError:
    raise SystemExit(1)

if parsed.scheme not in {"http", "https"} or not parsed.netloc:
    raise SystemExit(1)
if parsed.username or parsed.password:
    raise SystemExit(1)
if parsed.query or parsed.fragment:
    raise SystemExit(1)
if parsed.path not in {"", "/"}:
    raise SystemExit(1)
try:
    parsed.port
except ValueError:
    raise SystemExit(1)
if not parsed.hostname:
    raise SystemExit(1)

print(urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/"))
PY
}

write_env_value() {
    mkdir -p "$(dirname "$ENV_FILE")"
    touch "$ENV_FILE"
    "$PYTHON_BIN" - "$ENV_FILE" "$1" "$2" <<'PY'
import pathlib
import sys

env_path = pathlib.Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]

lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
needle = f"{key}="
updated = False
out = []

for line in lines:
    stripped = line.lstrip()
    if stripped.startswith(needle):
        out.append(f"{key}={value}")
        updated = True
    else:
        out.append(line)

if not updated:
    if out and out[-1] != "":
        out.append("")
    out.append(f"{key}={value}")

env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
}

BASE_URL=""
DO_SERVE=1
PORT=""

while [ "$#" -gt 0 ]; do
    case "$1" in
        --base-url)
            [ "$#" -ge 2 ] || { usage; exit 1; }
            BASE_URL="$2"
            shift 2
            ;;
        --no-serve)
            DO_SERVE=0
            shift
            ;;
        --port)
            [ "$#" -ge 2 ] || { usage; exit 1; }
            PORT="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1"
            usage
            exit 1
            ;;
    esac
done

load_env_file

if [ -z "$PORT" ]; then
    PORT="${APP_PORT:-7860}"
fi
if ! [[ "$PORT" =~ ^[0-9]+$ ]] || [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
    echo "Invalid port: $PORT"
    exit 1
fi

TAILSCALE_BIN_RESOLVED=""
TAILSCALE_SOCKET_RESOLVED=""
TAILSCALE_ARGS=()
TAILSCALE_TUN_ENABLED=""
if [ "$DO_SERVE" -eq 1 ] || [ -z "$BASE_URL" ]; then
    TAILSCALE_BIN_RESOLVED="$(find_tailscale_bin || true)"
    if [ -z "$TAILSCALE_BIN_RESOLVED" ]; then
        echo "Tailscale CLI was not found."
        echo "Install Tailscale and sign this Mac into your tailnet first, or pass --base-url with an existing private HTTPS origin."
        exit 1
    fi
fi

if [ -z "$BASE_URL" ]; then
    STATUS_JSON="$("$TAILSCALE_BIN_RESOLVED" status --json 2>/dev/null || true)"
    if [ -z "$STATUS_JSON" ]; then
        TAILSCALE_SOCKET_RESOLVED="$(detect_tailscale_socket || true)"
        if [ -n "$TAILSCALE_SOCKET_RESOLVED" ]; then
            TAILSCALE_ARGS=("--socket=$TAILSCALE_SOCKET_RESOLVED")
            STATUS_JSON="$("$TAILSCALE_BIN_RESOLVED" "${TAILSCALE_ARGS[@]}" status --json 2>/dev/null || true)"
        fi
    fi
    if [ -z "$STATUS_JSON" ]; then
        echo "Could not read Tailscale status."
        echo "Make sure Tailscale is running and this Mac is signed into your tailnet."
        exit 1
    fi
    TAILSCALE_TUN_ENABLED="$("$PYTHON_BIN" - "$STATUS_JSON" <<'PY'
import json
import sys

try:
    data = json.loads(sys.argv[1])
except json.JSONDecodeError:
    print("")
    raise SystemExit(0)

tun = data.get("TUN")
if isinstance(tun, bool):
    print("true" if tun else "false")
else:
    print("")
PY
)"
    DNS_NAME="$("$PYTHON_BIN" - "$STATUS_JSON" <<'PY'
import json
import sys

try:
    data = json.loads(sys.argv[1])
except json.JSONDecodeError:
    print("")
    raise SystemExit(0)

self = data.get("Self") or {}
dns_name = (self.get("DNSName") or "").rstrip(".")
print(dns_name)
PY
)"
    if [ -z "$DNS_NAME" ]; then
        echo "This Tailscale status does not include a DNS name yet."
        echo "Make sure MagicDNS is available and this Mac is logged into the tailnet."
        exit 1
    fi
    BASE_URL="https://$DNS_NAME"
fi

BASE_URL="$(normalize_origin "$BASE_URL" || true)"
if [ -z "$BASE_URL" ]; then
    echo "Invalid private origin. Use a plain http(s) origin such as https://odysseus-mac.example.ts.net"
    exit 1
fi

if [ "$DO_SERVE" -eq 1 ]; then
    if [ "${#TAILSCALE_ARGS[@]}" -gt 0 ]; then
        "$TAILSCALE_BIN_RESOLVED" "${TAILSCALE_ARGS[@]}" serve --bg --yes "http://127.0.0.1:$PORT" >/dev/null
    else
        "$TAILSCALE_BIN_RESOLVED" serve --bg --yes "http://127.0.0.1:$PORT" >/dev/null
    fi
fi

write_env_value "COMPANION_BASE_URL" "$BASE_URL"

APP_BIND_VALUE="${APP_BIND:-127.0.0.1}"
PAIR_PAGE_LOCAL="http://127.0.0.1:$PORT/api/companion/pair"

echo
echo "Companion Tailscale setup complete."
echo "  COMPANION_BASE_URL=$BASE_URL"
echo "  Local pair page: $PAIR_PAGE_LOCAL"
if [ "$DO_SERVE" -eq 1 ]; then
    echo "  Tailscale Serve target: http://127.0.0.1:$PORT"
    echo "  Tailscale URL: $BASE_URL"
fi
if [ -n "$TAILSCALE_SOCKET_RESOLVED" ]; then
    echo "  Tailscale socket: $TAILSCALE_SOCKET_RESOLVED"
fi
if [ "$TAILSCALE_TUN_ENABLED" = "false" ]; then
    echo
    echo "Warning: detected rootless/userspace Tailscale (TUN=false)."
    echo "The private ts.net origin can still work for tailnet devices, but normal"
    echo "macOS browsers and curl on this host may not resolve it unless they go"
    echo "through Tailscale's local proxy. For reliable system-wide testing and"
    echo "cleaner mobile debugging, prefer the full Tailscale.app install with the"
    echo "network extension enabled, then rerun this setup helper."
fi
if [ "$APP_BIND_VALUE" != "127.0.0.1" ] && [ "$APP_BIND_VALUE" != "localhost" ]; then
    echo
    echo "Note: APP_BIND is currently $APP_BIND_VALUE."
    echo "Tailscale Serve works with Odysseus bound only to loopback, so you can switch back to 127.0.0.1 if you do not need direct LAN access."
fi
echo
echo "Restart Odysseus if it is already running, then mint a fresh pairing code from the local pair page."
