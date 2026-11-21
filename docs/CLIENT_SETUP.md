# Universal Client Setup — Exposing Your Dual-Node Pool (`torre` + `portatil`) Everywhere

Your fine-tuned **`torre`** (`12 GiB RTX 3080 Ti`, CUDA `llama.cpp@2.79.0`) and **`portatil`** (`24 GiB Intel Arc 140V`, Vulkan `llama.cpp@2.79.0`) model pool runs behind LM Studio + **LM Link** on `http://127.0.0.1:1234`.

The **Cluster Router Gateway** ([cluster_gateway.py](../tools/gateway/cluster_gateway.py)) sits in front on **`http://127.0.0.1:1235`** (reachable from both **Windows** and **WSL** via `.wslconfig` `networkingMode=mirrored`) and gives every IDE, agent, CLI tool, and SDK:

1. **5 Protocols on One Endpoint (`127.0.0.1:1235`):**
   - OpenAI Chat / Completions / Embeddings (`/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, `/v1/models`)
   - OpenAI Responses API (`/v1/responses`)
   - Anthropic Messages API (`/v1/messages`)
   - LM Studio Native REST API (`/api/v1/chat`, `/api/v1/models`, default-safe `/api/v1/models/load`, `/api/v1/models/unload`, `/api/v1/models/download*`)
   - Legacy LM Studio API (`/api/v0/models`)
2. **Option B Smart-First Virtual Role Aliases (`cluster/*`) + Automatic Failover:**
   - Both computers online -> true dual-GPU split; either computer asleep/away -> seamless automatic failover to the online machine.
3. **Anti-Thrash VRAM Guardrails:**
   - If `torre` currently holds a heavy `12B–15B` model, `cluster/autocomplete` and `cluster/embed` automatically divert to `portatil` instead of triggering LM Studio's `unloadPreviousJITModelOnLoad` eviction.

---

## Quick One-Command Install / Update (PC or Laptop)

Run on **Ubuntu WSL / Linux**:
```bash
git pull && bash install.sh --os auto --targets agy,opencode,vscode --serving --doctor
```

Or on **Windows PowerShell**:
```powershell
git pull
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Os auto -Targets agy,opencode,vscode -Serving -Doctor
```

Warm the verified 4-model dual-GPU home stack anytime (optional):
```bash
python3 tools/cluster_ctl.py preset home-dual
```

Check live node reachability, loaded VRAM models, and resolved alias routes:
```bash
python3 tools/gateway/gateway_ctl.py status
```

---

## Option B Virtual Alias Table

| Alias | Role | Primary | Fallback | Tuned Context |
|---|---|---|---|---:|
| `cluster/code` | Primary coding & agent edits | `p-code-moe` (30B-A3B, `32.4 tok/s`) | `t-code-chat` (14B, `72.1 tok/s`) | `16K` (`12K` fb) |
| `cluster/code-fast` | Low-latency interactive coding | `t-code-chat` (14B, `72.1 tok/s`) | `p-code-moe` (30B-A3B, `32.4 tok/s`) | `12K` (`16K` fb) |
| `cluster/code-heavy` | Complex multi-file architecture | `p-code-moe` (30B-A3B, `32.4 tok/s`) | `t-code-chat` (14B, `72.1 tok/s`) | `16K` (`12K` fb) |
| `cluster/agent-heavy` | Deep autonomous multi-step agents | `p-agent-moe-alt` (30B-A3B, `27.5 tok/s`) | `t-general-agent` (14B, `76.2 tok/s`) | `16K` (`12K` fb) |
| `cluster/fast-agent` | Fast subagents, git commits, edits | `t-fast-agent` (8B, `129.8 tok/s`) | `p-battery-chat` (4B, `29.4 tok/s`) | `16K` |
| `cluster/vision` | Fast multimodal & screenshots | `t-vision-context` (12B+MTP4, `107.5 tok/s`) | `p-agent-moe` (26B+MTP3, `26.1 tok/s`) | `16K` (`32K` fb) |
| `cluster/vision-heavy` | Long-context `32K` multimodal | `p-agent-moe` (26B+MTP3, `26.1 tok/s`) | `t-vision-context` (12B+MTP4, `107.5 tok/s`) | `32K` (`16K` fb) |
| `cluster/reasoning` | Chain-of-thought (`<think>`) | `t-deep-reasoning` (R1-14B, `75.0 tok/s`) | `p-agent-moe-alt` (30B-A3B) | `8K` (`16K` fb) |
| `cluster/math` | Math, physics, formal proofs | `t-math-physics` (Phi-4 14B, `79.4 tok/s`) | `p-agent-moe-alt` (30B-A3B) | `8K` (`16K` fb) |
| `cluster/autocomplete` | IDE Tab FIM *(local-first + anti-thrash)* | Local node (`14.8 ms` PC / `41 ms` Laptop) | Sibling node | `4K` |
| `cluster/embed` | `@codebase` / RAG *(local-first + anti-thrash)* | Local node (`258 emb/s` PC / `120 emb/s` Laptop) | Sibling node | `2K` |

*(You can always request concrete `t-*` or `p-*` IDs directly too.)*

---

## Per-Client Integration Guide

### 1. OpenCode (`v2.3.2+`, WSL & Windows)
`setup_clients.py` registers provider **`cluster`** (`@ai-sdk/openai-compatible` -> `http://127.0.0.1:1235/v1`) in `opencode.json`.
```bash
opencode models
opencode run --model cluster/code "Explain this module"
# Or pick inside the TUI with /models -> Local Cluster (Torre + Portatil)
```

### 2. VS Code Copilot BYOK
`setup_clients.py` populates **`Local Cluster (LM Link)`** in `chatLanguageModels.json` while keeping your cloud providers intact.
- Open **Copilot Chat** -> Model Picker -> select any `Cluster *` model (`Cluster Code`, `Cluster Fast Agent`, `Cluster Vision Heavy`, etc.).

### 3. Continue (VS Code)
`setup_clients.py` backs up `~/.continue/config.yaml` and writes `schema: v1` roles:
- **Chat / Edit / Apply:** `cluster/code`, `cluster/code-fast`, `cluster/fast-agent`, `cluster/vision-heavy`, `cluster/reasoning`
- **Tab Autocomplete:** `cluster/autocomplete`
- **Embeddings (`@codebase`):** `cluster/embed`

### 4. Cline / Roo Code / Twinny (VS Code Extensions)
- **Cline / Roo Code:** API Provider = `OpenAI Compatible`, Base URL = `http://127.0.0.1:1235/v1`, API Key = `local-cluster`, Model ID = `cluster/code` (or `cluster/agent-heavy`).
- **Twinny:** Set Chat/FIM host `127.0.0.1`, port `1235`, model `cluster/code` (chat) and `cluster/autocomplete` (FIM).

### 5. LM Studio Native `/api/v1/chat` & Stateful MCP Helper
```bash
python3 tools/gateway/cluster_chat.py --model cluster/code --mcp browse --stateful "Inspect README.md"
```
- Chains `previous_response_id` pinned to the exact machine holding the state and prints `tok/s` + `TTFT`.
- `cluster-hw-llm` is also registered in `%USERPROFILE%\.lmstudio\mcp.json` alongside existing servers.

### 6. CLI Agents & SDKs (Opt-In Helpers)
Load helpers once in your shell (`source ~/.config/cluster-llm/env.sh`) or PowerShell (`. $HOME\.config\cluster-llm\env.ps1`):
- **Claude Code (`/v1/messages`):** `cluster-claude`
- **Codex CLI (`/v1/responses`):** `python3 scripts/setup_clients.py --clients codex` then `cluster-codex`
- **Aider:** `cluster-aider`
- **Python / Node OpenAI SDK:** run `use-cluster-openai`, then `OpenAI(base_url="http://127.0.0.1:1235/v1", api_key="local-cluster")`
- **Python Anthropic SDK:** `Anthropic(base_url="http://127.0.0.1:1235", api_key="local-cluster")`
