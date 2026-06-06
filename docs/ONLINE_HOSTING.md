# Hosting Odysseus From A Mac

These are the safer defaults for using Odysseus from a PC or phone.

1. Copy `.env.online.example` to `.env` on the Mac and edit `ALLOWED_ORIGINS`.
2. Keep `AUTH_ENABLED=true`, `LOCALHOST_BYPASS=false`, and `ODYSSEUS_SINGLE_USER=0`.
3. Start Odysseus locally and create the first admin from the Mac at `/setup`.
4. Put HTTPS in front of it before exposing it beyond your LAN. Tailscale is the simplest option; Caddy or nginx also work.
5. Set `SECURE_COOKIES=true` only when the browser reaches Odysseus over HTTPS.

Do not expose first-run setup publicly. If you must bootstrap without local Mac access, set `ODYSSEUS_SETUP_TOKEN` to a long one-time random value and send it as the `X-Odysseus-Setup-Token` header, then remove it after setup.

Private CalDAV targets are blocked by default. Leave `ODYSSEUS_ALLOW_PRIVATE_CALDAV=0` unless you intentionally need a user-configured calendar URL on your LAN/VPN. For shared online deployments, keep `EMBEDDING_BLOCK_PRIVATE_IPS=true`; set it false only when an admin intentionally points embeddings at a local model server such as Ollama.
