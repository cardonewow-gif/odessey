# Companion bridge

A thin, additive layer so a private companion client (e.g. a phone on LAN,
Tailscale, WireGuard, or an authenticated HTTPS proxy) can discover what an
Odysseus server offers and pair to it, without duplicating any LLM logic.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/companion/ping` | session or `chat` token | cheap, auth-validated health check |
| GET | `/api/companion/info` | session or `chat` token | server identity + capability flags |
| GET | `/api/companion/manifest` | session or `chat` token | stable mobile/PWA client contract |
| GET | `/api/companion/models` | session or `chat` token | the **caller's own** model endpoints |
| GET | `/api/companion/sessions` | session or `chat` token | compact chat sessions owned by the caller |
| POST | `/api/companion/sessions` | session or `chat` token | create a chat session from an owner-visible saved model endpoint |
| POST | `/api/chat_stream` | session or `chat` token | stream a chat/agent turn for an owner-owned session |
| GET | `/api/chat/resume/{session_id}` | session or `chat` token | resume a detached active stream |
| POST | `/api/chat/stop/{session_id}` | session or `chat` token | stop a detached active stream |
| GET | `/api/chat/stream_status/{session_id}` | session or `chat` token | check whether a stream is active |
| GET | `/api/companion/goals` | session or `chat` token | list server-owned goal runs |
| POST | `/api/companion/goals` | session or `chat` token | start a server-owned goal loop for an existing owned session |
| GET | `/api/companion/goals/{run_id}` | session or `chat` token | inspect one goal run |
| POST | `/api/companion/goals/{run_id}/resume` | session or `chat` token | resume a paused/blocked/error goal run |
| POST | `/api/companion/goals/{run_id}/stop` | session or `chat` token | stop a running goal run |
| GET | `/api/companion/keys` | admin session or `chat,remote_development` token | active approved command-key metadata for the caller |
| POST | `/api/companion/keys` | admin session or `chat,remote_development` token | register an on-device Ed25519 public key for future signed commands |
| DELETE | `/api/companion/keys/{key_id}` | admin session or `chat,remote_development` token | revoke one of the caller's approved command keys |
| POST | `/api/companion/commands` | admin session or `chat,remote_development` token + registered signature | run a fixed companion command |
| GET | `/api/companion/pair` | **admin cookie** | pairing page (a form; never mints) |
| POST | `/api/companion/pair` | **admin cookie** | mint a one-time pairing token (`?format=json` for an in-app screen) |

`/models` scopes to the caller's real owner plus legacy null-owner shared rows
(same rule as `owner_filter`) and never returns API-key material.

Bearer-token callers must carry the `chat` scope for mobile chat/session/model
work. Signed command key enrollment and command execution additionally require
the `remote_development` scope. A paired companion token has both scopes by
construction; unrelated integration tokens such as todos, email, documents,
calendar, or memory tokens are rejected by companion endpoints.

## Mobile client contract

`GET /api/companion/manifest` is the machine-readable contract for thin mobile
clients (PWA or React Native) that want to pair with a private Odysseus server.
It advertises:

- the required bearer scope (`chat`), command scope (`remote_development`), and
  pairing endpoint,
- the private-network deployment posture (LAN/Tailscale/WireGuard/private HTTPS
  proxy; not public internet) plus any configured private `base_url`,
- the current safe read endpoints,
- session listing/creation endpoints plus the existing chat stream/resume/stop
  endpoints for mobile chat/control state,
- server-owned goal-run endpoints for mobile "pursue until complete" workflows,
- the signed-command protocol, command catalogue, owner-approved public-key
  enrollment, and nonce storage,
- and the current remote-development boundary: signed workspace file
  inspection/edit commands are enabled for admin owners, while raw shell and
  broader host control are **not** enabled.

This lets a mobile client discover what the server safely supports today without
inventing a second privilege model or pretending raw shell control is ready.

## Mobile pairing flow (QR-first)

The companion pairing flow is designed for a separate mobile app with zero UI
coupling inside this repo.

1. An admin opens the Odysseus pairing endpoint (`POST /api/companion/pair`) in
   the existing web/PWA client (or a trusted admin browser).
2. The endpoint returns `pairing payload` fields:

   - `token` (shown-once API token with `chat,remote_development` scopes)
   - `host` and `port` (LAN fallback reachability target)
   - optional `base_url` when `COMPANION_BASE_URL` is configured for a private
     remote transport
   - `payload` JSON object with stable keys: `v`, `host`, `port`, `token`, and
     optional `base_url`
   - optional `hosts` discovery list and `qr` data URL

3. The Odysseus client can expose the returned `qr` image; the user scans it from
   the mobile app camera.
4. The mobile app parses the QR payload, configures its base URL from
   `base_url` when present or from `host`+`port` otherwise, and stores the
   bearer token.
5. The mobile app fetches `GET /api/companion/manifest` with `Authorization:
   Bearer <token>` and uses the advertised features/commands.
6. If signed commands are needed, the app registers its Ed25519 `public_key_b64`
   at `POST /api/companion/keys` and includes signed headers on command calls.

Notes:

- `GET /api/companion/pair` only renders a mint form and never creates a token.
- `POST /api/companion/pair` is the mint operation and should be called with an
  admin session.
- The token secret is shown once; the token remains valid until revoked in API
  token controls.
- To pair across networks without exposing Odysseus publicly, set
  `COMPANION_BASE_URL` to a private Tailscale/WireGuard/authenticated-proxy
  origin such as `https://odysseus.example.ts.net`. Direct public internet
  exposure with only the bearer token is not supported.
- For a Mac running Tailscale locally, the clean path is `tailscale serve`
  proxying `http://127.0.0.1:<port>`, with `COMPANION_BASE_URL` set to the
  resulting `https://<machine>.ts.net` origin. The mobile app will prefer that
  `base_url` first and fall back to LAN `host`/`port` only when the remote
  origin is unreachable.
- Prefer the full `Tailscale.app` install with the macOS network extension.
  Rootless/userspace `tailscaled` can leave the `ts.net` origin working only
  through Tailscale's local proxy on the host, so Safari or plain `curl` on the
  Mac may fail even when the tailnet endpoint itself is healthy.

## Mobile repo handoff checklist

The companion mobile app can live in a separate repository and treat Odysseus as
an HTTP/SSE server with this contract:

1. Scan or receive the pairing JSON (`v`, `token`, optional `base_url`, and
   LAN fallback `host`/`port`).
2. Build `baseUrl` from `base_url` when present, otherwise from `host`+`port`.
   Store the `ody_` token in platform secure storage and immediately call
   `GET /api/companion/manifest`.
3. Read `auth.token_scopes`, `auth.required_bearer_scope`, and
   `auth.required_command_scope`. A paired token should include both `chat` and
   `remote_development`.
4. Use `GET /api/companion/models` to render endpoint/model choices, then
   `POST /api/companion/sessions` with `endpoint_id` and `model`. Never accept
   arbitrary model endpoint URLs from the mobile UI.
5. Send chat turns with `POST /api/chat_stream` as multipart form data and parse
   Server-Sent Events (`data: {"delta": ...}`, `event: error`, `data: [DONE]`).
6. To pursue a long-running goal, create/select an owned session, then call
   `POST /api/companion/goals` with `session_id`, `goal`, and optional `use_web`.
   Poll `GET /api/companion/goals/{run_id}` to render progress. The server keeps
   issuing agent turns until the assistant returns `GOAL_STATUS: complete`,
   `GOAL_STATUS: blocked`, the owner stops the run, or a safety turn limit pauses
   it. `allow_bash=true` is accepted only for callers with `remote_development`.
   The same scope also permits `/api/chat_stream` turns that request
   `allow_bash=true`; signed-command raw shell remains disabled.
7. For command features, generate an Ed25519 key pair on-device, register only
   the raw base64 public key, and sign every `POST /api/companion/commands`
   request. Keep the private key in the platform/keychain-backed crypto provider.
8. Render available commands from `manifest.features.signed_commands.commands`
   and prefer `manifest.features.remote_development.allowed_workspace_roots`
   (or the signed-command mirror of that field) when the client needs a
   workspace picker. Do not hardcode command names or workspace paths, and do
   not assume raw shell is available.
9. Provide a local "forget device" action that deletes local token/key material.
   Server-side revocation remains available through
   `DELETE /api/companion/keys/{key_id}` and normal API-token management.

For JavaScript/React Native implementations, `static/js/companionClient.js`
contains framework-agnostic helpers for this whole flow: pairing payload parsing,
base URL construction, manifest/model/session/key calls, server-owned goal-run
calls, signed command header construction, and chat stream request shaping. It intentionally injects
`fetch`, `FormData`, `crypto`, and `sign(payloadBytes)` so the separate mobile
repo can use its own networking and secure-key libraries.

## Server-owned goal runs

`POST /api/companion/goals` starts a background loop owned by Odysseus, not by
the phone's foreground process. The mobile app supplies an existing
owner-visible `session_id` and a goal string. The backend appends normal user
and assistant messages to that chat session, runs agent mode, and inspects the
assistant's final marker after each turn:

- `GOAL_STATUS: complete` marks the run complete.
- `GOAL_STATUS: blocked` marks the run blocked until the user fixes the blocker
  and resumes it.
- `GOAL_STATUS: continue` or a missing marker causes the backend to issue the
  next goal-loop turn.

Run state is stored in `data/companion_goal_runs.json`; active coroutines are
process-local, so after a server restart any in-flight run is marked paused and
can be resumed by the owner. This is separate from `/api/companion/commands`:
goal runs use the chat/agent privilege path, and `allow_bash=true` still
requires the `remote_development` scope plus the normal owner tool privileges.

## Signed command protocol

Host-control/code-development routes must require Ed25519-signed command
envelopes in addition to the private transport and paired
`chat,remote_development` token. The current helper (`companion.signing`) binds
each signature to the HTTP method, request path, canonical JSON body hash, key
id, timestamp, and nonce. Timestamps must be fresh and the nonce store rejects
replayed `(owner, key_id, nonce)` tuples.

`companion.keys` adds the persistence layer for those future routes:

- `companion_device_keys` stores owner-approved Ed25519 public keys by
  `(owner, key_id)`, with active/revoked state and last-use metadata.
- `companion_command_nonces` stores owner-scoped replay-protection nonces until
  their freshness window expires.
- `verify_registered_signed_command(...)` looks up the owner's active key,
  verifies the signature, and records the nonce before returning success.

The companion key endpoints are the enrollment path for a mobile client:

1. The phone generates an Ed25519 key pair locally.
2. It keeps the private key on-device.
3. It sends only `public_key_b64`, plus optional `key_id` and `label`, to
   `POST /api/companion/keys` using the paired `chat,remote_development` token
   or an admin owner session.
4. The server returns safe key metadata and a public-key SHA-256 fingerprint,
   never a private key or bearer token.
5. The owner or paired client can revoke a key with
   `DELETE /api/companion/keys/{key_id}`.

`POST /api/companion/commands` accepts the signed command surface. The request
body is part of the signed payload and has this shape:

```json
{"command": "workspace_status", "args": {}}
```

Allowed commands today:

- `capabilities`: return the fixed command list and safety flags.
- `server_status`: return server/version/platform status.
- `workspace_status`: return current process workspace and git summary.
- `git_status`: return the git summary only.
- `list_files`: list files/directories under an explicit workspace.
- `read_file`: read a bounded UTF-8 text slice under an explicit workspace.
- `edit_file`: apply an exact string replacement under an explicit workspace
  and return a unified diff.
- `run_check`: run a bounded allowlisted verification command under an explicit
  workspace. Allowed checks are `git_status`, `git_diff`, `py_compile`, and
  `pytest`; the server builds argv itself and runs with `shell=False`.

Workspace file/check commands are admin-only and use the same root and workspace
confinement rules as the agent file tools: the requested workspace must live
under a configured tool root, relative paths resolve under that workspace,
absolute paths must still be inside that workspace, and sensitive paths such as
`.ssh`, `.gnupg`, `.env`, and private keys are rejected. `edit_file` is the only
intentional file-mutating command enabled in this PR; it cannot create files,
execute shell commands, or apply arbitrary patches.

The manifest includes the same command catalogue with descriptions, `mutating`
flags, `requires_admin`, and `args_schema` entries so a mobile client can render
actions without hardcoding this list.

`static/js/companionClient.js` provides a small framework-agnostic ES module for
mobile/PWA clients. It handles pairing payload parsing, base URL construction,
canonical JSON, SHA-256 body hashing, signed command header construction,
manifest/model/session/key calls, signed command POSTs, key revocation, and
server-owned goal-run calls, plus FormData-shaped chat stream requests. It also exposes
`createCompanionClient(...)`, a small workflow factory that can bootstrap the
manifest, expose token scopes and mobile-renderable command metadata, register
the current device key, list/revoke keys, list model endpoints, list/create
sessions, stream a chat/agent turn with `chatStream(...)`, call the current
status commands (`capabilities`, `server_status`, `workspace_status`,
`git_status`), and invoke the workspace file/check commands (`list_files`,
`read_file`, `edit_file`, `run_check`) without each React Native screen
hardcoding endpoint paths. Session creation only selects saved owner-visible
model endpoints and never accepts a raw endpoint URL. The signing helper
intentionally accepts an injected `sign(payloadBytes)` function so React Native
can keep private keys in the platform/keychain-backed crypto library it chooses.

Slow local models may take several minutes before first token, especially on
CPU-only hosts. Server operators can set `ODYSSEUS_STREAM_TIMEOUT=0` to disable
the upstream read timeout, or set a larger number of seconds, when companion
chat streams need to wait for those models.

Raw shell is not enabled here. A later host-control PR can add additional
owner-approved commands behind the same signed envelope, key registry, replay
protection, admin gate, workspace confinement, and command-policy pattern.

## Pairing CSRF posture

Minting happens **only on POST**. The session cookie is `SameSite=Lax`
(`routes/auth_routes.py`), so a browser will not send it on a cross-site POST —
the same protection `POST /api/tokens` relies on. A `GET` would be unsafe (Lax
cookies ride top-level GET navigations), so `GET /pair` only renders a form.
Minting invalidates the auth middleware's token cache, so a freshly minted token
works on the next request without a restart.

The pairing/scoping rules live in small, tested units (`token_owner`,
`owner_can_see`, `require_companion_scope`, `mint_pairing_token`, `pairing.*`,
`keys.*`) — see `tests/test_companion_readonly.py`,
`tests/test_companion_pairing.py`, `tests/test_companion_keys.py`, and
`tests/test_chat_api_token_scope.py`.
