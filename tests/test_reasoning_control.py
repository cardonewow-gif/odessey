"""Unit tests for per-model reasoning control (src/reasoning_control.py).

Scope: Category #1 — the "/think" soft-switch directive. Covers that it injects
only for /think-dialect models when reasoning is "on", never for off/auto, and
never for models that use a different mechanism (so no directive is leaked to a
model that wouldn't understand it).
"""
from src.reasoning_control import reasoning_directive, inject_directive, reasoning_mode_for, ON, OFF, AUTO


class TestReasoningDirective:
    def test_nemotron_vl_on_injects_think(self):
        assert reasoning_directive("nemotron-nano-12b-vl", ON) == "/think"

    def test_nemotron_vl_variant_matches(self):
        assert reasoning_directive("nvidia/nemotron-nano-vl-8b", ON) == "/think"

    def test_off_and_auto_inject_nothing(self):
        assert reasoning_directive("nemotron-nano-12b-vl", OFF) is None
        assert reasoning_directive("nemotron-nano-12b-vl", AUTO) is None

    def test_non_think_models_unchanged(self):
        # Models that use a different mechanism (or none) must NOT get /think.
        assert reasoning_directive("qwen3-vl-30b", ON) is None
        assert reasoning_directive("gpt-oss-120b", ON) is None
        assert reasoning_directive("llama-3.3-70b-instruct", ON) is None


class TestInjectDirective:
    def test_string_content(self):
        msgs = [{"role": "user", "content": "hello"}]
        inject_directive(msgs, "/think")
        assert msgs[0]["content"] == "/think hello"

    def test_multimodal_list_content(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
        inject_directive(msgs, "/think")
        assert msgs[0]["content"][0] == {"type": "text", "text": "/think"}

    def test_idempotent(self):
        msgs = [{"role": "user", "content": "/think hello"}]
        inject_directive(msgs, "/think")
        assert msgs[0]["content"] == "/think hello"

    def test_targets_latest_user_turn(self):
        msgs = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "x"},
            {"role": "user", "content": "second"},
        ]
        inject_directive(msgs, "/think")
        assert msgs[0]["content"] == "first"
        assert msgs[2]["content"] == "/think second"


class TestReasoningModeFor:
    def test_unknown_url_degrades_to_auto(self):
        assert reasoning_mode_for("some-model", "http://nonexistent.invalid:9/v1") == AUTO
