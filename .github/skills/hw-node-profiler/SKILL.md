---
name: hw-node-profiler
description: >-
  Inspect and certify the hardware configuration, BIOS status, drivers (AMD/Intel chipsets,
  NVIDIA/Intel Arc/AMD GPUs, NVMe), memory/PCIe/ReBAR topology, OS power plan, and stability logs
  (WHEA 18/19, nvlddmkm 153, igdkmdn, amdkmdag) of any cluster device — plus verify GPU undervolt +
  VRAM overclock and CPU Per-Core Curve Optimizer stability before fine-tuning LLMs on that node.
---

# Hardware, BIOS & Driver Node Profiler (`hw-node-profiler`)

Phase 1 of optimizing any cluster device: understand its exact hardware configuration, BIOS state, driver health, and stability margins before tuning LLM serving parameters.

## Reference Guides (Read On Demand)
- [references/gpu-uv-oc.md](references/gpu-uv-oc.md) — MSI Afterburner flattened V/F curve rules, CUDA `nvcuda.dll` bandwidth measurement, prefill stress, post-TDR throttling diagnosis.
- [references/cpu-co-bios.md](references/cpu-co-bios.md) — Ryzen `WHEA 18/19` APIC-to-core mapping, HWiNFO64 `Per-Core CO` workflow, and Windows pulsed affinity stress.
- [references/windows-os-tuning.md](references/windows-os-tuning.md) — Chipset drivers, `Balanced` power plan (`MinCPU 0%`, `ASPM Off`), ReBAR, HVCI, Defender exclusions, NVIDIA sysmem fallback.

## Standard Hardware Profiling & Verification Workflows
1. **Profile Any Cluster Node's Hardware, BIOS, Drivers & Stability Logs (read-only):**
   Run MCP `hw_node_profile` or:
   ```bash
   python3 tools/hw/hardware_probe.py [--node <node_name>] --days 14 [--hwinfo <hwinfo_export.json>] [--out node-profile.json]
   ```
   Inspects CPU, motherboard, BIOS version/date, RAM DIMMs/speeds, all GPUs (NVIDIA `nvidia-smi` + BAR1 ReBAR + Windows `Win32_VideoController` for Intel Arc / AMD iGPUs), platform/chipset/GPU/NVMe drivers, active power plan (`MinCPU` + `ASPM`) + battery state, VBS/HVCI/HAGS, Event Log (`WHEA 18/19`, `nvlddmkm`, `igdkmdn`, `amdkmdag`, `Kernel-Power 41`), and optional HWiNFO64 JSON (PBO/voltages, CPPC order -> 0-indexed CO table, RAM subtimings).
2. **Verify GPU Undervolt / VRAM OC & Memory Bandwidth:**
   ```bash
   python3 tools/hw/gpu_oc_verify.py [--node <node_name>] [--models <m1,m2>]
   ```
   Measures CUDA D2D bandwidth (`bw_test.py`), decodes active Afterburner `[Startup]` V/F curve (verifying flattened right tail), stresses live prefill/decode with `0.25s` GPU telemetry, and checks `0` `nvlddmkm 153` events.
3. **Combined CPU Per-Core CO + GPU/LLM Transient Stress Suite:**
   ```bash
   python3 tools/hw/stress_suite.py [--node <node_name>] [--cores 3,0,1,5]
   ```
   Runs bare-metal Windows `SetThreadAffinityMask` pulsed SHA/zlib/lzma/FP bursts across target cores + all cores, followed by 3× concurrent LLM prefill cycles during CPU transients and post-stress bandwidth & Event Log verification.
