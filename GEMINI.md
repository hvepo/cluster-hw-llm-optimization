# Antigravity Workspace Instructions

Follow all rules in [AGENTS.md](AGENTS.md) and activate the matching skill under `skills/` when relevant:
- `hw-node-profiler` — Inspect any cluster node's hardware, BIOS, drivers, power plan, Event Log (`WHEA 18/19`, `nvlddmkm 153`), HWiNFO64 exports, and certify GPU UV/OC & CPU Per-Core CO stability.
- `llm-node-finetune` — Compute theoretical ceilings for a target node, run controlled A/B LLM serving hill-climbs, run needle/equivalence gates, and persist optimal per-model configs.
- `cluster-install-migrate` — Install, migrate, or verify the toolkit across Windows and Ubuntu WSL (`install.sh`, `install.ps1`, `scripts/doctor.py`).

Prefer MCP tools exposed by `cluster-hw-llm` (`hw_node_profile`, `hw_gpu_telemetry`, `hw_event_log_check`, `llm_compute_ceiling`, `node_llm_status`, `node_best_configs`, `node_llm_control`) when connected.
