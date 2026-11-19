# LM Studio Dual-Node Playbook (`torre` CUDA + `portatil` Arc iGPU)

## 1. Non-Negotiable Node Rules

### `torre` (12 GiB RTX 3080 Ti)
- Never run `12B–14B` dense models at `>= 8K` context with `f16` KV (`~10.9+ GiB` spill risk caused `CUDA illegal memory access` and BSOD `0x0000007E`). Always start `12B–14B` at `flash_attention=true` + `q8_0` KV.
- **`phi3` / Phi-4 bug (`t-math-physics`):** `ggml_cuda_op_mul_mat_vec_q` / `ggml-impl.h:337` crashes on `V = q8_0` in `v2.73.0`. Use **split KV (`K=q8_0:V=f16`)**.
- **Full-GPU offload unlock:** Ensure `load.gpuStrictVramCap = false` in `hardware-config.json` + `autoFit = false` + `offloadRatio = "max"` and NVIDIA Control Panel `CUDA - Sysmem Fallback Policy = Prefer No Sysmem Fallback`.
- **Gemma-4 MTP sidecar:** Configure `draftMtpSidecar=true`, `draftModel`, `draftMaxTokens=3` inside the model default JSON only (never pass unsupported `--speculative-draft-mtp` flags on older CLI builds; confirm MTP active via >`95 tok/s` decode + LM Studio draft acceptance logs, noting `/api/v1/models` echoes `speculative_draft_mtp: false`).
- **Physical batch size (`pbs=2048`):** Wins `+2–3%` + faster prefill when `~0.4 GiB` extra VRAM fits cleanly (e.g. `t-code-chat` `8K` at `11.12 GiB`). Keep `pbs=512` where VRAM is tighter.

### `portatil` (Core Ultra 7 258V, Arc 140V, 24 GiB Shared Override)
- Always enable `flash_attention=true` (eliminates `~100–125s` slot-0 checkpoint stalls and speeds up prefill `12x–26x`).
- Leave KV cache as `f16` (`q8_0` disables fast FlashAttention paths on Intel Vulkan iGPU).
- Set `context_checkpoints=0` on `p-agent-moe-alt` under `p=4` to eliminate slot-reuse stalls.
- Note REST `POST /api/v1/models/load` clears omitted fields, so run `profiles/portatil/apply_defaults.ps1` directly on `portatil` to persist defaults.

## 2. Onboarding a Brand-New Model
1. Give it a prefixed key (`t-<name>` on `torre`, `p-<name>` on `portatil`) so LM Link isolation keeps working automatically.
2. Compute theoretical ceiling (`python3 tools/llm/ceiling.py ...`).
3. Sweep safely with `tools/llm/lmctl_bench.py load <key> ...` -> `bench.py` -> `lmctl_bench.py unload` (restoring prior defaults automatically).
4. Gate on 50% + 100% needle retrieval, then persist the winner into `profiles/<node>/apply_defaults.*` and `profiles/best-config.json`.
