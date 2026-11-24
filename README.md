# cluster-hw-llm-optimization

**Hardware, BIOS & Driver Node Profiler + Per-Node Local LLM Serving Fine-Tuner** for inspecting the hardware configuration of **any cluster device** (CPU, motherboard/BIOS, RAM/DOCP, discrete or unified GPUs, chipset/platform drivers, power plan, Event Log stability) and then **fine-tuning (hill-climbing) local LLM serving parameters** specifically for that cluster node.

Works across **any combination** of **Windows**, **Ubuntu WSL**, and **Linux** with **Antigravity (`agy`)**, **VS Code (`vscode`)**, and **OpenCode (`opencode`)** (plus Claude Code via `AGENTS.md`), with zero external Python/Node dependencies.

> Derived in part from [sybil-solutions/inference-optimisation-skill](https://github.com/sybil-solutions/inference-optimisation-skill) (MIT), expanded with universal hardware/BIOS/driver profiling (`tools/hw/hardware_probe.py`), GPU undervolt + VRAM OC verification, CPU Per-Core Curve Optimizer stress suites, fixed linear multi-stream `ceiling.py` math, default-preserving LM Studio controllers, a zero-dependency Stdio MCP server, lifecycle safety hooks, and cross-OS installers.

---

## Two-Phase Workflow (Any Cluster Node)

```text
Phase 1: Understand & Certify the Target Cluster Device (hw-node-profiler)
  tools/hw/hardware_probe.py [--node <name>] [--hwinfo <export.json>]
  tools/hw/{bw_test.py, gpu_oc_verify.py, cpu_co_stress.py, stress_suite.py}
        │
        ▼  verified hardware profile: real bandwidth, VRAM/unified cap, BIOS/driver/power health
Phase 2: Fine-Tune LLM Serving Parameters for That Node (llm-node-finetune)
  tools/llm/{ceiling.py, lmctl_bench.py, bench.py, queue.sh, run_gates.py}
        │
        ▼  persisted optimal per-model configs in profiles/<node>/ + profiles/best-config.json
```

---

## Verified Node Reference Results

Full methodology and per-model tables in [docs/BENCHMARKS.md](docs/BENCHMARKS.md) and [profiles/best-config.json](profiles/best-config.json):

### 1. Hardware, BIOS & Driver Optimization
- **Desktop CUDA Node (`torre` — `RTX 3080 Ti` 12 GiB + `Ryzen 9 5950X` + `32 GiB DDR4-3600 CL14`):**
  - **BIOS & CPU Per-Core Curve Optimizer:** Correlated Windows `WHEA 18` (`APIC ID 6/7`) with HWiNFO64 CPPC rankings (`Core 3 -> 0`, other star cores `-5`, rest `-10`), updated AMD X570 Chipset drivers (`8.09.28.1124` + `Ryzen Balanced`), switched Windows Power Plan to `Balanced` (`MinCPU 0%`, `ASPM Off`), and certified stability with `tools/hw/stress_suite.py` (`0` `WHEA 18/19`, `0` `nvlddmkm 153`).
  - **GPU Undervolt + VRAM OC (`tools/hw/gpu_oc_verify.py`):** Flattened MSI Afterburner V/F curve (`1,770 MHz @ 875 mV`, flat `1,785 MHz` ceiling through `1,237.5 mV`, `+500 MHz` GDDR6X `10,001 MHz`, `350 W` PL) raised CUDA D2D bandwidth from `788.48` -> **`831.81 GB/s`** post-stress (`0` throttle flags, `3 °C` cooler).
- **Unified iGPU Laptop Node (`portatil` — `Core Ultra 7 258V` + `Intel Arc 140V` + `32 GiB LPDDR5x-8533`):**
  - Configured `75%` (`~24 GiB`) Shared GPU Memory Override so `30B-A3B` and `26B-A4B` MoE models fit 100% in unified iGPU memory.

### 2. Per-Node LLM Serving Fine-Tuning (`12 LLMs + 2 Embeddings`)
- **`torre` (`12 GiB RTX 3080 Ti`):**
  - `t-code-chat` (`Qwen2.5-Coder-14B Q4_K_M`, `8K, p=2, q8_0, pbs=2048`): `18.43` -> `60.34` stock -> **`71.12 tok/s c1` (`128.53 c2`)** under GPU UV+OC (**3.86× speedup**), `2K` prefill `188.5 -> 2,069.6 tok/s`, `3/3` needle @ 50%/100%.
  - `t-vision-context` (`Gemma-4-12B-IT + MTP-3`, `16K, q8_0, ckpt=0`): `34.07` -> `97.69` stock -> **`107.44 tok/s c1` (`143.76 c2`, `2,505.6 pre8K`)** under UV+OC (**3.15× speedup**).
  - `t-fast-agent` (`Qwen3-8B`, `16K, p=4`): **`127.35 c1 / 323.01 c4`** (alt `q8_0` co-resides with `t-autocomplete-tab` `17.8 ms` FIM + `text-embedding-t-embed-index` in **`10.05 GiB`** VRAM).
  - `t-math-physics` (`Phi-4 14B`): solved `phi3` CUDA `v=q8_0` crash via **split KV (`K=q8_0:V=f16`)** -> `63.42 c1 / 118.41 c2`.
- **`portatil` (`24 GiB Intel Arc 140V`):**
  - Eliminated `100–125s` slot-0 checkpoint stalls (`flash_attention=true`, `ckpt=0` on `p-agent-moe-alt`): **12×–26× faster cold prefill** (`11–18` -> `134–495 tok/s`), `30B-A3B MoE` at `26–27 tok/s c1 / 37–39 c4` with `3/3` long-context needle recall.

---

## Agentic Stack (Antigravity · VS Code · OpenCode · Claude Code)

| Component | Path | Purpose |
|---|---|---|
| **Skills (3)** | `skills/{hw-node-profiler,llm-node-finetune,cluster-install-migrate}` | Phase 1 hardware/BIOS/driver profiling, Phase 2 per-node LLM fine-tuning, and cross-OS migration |
| **5-Protocol Cluster Gateway & SDK Helper** | `tools/gateway/{cluster_gateway.py,gateway_ctl.py,cluster_chat.py,agy_local_agent.py}` | Exposes the unified LM Link pool (`127.0.0.1:1235`) across LM Studio `/api/v1/*`, OpenAI `/v1/*`, Responses, and Anthropic `/v1/messages` with `cluster/*` failover, anti-thrash routing, NVIDIA NIM `nim/*` cloud proxying + native-chat bridge, and the Antigravity SDK runner |
| **Client Provisioner & Ollama Audit** | `scripts/{setup_clients.py,cleanup_ollama.py}` (`--serving`) | Configures OpenCode, VS Code Copilot BYOK, Continue, LM Studio `mcp.json`, Windows/WSL shell helpers (`cluster-agy`), and detects/removes leftover Windows/WSL `:11434` Ollama daemons |
| **Stdio MCP Server (15 tools, 3 resources)** | `mcp/cluster_mcp_server.py` (`cluster-hw-llm`) | `hw_node_profile` · `hw_gpu_telemetry` · `hw_event_log_check` · `llm_compute_ceiling` · `node_llm_status` · `node_best_configs` · `node_llm_control` · `cluster_models` · `cluster_chat` · `cluster_orchestrate` · `peer_status` · `peer_send_order` · `peer_order_status` · `peer_exec` · `openclaw_control` |
| **Bidirectional Peer Bridge & OpenClaw HA** | `tools/peer/{peer_daemon.py,peer_ctl.py,openclaw_ctl.py}` | Tailscale `:1236` + Google Drive offline queue for `torre` ↔ `portatil` orders (`WSL`/`Windows`/Crossed) + WSL OpenClaw Telegram HA leader control |
| **Antigravity Plugin, Agents & Hooks** | `plugins/cluster-hw-llm-optimization/` | 5 delegation agents (`cluster-coder`, `cluster-fast-agent`, `cluster-reasoning`, `cluster-vision`, `cluster-orchestrator`), `PreToolUse` default-config & LM Link safety guard + `SessionStart` node context |
| **Universal Rules** | `AGENTS.md`, `GEMINI.md`, `.github/copilot-instructions.md` | Cross-agent hardware & LLM benchmarking guardrails |

---

## Quick Start & Migration (Any OS × Client Combination)

### Ubuntu WSL / Linux (auto-bridges to Windows + configures Gateway & Clients with `--serving`)
```bash
bash install.sh --node <node_name> --os auto --targets agy,opencode,vscode --serving --doctor
```

### Windows PowerShell (auto-bridges to WSL + configures Gateway & Clients with `-Serving`)
```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Node <node_name> -Os auto -Targets agy,opencode,vscode -Serving -Doctor
```

### Specific Combinations (`--os windows|wsl|linux|all` × `--targets agy,vscode,opencode`)
```bash
python3 scripts/install.py --os windows,wsl --targets agy,vscode,opencode --serving --doctor
```

### Self-Test & Privacy Gate
```bash
python3 scripts/doctor.py --all --check-privacy
python3 scripts/doctor.py --all --check-privacy --live
```

See [docs/PEER_AND_OPENCLAW.md](docs/PEER_AND_OPENCLAW.md), [docs/AGENT_COLLABORATION.md](docs/AGENT_COLLABORATION.md), [docs/CLIENT_SETUP.md](docs/CLIENT_SETUP.md), [docs/MIGRATION.md](docs/MIGRATION.md), [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), and [PROMPT.md](PROMPT.md) for PC ↔ laptop order bridging, OpenClaw Telegram HA, local agent collaboration, unified `cluster/*` clients, and copy-ready tuning prompts.

## License
MIT ([LICENSE](LICENSE)) — retains upstream MIT notice for `sybil-solutions/inference-optimisation-skill`.
