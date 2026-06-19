const DEFAULT_COMMAND_PATH = '/api/companion/commands';
const DEFAULT_CHAT_STREAM_PATH = '/api/chat_stream';
const DEFAULT_PROTOCOL_VERSION = 1;
const DEFAULT_PAIRING_VERSION = 1;

export const SIGNED_COMMAND_HEADERS = Object.freeze({
  version: 'X-Odysseus-Command-Version',
  keyId: 'X-Odysseus-Command-Key-Id',
  timestamp: 'X-Odysseus-Command-Timestamp',
  nonce: 'X-Odysseus-Command-Nonce',
  signature: 'X-Odysseus-Command-Signature'
});

function isPlainObject(value) {
  return Object.prototype.toString.call(value) === '[object Object]';
}

function normalizeJson(value) {
  if (value === null) return null;
  if (Array.isArray(value)) return value.map(normalizeJson);
  if (isPlainObject(value)) {
    const out = {};
    for (const key of Object.keys(value).sort()) {
      const child = value[key];
      if (child === undefined || typeof child === 'function' || typeof child === 'symbol') {
        throw new TypeError('Body must be JSON-serializable');
      }
      out[key] = normalizeJson(child);
    }
    return out;
  }
  if (typeof value === 'number' && !Number.isFinite(value)) {
    throw new TypeError('Body must be JSON-serializable');
  }
  if (typeof value === 'bigint' || typeof value === 'function' || typeof value === 'symbol') {
    throw new TypeError('Body must be JSON-serializable');
  }
  return value;
}

export function canonicalJson(value) {
  const normalized = normalizeJson(value === undefined || value === null ? {} : value);
  return JSON.stringify(normalized);
}

export function utf8Bytes(value) {
  return new TextEncoder().encode(value);
}

export function bytesToHex(bytes) {
  return Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
}

export function bytesToBase64(bytes) {
  if (typeof btoa === 'function') {
    let binary = '';
    for (const byte of bytes) binary += String.fromCharCode(byte);
    return btoa(binary);
  }
  if (typeof Buffer !== 'undefined') {
    return Buffer.from(bytes).toString('base64');
  }
  throw new Error('No base64 encoder available');
}

export async function sha256Hex(value, cryptoImpl = globalThis.crypto) {
  if (!cryptoImpl?.subtle?.digest) {
    throw new Error('WebCrypto subtle.digest is required for SHA-256');
  }
  const bytes = value instanceof Uint8Array ? value : utf8Bytes(String(value));
  const digest = await cryptoImpl.subtle.digest('SHA-256', bytes);
  return bytesToHex(new Uint8Array(digest));
}

export async function bodySha256(body, cryptoImpl = globalThis.crypto) {
  return sha256Hex(canonicalJson(body), cryptoImpl);
}

export async function signingPayload({
  method = 'POST',
  path = DEFAULT_COMMAND_PATH,
  body = {},
  keyId,
  nonce,
  timestamp,
  version = DEFAULT_PROTOCOL_VERSION,
  cryptoImpl = globalThis.crypto
}) {
  const cleanMethod = String(method || '').trim().toUpperCase();
  const cleanPath = String(path || '').trim();
  const cleanKeyId = String(keyId || '').trim();
  const cleanNonce = String(nonce || '').trim();
  const cleanTimestamp = String(timestamp || '').trim();
  if (version !== DEFAULT_PROTOCOL_VERSION) throw new Error('Unsupported signed command version');
  if (!cleanMethod) throw new Error('HTTP method is required');
  if (!cleanPath.startsWith('/')) throw new Error('Request path must start with /');
  if (!cleanKeyId) throw new Error('Command key id is required');
  if (!cleanNonce) throw new Error('Command nonce is required');
  if (!cleanTimestamp) throw new Error('Command timestamp is required');

  return canonicalJson({
    body_sha256: await bodySha256(body, cryptoImpl),
    key_id: cleanKeyId,
    method: cleanMethod,
    nonce: cleanNonce,
    path: cleanPath,
    timestamp: cleanTimestamp,
    v: version
  });
}

export function generateNonce(byteLength = 16, cryptoImpl = globalThis.crypto) {
  if (!cryptoImpl?.getRandomValues) {
    throw new Error('crypto.getRandomValues is required for nonce generation');
  }
  const bytes = new Uint8Array(byteLength);
  cryptoImpl.getRandomValues(bytes);
  return bytesToBase64(bytes).replace(/=+$/g, '');
}

export async function signedCommandHeaders({
  keyId,
  body,
  sign,
  nonce,
  timestamp = new Date().toISOString(),
  method = 'POST',
  path = DEFAULT_COMMAND_PATH,
  version = DEFAULT_PROTOCOL_VERSION,
  cryptoImpl = globalThis.crypto
}) {
  if (typeof sign !== 'function') {
    throw new Error('A sign(payloadBytes) function is required');
  }
  const commandNonce = nonce || generateNonce(16, cryptoImpl);
  const payload = await signingPayload({
    method,
    path,
    body,
    keyId,
    nonce: commandNonce,
    timestamp,
    version,
    cryptoImpl
  });
  const signatureBytes = await sign(utf8Bytes(payload));
  return {
    [SIGNED_COMMAND_HEADERS.version]: String(version),
    [SIGNED_COMMAND_HEADERS.keyId]: String(keyId).trim(),
    [SIGNED_COMMAND_HEADERS.timestamp]: timestamp,
    [SIGNED_COMMAND_HEADERS.nonce]: commandNonce,
    [SIGNED_COMMAND_HEADERS.signature]: bytesToBase64(signatureBytes)
  };
}

function joinUrl(baseUrl, path) {
  return `${String(baseUrl).replace(/\/+$/g, '')}${path}`;
}

function authHeaders(token) {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function normalizePairingBaseUrl(value) {
  const raw = String(value || '').trim();
  if (!raw) return '';
  if (/\s/.test(raw)) throw new Error('Pairing base_url is invalid');
  let parsed;
  try {
    parsed = new URL(raw);
  } catch {
    throw new Error('Pairing base_url is invalid');
  }
  if (!['http:', 'https:'].includes(parsed.protocol)) {
    throw new Error('Pairing base_url must use http or https');
  }
  if (parsed.username || parsed.password) {
    throw new Error('Pairing base_url must not include credentials');
  }
  if ((parsed.pathname && parsed.pathname !== '/') || parsed.search || parsed.hash) {
    throw new Error('Pairing base_url must be an origin');
  }
  return parsed.origin.replace(/\/+$/g, '');
}

export function parsePairingPayload(input) {
  const payload = typeof input === 'string' ? JSON.parse(input) : input;
  if (!isPlainObject(payload)) throw new Error('Pairing payload must be a JSON object');
  const version = Number(payload.v);
  const host = String(payload.host || '').trim();
  const port = Number(payload.port);
  const baseUrl = normalizePairingBaseUrl(payload.base_url);
  const token = String(payload.token || '').trim();
  if (version !== DEFAULT_PAIRING_VERSION) throw new Error('Unsupported pairing payload version');
  if (!baseUrl || host) {
    if (!host) throw new Error('Pairing host is required');
    if (/[\s/\\?#]/.test(host)) throw new Error('Pairing host is invalid');
  }
  if (!baseUrl || payload.port !== undefined) {
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      throw new Error('Pairing port must be an integer between 1 and 65535');
    }
  }
  if (!token.startsWith('ody_')) throw new Error('Pairing token is invalid');
  const out = { v: version, token };
  if (host) out.host = host;
  if (Number.isInteger(port)) out.port = port;
  if (baseUrl) out.base_url = baseUrl;
  return out;
}

export function companionBaseUrlFromPairing(input, { protocol = 'http' } = {}) {
  const payload = parsePairingPayload(input);
  if (payload.base_url) return payload.base_url;
  const cleanProtocol = String(protocol || 'http').replace(/:$/g, '');
  if (!['http', 'https'].includes(cleanProtocol)) {
    throw new Error('Pairing protocol must be http or https');
  }
  const host = payload.host.includes(':') && !payload.host.startsWith('[')
    ? `[${payload.host}]`
    : payload.host;
  return `${cleanProtocol}://${host}:${payload.port}`;
}

function compactObject(value) {
  const out = {};
  for (const [key, child] of Object.entries(value || {})) {
    if (child !== undefined) out[key] = child;
  }
  return out;
}

function appendFormField(formData, key, value) {
  if (value === undefined || value === null || value === false || value === '') return;
  if (value === true) {
    formData.append(key, 'true');
    return;
  }
  if (Array.isArray(value)) {
    if (value.length) formData.append(key, JSON.stringify(value));
    return;
  }
  formData.append(key, String(value));
}

export async function fetchCompanionManifest({ baseUrl = '', token, fetchImpl = globalThis.fetch } = {}) {
  if (typeof fetchImpl !== 'function') throw new Error('fetch is required');
  const response = await fetchImpl(joinUrl(baseUrl, '/api/companion/manifest'), {
    headers: authHeaders(token)
  });
  if (!response.ok) throw new Error(`Manifest request failed: ${response.status}`);
  return response.json();
}

export async function fetchCompanionModels({
  baseUrl = '',
  token,
  fetchImpl = globalThis.fetch
} = {}) {
  if (typeof fetchImpl !== 'function') throw new Error('fetch is required');
  const response = await fetchImpl(joinUrl(baseUrl, '/api/companion/models'), {
    headers: authHeaders(token)
  });
  if (!response.ok) throw new Error(`Models request failed: ${response.status}`);
  return response.json();
}

export async function sendCompanionChatStream({
  baseUrl = '',
  token,
  sessionId,
  message,
  mode = 'chat',
  attachments = [],
  activeDocId,
  useWeb = false,
  useResearch = false,
  allowBash = false,
  allowWebSearch = false,
  planMode = false,
  approvedPlan,
  useRag,
  incognito = false,
  noMemory = false,
  workspace,
  presetId,
  searchContext,
  compareMode = false,
  signal,
  timezoneOffsetMinutes,
  timezoneName,
  fetchImpl = globalThis.fetch,
  FormDataImpl = globalThis.FormData
} = {}) {
  if (typeof fetchImpl !== 'function') throw new Error('fetch is required');
  if (typeof FormDataImpl !== 'function') throw new Error('FormData is required');
  if (!sessionId) throw new Error('sessionId is required');
  if (message === undefined || message === null) throw new Error('message is required');

  const formData = new FormDataImpl();
  formData.append('message', String(message));
  formData.append('session', String(sessionId));
  appendFormField(formData, 'mode', mode || 'chat');
  appendFormField(formData, 'attachments', attachments);
  appendFormField(formData, 'active_doc_id', activeDocId);
  appendFormField(formData, 'use_web', useWeb);
  appendFormField(formData, 'use_research', useResearch);
  appendFormField(formData, 'allow_bash', allowBash);
  appendFormField(formData, 'allow_web_search', allowWebSearch);
  appendFormField(formData, 'plan_mode', planMode);
  appendFormField(formData, 'approved_plan', approvedPlan);
  if (useRag === false) formData.append('use_rag', 'false');
  appendFormField(formData, 'incognito', incognito);
  appendFormField(formData, 'no_memory', noMemory);
  appendFormField(formData, 'workspace', workspace);
  appendFormField(formData, 'preset_id', presetId);
  appendFormField(formData, 'search_context', searchContext);
  appendFormField(formData, 'compare_mode', compareMode);

  const headers = {
    ...authHeaders(token)
  };
  if (timezoneOffsetMinutes !== undefined && timezoneOffsetMinutes !== null) {
    headers['X-Tz-Offset'] = String(timezoneOffsetMinutes);
  }
  if (timezoneName) {
    headers['X-Tz-Name'] = String(timezoneName);
  }

  const response = await fetchImpl(joinUrl(baseUrl, DEFAULT_CHAT_STREAM_PATH), {
    method: 'POST',
    headers,
    body: formData,
    signal
  });
  if (!response.ok) throw new Error(`Chat stream failed: ${response.status}`);
  return response;
}

export async function registerCompanionKey({
  baseUrl = '',
  token,
  publicKeyB64,
  keyId,
  label,
  fetchImpl = globalThis.fetch
}) {
  if (typeof fetchImpl !== 'function') throw new Error('fetch is required');
  const response = await fetchImpl(joinUrl(baseUrl, '/api/companion/keys'), {
    method: 'POST',
    headers: {
      ...authHeaders(token),
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      public_key_b64: publicKeyB64,
      key_id: keyId,
      label
    })
  });
  if (!response.ok) throw new Error(`Key registration failed: ${response.status}`);
  return response.json();
}

export async function fetchCompanionKeys({
  baseUrl = '',
  token,
  fetchImpl = globalThis.fetch
} = {}) {
  if (typeof fetchImpl !== 'function') throw new Error('fetch is required');
  const response = await fetchImpl(joinUrl(baseUrl, '/api/companion/keys'), {
    headers: authHeaders(token)
  });
  if (!response.ok) throw new Error(`Keys request failed: ${response.status}`);
  return response.json();
}

export async function revokeCompanionKey({
  baseUrl = '',
  token,
  keyId,
  fetchImpl = globalThis.fetch
}) {
  if (typeof fetchImpl !== 'function') throw new Error('fetch is required');
  const cleanKeyId = String(keyId || '').trim();
  if (!cleanKeyId) throw new Error('keyId is required');
  const response = await fetchImpl(
    joinUrl(baseUrl, `/api/companion/keys/${encodeURIComponent(cleanKeyId)}`),
    {
      method: 'DELETE',
      headers: authHeaders(token)
    }
  );
  if (!response.ok) throw new Error(`Key revoke failed: ${response.status}`);
  return response.json();
}

export async function fetchCompanionSessions({
  baseUrl = '',
  token,
  fetchImpl = globalThis.fetch
} = {}) {
  if (typeof fetchImpl !== 'function') throw new Error('fetch is required');
  const response = await fetchImpl(joinUrl(baseUrl, '/api/companion/sessions'), {
    headers: authHeaders(token)
  });
  if (!response.ok) throw new Error(`Sessions request failed: ${response.status}`);
  return response.json();
}

export async function createCompanionSession({
  baseUrl = '',
  token,
  name,
  endpointId,
  model,
  rag = false,
  fetchImpl = globalThis.fetch
}) {
  if (typeof fetchImpl !== 'function') throw new Error('fetch is required');
  const response = await fetchImpl(joinUrl(baseUrl, '/api/companion/sessions'), {
    method: 'POST',
    headers: {
      ...authHeaders(token),
      'Content-Type': 'application/json'
    },
    body: JSON.stringify(compactObject({
      name,
      endpoint_id: endpointId,
      model,
      rag
    }))
  });
  if (!response.ok) throw new Error(`Session creation failed: ${response.status}`);
  return response.json();
}

export async function fetchCompanionGoalRuns({
  baseUrl = '',
  token,
  fetchImpl = globalThis.fetch
} = {}) {
  if (typeof fetchImpl !== 'function') throw new Error('fetch is required');
  const response = await fetchImpl(joinUrl(baseUrl, '/api/companion/goals'), {
    headers: authHeaders(token)
  });
  if (!response.ok) throw new Error(`Goal runs request failed: ${response.status}`);
  return response.json();
}

export async function startCompanionGoalRun({
  baseUrl = '',
  token,
  sessionId,
  goal,
  useWeb = false,
  allowBash = false,
  maxTurns = 0,
  fetchImpl = globalThis.fetch
}) {
  if (typeof fetchImpl !== 'function') throw new Error('fetch is required');
  if (!sessionId) throw new Error('sessionId is required');
  if (!String(goal || '').trim()) throw new Error('goal is required');
  const response = await fetchImpl(joinUrl(baseUrl, '/api/companion/goals'), {
    method: 'POST',
    headers: {
      ...authHeaders(token),
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      session_id: sessionId,
      goal,
      use_web: useWeb,
      allow_bash: allowBash,
      max_turns: maxTurns
    })
  });
  if (!response.ok) throw new Error(`Goal run start failed: ${response.status}`);
  return response.json();
}

export async function fetchCompanionGoalRun({
  baseUrl = '',
  token,
  runId,
  fetchImpl = globalThis.fetch
}) {
  if (typeof fetchImpl !== 'function') throw new Error('fetch is required');
  const cleanRunId = String(runId || '').trim();
  if (!cleanRunId) throw new Error('runId is required');
  const response = await fetchImpl(
    joinUrl(baseUrl, `/api/companion/goals/${encodeURIComponent(cleanRunId)}`),
    { headers: authHeaders(token) }
  );
  if (!response.ok) throw new Error(`Goal run request failed: ${response.status}`);
  return response.json();
}

export async function resumeCompanionGoalRun({
  baseUrl = '',
  token,
  runId,
  fetchImpl = globalThis.fetch
}) {
  if (typeof fetchImpl !== 'function') throw new Error('fetch is required');
  const cleanRunId = String(runId || '').trim();
  if (!cleanRunId) throw new Error('runId is required');
  const response = await fetchImpl(
    joinUrl(baseUrl, `/api/companion/goals/${encodeURIComponent(cleanRunId)}/resume`),
    {
      method: 'POST',
      headers: authHeaders(token)
    }
  );
  if (!response.ok) throw new Error(`Goal run resume failed: ${response.status}`);
  return response.json();
}

export async function stopCompanionGoalRun({
  baseUrl = '',
  token,
  runId,
  fetchImpl = globalThis.fetch
}) {
  if (typeof fetchImpl !== 'function') throw new Error('fetch is required');
  const cleanRunId = String(runId || '').trim();
  if (!cleanRunId) throw new Error('runId is required');
  const response = await fetchImpl(
    joinUrl(baseUrl, `/api/companion/goals/${encodeURIComponent(cleanRunId)}/stop`),
    {
      method: 'POST',
      headers: authHeaders(token)
    }
  );
  if (!response.ok) throw new Error(`Goal run stop failed: ${response.status}`);
  return response.json();
}

export async function sendSignedCompanionCommand({
  baseUrl = '',
  token,
  keyId,
  command,
  args = {},
  sign,
  nonce,
  timestamp,
  fetchImpl = globalThis.fetch,
  cryptoImpl = globalThis.crypto
}) {
  if (typeof fetchImpl !== 'function') throw new Error('fetch is required');
  const body = { command, args };
  const headers = await signedCommandHeaders({
    keyId,
    body,
    sign,
    nonce,
    timestamp,
    cryptoImpl
  });
  const response = await fetchImpl(joinUrl(baseUrl, DEFAULT_COMMAND_PATH), {
    method: 'POST',
    headers: {
      ...authHeaders(token),
      'Content-Type': 'application/json',
      ...headers
    },
    body: JSON.stringify(body)
  });
  if (!response.ok) throw new Error(`Companion command failed: ${response.status}`);
  return response.json();
}

function commandListFromManifest(manifest) {
  const commands = manifest?.features?.signed_commands?.commands;
  return Array.isArray(commands) ? commands : [];
}

function readOnlyCommandNamesFromManifest(manifest) {
  return commandListFromManifest(manifest)
    .filter(command => command && command.mutating === false)
    .map(command => command.name)
    .filter(Boolean);
}

function mutatingCommandNamesFromManifest(manifest) {
  return commandListFromManifest(manifest)
    .filter(command => command && command.mutating === true)
    .map(command => command.name)
    .filter(Boolean);
}

function workspaceCommandNamesFromManifest(manifest) {
  return commandListFromManifest(manifest)
    .filter(command => command && String(command.mode || '').startsWith('workspace_'))
    .map(command => command.name)
    .filter(Boolean);
}

function executionCommandNamesFromManifest(manifest) {
  return commandListFromManifest(manifest)
    .filter(command => command && command.mode === 'workspace_exec')
    .map(command => command.name)
    .filter(Boolean);
}

export function createCompanionClient({
  baseUrl = '',
  token,
  keyId,
  sign,
  fetchImpl = globalThis.fetch,
  cryptoImpl = globalThis.crypto
} = {}) {
  const config = { baseUrl, token, fetchImpl };
  const signedConfig = { ...config, keyId, sign, cryptoImpl };
  const runCommand = (command, args = {}, options = {}) => sendSignedCompanionCommand({
    ...signedConfig,
    command,
    args,
    nonce: options.nonce,
    timestamp: options.timestamp
  });

  return {
    async manifest() {
      return fetchCompanionManifest(config);
    },

    async bootstrap() {
      const manifest = await fetchCompanionManifest(config);
      return {
        manifest,
        commands: commandListFromManifest(manifest),
        readOnlyCommands: readOnlyCommandNamesFromManifest(manifest),
        mutatingCommands: mutatingCommandNamesFromManifest(manifest),
        workspaceCommands: workspaceCommandNamesFromManifest(manifest),
        executionCommands: executionCommandNamesFromManifest(manifest),
        tokenScopes: manifest?.auth?.token_scopes || [],
        requiredBearerScope: manifest?.auth?.required_bearer_scope,
        requiredCommandScope: manifest?.auth?.required_command_scope,
        goalRuns: manifest?.features?.goal_runs || {},
        remoteDevelopment: manifest?.features?.remote_development || {},
        signedCommands: manifest?.features?.signed_commands || {}
      };
    },

    async registerKey({ publicKeyB64, keyId: registerKeyId = keyId, label } = {}) {
      return registerCompanionKey({
        ...config,
        publicKeyB64,
        keyId: registerKeyId,
        label
      });
    },

    async models() {
      return fetchCompanionModels(config);
    },

    async keys() {
      return fetchCompanionKeys(config);
    },

    async revokeKey({ keyId: revokeKeyId = keyId } = {}) {
      return revokeCompanionKey({
        ...config,
        keyId: revokeKeyId
      });
    },

    async sessions() {
      return fetchCompanionSessions(config);
    },

    async createSession({ name, endpointId, model, rag = false } = {}) {
      return createCompanionSession({
        ...config,
        name,
        endpointId,
        model,
        rag
      });
    },

    chatStream(params = {}) {
      return sendCompanionChatStream({
        ...config,
        ...params
      });
    },

    async goalRuns() {
      return fetchCompanionGoalRuns(config);
    },

    async startGoal({ sessionId, goal, useWeb = false, allowBash = false, maxTurns = 0 } = {}) {
      return startCompanionGoalRun({
        ...config,
        sessionId,
        goal,
        useWeb,
        allowBash,
        maxTurns
      });
    },

    async goalRun({ runId } = {}) {
      return fetchCompanionGoalRun({
        ...config,
        runId
      });
    },

    async resumeGoal({ runId } = {}) {
      return resumeCompanionGoalRun({
        ...config,
        runId
      });
    },

    async stopGoal({ runId } = {}) {
      return stopCompanionGoalRun({
        ...config,
        runId
      });
    },

    async command(command, args = {}, options = {}) {
      return runCommand(command, args, options);
    },

    capabilities(options) {
      return runCommand('capabilities', {}, options);
    },

    serverStatus(options) {
      return runCommand('server_status', {}, options);
    },

    workspaceStatus(options) {
      return runCommand('workspace_status', {}, options);
    },

    gitStatus(options) {
      return runCommand('git_status', {}, options);
    },

    listFiles({ workspace, path = '', maxEntries } = {}, options) {
      return runCommand('list_files', compactObject({
        workspace,
        path,
        max_entries: maxEntries
      }), options);
    },

    readFile({ workspace, path, offset, limit } = {}, options) {
      return runCommand('read_file', compactObject({
        workspace,
        path,
        offset,
        limit
      }), options);
    },

    editFile({ workspace, path, oldString, newString, replaceAll = false } = {}, options) {
      return runCommand('edit_file', compactObject({
        workspace,
        path,
        old_string: oldString,
        new_string: newString,
        replace_all: replaceAll
      }), options);
    },

    runCheck({ workspace, check, targets = [], timeoutSeconds } = {}, options) {
      return runCommand('run_check', compactObject({
        workspace,
        check,
        targets,
        timeout_seconds: timeoutSeconds
      }), options);
    }
  };
}
