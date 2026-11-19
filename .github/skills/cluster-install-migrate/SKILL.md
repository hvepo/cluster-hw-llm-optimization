---
name: cluster-install-migrate
description: >-
  Install, configure, migrate, verify, or uninstall cluster-hw-llm-optimization on any cluster node
  across any combination of Windows, WSL, and Linux with Antigravity, VS Code, and OpenCode.
  Scaffolds gitignored cluster.local.json for any node name, registers stdio MCP tools, deploys
  Antigravity plugin/hooks, syncs global & workspace skills, bridges Windows <-> WSL, and runs
  scripts/doctor.py verification.
---

# Cluster Node Install & Migration (`cluster-install-migrate`)

Deploy the hardware profiler and LLM fine-tuning toolkit on any cluster node (`torre`, `portatil`, or any new node name) across **any combination** of OS (**Windows**, **WSL**, **Linux**) and client (**Antigravity**, **VS Code**, **OpenCode**).

## Universal Install Commands (`scripts/install.py`, `install.sh`, `install.ps1`)

1. **From Ubuntu WSL / Linux (auto-bridges to Windows host on dual Windows+WSL machines):**
   ```bash
   bash install.sh --node <node_name> --os auto --targets agy,opencode,vscode --doctor
   ```
2. **From Windows PowerShell (auto-bridges to WSL when present):**
   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Node <node_name> -Os auto -Targets agy,opencode,vscode -Doctor
   ```
3. **Target Specific Combinations (`--os` × `--targets`):**
   ```bash
   # Only WSL + OpenCode & VS Code
   python3 scripts/install.py --os wsl --targets opencode,vscode
   # Only Windows + Antigravity, VS Code, & OpenCode (runnable from either Windows or WSL!)
   python3 scripts/install.py --os windows --targets agy,vscode,opencode
   # Both Windows and WSL + all 3 clients
   python3 scripts/install.py --os all --targets all --doctor
   ```

## Verification & Privacy Gate
- **Offline + install matrix + privacy self-test (run before `git push`):**
  ```bash
  python3 scripts/doctor.py --all --check-privacy
  ```
- **Live hardware + MCP + LM Studio node check:**
  ```bash
  python3 scripts/doctor.py --all --check-privacy --live
  ```
- See [docs/MIGRATION.md](../../docs/MIGRATION.md) for adding new nodes, persisting LM Studio defaults, and `--uninstall`.
