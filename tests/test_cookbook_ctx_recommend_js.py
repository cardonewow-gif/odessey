"""Pin the Cookbook context recommender.

`static/js/cookbookCtxRecommend.js` picks the default `--max-model-len`
the Cookbook Serve panel offers for vLLM/SGLang/llama.cpp. The
recommendation has to match what the serving engine can actually
allocate — too high and the launch OOMs; too low and we leave context
on the table. Concretely:

  KV bytes/tok = 2 * num_layers * num_kv_heads * head_dim * kv_dtype_size
  usable KV    = poolGb * 0.9 − weightsGb − 0.5         (vLLM default)
  recommended  = largest power-of-two ≤ min(native, usable / kv)

The KV table was calibrated against real HF configs (Llama, Qwen, Mixtral,
Gemma, DeepSeek, Phi). MoE models do NOT get a discount — attention is
dense even in MoE, only FFN expert routing is sparse.

This test runs the helper through node and pins the recommended value
for representative model+VRAM combos. Skips when `node` isn't on PATH.
"""

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HAS_NODE = shutil.which("node") is not None


@pytest.fixture(scope="module")
def node_available():
    if not _HAS_NODE:
        pytest.skip("node binary not on PATH")


def _recommend(cases: list[dict]) -> list[dict]:
    """Run recommendContext() against `cases` under node. Returns the
    parsed result for each case in input order."""
    script = textwrap.dedent(
        """
        import { recommendContext, formatContext, kvKbPerToken } from './static/js/cookbookCtxRecommend.js';
        const cases = %s;
        const out = cases.map(c => {
          const r = recommendContext({
            modelName: c.name,
            weightsGb: c.weights,
            poolGb: c.pool,
            nativeCtx: c.native,
          });
          return {
            name: c.name,
            kv_kb_per_tok: r.kvKbPerTok,
            budget_gb: Math.round(r.budgetGb * 10) / 10,
            recommended: r.recommended,
            recommended_fmt: formatContext(r.recommended),
          };
        });
        console.log(JSON.stringify(out));
        """
    ) % json.dumps(cases)
    res = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=_REPO,
        capture_output=True,
        timeout=15,
        text=True,
    )
    if res.returncode != 0:
        raise AssertionError(f"node failed:\n{res.stderr}")
    last = [ln for ln in res.stdout.splitlines() if ln.strip()][-1]
    return json.loads(last)


def test_kv_bytes_per_token_calibration(node_available):
    """The KV table is calibrated against real HF configs. Lock the key
    family entries so a refactor of the regex order can't shift
    Llama-2 (no GQA, 512 KB) and Qwen2.5-7B (GQA-4, 56 KB) into the
    same bucket."""
    cases = [
        # (name, weights, pool, native) — values irrelevant here, we only
        # check kv_kb_per_tok
        {"name": "Qwen/Qwen2.5-7B-Instruct",            "weights": 0, "pool": 0, "native": 32768},
        {"name": "Qwen/Qwen2.5-Coder-14B-Instruct-AWQ", "weights": 0, "pool": 0, "native": 32768},
        {"name": "Qwen/Qwen3-Coder-30B-A3B-Instruct",   "weights": 0, "pool": 0, "native": 262144},
        {"name": "meta-llama/Llama-3.1-8B-Instruct",    "weights": 0, "pool": 0, "native": 131072},
        {"name": "meta-llama/Llama-2-7B-chat",          "weights": 0, "pool": 0, "native": 4096},
        {"name": "mistralai/Mixtral-8x7B-Instruct",     "weights": 0, "pool": 0, "native": 32768},
        {"name": "google/gemma-2-9b-it",                "weights": 0, "pool": 0, "native": 8192},
        {"name": "deepseek-ai/DeepSeek-Coder-V2-Lite",  "weights": 0, "pool": 0, "native": 163840},
        {"name": "microsoft/Phi-3-mini-4k-instruct",    "weights": 0, "pool": 0, "native": 4096},
    ]
    expected_kv = {
        "Qwen/Qwen2.5-7B-Instruct": 56,
        "Qwen/Qwen2.5-Coder-14B-Instruct-AWQ": 192,
        "Qwen/Qwen3-Coder-30B-A3B-Instruct": 96,
        "meta-llama/Llama-3.1-8B-Instruct": 128,
        "meta-llama/Llama-2-7B-chat": 512,
        "mistralai/Mixtral-8x7B-Instruct": 128,
        "google/gemma-2-9b-it": 336,
        "deepseek-ai/DeepSeek-Coder-V2-Lite": 324,
        "microsoft/Phi-3-mini-4k-instruct": 384,
    }
    got = {row["name"]: row["kv_kb_per_tok"] for row in _recommend(cases)}
    for name, kv in expected_kv.items():
        assert got[name] == kv, f"{name}: got {got[name]} KB/tok, want {kv} KB/tok"


def test_recommend_on_24gb_4090(node_available):
    """Pin the recommended context for a 24 GB RTX 4090 — the most
    common single-GPU target. Values verified against actual vLLM
    --max-model-len ceilings on this hardware."""
    cases = [
        # (model, weights_gb, expected_recommended)
        {"name": "Qwen/Qwen3-Coder-30B-A3B-Instruct-AWQ", "weights": 17.0, "pool": 24, "native": 262144,
         "expected": 32768},
        {"name": "Qwen/Qwen2.5-Coder-14B-Instruct-AWQ",   "weights": 9.3,  "pool": 24, "native": 32768,
         "expected": 32768},
        {"name": "meta-llama/Llama-3.1-8B-Instruct-AWQ",  "weights": 6.0,  "pool": 24, "native": 131072,
         "expected": 65536},
        {"name": "microsoft/Phi-3-mini-4k-instruct",      "weights": 7.0,  "pool": 24, "native": 4096,
         "expected": 4096},
    ]
    rows = _recommend(cases)
    for c, row in zip(cases, rows):
        assert row["recommended"] == c["expected"], (
            f"{c['name']}: got {row['recommended_fmt']} (kv={row['kv_kb_per_tok']} KB/tok, "
            f"budget={row['budget_gb']} GB), want {c['expected']}"
        )


def test_recommend_with_no_hardware_info(node_available):
    """No GPU detected (poolGb=0): falls back to a modest 32k cap
    (capped further by the model's native window)."""
    cases = [
        {"name": "Qwen/Qwen2.5-7B-Instruct", "weights": 0, "pool": 0, "native": 32768,
         "expected": 32768},
        {"name": "microsoft/Phi-3-mini-4k-instruct", "weights": 0, "pool": 0, "native": 4096,
         "expected": 4096},
        # Unknown weights AND unknown pool — should still produce a power-of-two ≤ native.
        {"name": "some-org/Mystery-7B", "weights": 0, "pool": 0, "native": 16384,
         "expected": 16384},
    ]
    rows = _recommend(cases)
    for c, row in zip(cases, rows):
        assert row["recommended"] == c["expected"], (
            f"{c['name']}: got {row['recommended_fmt']}, want {c['expected']}"
        )


def test_recommend_always_power_of_two(node_available):
    """The recommendation must always be a power of two ≥ 2048 and
    ≤ the model's native window. Sweep a range of pool sizes to
    catch any rounding regression."""
    cases = [
        {"name": "Qwen/Qwen2.5-7B-Instruct", "weights": 5, "pool": p, "native": 32768}
        for p in (8, 12, 16, 24, 48, 80)
    ]
    rows = _recommend(cases)
    for row in rows:
        n = row["recommended"]
        assert n >= 2048, f"{row['name']}: rec {n} below 2048 floor"
        assert n <= 32768, f"{row['name']}: rec {n} above native"
        # power-of-two check
        assert (n & (n - 1)) == 0, f"{row['name']}: rec {n} is not a power of two"
