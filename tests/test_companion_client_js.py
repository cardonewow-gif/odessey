"""Node-backed tests for the browser/RN companion client helper."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest


pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")


def _run_node(script: str):
    proc = subprocess.run(
        ["node", "--input-type=module"],
        input=script,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"node failed:\nSTDERR:\n{proc.stderr}\nSTDOUT:\n{proc.stdout}"
    return json.loads(proc.stdout)


def test_companion_client_canonical_json_and_signing_payload():
    out = _run_node(
        """
        import { webcrypto } from 'node:crypto';
        import {
          bodySha256,
          canonicalJson,
          signingPayload
        } from './static/js/companionClient.js';

        const body = { command: 'capabilities', args: { z: 2, a: 1 } };
        const canonical = canonicalJson({ b: 2, a: { z: 3, c: 1 } });
        const hash = await bodySha256(body, webcrypto);
        const payload = await signingPayload({
          method: 'post',
          path: '/api/companion/commands',
          body,
          keyId: 'phone-1',
          nonce: 'nonce-1',
          timestamp: '2026-06-08T12:00:00Z',
          cryptoImpl: webcrypto
        });
        console.log(JSON.stringify({ canonical, hash, payload: JSON.parse(payload) }));
        """
    )

    assert out["canonical"] == '{"a":{"c":1,"z":3},"b":2}'
    assert len(out["hash"]) == 64
    assert out["payload"] == {
        "body_sha256": out["hash"],
        "key_id": "phone-1",
        "method": "POST",
        "nonce": "nonce-1",
        "path": "/api/companion/commands",
        "timestamp": "2026-06-08T12:00:00Z",
        "v": 1,
    }


def test_companion_client_signed_headers_use_injected_signer():
    out = _run_node(
        """
        import { webcrypto } from 'node:crypto';
        import { signedCommandHeaders } from './static/js/companionClient.js';

        const calls = [];
        const headers = await signedCommandHeaders({
          keyId: 'phone-1',
          nonce: 'nonce-1',
          timestamp: '2026-06-08T12:00:00Z',
          body: { command: 'capabilities', args: {} },
          cryptoImpl: webcrypto,
          sign: async (payloadBytes) => {
            calls.push(new TextDecoder().decode(payloadBytes));
            return new Uint8Array([1, 2, 3]);
          }
        });
        console.log(JSON.stringify({ headers, signedPayload: JSON.parse(calls[0]) }));
        """
    )

    assert out["headers"]["X-Odysseus-Command-Version"] == "1"
    assert out["headers"]["X-Odysseus-Command-Key-Id"] == "phone-1"
    assert out["headers"]["X-Odysseus-Command-Nonce"] == "nonce-1"
    assert out["headers"]["X-Odysseus-Command-Signature"] == "AQID"
    assert out["signedPayload"]["method"] == "POST"
    assert out["signedPayload"]["path"] == "/api/companion/commands"


def test_companion_client_pairing_payload_helpers():
    out = _run_node(
        """
        import {
          companionBaseUrlFromPairing,
          parsePairingPayload
        } from './static/js/companionClient.js';

        const payload = parsePairingPayload('{"v":1,"host":"192.168.1.50","port":7860,"token":"ody_demo"}');
        const baseUrl = companionBaseUrlFromPairing(payload);
        const ipv6BaseUrl = companionBaseUrlFromPairing({
          v: 1,
          host: 'fd00::1',
          port: 7860,
          token: 'ody_demo'
        }, {
          protocol: 'https'
        });
        const remotePayload = parsePairingPayload({
          v: 1,
          base_url: 'https://odysseus.example.ts.net/',
          token: 'ody_demo'
        });
        const remoteBaseUrl = companionBaseUrlFromPairing(remotePayload);
        let error = '';
        try {
          parsePairingPayload({ v: 2, host: '192.168.1.50', port: 7860, token: 'ody_demo' });
        } catch (exc) {
          error = exc.message;
        }
        let protocolError = '';
        try {
          companionBaseUrlFromPairing(payload, { protocol: 'javascript' });
        } catch (exc) {
          protocolError = exc.message;
        }
        let hostError = '';
        try {
          parsePairingPayload({ v: 1, host: '192.168.1.50/path', port: 7860, token: 'ody_demo' });
        } catch (exc) {
          hostError = exc.message;
        }
        let baseUrlError = '';
        try {
          parsePairingPayload({ v: 1, base_url: 'https://user:pass@example.test', token: 'ody_demo' });
        } catch (exc) {
          baseUrlError = exc.message;
        }
        let baseUrlPathError = '';
        try {
          parsePairingPayload({ v: 1, base_url: 'https://example.test/path', token: 'ody_demo' });
        } catch (exc) {
          baseUrlPathError = exc.message;
        }
        console.log(JSON.stringify({
          payload,
          baseUrl,
          ipv6BaseUrl,
          remotePayload,
          remoteBaseUrl,
          error,
          protocolError,
          hostError,
          baseUrlError,
          baseUrlPathError
        }));
        """
    )

    assert out["payload"] == {
        "v": 1,
        "host": "192.168.1.50",
        "port": 7860,
        "token": "ody_demo",
    }
    assert out["baseUrl"] == "http://192.168.1.50:7860"
    assert out["ipv6BaseUrl"] == "https://[fd00::1]:7860"
    assert out["remotePayload"] == {
        "v": 1,
        "base_url": "https://odysseus.example.ts.net",
        "token": "ody_demo",
    }
    assert out["remoteBaseUrl"] == "https://odysseus.example.ts.net"
    assert out["error"] == "Unsupported pairing payload version"
    assert out["protocolError"] == "Pairing protocol must be http or https"
    assert out["hostError"] == "Pairing host is invalid"
    assert out["baseUrlError"] == "Pairing base_url must not include credentials"
    assert out["baseUrlPathError"] == "Pairing base_url must be an origin"


def test_companion_client_fetch_helpers_shape_requests():
    out = _run_node(
        """
        import { webcrypto } from 'node:crypto';
        import {
          createCompanionSession,
          fetchCompanionKeys,
          fetchCompanionManifest,
          fetchCompanionModels,
          fetchCompanionSessions,
          registerCompanionKey,
          revokeCompanionKey,
          sendCompanionChatStream,
          sendSignedCompanionCommand
        } from './static/js/companionClient.js';

        const calls = [];
        const fetchImpl = async (url, options = {}) => {
          const body = options.body && typeof options.body.entries === 'function'
            ? Array.from(options.body.entries())
            : options.body;
          calls.push({ url, options: { ...options, body } });
          return {
            ok: true,
            status: 200,
            json: async () => ({ ok: true })
          };
        };

        await fetchCompanionManifest({
          baseUrl: 'http://phone-host.test/',
          token: 'ody_token',
          fetchImpl
        });
        await fetchCompanionManifest({
          baseUrl: 'http://phone-host.test/',
          fetchImpl
        });
        await fetchCompanionModels({
          baseUrl: 'http://phone-host.test/',
          token: 'ody_token',
          fetchImpl
        });
        await registerCompanionKey({
          baseUrl: 'http://phone-host.test/',
          token: 'ody_token',
          publicKeyB64: 'abc',
          keyId: 'phone-1',
          label: 'Phone',
          fetchImpl
        });
        await fetchCompanionKeys({
          baseUrl: 'http://phone-host.test/',
          token: 'ody_token',
          fetchImpl
        });
        await revokeCompanionKey({
          baseUrl: 'http://phone-host.test/',
          token: 'ody_token',
          keyId: 'phone/1',
          fetchImpl
        });
        await fetchCompanionSessions({
          baseUrl: 'http://phone-host.test/',
          token: 'ody_token',
          fetchImpl
        });
        await createCompanionSession({
          baseUrl: 'http://phone-host.test/',
          token: 'ody_token',
          name: 'Phone work',
          endpointId: 'ep-1',
          model: 'model-a',
          rag: true,
          fetchImpl
        });
        await sendSignedCompanionCommand({
          baseUrl: 'http://phone-host.test/',
          token: 'ody_token',
          keyId: 'phone-1',
          command: 'capabilities',
          nonce: 'nonce-1',
          timestamp: '2026-06-08T12:00:00Z',
          fetchImpl,
          cryptoImpl: webcrypto,
          sign: async () => new Uint8Array([9])
        });
        await sendCompanionChatStream({
          baseUrl: 'http://phone-host.test/',
          token: 'ody_token',
          sessionId: 'session-1',
          message: 'hello from phone',
          mode: 'agent',
          attachments: ['att-1'],
          allowBash: true,
          allowWebSearch: true,
          useRag: false,
          workspace: '/repo',
          presetId: 'preset-1',
          timezoneOffsetMinutes: -240,
          timezoneName: 'America/New_York',
          fetchImpl
        });
        console.log(JSON.stringify(calls));
        """
    )

    assert out[0]["url"] == "http://phone-host.test/api/companion/manifest"
    assert out[0]["options"]["headers"]["Authorization"] == "Bearer ody_token"
    assert out[1]["url"] == "http://phone-host.test/api/companion/manifest"
    assert out[1]["options"]["headers"] == {}
    assert out[2]["url"] == "http://phone-host.test/api/companion/models"
    assert out[2]["options"]["headers"]["Authorization"] == "Bearer ody_token"
    assert out[3]["url"] == "http://phone-host.test/api/companion/keys"
    assert out[3]["options"]["method"] == "POST"
    assert json.loads(out[3]["options"]["body"]) == {
        "public_key_b64": "abc",
        "key_id": "phone-1",
        "label": "Phone",
    }
    assert out[4]["url"] == "http://phone-host.test/api/companion/keys"
    assert out[4]["options"]["headers"]["Authorization"] == "Bearer ody_token"
    assert out[5]["url"] == "http://phone-host.test/api/companion/keys/phone%2F1"
    assert out[5]["options"]["method"] == "DELETE"
    assert out[5]["options"]["headers"]["Authorization"] == "Bearer ody_token"
    assert out[6]["url"] == "http://phone-host.test/api/companion/sessions"
    assert out[6]["options"]["headers"]["Authorization"] == "Bearer ody_token"
    assert out[7]["url"] == "http://phone-host.test/api/companion/sessions"
    assert out[7]["options"]["method"] == "POST"
    assert json.loads(out[7]["options"]["body"]) == {
        "name": "Phone work",
        "endpoint_id": "ep-1",
        "model": "model-a",
        "rag": True,
    }
    assert out[8]["url"] == "http://phone-host.test/api/companion/commands"
    assert out[8]["options"]["method"] == "POST"
    assert out[8]["options"]["headers"]["X-Odysseus-Command-Key-Id"] == "phone-1"
    assert out[8]["options"]["headers"]["X-Odysseus-Command-Nonce"] == "nonce-1"
    assert out[8]["options"]["headers"]["X-Odysseus-Command-Signature"] == "CQ=="
    assert json.loads(out[8]["options"]["body"]) == {
        "command": "capabilities",
        "args": {},
    }
    assert out[9]["url"] == "http://phone-host.test/api/chat_stream"
    assert out[9]["options"]["method"] == "POST"
    assert out[9]["options"]["headers"] == {
        "Authorization": "Bearer ody_token",
        "X-Tz-Offset": "-240",
        "X-Tz-Name": "America/New_York",
    }
    assert out[9]["options"]["body"] == [
        ["message", "hello from phone"],
        ["session", "session-1"],
        ["mode", "agent"],
        ["attachments", "[\"att-1\"]"],
        ["allow_bash", "true"],
        ["allow_web_search", "true"],
        ["use_rag", "false"],
        ["workspace", "/repo"],
        ["preset_id", "preset-1"],
    ]


def test_companion_client_goal_helpers_shape_requests():
    out = _run_node(
        """
        import {
          fetchCompanionGoalRun,
          fetchCompanionGoalRuns,
          resumeCompanionGoalRun,
          startCompanionGoalRun,
          stopCompanionGoalRun
        } from './static/js/companionClient.js';

        const calls = [];
        const fetchImpl = async (url, options = {}) => {
          calls.push({ url, options });
          return {
            ok: true,
            status: 200,
            json: async () => ({ ok: true })
          };
        };

        await fetchCompanionGoalRuns({
          baseUrl: 'http://phone-host.test/',
          token: 'ody_token',
          fetchImpl
        });
        await startCompanionGoalRun({
          baseUrl: 'http://phone-host.test/',
          token: 'ody_token',
          sessionId: 'session-1',
          goal: 'Ship it',
          useWeb: true,
          allowBash: false,
          fetchImpl
        });
        await fetchCompanionGoalRun({
          baseUrl: 'http://phone-host.test/',
          token: 'ody_token',
          runId: 'run/1',
          fetchImpl
        });
        await resumeCompanionGoalRun({
          baseUrl: 'http://phone-host.test/',
          token: 'ody_token',
          runId: 'run/1',
          fetchImpl
        });
        await stopCompanionGoalRun({
          baseUrl: 'http://phone-host.test/',
          token: 'ody_token',
          runId: 'run/1',
          fetchImpl
        });
        console.log(JSON.stringify(calls));
        """
    )

    assert out[0]["url"] == "http://phone-host.test/api/companion/goals"
    assert out[0]["options"]["headers"]["Authorization"] == "Bearer ody_token"
    assert out[1]["url"] == "http://phone-host.test/api/companion/goals"
    assert out[1]["options"]["method"] == "POST"
    assert json.loads(out[1]["options"]["body"]) == {
        "session_id": "session-1",
        "goal": "Ship it",
        "use_web": True,
        "allow_bash": False,
        "max_turns": 0,
    }
    assert out[2]["url"] == "http://phone-host.test/api/companion/goals/run%2F1"
    assert out[3]["url"] == "http://phone-host.test/api/companion/goals/run%2F1/resume"
    assert out[3]["options"]["method"] == "POST"
    assert out[4]["url"] == "http://phone-host.test/api/companion/goals/run%2F1/stop"
    assert out[4]["options"]["method"] == "POST"


def test_companion_client_factory_bootstrap_and_status_methods():
    out = _run_node(
        """
        import { webcrypto } from 'node:crypto';
        import { createCompanionClient } from './static/js/companionClient.js';

        const calls = [];
        const fetchImpl = async (url, options = {}) => {
          const body = options.body && typeof options.body.entries === 'function'
            ? Array.from(options.body.entries())
            : options.body;
          calls.push({ url, options: { ...options, body } });
          if (url.endsWith('/api/companion/manifest')) {
            return {
              ok: true,
              status: 200,
              json: async () => ({
                features: {
                  signed_commands: {
                    commands: [
                      { name: 'capabilities', mode: 'read_only', mutating: false },
                      { name: 'read_file', mode: 'workspace_read', mutating: false },
                      { name: 'edit_file', mode: 'workspace_edit', mutating: true },
                      { name: 'run_check', mode: 'workspace_exec', mutating: false }
                    ]
                  },
                  remote_development: {
                    status: 'signed_workspace_file_control_ready'
                  },
                  goal_runs: {
                    status: 'server_owned_loop_ready',
                    start_path: '/api/companion/goals'
                  }
                },
                auth: {
                  required_bearer_scope: 'chat',
                  required_command_scope: 'remote_development',
                  token_scopes: ['chat', 'remote_development']
                }
              })
            };
          }
          return {
            ok: true,
            status: 200,
            json: async () => ({ ok: true })
          };
        };

        const client = createCompanionClient({
          baseUrl: 'http://phone-host.test/',
          token: 'ody_token',
          keyId: 'phone-1',
          fetchImpl,
          cryptoImpl: webcrypto,
          sign: async () => new Uint8Array([7])
        });

        const bootstrap = await client.bootstrap();
        const { workspaceStatus } = client;
        await client.models();
        await client.keys();
        await client.revokeKey({ keyId: 'phone/1' });
        await client.sessions();
        await client.createSession({
          name: 'Phone work',
          endpointId: 'ep-1',
          model: 'model-a'
        });
        await workspaceStatus({
          nonce: 'nonce-1',
          timestamp: '2026-06-08T12:00:00Z'
        });
        await client.registerKey({
          publicKeyB64: 'pub',
          label: 'Phone'
        });
        await client.listFiles({
          workspace: '/repo',
          path: 'src'
        }, {
          nonce: 'nonce-2',
          timestamp: '2026-06-08T12:00:01Z'
        });
        await client.readFile({
          workspace: '/repo',
          path: 'src/app.py',
          limit: 20
        }, {
          nonce: 'nonce-3',
          timestamp: '2026-06-08T12:00:02Z'
        });
        await client.editFile({
          workspace: '/repo',
          path: 'src/app.py',
          oldString: 'old',
          newString: 'new'
        }, {
          nonce: 'nonce-4',
          timestamp: '2026-06-08T12:00:03Z'
        });
        await client.runCheck({
          workspace: '/repo',
          check: 'py_compile',
          targets: ['src/app.py'],
          timeoutSeconds: 15
        }, {
          nonce: 'nonce-5',
          timestamp: '2026-06-08T12:00:04Z'
        });
        await client.chatStream({
          sessionId: 'session-1',
          message: 'continue from phone',
          mode: 'agent',
          allowBash: true,
          workspace: '/repo',
          timezoneName: 'America/New_York'
        });

        console.log(JSON.stringify({ bootstrap, calls }));
        """
    )

    assert out["bootstrap"]["readOnlyCommands"] == ["capabilities", "read_file", "run_check"]
    assert out["bootstrap"]["mutatingCommands"] == ["edit_file"]
    assert out["bootstrap"]["workspaceCommands"] == ["read_file", "edit_file", "run_check"]
    assert out["bootstrap"]["executionCommands"] == ["run_check"]
    assert out["bootstrap"]["tokenScopes"] == ["chat", "remote_development"]
    assert out["bootstrap"]["requiredBearerScope"] == "chat"
    assert out["bootstrap"]["requiredCommandScope"] == "remote_development"
    assert out["bootstrap"]["goalRuns"]["status"] == "server_owned_loop_ready"
    assert out["bootstrap"]["remoteDevelopment"]["status"] == "signed_workspace_file_control_ready"
    assert out["calls"][0]["url"] == "http://phone-host.test/api/companion/manifest"
    assert out["calls"][1]["url"] == "http://phone-host.test/api/companion/models"
    assert out["calls"][2]["url"] == "http://phone-host.test/api/companion/keys"
    assert out["calls"][3]["url"] == "http://phone-host.test/api/companion/keys/phone%2F1"
    assert out["calls"][3]["options"]["method"] == "DELETE"
    assert out["calls"][4]["url"] == "http://phone-host.test/api/companion/sessions"
    assert out["calls"][5]["url"] == "http://phone-host.test/api/companion/sessions"
    assert out["calls"][5]["options"]["method"] == "POST"
    assert json.loads(out["calls"][5]["options"]["body"]) == {
        "name": "Phone work",
        "endpoint_id": "ep-1",
        "model": "model-a",
        "rag": False,
    }
    assert out["calls"][6]["url"] == "http://phone-host.test/api/companion/commands"
    assert out["calls"][6]["options"]["headers"]["X-Odysseus-Command-Key-Id"] == "phone-1"
    assert out["calls"][6]["options"]["headers"]["X-Odysseus-Command-Signature"] == "Bw=="
    assert json.loads(out["calls"][6]["options"]["body"]) == {
        "command": "workspace_status",
        "args": {},
    }
    assert out["calls"][7]["url"] == "http://phone-host.test/api/companion/keys"
    assert json.loads(out["calls"][7]["options"]["body"]) == {
        "public_key_b64": "pub",
        "key_id": "phone-1",
        "label": "Phone",
    }
    assert json.loads(out["calls"][8]["options"]["body"]) == {
        "command": "list_files",
        "args": {
            "workspace": "/repo",
            "path": "src",
        },
    }
    assert json.loads(out["calls"][9]["options"]["body"]) == {
        "command": "read_file",
        "args": {
            "workspace": "/repo",
            "path": "src/app.py",
            "limit": 20,
        },
    }
    assert json.loads(out["calls"][10]["options"]["body"]) == {
        "command": "edit_file",
        "args": {
            "workspace": "/repo",
            "path": "src/app.py",
            "old_string": "old",
            "new_string": "new",
            "replace_all": False,
        },
    }
    assert json.loads(out["calls"][11]["options"]["body"]) == {
        "command": "run_check",
        "args": {
            "workspace": "/repo",
            "check": "py_compile",
            "targets": ["src/app.py"],
            "timeout_seconds": 15,
        },
    }
    assert out["calls"][12]["url"] == "http://phone-host.test/api/chat_stream"
    assert out["calls"][12]["options"]["method"] == "POST"
    assert out["calls"][12]["options"]["headers"] == {
        "Authorization": "Bearer ody_token",
        "X-Tz-Name": "America/New_York",
    }
    assert out["calls"][12]["options"]["body"] == [
        ["message", "continue from phone"],
        ["session", "session-1"],
        ["mode", "agent"],
        ["allow_bash", "true"],
        ["workspace", "/repo"],
    ]
