# Cluster Horizontal Platform Rules (HW, LLMs, Peer & Telegram)

- `cluster-hw-llm-optimization` is a cross-project horizontal platform: use global MCP `cluster-hw-llm`, global skills (`cluster-ops-bridge`, `hw-node-profiler`, `llm-node-finetune`, `cluster-install-migrate`), and global `PATH` CLIs (`cluster-peer`, `cluster-telegram`, `cluster-llm`, `cluster-gateway`, `cluster-chat`, `cluster-hw`, `cluster-doctor`) from any workspace.
- Treat local cluster models (`:1235` / `cluster_chat` / `cluster-*` subagents) as opt-in delegation targets; never wake local LM Studio models unnecessarily for routine cloud or Telegram turns unless delegation benefits the task.
- The unified `:1235` gateway and MCP server also expose NVIDIA NIM cloud models (`nim/ultra`, `nim/fast`, `nim/vision`, `nim/reasoning`, `nim/code`, etc., gated on `~/.config/nvidia_api_key` or `NVIDIA_API_KEY`) as zero-VRAM callable resources AND autonomous tool-calling orchestrators (`cluster_orchestrate` MCP tool, `cluster-orchestrator` subagent, `cluster-chat --orchestrate`) across `torre`, `portatil`, `raspberrypi`, and `arduq`.
- Peer orders (`peer_send_order` / `peer_exec`) and the Antigravity-first Telegram responder operate project-independently (`cwd=~`) unless an explicit project `cwd` is provided.
- Profile hardware, BIOS, drivers, power plan, and Event Log stability (`hw_node_profile` / `cluster-hw`) before tuning LLM parameters on a node.
- Protect `user-concrete-model-default-config/*.json` on LM Studio hosts; use `cluster-llm load <key> --node <node>` (`node_llm_control`) to verify persisted defaults, and use `tools/llm/lmctl_bench.py` only for temporary A/B overrides.
- Scope all LM Link model loads/unloads strictly by the target node's `model_prefixes`.
- When calling Windows `.exe` from WSL, always redirect stdin (`< /dev/null` or `stdin=subprocess.DEVNULL`).
- Verify `0` `nvlddmkm 153` and `0` `WHEA 18/19` events after GPU UV/OC or BIOS Curve Optimizer changes.
- Keep all endpoints, usernames, hostnames, and serials in gitignored `cluster.local.json` only.
