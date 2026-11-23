# Bidirectional Peer Order Bridge & OpenClaw Telegram HA

Send autonomous tasks bidirectionally between **`torre` (PC)** and **`portatil` (Laptop)** across **WSL, Windows, or Crossed** sessions, and control **OpenClaw** + your **Telegram Mobile Bot** from either machine without `409 Conflict` split-brain errors.

---

## 1. Architecture

```mermaid
flowchart TD
    Phone["Mobile Phone (Telegram Bot)"]
    OpenClaw["OpenClaw Gateway in WSL (:18789, Active Leader)"]
    TorreAgy["torre Antigravity / IDE (WSL or Windows)"]
    PortatilAgy["portatil Antigravity / IDE (WSL or Windows)"]
    TorrePeer["torre Peer Daemon (:1236 Tailscale + 127.0.0.1)"]
    PortatilPeer["portatil Peer Daemon (:1236 Tailscale + 127.0.0.1)"]
    Drive["Google Drive Sync (peer-queue + openclaw-ha/leader.json)"]
    Gateway["Cluster Router Gateway (:1235 -> cluster/* LM Link)"]

    Phone <-->|"Allowlisted Telegram DM"| OpenClaw
    OpenClaw -->|"cluster/code · cluster/fast-agent"| Gateway
    OpenClaw -->|"peer_ctl.py send --to torre|portatil"| TorrePeer
    OpenClaw -->|"peer_ctl.py send --to torre|portatil"| PortatilPeer
    TorreAgy <-->|"MCP peer_* / openclaw_control"| TorrePeer
    PortatilAgy <-->|"MCP peer_* / openclaw_control"| PortatilPeer
    TorrePeer <-->|"Fast-Path HTTP (:1236 + Bearer Token)"| PortatilPeer
    TorrePeer <-->|"Offline Queue + HA Lease"| Drive
    PortatilPeer <-->|"Offline Queue + HA Lease"| Drive
```

---

## 2. How Cross-OS & Bidirectional Control Works

1. **Fast-Path Over Tailscale (`:1236`) + Google Drive Offline Queue:**
   - Each machine runs `tools/peer/peer_daemon.py` bound strictly to `127.0.0.1` + its Tailscale IP (never `0.0.0.0`), protected by a 256-bit `Bearer` token stored in gitignored `cluster.local.json` and synced to `<sync_dir>/peer.token`.
   - When the sibling machine is online, orders travel over Tailscale in `<20 ms`.
   - When the sibling machine is asleep or offline, `peer_ctl.py` / `peer_send_order` automatically drops the task into `<sync_dir>/peer-queue/to_<node>/`, and the sibling daemon runs it as soon as it wakes up.
2. **WSL, Windows, or Crossed Execution (`--os auto | wsl | windows`):**
   - Because WSL2 uses `networkingMode=mirrored`, Windows and WSL on each machine share `127.0.0.1` and the Tailscale IP.
   - `target_os="wsl"` executes `~/.gemini/bin/agy -p ... --mode auto --dangerously-skip-permissions --output-format json` (or `bash -lc`).
   - `target_os="windows"` runs `powershell.exe -NoProfile -Command ...` with `stdin=subprocess.DEVNULL` (for `exec`) or routes an `agy` task targeting Windows PowerShell.
3. **OpenClaw in WSL Only + Active-Standby HA:**
   - OpenClaw runs in **WSL** on both machines (accessible from Windows browsers via `localhost:18789` thanks to mirrored networking) and is pre-configured to use `http://127.0.0.1:1235/v1` (`cluster/code`, `cluster/fast-agent`, `cluster/reasoning`, `cluster/vision-heavy`) while keeping your existing Telegram `botToken` and `allowFrom` allowlist intact.
   - Only **one** machine holds `<sync_dir>/openclaw-ha/leader.json` and runs `openclaw gateway run` at any time:
     - **`torre`** is primary when online.
     - **`portatil`** promotes itself automatically if `torre` fails 3 consecutive probes and the 30s lease expires.
     - **Manual Takeover:** Either PC or Laptop can run `openclaw_ctl.py takeover` (or MCP `openclaw_control(action="takeover")`) to cleanly fence the sibling over `:1236` and assume Telegram bot leadership immediately.

---

## 3. CLI & MCP Usage

### From Antigravity / VS Code / Continue / OpenCode (MCP Tools)
- `peer_status()` — Check `torre` and `portatil` `:1236` health, `agy` availability, queued jobs, and OpenClaw HA lease.
- `peer_send_order(node="portatil", prompt="Pull latest main and run doctor", target_os="wsl", mode="auto", wait_s=120)`
- `peer_order_status(job_id="...")` — Check async job output and `conversation_id` for multi-turn follow-ups.
- `peer_exec(node="torre", command="Get-Service Tailscale", target_os="windows")` — Run PowerShell or bash directly on either node.
- `openclaw_control(action="takeover")` / `openclaw_control(action="status")` / `openclaw_control(action="telegram_reply", message="...")`

### From Terminal (`peer_ctl.py` & `openclaw_ctl.py`)
```bash
# Check peer & OpenClaw status
python3 tools/peer/peer_ctl.py status
python3 tools/peer/openclaw_ctl.py status

# Send an Antigravity task from PC -> Laptop (WSL) or Laptop -> PC (Windows)
python3 tools/peer/peer_ctl.py send --to portatil --os wsl --wait 120 "Run git pull and doctor"
python3 tools/peer/peer_ctl.py exec --to torre --os windows "nvidia-smi"

# Transfer OpenClaw Telegram leadership to the current machine
python3 tools/peer/openclaw_ctl.py takeover
```

---

## 4. Setup on Both Machines

1. **On `torre` (Primary PC):**
   ```bash
   bash install.sh --node torre --os auto --targets agy,opencode,vscode --serving --peer --openclaw --doctor
   ```
2. **On `portatil` (Standby Laptop):**
   ```bash
   git pull && bash install.sh --node portatil --os auto --targets agy,opencode,vscode --serving --peer --openclaw --doctor
   ```
   *(On Windows PowerShell, `.\install.ps1 -Node portatil -Os auto -Targets agy,opencode,vscode -Serving -Peer -OpenClaw -Doctor` bridges into WSL automatically.)*
