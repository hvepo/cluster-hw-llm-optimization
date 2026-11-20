# Portátil Hardware & OS Tuning Profile (Unified iGPU Node)

- **System:** HP OmniBook Ultra Flip 14-fh0005ns (Board `HP 8CDE`, BIOS `W75 Ver. 01.05.00`, `2026-04-13`)
- **CPU / iGPU / NPU:** Intel Core Ultra 7 258V, Intel Arc 140V iGPU (`32.0.101.9175`, `2026-11-04`), Intel AI Boost NPU (`32.0.100.5600`, `2026-11-02`)
- **Unified Memory:** 32 GiB `LPDDR5x-8533` (~`110 GB/s` practical bandwidth), Shared GPU Memory Override = **`75%`** (verified via DXGI: **`23.61 GiB`** / `31.48 GiB` usable)
- **OS & Runtime:** Windows 11 Pro (`10.0.26300`, inbox NVMe/PCIe `10.0.26100.10068`), `Balanced` power plan (`MinCPU=0%` AC/DC, `ASPM=Off` AC / `Max` DC), `.wslconfig` (`memory=6GB`, `autoMemoryReclaim=gradual`), LM Studio `llama.cpp-win-x86_64-vulkan-avx2@2.79.0`
- **Stability Certification (14-day Event Log):** `0` `igdkmdn` / `Display`, `0` `WHEA 18/19`, `0` `Kernel-Power 41`

## Critical Tuning Rules (Arc 140V Unified Memory + Vulkan `2.79.0`)
1. **`flash_attention = true` ALWAYS:** Eliminates the catastrophic ~`100–125s` slot-0 prompt checkpoint stall on `p-*` models and delivers `488–2427 tok/s` cold prefill on Vulkan `2.79.0`.
2. **Gemma-4 MTP sidecar (`p-agent-moe`, `Gemma-4-26B-A4B-IT`):** Verified active and enabled by default in `apply_defaults.ps1` (`draftMtpSidecar=true`, `draftMaxTokens=3`, `P-draft-gemma-moe-mtp.gguf`). Lifts `c1` to `26.12 tok/s` (up to `30.76 tok/s` on code), `20.6K`-token needle decode to `28.31 tok/s` (`3/3`, `loop=false`), and `8K` prefill to `653.3 tok/s`.
3. **Physical batch size (`pbs`):** Keep `pbs=512` on 17 GiB MoE models (`pbs=1024` regresses `p-agent-moe` and does not beat `512` on `p-agent-moe-alt`). Use `pbs=1024, context_checkpoints=0` on `p-autocomplete-tab` (`1.53 GiB`) for `48.0 tok/s` `c1`, `2426.7 tok/s` prefill, and `41.0 ms` FIM TTFT.
4. **`context_checkpoints = 0` on `p-agent-moe-alt` (`p=4`) and `p-autocomplete-tab` (`p=4`):** Keep `context_checkpoints = 32` on `p-code-moe`, `p-agent-moe`, and `p-battery-chat`.
5. **Avoid `q8_0` KV quant on Intel Vulkan `Arc 140V` (`p-*` models):** Uses default `f16` KV cache so fast FlashAttention prefill kernels stay active; unified `23.61 GiB` pool holds `16K–32K` KV cleanly anyway.
6. **Stay on AC Power + allow thermal cool-down between back-to-back 25+ minute MoE sweeps:** Thin chassis settles from PL2 to sustained PL1 after ~20–30 min continuous MoE load.
7. **Persistence (`apply_defaults.ps1`):** Because LM Studio REST `POST /api/v1/models/load` strips unspecified keys from stored defaults, run `profiles/portatil/apply_defaults.ps1` directly on Portátil to persist permanent per-model defaults cleanly.
