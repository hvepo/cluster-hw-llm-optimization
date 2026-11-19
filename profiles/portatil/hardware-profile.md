# Portátil Hardware & OS Tuning Profile (Unified iGPU Node)

- **System:** HP OmniBook Ultra Flip 14-fh0005ns
- **CPU / iGPU:** Intel Core Ultra 7 258V, Intel Arc 140V iGPU
- **Unified Memory:** 32 GiB `LPDDR5x-8533` (~`110 GB/s` practical bandwidth), Shared GPU Memory Override = **`75%` (~`24 GiB` addressable GPU allocation)**

## Critical Tuning Rules (Arc 140V Unified Memory)
1. **`flash_attention = true` ALWAYS:** Eliminates the catastrophic ~`100–125s` slot-0 prompt checkpoint stall on `p-*` models and gives **12×–24×** faster 2K cold prefill (`11–18 tok/s` -> `134–495 tok/s`).
2. **`context_checkpoints = 0` on heavy MoE parallel workloads** (`p-agent-moe-alt`): removes residual slot-reuse stalls under `p=4`.
3. **Avoid `q8_0` KV quant on Intel Vulkan `Arc 140V` (`p-*` models):** Uses default `f16` KV cache so fast FlashAttention prefill kernels stay active; unified `~24 GiB` pool holds `16K–32K` KV cleanly anyway.
4. **Stay on AC Power + Windows `Best performance` for heavy 30B MoE runs; allow thermal cool-down between back-to-back 35+ minute runs** (thin chassis can heat-soak and throttle after ~35 min continuous MoE inference).
5. **Remote persistence (`apply_defaults.ps1`):** Because LM Studio REST `POST /api/v1/models/load` strips unspecified keys from stored defaults, run `profiles/portatil/apply_defaults.ps1` directly on Portátil to persist permanent per-model defaults cleanly.
6. **Optional Gemma-4 MTP on 26B-A4B (`p-agent-moe`):** Requires `llama.cpp >= b8724` in LM Studio Windows runtime (`-EnableGemmaMtpSidecar`).
