// ============================================
// COOKBOOK CONTEXT RECOMMENDER
// Pure helper: given a model + detected hardware, pick a sensible
// default --max-model-len for vLLM/SGLang/llama.cpp serving.
//
// Extracted from cookbookServe.js so the math is testable and reusable.
// Numbers are calibrated from real HuggingFace configs (fp16 KV cache).
// MoE models are NOT given a discount — attention is dense in MoE
// architectures, so KV per token is identical to a dense model of the
// same shape; only FFN expert routing is sparse.
// ============================================

/** KV cache bytes per token for known model families.
 *
 *   KV = 2 * num_layers * num_kv_heads * head_dim * kv_dtype_size
 *
 * Range across modern models is ~10x (Qwen2.5-7B ≈ 56 KB/tok vs.
 * Llama-2-7B ≈ 512 KB/tok thanks to GQA), so a single average won't
 * work for the whole catalog — family detection is required.
 *
 * Returns kilobytes per token (fp16 KV).
 */
export function kvKbPerToken(modelName, weightsGb = 0) {
  const n = String(modelName || '').toLowerCase();
  // Qwen3 family
  if (/qwen3-(?:coder-)?(?:30b|next)/.test(n))               return 96;   // 48L, 4kv, 128dim
  if (/qwen3-235b|qwen3-coder-235b|qwen3-480b/.test(n))      return 384;  // 94L, 4kv
  if (/qwen3-?32b/.test(n))                                  return 256;
  if (/qwen3-?14b/.test(n))                                  return 192;
  if (/qwen3-?(?:0\.6b|1\.7b|4b|8b)/.test(n))                return 64;
  // Qwen2.5 family
  if (/qwen2\.5-?(?:coder-)?(?:72b|110b)/.test(n))           return 320;
  if (/qwen2\.5-?(?:coder-)?32b/.test(n))                    return 256;
  if (/qwen2\.5-?(?:coder-)?14b/.test(n))                    return 192;
  if (/qwen2\.5-?(?:coder-)?(?:0\.5b|1\.5b|3b|7b)/.test(n))  return 56;
  // Llama 3 / 3.1 / 3.2 / 3.3 — all GQA-8 dim 128
  if (/llama-?3.*(?:70b|405b)/.test(n))                      return 320;
  if (/llama-?3.*(?:1b|3b|8b)/.test(n))                      return 128;
  // Llama 2 — no GQA on 7B/13B
  if (/llama-?2.*70b/.test(n))                               return 320;
  if (/llama-?2.*13b/.test(n))                               return 800;  // 40L, 40H no GQA
  if (/llama-?2.*7b/.test(n))                                return 512;  // 32L, 32H no GQA
  // Mixtral / Mistral
  if (/mixtral.*8x22b/.test(n))                              return 224;
  if (/mixtral.*8x7b/.test(n))                               return 128;
  if (/mistral-?(?:small|medium|7b|nemo)/.test(n))           return 128;
  // Gemma 2
  if (/gemma-?2.*27b/.test(n))                               return 368;
  if (/gemma-?2.*(?:2b|9b)/.test(n))                         return 336;
  // DeepSeek
  if (/deepseek-coder-v2-lite/.test(n))                      return 324;  // MLA, 27L
  if (/deepseek-(?:v2|v3|r1|coder-v2(?!-lite))/.test(n))     return 384;
  // Phi
  if (/phi-?3(?:\.5)?-?(?:mini|small)/.test(n))              return 384;
  if (/phi-?3(?:\.5)?-?medium/.test(n))                      return 480;
  // Generic fallback by weight size (assumes modern GQA-8 + head_dim 128
  // + layers roughly proportional to params). Conservative side.
  if (weightsGb >= 50) return 320;
  if (weightsGb >= 25) return 256;
  if (weightsGb >= 10) return 192;
  if (weightsGb >= 4)  return 128;
  return 96;
}

/** Pick the recommended Context for the Cookbook serve panel.
 *
 * Inputs:
 *   modelName:   string (HF repo, used for family detection)
 *   weightsGb:   estimated VRAM weights consume (0 if unknown)
 *   poolGb:      VRAM available in the chosen GPU pool (0 if no GPU)
 *   nativeCtx:   model's trained context window (caps the result)
 *
 * Budget: vLLM defaults to gpu_memory_utilization=0.9 and reserves
 *   ~0.5 GB for activations + CUDA graphs:
 *     usable_kv_gb = poolGb * 0.9 − weightsGb − 0.5
 *
 * Output: largest power-of-two ≤ min(nativeCtx, fits-in-budget).
 * Floors at 2048 to avoid silly recommendations on tiny budgets.
 *
 * Returns { recommended, kvKbPerTok, budgetGb, maxFit }.
 */
export function recommendContext({ modelName, weightsGb = 0, poolGb = 0, nativeCtx = 32768 } = {}) {
  const kvKb = kvKbPerToken(modelName, weightsGb);
  const budgetGb = Math.max(0, poolGb * 0.9 - weightsGb - 0.5);
  let maxFit;
  if (budgetGb > 0 && weightsGb > 0) {
    maxFit = Math.floor((budgetGb * 1024 * 1024) / kvKb);
  } else {
    // No hardware/model info — modest fallback.
    maxFit = 32768;
  }
  const target = Math.max(2048, Math.min(nativeCtx, maxFit));
  const recommended = Math.pow(2, Math.floor(Math.log2(target)));
  return { recommended, kvKbPerTok: kvKb, budgetGb, maxFit };
}

/** Format a context length the way the dropdown shows it: "8k", "128k", "1M". */
export function formatContext(n) {
  if (n >= 1048576 && n % 1048576 === 0) return (n / 1048576) + 'M';
  if (n >= 1024 && n % 1024 === 0)       return (n / 1024) + 'k';
  return String(n);
}

/** Common context sizes the dropdown offers (capped to the model's native window). */
export const CTX_COMMON_SIZES = [2048, 4096, 8192, 16384, 32768, 65536, 131072, 262144, 524288, 1048576];
