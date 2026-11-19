# GPU Undervolt + VRAM Overclock Verification Playbook

## Why Gaming Benchmarks Lie About LLM Stability
Games rarely trigger the instantaneous dense Tensor/GEMM current spikes of `2K–8K` cold LLM prefill (`300–345 W`) mixed with full-rate GDDR6X KV reads (`10,001 MHz`). A GPU undervolt that looks stable in 3DMark can still crash inside `ggml_backend_cuda_synchronize` / `ggml_cuda_kernel_launch` with Windows **`nvlddmkm` Event ID `153`** (brief black screen / TDR reset).

## Critical V/F Curve Rule (MSI Afterburner)
1. **Do NOT push `+212` to `+226 MHz` at `850–875 mV` on mid-ASIC GA102 bins (`~52%`):** Caused immediate `nvlddmkm 153` CUDA crashes during `t-code-chat prefill_4096` and `t-vision-context prefill_2048/8192`.
2. **MUST flatten the ENTIRE right tail up to `1,250 mV`:** If you flatten only up to `975 mV` and leave `981–1,250 mV` rising, Boost climbs to `1,905 MHz` at load transitions and crashes. Shift-select every dot right of the target voltage up to `1,250 mV`, drag below the plateau, and click Apply so `tail_flattened_ge950mv = true`.
3. **Always reboot once after any `nvlddmkm 153` TDR:** Windows leaves a sticky post-TDR `SW_POWER_CAP` throttle (`0x0000000000000004`) that drops CUDA D2D bandwidth even after resetting clocks.

## Verified GA102 Profile (`RTX 3080 Ti FE`, `52.4%` ASIC)
- `PowerLimit = 100%` (`350 W`), `Memory = +500 MHz` (`10,001 MHz`), `850 mV -> 1,695 MHz`, `875 mV -> 1,770 MHz`, flat cap `1,785 MHz` (`937.5–1,237.5 mV`).
- Verified by `tools/hw/gpu_oc_verify.py`: `831.81–860.03 GB/s` D2D (`0x0`/`0x1`), `+17.9% c1` on `t-code-chat` (`71.12 tok/s`), `+10.0% c1` on `t-vision-context` (`107.44 tok/s`), `0` errors.
