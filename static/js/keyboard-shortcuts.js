// ============================================
// Keyboard Shortcuts — dynamic keybinds
// ============================================

import { IS_MAC, isAltGrEvent } from './platform.js';

const _defaultKeybinds = {
  search: 'ctrl+k', toggle_sidebar: 'ctrl+alt+b', new_session: 'ctrl+alt+n',
  fav_session: 'ctrl+alt+f', delete_session: 'ctrl+alt+d',
  cancel: 'escape', tts: 'alt+shift+t',
  incognito: 'ctrl+alt+i', settings: 'ctrl+,', focus_input: 'ctrl+/',
  // Command palette (Search Everywhere). Empty = double-Shift only; a bound
  // combo ALSO opens it. Registered in settings.js SHORTCUT_DEFAULTS/LABELS
  // too — these maps are separate and must not drift.
  search_everywhere: '',
  // Open-tool shortcuts (Calendar bound by default; rest unbound).
  open_calendar: 'ctrl+alt+c', open_compare: '', open_cookbook: '',
  open_research: '', open_gallery: '', open_library: '', open_memory: '',
  open_notes: '', open_tasks: '', open_theme: '',
};

// ── "Toggle Window" / command-palette map — each tool window's modal element
// → the button/title that opens it (mirrors modalManager's _AUTO_WIRE, plus
// email's section title). Module-scope + exported so command-registry.js can
// build palette items from the same single source of trigger IDs.
export const _WINDOW_TRIGGERS = {
  'settings-modal':         'user-bar-settings',
  'theme-modal':            'tool-theme-btn',
  'tasks-modal':            'tool-tasks-btn',
  'notes-panel':            'tool-notes-btn',
  'memory-modal':           'tool-memory-btn',
  'doclib-modal':           'tool-library-btn',
  'gallery-modal':          'tool-gallery-btn',
  'research-overlay':       'tool-research-btn',
  'cookbook-modal':         'tool-cookbook-btn',
  'compare-model-overlay':  'tool-compare-btn',
  'calendar-modal':         'tool-calendar-btn',
  'email-lib-modal':        'email-section-title',
};

// ── Double-Shift detector (pure decision core, exported for tests) ──
// Two ShiftLeft/ShiftRight keydowns within `windowMs`, where the FIRST shift's
// keyup already fired (protects Shift-pause-Shift while typing capitals).
// Resets on: any non-Shift keydown, repeat, alt/ctrl/meta held (protects
// alt+shift+t TTS), IME composition. Callers must also reset on window blur
// and visibilitychange (alt-tab between presses).
//
// state: {lastShiftAt: number|null, firstReleased} | null. Returns {open, state}.
export function _shiftPulse(state, ev, now, windowMs = 350) {
  const s = state || { lastShiftAt: null, firstReleased: false };
  const fresh = { lastShiftAt: null, firstReleased: false };
  const isShift = ev.code === 'ShiftLeft' || ev.code === 'ShiftRight';

  if (ev.type === 'keyup') {
    if (isShift && s.lastShiftAt !== null) return { open: false, state: { ...s, firstReleased: true } };
    return { open: false, state: s };
  }
  // keydown
  if (!isShift) return { open: false, state: fresh };
  if (ev.repeat || ev.altKey || ev.ctrlKey || ev.metaKey || ev.isComposing) {
    return { open: false, state: fresh };
  }
  if (s.lastShiftAt !== null && s.firstReleased && now - s.lastShiftAt <= windowMs) {
    return { open: true, state: fresh };
  }
  return { open: false, state: { lastShiftAt: now, firstReleased: false } };
}

export function _matchesCombo(e, combo, isMac = IS_MAC) {
  if (!combo) return false;
  // Drop AltGr keystrokes so typing characters on non-US layouts can't fire a
  // Ctrl+Alt shortcut — e.g. the destructive delete_session. See platform.js.
  if (isAltGrEvent(e, isMac)) return false;
  const parts = combo.split('+');
  const needCtrl = parts.includes('ctrl');
  const needAlt = parts.includes('alt');
  const needShift = parts.includes('shift');
  const key = parts.filter(p => p !== 'ctrl' && p !== 'alt' && p !== 'shift')[0] || '';
  if (needCtrl !== (e.ctrlKey || e.metaKey)) return false;
  if (needAlt !== e.altKey) return false;
  if (needShift !== e.shiftKey) return false;
  return e.key.toLowerCase() === key;
}

/**
 * Initialize keyboard shortcuts.
 * @param {Object} modules - References to app modules and helpers
 * @param {Function} modules.el - Element lookup helper (uiModule.el)
 * @param {Object} modules.Storage - Storage module
 * @param {Object} modules.sessionModule
 * @param {Object} modules.uiModule
 * @param {Object} modules.chatModule
 * @param {Object} modules.adminModule
 * @param {Object} modules.settingsModule
 * @param {Object} modules.searchChatModule
 * @param {Function} modules._closeCompareIfActive
 * @param {Function} modules._deactivateIncognito
 * @param {string} modules.API_BASE
 */
export function initKeyboardShortcuts(modules) {
  const {
    el, Storage, sessionModule, uiModule, chatModule,
    adminModule, settingsModule, searchChatModule, commandPaletteModule,
    _closeCompareIfActive, _deactivateIncognito, API_BASE
  } = modules;

  window._odysseusKeybinds = { ..._defaultKeybinds };

  // Load saved keybinds
  fetch('/api/auth/settings', { credentials: 'same-origin' })
    .then(r => r.json())
    .then(s => { if (s.keybinds) window._odysseusKeybinds = { ..._defaultKeybinds, ...s.keybinds }; })
    .catch(() => {});

  // Load the double-Shift disable pref (set in Settings → Shortcuts; the
  // toggle there updates window._odyDoubleShiftDisabled live).
  fetch('/api/prefs/disable_double_shift', { credentials: 'same-origin' })
    .then(r => r.json())
    .then(d => { window._odyDoubleShiftDisabled = d.value === true; })
    .catch(() => {});

  // ── Esc cancels select mode (capture phase, before modal-close) ──
  // Every tool's bulk-select bar has a `*-bulk-cancel` button whose click
  // already runs the correct teardown (clears selection, hides the bar,
  // re-renders). So a single global handler that clicks whichever cancel
  // button is currently visible covers all of them — notes, skills,
  // memory, gallery, sessions, doc library (chats/archive/research/docs),
  // email, cookbook serve — without each module wiring its own listener.
  // Capture phase + stopPropagation so Esc cancels select instead of
  // closing the surrounding modal.
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    const cancels = document.querySelectorAll('[id$="-bulk-cancel"]');
    for (const btn of cancels) {
      // Do not rely on offsetParent: visible fixed-position or modal-contained
      // controls can report null. Check the rendered box and hidden ancestors.
      const visible = (() => {
        if (btn.disabled || btn.closest('.hidden,[hidden]')) return false;
        const cs = getComputedStyle(btn);
        if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') return false;
        return btn.offsetWidth > 0 || btn.offsetHeight > 0 || btn.getClientRects().length > 0;
      })();
      if (visible) {
        e.preventDefault();
        e.stopPropagation();
        e.stopImmediatePropagation();
        btn.click();
        return;
      }
    }
  }, true);

  // ── "Toggle Window" — close whatever tool window is open, or reopen the
  // last one. Uses the module-scope _WINDOW_TRIGGERS map above.
  let _lastWindow = 'settings-modal';

  const _windowVisible = (id) => {
    const m = document.getElementById(id);
    if (!m || m.classList.contains('hidden')) return false;
    const cs = getComputedStyle(m);
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') return false;
    return m.offsetWidth > 0 || m.offsetHeight > 0 || m.getClientRects().length > 0;
  };

  const _toggleActiveWindow = () => {
    // Close the first open window (remembering it), else reopen the last one.
    let openId = null;
    for (const id in _WINDOW_TRIGGERS) {
      if (_windowVisible(id)) { openId = id; break; }
    }
    if (openId) {
      _lastWindow = openId;
      const m = document.getElementById(openId);
      const closeBtn = m && m.querySelector('.close-btn, .modal-close, [data-close]');
      if (closeBtn) closeBtn.click();
      else if (openId === 'settings-modal' && settingsModule) settingsModule.close();
      else { const t = el(_WINDOW_TRIGGERS[openId]); if (t) t.click(); }
    } else if (_lastWindow === 'settings-modal') {
      if (settingsModule) settingsModule.open();
    } else {
      const t = el(_WINDOW_TRIGGERS[_lastWindow]);
      if (t) t.click();
      else if (settingsModule) settingsModule.open();
    }
  };

  // ── Double-Shift → command palette (Search Everywhere) ──
  // Decision logic lives in the pure _shiftPulse helper above; this block
  // only owns the state + the open/toggle side effect.
  let _shiftState = null;
  const _resetShiftState = () => { _shiftState = null; };
  window.addEventListener('blur', _resetShiftState);
  document.addEventListener('visibilitychange', _resetShiftState);
  document.addEventListener('keyup', (e) => {
    _shiftState = _shiftPulse(_shiftState, e, performance.now()).state;
  });

  const _togglePalette = () => {
    if (!commandPaletteModule) return;
    commandPaletteModule.isOpen() ? commandPaletteModule.close() : commandPaletteModule.open();
  };

  document.addEventListener('keydown', (e) => {
    const kb = window._odysseusKeybinds;

    // Double-Shift first (top of listener so EVERY keydown feeds the state
    // machine — any non-Shift key resets it). Triggers everywhere, inputs
    // included (JetBrains parity; the settings toggle can disable it).
    const _pulse = _shiftPulse(_shiftState, e, performance.now());
    _shiftState = _pulse.state;
    if (_pulse.open && window._odyDoubleShiftDisabled !== true) {
      e.preventDefault();
      _togglePalette();
      return;
    }

    if (_matchesCombo(e, kb.search_everywhere)) {
      e.preventDefault();
      _togglePalette();
      return;
    }
    if (_matchesCombo(e, kb.search)) {
      e.preventDefault();
      if (searchChatModule) {
        searchChatModule.isOpen() ? searchChatModule.closeSearch() : searchChatModule.openSearch();
      }
      return;
    }
    if (_matchesCombo(e, kb.toggle_sidebar)) {
      e.preventDefault();
      var sb = document.getElementById('sidebar');
      var ir = document.getElementById('icon-rail');
      if (sb && !sb.classList.contains('hidden')) {
        sb.classList.add('hidden');
      } else {
        if (ir) ir.classList.remove('rail-hidden');
        if (sb) sb.classList.remove('hidden');
      }
      if (typeof syncRailSide === 'function') syncRailSide();
      return;
    }
    if (_matchesCombo(e, kb.tts)) {
      e.preventDefault();
      var mgr = window.aiTTSManager;
      if (!mgr || !mgr.available) return;
      if (mgr.isPlaying || mgr._processing) { mgr.stop(); return; }
      var allAI = document.querySelectorAll('#chat-history .msg-ai');
      for (var i = allAI.length - 1; i >= 0; i--) {
        var ttsBtn = allAI[i].querySelector('.ai-tts-button');
        if (ttsBtn) { ttsBtn.click(); return; }
      }
      return;
    }
    if (_matchesCombo(e, kb.fav_session)) {
      e.preventDefault();
      const sid = sessionModule && sessionModule.getCurrentSessionId();
      if (!sid) return;
      const s = sessionModule.getSessions().find(x => x.id === sid);
      if (!s) return;
      const newVal = !s.is_important;
      const fd = new FormData();
      fd.append('important', newVal);
      fetch(`${API_BASE}/api/session/${sid}/important`, { method: 'POST', body: fd });
      s.is_important = newVal;
      sessionModule.renderSessionList();
      uiModule.showToast(newVal ? 'Session favorited' : 'Session unfavorited');
      return;
    }
    if (_matchesCombo(e, kb.delete_session)) {
      e.preventDefault();
      const sid = sessionModule && sessionModule.getCurrentSessionId();
      if (!sid) return;
      const s = sessionModule.getSessions().find(x => x.id === sid);
      if (!s) return;
      if (s.is_important) { uiModule.showToast('Unstar before deleting'); return; }
      uiModule.styledConfirm('Delete this session?', { confirmText: 'Delete', danger: true }).then(ok => {
        if (!ok) return;
        const allSessions = sessionModule.getSessions();
        const idx = allSessions.findIndex(x => x.id === sid);
        const nextSession = allSessions.filter(x => !x.archived && x.id !== sid)[Math.max(0, idx)] ||
                            allSessions.find(x => !x.archived && x.id !== sid);
        fetch(`${API_BASE}/api/session/${sid}`, { method: 'DELETE' }).then(async () => {
          await sessionModule.loadSessions();
          if (nextSession) {
            await sessionModule.selectSession(nextSession.id);
          } else {
            sessionModule.setCurrentSessionId(null);
            el('chat-history').innerHTML = '';
            el('current-meta').textContent = 'Odysseus Chat';
            Storage.remove('lastSessionId');
            if (chatModule && chatModule.showWelcomeScreen) chatModule.showWelcomeScreen();
          }
        });
      });
      return;
    }
    if (_matchesCombo(e, kb.new_session)) {
      e.preventDefault();
      if (_closeCompareIfActive()) return;
      _deactivateIncognito();
      const sid = sessionModule && sessionModule.getCurrentSessionId();
      const sessions = sessionModule ? sessionModule.getSessions() : [];
      const cur = sessions.find(s => s.id === sid);
      const name = new Date().toLocaleTimeString();
      const fd = new FormData();
      fd.append('name', name);
      fd.append('endpoint_url', cur ? cur.endpoint_url || '' : '');
      fd.append('model', cur ? cur.model || '' : '');
      if (cur && cur.endpoint_id) fd.append('endpoint_id', cur.endpoint_id);
      fd.append('skip_validation', 'true');
      fetch(`${API_BASE}/api/session`, { method: 'POST', body: fd, credentials: 'same-origin' })
        .then(r => r.ok ? r.json() : null)
        .then(async data => {
          if (data) {
            await sessionModule.loadSessions();
            await sessionModule.selectSession(data.id);
          }
        });
      return;
    }
    if (_matchesCombo(e, kb.cancel)) {
      if (chatModule) chatModule.abortCurrentRequest();
    }
    if (_matchesCombo(e, kb.incognito)) {
      e.preventDefault();
      // Drive the visible button so the real toggle logic runs (visual
      // state, welcome-screen guard, checkbox sync) — flipping the hidden
      // checkbox alone did nothing.
      const btn = el('incognito-btn');
      if (btn) btn.click();
      return;
    }
    if (_matchesCombo(e, kb.settings)) {
      e.preventDefault();
      _toggleActiveWindow();
      return;
    }
    // Open-tool shortcuts — click the sidebar tool button so each tool's
    // own open/toggle logic runs. Unbound (empty) combos never match.
    const _toolBtns = {
      open_calendar: 'tool-calendar-btn',
      open_compare:  'tool-compare-btn',
      open_cookbook: 'tool-cookbook-btn',
      open_research: 'tool-research-btn',
      open_gallery:  'tool-gallery-btn',
      open_library:  'tool-library-btn',
      open_memory:   'tool-memory-btn',
      open_notes:    'tool-notes-btn',
      open_tasks:    'tool-tasks-btn',
      open_theme:    'tool-theme-btn',
    };
    for (const action in _toolBtns) {
      if (_matchesCombo(e, kb[action])) {
        e.preventDefault();
        const b = el(_toolBtns[action]);
        if (b) b.click();
        return;
      }
    }
    if (_matchesCombo(e, kb.focus_input)) {
      e.preventDefault();
      const inp = el('message');
      if (inp) inp.focus();
      return;
    }
  });
}
