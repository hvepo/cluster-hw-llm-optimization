# cluster-hw-llm-optimization Antigravity Plugin

Bundles the 3 hardware/LLM optimization skills (`hw-node-profiler`, `llm-node-finetune`, `cluster-install-migrate`), 4 custom local-cluster delegator agents (`cluster-coder`, `cluster-fast-agent`, `cluster-reasoning`, `cluster-vision`), safety hooks, always-on rules, and the `cluster-hw-llm` MCP server (14 tools, 3 resources — hardware/LLM tuning, `cluster_models`/`cluster_chat`, bidirectional peer orders, and OpenClaw HA control).

Install or update via:
- Ubuntu WSL / Linux: `bash install.sh --node <node_name> --targets agy`
- Windows PowerShell: `.\install.ps1 -Node <node_name> -Targets agy`
