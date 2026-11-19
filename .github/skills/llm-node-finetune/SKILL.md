---
name: llm-node-finetune
description: >-
  Fine-tune (hill-climb) local LLM serving parameters for any desired cluster node based on its
  hardware profile (discrete NVIDIA CUDA GPUs, unified Intel Arc / AMD iGPUs, Apple Silicon):
  compute theoretical ceilings first, run noise-controlled interleaved A/B sweeps under a memory
  watchdog, gate on 50%/100% context needle retrieval and greedy equivalence, and persist verified
  per-model serving defaults safely.
---

# Per-Node LLM Serving Fine-Tuning (`llm-node-finetune`)

Phase 2 of optimizing a cluster device: take the node's hardware profile (real memory bandwidth, VRAM/unified memory cap, driver/runtime quirks) and systematically hill-climb LLM decode, prefill, TTFT, and KV capacity for that specific node.

## Reference Guides (Read On Demand)
- [references/lmstudio-dual-node.md](references/lmstudio-dual-node.md) — Node-specific lessons (discrete 12 GiB CUDA vs 24 GiB unified Intel Arc iGPU), onboarding new models, persisting LM Studio defaults without clobbering, `phi3` split KV (`K=q8_0:V=f16`), Gemma MTP sidecar, and prefix isolation.
- [references/ceilings.md](references/ceilings.md) — Theoretical decode, prefill, TTFT, and KV maxima (`tools/llm/ceiling.py` with fixed linear `cN` scaling).
- [references/methodology.md](references/methodology.md) — Noise control (`+-5–10%`), server-usage token counting, and drift/thermal guards.
- [references/levers.md](references/levers.md) — High-payoff optimization levers ordered by expected gain.
- [references/gotchas.md](references/gotchas.md) — Engine and hardware traps (`llama.cpp`, vLLM, SGLang, CUDA, Vulkan/iGPU).
- [references/publishing.md](references/publishing.md) — Pinning and qualification gates.

## End-to-End Node LLM Fine-Tuning Loop
1. **Verify hardware baseline first (`hw-node-profiler`):** Ensure drivers, BIOS, power plan, and GPU/CPU clocks are clean (`0` `WHEA 18/19`, `0` GPU driver resets) and measure/estimate memory bandwidth.
2. **Compute theoretical ceilings for the target node & model:**
   Run MCP `llm_compute_ceiling` or:
   ```bash
   python3 tools/llm/ceiling.py <node_model_ceiling.json>
   ```
3. **Run isolated one-lever-at-a-time A/B experiments:**
   - Use `tools/llm/lmctl_bench.py` for temporary overrides (snapshots and restores permanent LM Studio defaults on unload).
   - Benchmark with `tools/llm/bench.py` via `tools/llm/queue.sh ctl cand ctl` under `tools/llm/mem_nvidia.sh` or `tools/llm/mem_docker.sh`.
   - Accept a lever (`flash_attention`, `q8_0` vs `K=q8_0:V=f16` vs `f16` KV, `context_checkpoints=0`, `physical_batch_size`, speculative/MTP sidecar) only if it beats both flanking controls and regresses nothing by `>3%`.
4. **Gate every new winner (`tools/llm/run_gates.py`):**
   - `3/3` needle retrieval at 50% and 100% nominal context (`loop == false`).
   - Greedy equivalence check (`temperature=0, seed=42`) for speculative/MTP changes.
   - Multi-model co-residency soak if the node runs concurrent models (e.g. agent + autocomplete + embeddings).
5. **Persist verified defaults onto the target node:**
   Update `profiles/<node>/` and `profiles/best-config.json`, apply defaults (`profiles/torre/apply_defaults.py` or `profiles/portatil/apply_defaults.ps1`), cold-load verify via `tools/cluster_ctl.py load <key> --node <node>`, and unload cleanly (`tools/cluster_ctl.py unload --node <node>`).
