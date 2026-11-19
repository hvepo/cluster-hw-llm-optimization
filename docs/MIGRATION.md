# Cross-Machine Migration & Installation Guide

This repo installs cleanly across **Windows (Antigravity IDE, VS Code)** and **Ubuntu WSL / Linux (Antigravity, VS Code Remote, OpenCode)** on both desktop (`torre`) and laptop (`portatil`).

---

## 1. Quick Install on Ubuntu WSL / Linux

```bash
git clone <YOUR_REPO_URL> ~/projects/cluster-hw-llm-optimization
cd ~/projects/cluster-hw-llm-optimization

# Install for this node (creates gitignored cluster.local.json from template)
bash install.sh --node torre --targets agy,opencode,vscode --doctor
# Or on Portátil WSL:
bash install.sh --node portatil --portatil-url http://127.0.0.1:1234 --targets agy,opencode,vscode --doctor
```

What `install.sh` configures:
- **`cluster.local.json`** (gitignored): sets `local_node`, endpoints, and LM Studio paths without modifying tracked repo files.
- **Antigravity (`agy`):** installs `plugins/cluster-hw-llm-optimization/` into `~/.gemini/config/plugins/cluster-hw-llm-optimization/` with rendered WSL `python3` paths in `mcp_config.json` and `hooks.json`.
- **OpenCode (`opencode`):** registers `cluster-hw-llm` in `~/.config/opencode/opencode.json` (using OpenCode v2.3.2's `mcp.servers.<name>.type="local"` schema) and syncs all 4 skills into `~/.config/opencode/skills/`.
- **VS Code (`vscode`):** ensures `.github/skills/` and `.vscode/mcp.json` are synced inside the workspace.

---

## 2. Quick Install on Windows PowerShell

Run from native PowerShell inside the cloned repo (or via `\\wsl.localhost\Ubuntu\home\<USER>\projects\cluster-hw-llm-optimization`):

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1 -Node torre -Targets agy,vscode -Doctor
```

What `install.ps1` configures:
- Auto-detects Windows `python.exe` (preferring `%LOCALAPPDATA%\Python\bin\python.exe` or `py -3`).
- Installs the Antigravity plugin into `%USERPROFILE%\.gemini\config\plugins\cluster-hw-llm-optimization\` with Windows `python.exe` paths in `mcp_config.json` and `hooks.json`.
- Safely merges the `cluster-hw-llm` stdio server into `%APPDATA%\Code\User\mcp.json` under `servers` (preserving existing `inputs` and other MCP servers).

> [!NOTE]
> Antigravity picks up newly installed/updated plugins live or on next turn without needing to hand-edit `config.json`. Never edit `~/.gemini/config/config.json` manually; manage plugin enable/disable via UI (`customization://cluster-hw-llm-optimization`) or `agy plugin enable|disable`.

---

## 3. Syncing LM Studio Permanent Model Defaults on a New/Fresh Machine

- **Torre (Windows or WSL):**
  ```bash
  python3 profiles/torre/apply_defaults.py --verify
  ```
- **Portátil (Windows PowerShell):**
  ```powershell
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\profiles\portatil\apply_defaults.ps1
  ```

---

## 4. Verification & Uninstall

```bash
# Full offline + privacy + MCP + hook self-test
python3 scripts/doctor.py --all --check-privacy

# Include live LM Studio + nvidia-smi checks
python3 scripts/doctor.py --all --check-privacy --live

# Remove installed hooks/MCP/skills cleanly
bash install.sh --uninstall
.\install.ps1 -Uninstall
```
