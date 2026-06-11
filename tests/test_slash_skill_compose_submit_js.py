"""Issue #3748 — slash skill invocation silently drops the composed prompt.

Two independent guards eat the synthetic re-submit that
`_submitComposedMessage` (static/js/slashCommands.js) performs:

1. app.js (`chatForm.onsubmit = handleSubmit`) debounces ALL submits for
   300ms after the user's Enter (`if (_submitting) return;`). The composed
   re-submit lands inside that window whenever the skill invoke round-trip
   is fast.
2. chat.js holds its `_sendInFlight` re-click guard for the whole slash turn;
   a synchronous `form.requestSubmit()` re-enters the handler and is dropped,
   and chat.js then clears the input, wiping the composed prompt.

The fix defers the value-set + submit to a macrotask (clears guard 2 and the
input wipe) and marks the form with a one-shot `composedSubmit` flag that the
app.js debounce lets through (clears guard 1).

The harness extracts the real `_submitComposedMessage` source and drives it
against a fake input/form replicating both guards exactly as wired on dev
(app.js debounce in front, chat.js guard inside, input wipe after the slash
turn). Fails on the synchronous dev implementation, passes with the fix.
Driven through `node --input-type=module` (same approach as
tests/test_censor_pref_js.py); skips when node is not installed. The app.js
side of the contract is pinned by static source assertions (same approach as
tests/test_setup_device_auth_static.py).
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_SLASH_SRC = (ROOT / "static" / "js" / "slashCommands.js").read_text(encoding="utf-8")
_APP_SRC = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

_HAS_NODE = shutil.which("node") is not None


def _extract_submit_composed() -> str:
    m = re.search(
        r"function _submitComposedMessage\(text\) \{.*?\n\}",
        _SLASH_SRC,
        re.DOTALL,
    )
    assert m, "_submitComposedMessage not found in slashCommands.js"
    return m.group(0)


_HARNESS_TEMPLATE = r"""
__FUNCTION_SOURCE__

// Fake DOM replicating BOTH submit guards as wired on dev (#3748):
// - app.js handleSubmit debounces all submits for 300ms (one-shot
//   composedSubmit flag exempts a single programmatic re-submit)
// - chat.js handleChatSubmit holds _sendInFlight during the slash turn
// - chat.js clears the input after handleSlashCommand resolves
const sent = [];
let appSubmitting = false;
let sendInFlight = false;

const msgInput = {
  value: '',
  dispatchEvent() {},
};

const form = {
  dataset: {},
  requestSubmit() { appHandleSubmit(); },
  dispatchEvent() { appHandleSubmit(); },
};

function appHandleSubmit() {                  // app.js:3520 handleSubmit
  const isComposed = form.dataset.composedSubmit === '1';
  if (isComposed) delete form.dataset.composedSubmit;
  if (appSubmitting && !isComposed) return;   // 300ms debounce
  appSubmitting = true;
  setTimeout(() => { appSubmitting = false; }, 300);
  chatHandleSubmit();
}

function chatHandleSubmit() {                 // chat.js:419 handleChatSubmit
  if (sendInFlight) return;
  sendInFlight = true;
  const msg = msgInput.value;
  if (msg.trim()) sent.push(msg);
  sendInFlight = false;
}

globalThis.document = {
  getElementById(id) {
    if (id === 'message') return msgInput;
    if (id === 'chat-form') return form;
    return null;
  },
};

// Replay the dev sequence: the user's Enter arms the app.js debounce and the
// chat.js guard; the slash path calls _submitComposedMessage while both are
// held; chat.js then wipes the input and releases its guard. The skill
// invoke round-trip is fast (well under the 300ms debounce), which is the
// case that loses the prompt on dev.
appSubmitting = true;
setTimeout(() => { appSubmitting = false; }, 300);
sendInFlight = true;
const composed = 'Apply the skill below to my request --- BEGIN SKILL ---';
const accepted = _submitComposedMessage(composed);
msgInput.value = '';                          // chat.js:466 input wipe
sendInFlight = false;                         // _releaseSendFlag()

// Flush a short macrotask window — far less than the 300ms debounce, so a
// fix that merely waits out the debounce would still fail here.
await new Promise((resolve) => setTimeout(resolve, 50));

console.log(JSON.stringify({ accepted, sent }));
"""


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_composed_skill_prompt_survives_both_submit_guards():
    """The composed prompt must reach the send path even though the slash
    turn runs under chat.js's _sendInFlight guard, app.js's 300ms submit
    debounce is armed, and the input is cleared after the slash turn."""
    source = _HARNESS_TEMPLATE.replace("__FUNCTION_SOURCE__", _extract_submit_composed())
    result = subprocess.run(
        ["node", "--input-type=module", "-e", source],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    values = json.loads(result.stdout)
    assert values["accepted"] is True
    assert values["sent"] == [
        "Apply the skill below to my request --- BEGIN SKILL ---"
    ], f"composed prompt was dropped: sent={values['sent']!r}"


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_composed_submit_reports_failure_when_dom_is_missing():
    """Contract pin: with no #message/#chat-form in the DOM,
    _submitComposedMessage must return false so the caller shows the
    'Could not start skill invocation.' error."""
    source = (
        _extract_submit_composed()
        + "\nglobalThis.document = { getElementById() { return null; } };"
        + "\nconsole.log(JSON.stringify({ accepted: _submitComposedMessage('x') }));"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", source],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == {"accepted": False}


def test_app_js_debounce_exempts_the_one_shot_composed_submit():
    """Static pin of the app.js side: handleSubmit must read and clear the
    one-shot composedSubmit flag and exempt exactly that submit from the
    double-submit debounce."""
    start = _APP_SRC.index("function handleSubmit(e)")
    end = _APP_SRC.index("chatForm.onsubmit = handleSubmit")
    block = _APP_SRC[start:end]

    assert "chatForm.dataset.composedSubmit === '1'" in block
    assert "delete chatForm.dataset.composedSubmit" in block
    assert "if (_submitting && !isComposedSubmit) return;" in block


def test_submit_composed_message_sets_the_flag_before_submitting():
    """Static pin of the slashCommands.js side: the deferred composed submit
    must announce itself via the form dataset flag right before submitting."""
    src = _extract_submit_composed()
    assert "setTimeout(" in src
    flag_idx = src.index("form.dataset.composedSubmit = '1'")
    submit_idx = src.index("form.requestSubmit()")
    assert flag_idx < submit_idx
