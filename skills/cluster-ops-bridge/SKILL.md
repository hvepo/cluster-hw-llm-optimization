---
name: cluster-ops-bridge
description: Use from any project to access global horizontal cluster resources: local LLM delegation (:1235 / cluster-*), bidirectional cross-OS peer execution (:1236 / Drive queue), and the Antigravity-first Active-Standby Telegram responder.
---

# Cluster Ops Bridge (Cross-Project Horizontal Resources)

`cluster-hw-llm-optimization` provides a shared horizontal infrastructure layer across all workspaces on `torre` (desktop PC) and `portatil` (laptop) via the global `cluster-hw-llm` MCP server, global skills/rules/hooks, and global `PATH` commands.

## 1. Local LLM Delegation (`:1235` Cluster Router Gateway)

Local models are **opt-in** delegation targets — do not wake them for routine cloud/Telegram turns unless delegation clearly adds value or is explicitly requested.

- **MCP tools:** `cluster_models` (inspect live alias -> model/node mapping) and `cluster_chat` (send prompts or continue stateful threads via `previous_response_id`).
- **Global subagents:** `cluster-coder`, `cluster-fast-agent`, `cluster-reasoning`, `cluster-vision`.
- **OpenAI / Anthropic / LM Studio endpoints:** `http://127.0.0.1:1235` (`/v1/chat/completions`, `/v1/responses`, `/v1/messages`, `/api/v1/chat`).
- **Virtual role aliases:** `cluster/code`, `cluster/code-fast`, `cluster/agent-heavy`, `cluster/fast-agent`, `cluster/vision`, `cluster/vision-heavy`, `cluster/reasoning`, `cluster/math`, `cluster/autocomplete`, `cluster/embed`.

## 2. Bidirectional Cross-OS Peer Bridge (`:1236` + Drive Queue)

Run commands or autonomous Antigravity orders across `torre` and `portatil` (WSL, Windows, or `auto`) from any project:

- **MCP tools:** `peer_status`, `peer_exec`, `peer_send_order`, `peer_order_status`.
- **CLI (`PATH`):**
  ```bash
  cluster-peer status
  cluster-peer exec --to torre --os windows "nvidia-smi"
  cluster-peer exec --to portatil --os wsl "hostname"
  cluster-peer send --to torre --os auto --wait 120 "Inspect disk space and report"
  ```
- **Default working directory:** Unless `cwd` is explicitly provided, remote commands and orders execute in `~` (project-independent).
- **Offline resilience:** When a sibling node is offline over Tailscale `:1236`, orders automatically queue to `<sync_dir>/peer-queue/to_<node>/` via Google Drive.

## 3. Antigravity-First Telegram Responder (Active-Standby HA)

- By default (`peer.openclaw.responder = "antigravity"`), the active HA leader polls Telegram via `telegram_agy_daemon.py` and routes DMs directly to Antigravity (`agy -p`) from `cwd=~` with full permissions and persistent per-chat `conversation_id`.
- Only one node holds `<sync_dir>/openclaw-ha/leader.json` at a time (`torre` primary, `portatil` automatic standby failover).
- **MCP & CLI controls:**
  - `openclaw_control(action="status" | "takeover" | "telegram_reply" | "sync_secrets")`
  - `cluster-telegram status` / `cluster-telegram takeover` / `cluster-telegram telegram-reply --message "..."`

## 4. Global `PATH` Commands (Usable From Any Directory)

- `cluster-peer` — Peer Bridge & cross-OS execution (`tools/peer/peer_ctl.py`)
- `cluster-telegram` — Telegram / OpenClaw HA controller (`tools/peer/openclaw_ctl.py`)
- `cluster-llm` — Node-level LM Studio load/unload/preset control (`tools/cluster_ctl.py`)
- `cluster-gateway` — Cluster Router Gateway `:1235` control (`tools/gateway/gateway_ctl.py`)
- `cluster-chat` — Quick CLI chat against `:1235` (`tools/gateway/cluster_chat.py`)
- `cluster-hw` — Hardware/BIOS/driver/Event Log probe (`tools/hw/hardware_probe.py`)
- `cluster-doctor` — Full verification & privacy scan (`scripts/doctor.py`)
