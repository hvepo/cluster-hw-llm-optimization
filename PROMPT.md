# Copy-Ready Prompts (`cluster-hw-llm-optimization`)

Paste any prompt below into Antigravity, VS Code Copilot, OpenCode, or Claude Code to profile a cluster node's hardware/BIOS/drivers or fine-tune LLMs for that node.

---

## 1. Profile & Optimize Any Cluster Node's Hardware, BIOS & Drivers (Phase 1)

```markdown
Use `skills/hw-node-profiler/SKILL.md` (and MCP tool `hw_node_profile` or `tools/hw/hardware_probe.py`) to inspect this cluster node (`--node <node_name>`):
1. Audit CPU, motherboard, BIOS version/date, RAM DIMMs/clock/timings, all GPUs (PCIe Gen/Width, ReBAR/BAR1, or unified iGPU shared memory override), chipset/platform/GPU/NVMe driver versions & dates, Windows Power Plan (`MinCPU`, `PCIe ASPM`, AC/battery), VBS/HVCI/HAGS, `.wslconfig`, and storage health.
2. Check Windows Event Log (`hw_event_log_check`) over the past 14 days for `WHEA 18/19` (grouped by APIC ID), `nvlddmkm 153`, `igdkmdn`, `amdkmdag`, `Display`, or `Kernel-Power 41`. (If a HWiNFO64 JSON export is available at `<path>`, pass `--hwinfo <path>` to generate a 0-indexed Per-Core Curve Optimizer table and memory subtiming report.)
3. Report what is already optimal and give a prioritized list of BIOS, driver, OS, or GPU/CPU curve improvements that increase performance without sacrificing stability.
```

---

## 2. Certify GPU Undervolt/VRAM OC & CPU Curve Optimizer Stability (Phase 1 Verification)

```markdown
Using `skills/hw-node-profiler/SKILL.md`, stress-verify hardware stability on `<node_name>`:
1. Run `python3 tools/hw/gpu_oc_verify.py --node <node_name>` to decode the active MSI Afterburner V/F curve (confirming a flattened right tail through 1,250 mV), measure CUDA D2D bandwidth (`bw_test.py`), and stress live LLM decode + cold prefills while sampling `nvidia-smi` every 0.25s.
2. If CPU Per-Core Curve Optimizer or BIOS settings were changed, run `python3 tools/hw/stress_suite.py --node <node_name>` (bare-metal Windows `SetThreadAffinityMask` pulsed cache/SHA/FP per-core torture + simultaneous GPU/LLM prefill cycles + post-stress bandwidth check).
3. Confirm `0` `WHEA 18/19`, `0` `nvlddmkm 153`, `0` throttle caps, and leave all models unloaded.
```

---

## 3. Fine-Tune (Hill-Climb) an LLM for a Desired Cluster Node (Phase 2)

```markdown
Use `skills/llm-node-finetune/SKILL.md` to fine-tune serving parameters for `<model_key>` on cluster node `<node_name>`:
- Model / Quant: `<repo & quant, e.g. Qwen3-14B Q4_K_M or Qwen3-30B-A3B Q4_K_M>`
- Node hardware cap: `<e.g. 11.75 GiB discrete VRAM on torre | 24.0 GiB unified iGPU on portatil>`
- Hard constraints: lossless retrieval (`3/3` needles at 50% and 100% target context, `loop == false`); preserve all pre-existing LM Studio permanent defaults.

Steps:
1. Verify node hardware health (`hw_node_profile` / `node_llm_status`), build `profiles/<node_name>/ceiling-<model>.json`, and compute theoretical decode/prefill/KV ceilings via `llm_compute_ceiling` (`tools/llm/ceiling.py`).
2. Run baseline + one-lever-at-a-time A/B experiments (`tools/llm/lmctl_bench.py` + `tools/llm/bench.py` + `tools/llm/queue.sh`) with flanking controls (`ctl -> cand -> ctl`) and memory watchdog (`mem_nvidia.sh` or `mem_docker.sh`).
3. Gate the winning configuration with `tools/llm/run_gates.py --node <node_name>` (50% & 100% context needle retrieval + greedy equivalence if using speculative/MTP decoding).
4. Persist verified winner defaults (`profiles/<node_name>/apply_defaults.*`), update `profiles/best-config.json`, cold-load verify via `tools/cluster_ctl.py load <model_key> --node <node_name>`, and unload cleanly.
```
