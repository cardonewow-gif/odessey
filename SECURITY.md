# Security Policy

Odysseus is a self-hosted AI workspace with privileged local capabilities. Please do not run it as a public, unauthenticated service.

## Supported Versions

Security fixes are handled on the default branch until formal releases are cut.

## Deployment Guidance

- Keep `AUTH_ENABLED=true` for any network-accessible deployment.
- Keep `LOCALHOST_BYPASS=false` outside local development.
- Set `SECURE_COOKIES=true` when Odysseus is served through HTTPS by a trusted reverse proxy or private access gateway.
- Use HTTPS when exposing the app beyond localhost.
- Put the authenticated Odysseus web/API entrypoint behind a trusted reverse proxy or private access layer such as Cloudflare Access, Tailscale, or a VPN.
- Keep ChromaDB, SearXNG, ntfy, Ollama, vLLM, llama.cpp, databases, and raw model/provider APIs internal-only.
- Protect `.env`, `data/`, `logs/`, uploads, generated media, backups, auth/session files, database files, API keys, and model/provider tokens.
- Disable open signup unless you intentionally want new accounts.
- Keep demo/test users non-admin, and remove them entirely on serious deployments.
- Give admin accounts strong passwords and enable 2FA where possible.
- Leave high-risk agent tools restricted to admins: shell, Python, file read/write, email send/read, MCP, app API, task/skill/memory management, settings, tokens, and model serving.
- Rotate API keys, webhook secrets, and Odysseus API tokens if they appear in logs, screenshots, demos, or shared chats.
- Treat shell, model-serving, MCP, email, calendar, and vault features as privileged admin functionality.
- Common internal-only ports are Odysseus `7000`, SearXNG `8080`, ntfy `8091`, ChromaDB `8100`, Ollama `11434`, and local model/provider APIs such as `8000-8020`.

## Optional Sandboxed Code Execution

By default the agent's `bash` and `python` tools run **directly on the host**. For defense-in-depth (untrusted prompts, shared deployments) you can run them inside a hardened throwaway container instead:

```bash
ODYSSEUS_SANDBOX=1            # opt in; requires podman or docker on PATH
```

Scope and behavior:

- **Affects the `bash` and `python` tools only** — *not* other agent tools (file read/write, web, email, MCP, etc.). Those still run with their normal host privileges; sandboxing here is about arbitrary code execution, not a general capability jail.
- **Enabling it changes the execution environment for bash/python.** Code runs inside the container image (default `python:3.12-slim`), not on the host: a different filesystem, different installed packages/interpreters, `--network none` (no network), read-only root with a writable `/tmp` tmpfs, all capabilities dropped, `no-new-privileges`, non-root (`nobody`), and memory/CPU/pid caps. Host files are **not** bind-mounted in. Scripts that expect host tools, host files, or network access will behave differently.
- **Fails closed.** If `ODYSSEUS_SANDBOX=1` but no container runtime is found, bash/python **refuse to run** (clear error, exit 126) rather than silently executing on the host. A security toggle must not downgrade itself.
- **Explicit host fallback.** If you want "prefer the sandbox, but fall back to the host when no runtime is available", opt in separately with `ODYSSEUS_SANDBOX_FALLBACK=host`. Enabling the sandbox alone never implies host fallback.
- Tunables: `ODYSSEUS_SANDBOX_{IMAGE,MEMORY,CPUS,PIDS,TMPFS}`.

Verify the isolation is real (with `ODYSSEUS_SANDBOX=1` and a runtime installed):

```bash
# network is unreachable from inside the sandbox (expect a failure, not 200)
podman run --rm --network none --read-only --tmpfs /tmp python:3.12-slim \
  python -I -c "import urllib.request as u; u.urlopen('https://example.com', timeout=5)"
# -> urllib.error.URLError / OSError (Errno 101, Network is unreachable)

# root filesystem is read-only (expect OSError: [Errno 30] Read-only file system)
podman run --rm --read-only --tmpfs /tmp python:3.12-slim \
  python -I -c "open('/etc/pwned','w').write('x')"
```

## Publishing A Fork

Before pushing a public fork, run:

```bash
git status --short
git check-ignore -v .env data/auth.json data/app.db logs/compound.log odysseus.db
git grep -n -I -E "(sk-[A-Za-z0-9_-]{20,}|xox[baprs]-|AIza[0-9A-Za-z_-]{20,}|Bearer [A-Za-z0-9._~+/-]{20,})" -- . ':!static/lib/**' ':!package-lock.json'
```

Only `.env.example`, docs, source, tests, and static assets should be committed. Never commit live `.env` values, `data/` contents, local databases, uploaded files, generated media, logs, backups, auth/session files, API keys, model/provider tokens, password hashes, or personal documents.

## Reporting

Please report vulnerabilities privately via GitHub security advisories if available, or by opening a minimal issue that does not disclose exploit details.
