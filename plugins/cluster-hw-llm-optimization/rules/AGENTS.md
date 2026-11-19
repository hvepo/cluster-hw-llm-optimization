# Cluster HW & LLM Optimization Rules

- Profile hardware, BIOS, drivers, power plan, and Event Log stability (`hw_node_profile` / `tools/hw/hardware_probe.py`) before tuning LLM parameters on a node.
- Protect `user-concrete-model-default-config/*.json` on LM Studio hosts; use `tools/cluster_ctl.py load <key> --node <node>` (`node_llm_control`) to verify persisted defaults, and use `tools/llm/lmctl_bench.py` only for temporary A/B overrides.
- Scope all LM Link model loads/unloads strictly by the target node's `model_prefixes`.
- When calling Windows `.exe` from WSL, always redirect stdin (`< /dev/null` or `stdin=subprocess.DEVNULL`).
- Verify `0` `nvlddmkm 153` and `0` `WHEA 18/19` events after GPU UV/OC or BIOS Curve Optimizer changes.
- Keep all endpoints, usernames, hostnames, and serials in gitignored `cluster.local.json` only.
