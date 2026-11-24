# cluster-hw-llm-optimization Antigravity Plugin

Bundles the 3 hardware/LLM optimization skills (`hw-node-profiler`, `llm-node-finetune`, `cluster-install-migrate`), 5 custom delegation agents (`cluster-coder`, `cluster-fast-agent`, `cluster-reasoning`, `cluster-vision`, `cluster-orchestrator`), safety hooks, always-on rules, and the `cluster-hw-llm` MCP server (15 tools, 3 resources — hardware/LLM tuning, `cluster_models`/`cluster_chat` over the unified `cluster/*` local + `nim/*` NVIDIA NIM cloud catalogs, bidirectional peer orders, autonomous cloud tool-calling orchestration via `cluster_orchestrate`, and OpenClaw HA control).

Install or update via:
- Ubuntu WSL / Linux: `bash install.sh --node <node_name> --targets agy`
- Windows PowerShell: `.\install.ps1 -Node <node_name> -Targets agy`
