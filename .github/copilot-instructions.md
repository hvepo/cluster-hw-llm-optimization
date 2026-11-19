# GitHub Copilot Instructions — `cluster-hw-llm-optimization`

Read [AGENTS.md](../AGENTS.md) as the primary rulebook for this repository.

- **Phase 1 (Hardware/BIOS/Drivers):** Use `skills/hw-node-profiler/SKILL.md` and `tools/hw/hardware_probe.py` (or MCP `hw_node_profile`) to understand and certify any cluster node's hardware before tuning models.
- **Phase 2 (Per-Node LLM Fine-Tuning):** Use `skills/llm-node-finetune/SKILL.md`, `tools/llm/ceiling.py`, `tools/llm/lmctl_bench.py`, and `tools/llm/bench.py` to fine-tune serving configs for the target node.
- Never commit `cluster.local.json`, raw `ledger.jsonl`, `runs-*.log`, usernames, hostnames, Tailscale IPs, or secrets. Run `python3 scripts/doctor.py --all --check-privacy` before `git commit` or `git push`.
