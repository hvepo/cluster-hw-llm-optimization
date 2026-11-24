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
3. **NVIDIA NIM Cloud Aliases (`nim/*`) + Zero-VRAM Cloud Orchestrators:**
   - When the NVIDIA NIM API key is present, the gateway proxies NVIDIA NIM models (`nim/ultra`, `nim/fast`, `nim/vision`, `nim/reasoning`, `nim/code`, `nim/mistral-large`, `nim/kimi`, `nim/step`, `nim/glm-flash`, `nim/minimax-flash`, `nim/gemma-31b`, `nim/embed`) over `https://integrate.api.nvidia.com/v1` as ordinary chat/agent models, and `nim/ultra`/`nim/fast` also act as autonomous tool-calling orchestrators (`cluster_orchestrate`).
4. **Anti-Thrash VRAM Guardrails:**
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

**NVIDIA NIM Cloud Alias Table (requires the NVIDIA NIM API key; zero local VRAM):**

| Alias | Role | Model (NVIDIA) | Context / Out |
|---|---|---|---|
| `nim/ultra` | Flagship cloud orchestrator (tool-calling) | `nvidia/nemotron-3-ultra-550b-a55b` | `131072` / `16384` |
| `nim/fast` | Fast cloud agent/orchestrator (tool-calling) | `nvidia/nemotron-3.5-lightning-30b-a3b` | `131072` / `8192` |
| `nim/vision` | Multimodal cloud vision (Super-VL 120B) | `nvidia/nemotron-3.5-super-vl-120b-a12b` | `131072` / `8192` |
| `nim/reasoning` | Cloud chain-of-thought reasoning | `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning` | `131072` / `8192` |
| `nim/code` | Cloud fast code completion (Laguna-XS 2.1) | `poolside/laguna-xs-2.1` | `65536` / `8192` |
| `nim/mistral-large` | Cloud frontier general/code (Mistral Large 4 1T-A52B) | `mistralai/mistral-large-4-1t-a52b` | `131072` / `16384` |
| `nim/kimi` | Cloud long-context reasoning/code (K3.1 Pro) | `moonshotai/kimi-k3.1-pro` | `131072` / `16384` |
| `nim/step` | Cloud frontier reasoning (Step-5 Preview) | `stepfun-ai/step-5-preview` | `131072` / `16384` |
| `nim/glm-flash` | Cloud fast coding/agent (GLM-5.3 Flash) | `z-ai/glm-5.3-flash` | `131072` / `8192` |
| `nim/minimax-flash` | Cloud fast long-context (MiniMax M3.1 Flash) | `minimaxai/minimax-m3.1-flash` | `131072` / `8192` |
| `nim/gemma-31b` | Cloud instruction/code (Gemma-4 31B) | `google/gemma-4-31b-it` | `131072` / `8192` |
| `nim/embed` | Cloud semantic embeddings (Nemotron-3.5 8B) | `nvidia/nemotron-3.5-embed-8b` | `8192` |

---

## Per-Client Integration Guide

### 1. OpenCode (`v2.3.2+`, WSL & Windows)
`setup_clients.py` registers provider **`cluster`** (`@ai-sdk/openai-compatible` -> `http://127.0.0.1:1235/v1`) in `opencode.json` — unified catalog of `cluster/*` local aliases plus `nim/*` cloud aliases when the NVIDIA NIM key is present. When the key exists, it also registers provider **`nvidia-nim`** (`https://integrate.api.nvidia.com/v1`) so `opencode run --model nim/ultra "Explain this module"` works with zero local VRAM, and selecting `nim/ultra`/`nim/fast` in any chat/agent gives it the `cluster-hw-llm` MCP tools (see Section 7).
```bash
opencode models
opencode run --model cluster/code "Explain this module"
# Or pick inside the TUI with /models -> Local Cluster (Torre + Portatil)
```

### 2. VS Code Copilot BYOK & Built-in MCP
`install.py` registers **`cluster-hw-llm`** (15 tools, 3 resources) in `mcp.json` (and re-enables `"chat.disableAIFeatures": false` if disabled), while `setup_clients.py` populates **`Local Cluster (LM Link)`** (`cluster/*` + `nim/*`) in `chatLanguageModels.json` without touching cloud providers. When the NVIDIA NIM key is present, it also registers the **`NVIDIA NIM Cloud`** entry pointing at `https://integrate.api.nvidia.com/v1/chat/completions`.
- Open **Copilot Chat** -> Model Picker -> select any `Cluster *` model (`Cluster Code`, `Cluster Fast Agent`, `Cluster Vision Heavy`, etc.) or `NVIDIA NIM *` model and attach `cluster-hw-llm` tools/resources.

### 3. Continue (VS Code — PC `torre` & Laptop `portatil`)
`scripts/install_continue.py` (also called automatically by `setup_clients.py`) backs up any existing `config.yaml` and installs a node-tailored `schema: v1` config to **both Windows** (`%USERPROFILE%\.continue\config.yaml`) **and WSL** (`~/.continue/config.yaml`) with `tool_use` / `image_input` capabilities, tuned `contextLength`, cluster rules, and the `cluster-hw-llm` MCP server:
- **Reference templates:** [profiles/torre/continue-config.yaml](../profiles/torre/continue-config.yaml) (PC) and [profiles/portatil/continue-config.yaml](../profiles/portatil/continue-config.yaml) (Laptop)
- **Standalone Continue installer (Windows + WSL):**
  - PC (`torre`): `python3 scripts/install_continue.py --node torre --os auto`
  - Laptop (`portatil`): `python3 scripts/install_continue.py --node portatil --os auto` (or `--node auto` to read `cluster.local.json`)
- **Chat / Edit / Apply:** `cluster/code`, `cluster/code-fast`, `cluster/code-heavy`, `cluster/agent-heavy`, `cluster/fast-agent`, `cluster/vision`, `cluster/vision-heavy`, `cluster/reasoning`, `cluster/math`
- **Tab Autocomplete:** `cluster/autocomplete`
- **Embeddings (`@codebase`):** `cluster/embed`
- **NVIDIA NIM cloud models** (when the NVIDIA NIM key is present, see the Cloud Alias Table above): `nim/ultra`, `nim/fast`, `nim/vision`, `nim/reasoning`, `nim/code`, `nim/mistral-large`, `nim/kimi`, `nim/step`, `nim/glm-flash`, `nim/minimax-flash`, `nim/gemma-31b`, `nim/embed`
- **MCP:** `cluster-hw-llm` (15 tools, 3 resources)

### 4. Cline / Roo Code / Kilo Code / Twinny (VS Code Extensions)
`install.sh` / `install.ps1` (`--targets vscode`) automatically registers `cluster-hw-llm` across installed extensions on both Windows and WSL (`torre` and `portatil`):
- **Twinny:** Populates `twinny.chatToolsMcpServers.cluster-hw-llm` in VS Code `settings.json` (and WSL Remote `Machine/settings.json`). Set Chat/FIM host `127.0.0.1`, port `1235`, model `cluster/code` (chat) and `cluster/autocomplete` (FIM).
- **Cline / Roo Code / Kilo Code:** Automatically merges `mcpServers.cluster-hw-llm` into each installed extension's `globalStorage/*/settings/*mcp_settings.json`. Set API Provider = `OpenAI Compatible`, Base URL = `http://127.0.0.1:1235/v1`, API Key = `local-cluster`, Model ID = `cluster/code` (or `cluster/agent-heavy`).

### 5. LM Studio Native `/api/v1/chat` & Stateful MCP Helper
```bash
python3 tools/gateway/cluster_chat.py --model cluster/code --mcp browse --stateful "Inspect README.md"
```
- Chains `previous_response_id` pinned to the exact machine holding the state and prints `tok/s` + `TTFT`.
- `cluster-hw-llm` is also registered in `%USERPROFILE%\.lmstudio\mcp.json` alongside existing servers.

### 6. CLI Agents & SDKs (Opt-In Helpers)
Load helpers once in your shell (`source ~/.config/cluster-llm/env.sh`) or PowerShell (`. $HOME\.config\cluster-llm\env.ps1`):
- **Claude Code (`/v1/messages`):** `cluster-claude`
- **Codex CLI (`/v1/responses`):** `python3 scripts/setup_clients.py --clients codex` then `cluster-codex` (with the NVIDIA NIM key present, a `[model_providers.nvidia]` block is also added: `NVIDIA_API_KEY="$(cat ~/.config/nvidia_api_key)" codex -c model_provider="nvidia"`)
- **Aider:** `cluster-aider`
- **Antigravity SDK local agent:** `cluster-agy "Inspect src/ and summarize"` (requires `pip install google-antigravity`)
- **Python / Node OpenAI SDK:** run `use-cluster-openai`, then `OpenAI(base_url="http://127.0.0.1:1235/v1", api_key="local-cluster")`
- **Python Anthropic SDK:** `Anthropic(base_url="http://127.0.0.1:1235", api_key="local-cluster")`
- **NVIDIA NIM cloud helpers** (require the key at `~/.config/nvidia_api_key` or `NVIDIA_API_KEY`): `use-nvidia-nim` (exports `OPENAI_BASE_URL=https://integrate.api.nvidia.com/v1` + `OPENAI_API_KEY`), `nim-chat` (`cluster-chat --model nim/ultra`), `nim-orchestrate` (`cluster-chat --orchestrate --model nim/ultra`), `nim-aider` (`aider --model openai/nim/ultra`)

---

### 7. Antigravity (App · IDE · CLI · Python SDK)

Antigravity's native UI model dropdown only lists Google-managed cloud models (Gemini 4 / Gemini 3.x / Claude / Orchestrator, or direct Gemini API via `modelProvider: "gemini"` + `GEMINI_API_KEY`); it does not support custom local OpenAI-compatible BYOK endpoints inside the UI selector.

To use your local `torre` + `portatil` `cluster/*` pool with Antigravity on either machine:

1. **Bundled Plugin Agents in Antigravity's Agents Menu (`cluster-coder`, `cluster-fast-agent`, `cluster-reasoning`, `cluster-vision`, `cluster-orchestrator`):**
   - Packaged under `plugins/cluster-hw-llm-optimization/agents/` and deployed globally to `~/.gemini/config/plugins/cluster-hw-llm-optimization` (both Windows and WSL).
   - `cluster-coder`/`cluster-fast-agent`/`cluster-reasoning`/`cluster-vision` route strictly through virtual `cluster/*` aliases via `cluster_chat` (with `tools/gateway/cluster_chat.py` CLI fallback) — or through zero-VRAM cloud `nim/*` aliases — so they work identically on `torre` and `portatil` (dual-GPU over LM Link at home, or automatic local Arc 140V failover when away).
   - `cluster-orchestrator` is the multi-node/hybrid cloud+local delegation agent: it calls `cluster_orchestrate` (NVIDIA NIM `nim/ultra`/`nim/fast`) which autonomously drives `peer_status`, `peer_exec`, `peer_send_order`, `cluster_models`, `cluster_chat`, and more across the mesh.
   - See [AGENT_COLLABORATION.md](AGENT_COLLABORATION.md) for the full architecture and collaboration guide.
   - Sync both Windows and WSL plugins anytime on either machine with:
     - WSL / Linux: `bash install.sh --os auto --targets agy --scope user`
     - Windows PowerShell: `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Os auto -Targets agy -Scope user`
2. **Inside Antigravity App / IDE / CLI via MCP (`cluster-hw-llm`):**
   - `cluster_models` & `cluster_chat`: Inspect live `cluster/*` and `nim/*` routes and delegate coding, reasoning, or multimodal prompts to your local pool or NVIDIA NIM cloud models with stateful `previous_response_id` chaining.
   - `cluster_orchestrate`: Let `nim/ultra`/`nim/fast` autonomously execute multi-step cluster-horizontal goals (peer status, exec, orders, LLM status, hardware profiles) across all nodes and return a `tool_trace`.
   - `peer_status`, `peer_send_order`, `peer_order_status`, `peer_exec`, & `openclaw_control`: Send autonomous orders bidirectionally between `torre` and `portatil` across WSL/Windows and control OpenClaw + Telegram HA leadership — see [PEER_AND_OPENCLAW.md](PEER_AND_OPENCLAW.md).
3. **Full Local Agent via the Official Antigravity Python SDK (`google-antigravity`):**
   - Antigravity's Python SDK natively supports external OpenAI-compatible servers via `LocalOpenAIAgentConfig`.
   - Use [tools/gateway/agy_local_agent.py](../tools/gateway/agy_local_agent.py) (or the `cluster-agy` helper from `env.sh`/`env.ps1`):
     ```bash
     pip install google-antigravity
     python3 tools/gateway/agy_local_agent.py --model cluster/code --workspace . "Review this repo for bugs"
     ```

---

## Cleaning Up Leftover Ollama Ghosts (PC & Laptop)

If `ollama/*` models (`gemma4:12b`, etc.) still appear in OpenCode or VS Code after uninstalling the Windows Ollama app, it is almost always a **second systemd Ollama service still running inside Ubuntu WSL** on `:11434` (which `.wslconfig` `networkingMode=mirrored` exposes directly to Windows `127.0.0.1:11434`), or a stale Windows User `PATH` entry.

Run the cross-OS audit and cleaner from either **WSL** or **Windows PowerShell** on both `torre` and `portatil`:

```bash
# Read-only check across Windows + WSL + port 11434 + IDE configs:
python3 scripts/cleanup_ollama.py --check

# Remove user leftovers + WSL systemd Ollama (prompts once for sudo password if needed):
python3 scripts/cleanup_ollama.py --apply
```
