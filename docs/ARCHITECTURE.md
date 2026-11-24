# Architecture: Hardware Profiler + Per-Node LLM Fine-Tuner

```text
+-------------------------------------------------------------------------+
|                    AI Coding & Optimization Clients                     |
|  Windows: Antigravity IDE, VS Code Copilot                              |
|  Ubuntu WSL / Linux: Antigravity, VS Code Remote, OpenCode, Claude Code |
+--------------------+--------------------------+-------------------------+
                     |                          |
         3 Skills / Rules / Hooks         Stdio MCP Server: cluster-hw-llm
       (AGENTS.md, skills/, hooks/)       (15 Tools · 6 Resources · v1.4.0)
                     |                          |
                     +------------+-------------+
                                  |
     +----------------------------+----------------------------+
     |                                                         |
     v                                                         v
+----------------------------------------+   +-----------------------------------------+
| PHASE 1: Hardware, BIOS & Driver       |   | PHASE 2: Per-Node LLM Serving           |
|          Profiler (tools/hw/)          |   |          Fine-Tuner (tools/llm/)        |
| - hardware_probe.py (+ --hwinfo JSON)  |   | - ceiling.py (fixed linear cN math)     |
| - bw_test.py (CUDA nvcuda.dll D2D BW)  |-->| - lmctl_bench.py (safe A/B overrides)   |
| - gpu_oc_verify.py (V/F + prefill)     |   | - bench.py + queue.sh + mem_*.sh        |
| - cpu_co_stress.py + stress_suite.py   |   | - run_gates.py (needle/FIM/equivalence) |
+----------------------------------------+   +-----------------------------------------+
```

## Design Pillars

1. **Hardware First, Then Model Parameters (`Phase 1 -> Phase 2`):**
   - Tuning inference parameters on a machine with outdated chipset drivers, an unflattened GPU V/F curve (`nvlddmkm 153`), or an unstable CPU Curve Optimizer core (`WHEA 18`) produces noisy benchmarks and random crashes.
   - `tools/hw/hardware_probe.py` inventories any cluster device's CPU, motherboard, BIOS, RAM topology, discrete or unified GPUs, chipset/platform drivers, power plan (`MinCPU`/`ASPM`), virtualization state, and stability logs before Phase 2 begins.

2. **Device-Agnostic Cluster Node Model (`cluster.local.json`):**
   - Any cluster node name (`torre`, `portatil`, or new devices) can be defined in gitignored `cluster.local.json` with its endpoint, hardware caps, and optional LM Link `model_prefixes`.

3. **Permanent Default Protection & Prefix Safety:**
   - `tools/cluster_ctl.py load` inherits persisted defaults (`lms load <key> -y` locally or clean REST remotely).
   - `tools/llm/lmctl_bench.py` snapshots existing permanent LM Studio defaults before temporary A/B overrides and restores them automatically on unload.
   - All unloads respect node `model_prefixes` so benchmarking one node never unloads a sibling LM Link node.

4. **Public-Safe Portability & Token Security:**
   - Tracked files contain **zero** personal hostnames, IPs, usernames, or hardware serials (`python3 scripts/doctor.py --all --check-privacy`).
   - `tools/catalog.py` is the single source of truth for the 13-model catalog (`cluster/*`, `nim/*`, `gemini/*`).
   - `:1235` Cluster Gateway enforces auto-generated `gateway.token` (`Bearer` / `X-Api-Key`) on all POST routes (`/v1/*`, `/api/v1/*`), omits wildcard CORS headers, and restricts `--mode auto --dangerously-skip-permissions` strictly to `gemini/orchestrator` (`gemini/pro` and `gemini/flash` default to read-only `--mode plan`).
   - `:1236` Peer Bridge keeps `peer.token` exclusively in `0600` gitignored `cluster.local.json` (never stored in Google Drive); Google Drive carries only HMAC-SHA256 signed offline queues (`peer-queue/`) and HA lease mirrors (`openclaw-ha/leader.json`).
