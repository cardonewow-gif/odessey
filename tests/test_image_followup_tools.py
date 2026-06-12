"""A vague image follow-up after a real image turn ("do another", "try again to
match your vision") references the image only anaphorically — no keyword — so the
tool-RAG classifier flagged it low_signal and stripped generate_image. Tool-less,
a small model confabulated a fake generation (invented <turn_data>/<tool_response>
scaffolding + a fabricated image URL). Fix: re-arm the `images` domain when a recent
assistant turn actually used an image tool, so the tool stays offered.

Covers _assistant_used_image_tool (both signals + lookback) and the classifier's
additive re-arm — and that it does NOT hijack genuine new-topic follow-ups.
"""

from src.agent_loop import _assistant_used_image_tool, _classify_agent_request


def _img_turn_via_events():
    return {"role": "assistant", "content": "Here's your image.",
            "metadata": {"tool_events": [{"round": 1, "tool": "generate_image", "exit_code": 0}]}}


def _img_turn_via_url():
    return {"role": "assistant",
            "content": "Done!\nDirect link: https://x/api/generated-image/abc123.png"}


def _plain(role, text):
    return {"role": role, "content": text}


# --- _assistant_used_image_tool --------------------------------------------

def test_detects_image_tool_via_tool_events():
    msgs = [_plain("user", "make an image of a fox"), _img_turn_via_events(),
            _plain("user", "try again")]
    assert _assistant_used_image_tool(msgs) is True


def test_detects_image_via_generated_url_in_content():
    msgs = [_plain("user", "make an image"), _img_turn_via_url(), _plain("user", "try again")]
    assert _assistant_used_image_tool(msgs) is True


def test_detects_image_two_assistant_turns_back():
    """The real repro: gen -> comment -> follow-up (image turn is 2 back)."""
    msgs = [
        _plain("user", "make an image"), _img_turn_via_events(),
        _plain("user", "what do you think?"), _plain("assistant", "Looks great — the pose is dynamic."),
        _plain("user", "how about you try again to match your vision"),
    ]
    assert _assistant_used_image_tool(msgs) is True


def test_no_recent_image_returns_false():
    msgs = [_plain("user", "what's the capital of France"),
            _plain("assistant", "Paris."), _plain("user", "and Germany?")]
    assert _assistant_used_image_tool(msgs) is False


def test_image_beyond_lookback_window_is_ignored():
    """An image generated many turns ago must NOT re-arm a now-unrelated chat."""
    msgs = [_plain("user", "make an image"), _img_turn_via_events()]
    for _ in range(5):  # 5 more assistant turns of unrelated chat
        msgs += [_plain("user", "tell me more"), _plain("assistant", "Sure, here's more.")]
    msgs.append(_plain("user", "try again"))
    assert _assistant_used_image_tool(msgs) is False


# --- classifier integration -------------------------------------------------

def test_vague_followup_after_image_rearms_images_domain():
    msgs = [_plain("user", "imagine two xmen and generate an image"), _img_turn_via_events(),
            _plain("user", "how about you try again to match your vision")]
    out = _classify_agent_request(msgs, "how about you try again to match your vision")
    assert "images" in out["domains"]
    assert out["low_signal"] is False  # tool no longer stripped


def test_same_vague_message_without_prior_image_stays_low_signal():
    """Control: the fix must NOT fire without a real prior image turn."""
    msgs = [_plain("user", "hello"), _plain("assistant", "Hi!"),
            _plain("user", "how about you try again to match your vision")]
    out = _classify_agent_request(msgs, "how about you try again to match your vision")
    assert "images" not in out["domains"]
    assert out["low_signal"] is True


def test_new_topic_after_image_keeps_its_own_domain_and_is_additive():
    """A genuine new request after an image turn still classifies normally — it
    gains images (harmless) but keeps its real domain; retrieval_query not hijacked."""
    msgs = [_plain("user", "make an image"), _img_turn_via_events(),
            _plain("user", "now search the web for the latest x-men news")]
    out = _classify_agent_request(msgs, "now search the web for the latest x-men news")
    assert "web" in out["domains"]      # real intent preserved
    assert "images" in out["domains"]   # additively re-armed
    assert out["retrieval_query"] == "now search the web for the latest x-men news"
