# Cross-Machine Migration & Universal Installation Guide

This repo installs cleanly across **any combination** of **Windows**, **Ubuntu WSL**, and **Linux** with **Antigravity (`agy`)**, **VS Code (`vscode`)**, and **OpenCode (`opencode`)** on desktop (`torre`), laptop (`portatil`), or any custom node name.

---

## 1. Universal Support Matrix (`--os` × `--targets`)

Both `install.sh` and `install.ps1` delegate to `scripts/install.py` and accept any combination of:
- `--os` / `-Os`: `auto` (default: configures active OS + bridges to sibling Windows/WSL host when detected), `windows`, `wsl`, `linux`, `all`, or comma-separated (e.g. `windows,wsl`).
- `--targets` / `-Targets`: `all`, or any comma-separated subset of `agy`, `vscode`, `opencode` (default: `agy,opencode,vscode`).
- `--scope` / `-Scope`: `all` (default), `user`, or `workspace`.

| OS Target | Client Target | Installed Paths & Integrations |
|---|---|---|
| `windows` | `agy` | `%USERPROFILE%\.gemini\config\plugins\cluster-hw-llm-optimization\` (`plugin.json`, `mcp_config.json`, `hooks.json`, `rules/AGENTS.md`, `skills/`, `hooks/`, `repo_root.txt`) |
| `windows` | `vscode` | `%APPDATA%\Code\User\mcp.json` (+ `Code - Insiders`), `%USERPROFILE%\.copilot\skills\<skill>\SKILL.md`, workspace `.vscode/mcp.json` & `.github/skills/` |
| `windows` | `opencode` | `%USERPROFILE%\.config\opencode\opencode.json` (+ `%APPDATA%\opencode\opencode.json`), `%USERPROFILE%\.config\opencode\skills\<skill>\SKILL.md` |
| `wsl` / `linux` | `agy` | `~/.gemini/config/plugins/cluster-hw-llm-optimization/` |
| `wsl` / `linux` | `vscode` | `~/.config/Code/User/mcp.json`, `~/.vscode-server/data/User/mcp.json`, `~/.copilot/skills/<skill>/SKILL.md`, workspace `.vscode/mcp.json` & `.github/skills/` |
| `wsl` / `linux` | `opencode` | `~/.config/opencode/opencode.json` (or `$XDG_CONFIG_HOME/opencode/opencode.json`), `~/.config/opencode/skills/<skill>/SKILL.md` |

---

## 2. Quick Install from Ubuntu WSL / Linux

```bash
git clone <YOUR_REPO_URL> ~/projects/cluster-hw-llm-optimization
cd ~/projects/cluster-hw-llm-optimization

# Install for WSL + auto-bridge to Windows host (all 3 clients: Antigravity, OpenCode, VS Code)
bash install.sh --node torre --os auto --targets agy,opencode,vscode --doctor

# Or restrict strictly to WSL and specific clients:
bash install.sh --node portatil --os wsl --targets opencode,vscode --doctor
```

---

## 3. Quick Install from Windows PowerShell

Run from native PowerShell inside the cloned repo (or via `\\wsl.localhost\Ubuntu\home\<USER>\projects\cluster-hw-llm-optimization`):

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1 -Node torre -Os auto -Targets agy,opencode,vscode -Doctor
```

- Auto-detects Windows `python.exe` (preferring `%LOCALAPPDATA%\Python\bin\python.exe` or `py -3`).
- If native Windows `python.exe` is not installed on the Windows host, automatically falls back to a `wsl.exe -e python3` bridge so Windows Antigravity, Windows VS Code, and Windows OpenCode still run all MCP tools and hooks seamlessly.

> [!NOTE]
> Antigravity picks up newly installed/updated plugins live or on next turn without needing to hand-edit `config.json`. Never edit `~/.gemini/config/config.json` manually; manage plugin enable/disable via UI (`customization://cluster-hw-llm-optimization`) or `agy plugin enable|disable`.

---

## 4. Syncing LM Studio Permanent Model Defaults on a New/Fresh Machine

- **Torre (Windows or WSL):**
  ```bash
  python3 profiles/torre/apply_defaults.py --verify
  ```
- **Portátil (Windows PowerShell):**
  ```powershell
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\profiles\portatil\apply_defaults.ps1
  ```

---

## 5. Verification & Uninstall

```bash
# Full offline + install matrix + privacy + MCP + hook self-test
python3 scripts/doctor.py --all --check-privacy

# Include live LM Studio + nvidia-smi checks
python3 scripts/doctor.py --all --check-privacy --live

# Remove installed hooks/MCP/skills cleanly (supports --os and --targets filters)
bash install.sh --uninstall
.\install.ps1 -Uninstall
```
