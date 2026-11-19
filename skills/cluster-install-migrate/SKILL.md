---
name: cluster-install-migrate
description: >-
  Install, configure, migrate, verify, or uninstall cluster-hw-llm-optimization on any cluster node
  across Windows (Antigravity & VS Code) and Ubuntu WSL / Linux (Antigravity, VS Code Remote, &
  OpenCode). Scaffolds gitignored cluster.local.json for any node name, registers stdio MCP tools,
  deploys Antigravity plugin/hooks, syncs skills, and runs scripts/doctor.py verification.
---

# Cluster Node Install & Migration (`cluster-install-migrate`)

Deploy the hardware profiler and LLM fine-tuning toolkit on any cluster node (`torre`, `portatil`, or any new node name) across Windows and Ubuntu WSL/Linux.

## Install Commands
1. **Ubuntu WSL / Linux:**
   ```bash
   bash install.sh --node <node_name> --targets agy,opencode,vscode --doctor
   ```
2. **Windows PowerShell:**
   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Node <node_name> -Targets agy,vscode -Doctor
   ```

## Verification & Privacy Gate
- **Offline + privacy self-test (run before `git push`):**
  ```bash
  python3 scripts/doctor.py --all --check-privacy
  ```
- **Live hardware + MCP + LM Studio node check:**
  ```bash
  python3 scripts/doctor.py --all --check-privacy --live
  ```
- See [docs/MIGRATION.md](../../docs/MIGRATION.md) for adding new nodes, persisting LM Studio defaults, and `--uninstall`.
