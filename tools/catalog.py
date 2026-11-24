#!/usr/bin/env python3
"""Single source of truth for the curated 13-model cluster catalog and power-user aliases."""

HEAVY_TORRE_MODELS = {
    "t-code-chat",
    "t-general-agent",
    "t-vision-context",
    "t-math-physics",
    "t-deep-reasoning",
}

NIM_BASE_URL = "https://integrate.api.nvidia.com"
NIM_HOST = "integrate.api.nvidia.com"
NIM_PORT = 443

DEFAULT_ALIAS_SPECS = {
    "cluster/code": {
        "candidates": ["p-code-moe", "t-code-chat"],
        "tools": True,
        "vision": False,
        "thinking": None,
        "description": "Smart-first coding (Laptop 30B-A3B MoE -> PC 14B fallback)",
    },
    "cluster/code-heavy": {
        "candidates": ["p-code-moe", "t-code-chat"],
        "tools": True,
        "vision": False,
        "thinking": None,
        "description": "Heavy multi-file coding & refactoring (Laptop 30B-A3B MoE -> PC 14B)",
    },
    "cluster/code-fast": {
        "candidates": ["t-code-chat", "p-code-moe"],
        "tools": True,
        "vision": False,
        "thinking": None,
        "description": "Low-latency interactive coding (PC 14B 72 tok/s -> Laptop 30B-A3B)",
    },
    "cluster/agent-heavy": {
        "candidates": ["p-agent-moe-alt", "t-general-agent"],
        "tools": True,
        "vision": False,
        "thinking": False,
        "description": "Deep autonomous multi-step agent (Laptop 30B-A3B -> PC Qwen3-14B)",
    },
    "cluster/fast-agent": {
        "candidates": ["t-fast-agent", "p-battery-chat"],
        "tools": True,
        "vision": False,
        "thinking": False,
        "description": "High-speed subagent & quick tasks (PC 8B 130 tok/s -> Laptop 4B)",
    },
    "cluster/vision": {
        "candidates": ["t-vision-context", "p-agent-moe"],
        "tools": True,
        "vision": True,
        "thinking": False,
        "description": "Fast multimodal & OCR (PC Gemma-4-12B+MTP4 -> Laptop 26B+MTP3)",
    },
    "cluster/vision-heavy": {
        "candidates": ["p-agent-moe", "t-vision-context"],
        "tools": True,
        "vision": True,
        "thinking": False,
        "description": "Long-context 32K multimodal & docs (Laptop 26B+MTP3 -> PC 12B+MTP4)",
    },
    "cluster/reasoning": {
        "candidates": ["t-deep-reasoning", "p-agent-moe-alt"],
        "tools": True,
        "vision": False,
        "thinking": True,
        "description": "Chain-of-thought reasoning (PC R1-14B -> Laptop 30B-A3B)",
    },
    "cluster/math": {
        "candidates": ["t-math-physics", "p-agent-moe-alt"],
        "tools": False,
        "vision": False,
        "thinking": None,
        "description": "Formal math & physics proofs (PC Phi-4 14B -> Laptop 30B-A3B)",
    },
    "cluster/autocomplete": {
        "candidates": ["t-autocomplete-tab", "p-autocomplete-tab"],
        "tools": False,
        "vision": False,
        "thinking": None,
        "fim": True,
        "local_first": True,
        "anti_thrash": True,
        "description": "IDE Tab FIM autocomplete (local-first; anti-thrash diverts when torre holds 12B-15B)",
    },
    "cluster/embed": {
        "candidates": ["text-embedding-t-embed-index", "text-embedding-p-embed-index"],
        "tools": False,
        "vision": False,
        "thinking": None,
        "embedding": True,
        "local_first": True,
        "anti_thrash": True,
        "description": "Codebase & RAG embeddings (local-first; anti-thrash diverts when torre holds 12B-15B)",
    },
}

CODE_PROFILES = {
    "smart-first": ["p-code-moe", "t-code-chat"],
    "fast-first": ["t-code-chat", "p-code-moe"],
}

NIM_ALIAS_SPECS = {
    "nim/ultra": {
        "candidates": ["nvidia/nemotron-3-ultra-550b-a55b"],
        "cloud": "nvidia-nim",
        "context_length": 131072,
        "max_output_tokens": 16384,
        "tools": True,
        "vision": False,
        "thinking": None,
        "orchestrator": True,
        "description": "NVIDIA NIM Flagship 550B-A55B MoE Cloud Orchestrator & Coder (0 local VRAM)",
    },
    "nim/fast": {
        "candidates": ["nvidia/nemotron-3.5-lightning-30b-a3b"],
        "cloud": "nvidia-nim",
        "context_length": 131072,
        "max_output_tokens": 8192,
        "tools": True,
        "vision": False,
        "thinking": False,
        "orchestrator": True,
        "description": "NVIDIA NIM Ultra-Low-Latency 30B-A3B Lightning Cloud Agent & Orchestrator",
    },
    "nim/vision": {
        "candidates": ["nvidia/nemotron-3.5-super-vl-120b-a12b"],
        "cloud": "nvidia-nim",
        "context_length": 131072,
        "max_output_tokens": 8192,
        "tools": True,
        "vision": True,
        "thinking": False,
        "description": "NVIDIA NIM 120B-A12B Super-VL Multimodal Vision-Language Cloud Resource",
    },
    "nim/reasoning": {
        "candidates": ["nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"],
        "cloud": "nvidia-nim",
        "context_length": 131072,
        "max_output_tokens": 8192,
        "tools": True,
        "vision": False,
        "thinking": True,
        "description": "NVIDIA NIM 30B-A3B Chain-of-Thought Cloud Reasoning Resource",
    },
    "nim/code": {
        "candidates": ["poolside/laguna-xs-2.1"],
        "cloud": "nvidia-nim",
        "context_length": 65536,
        "max_output_tokens": 8192,
        "tools": True,
        "vision": False,
        "thinking": None,
        "description": "NVIDIA NIM Poolside Laguna-XS 2.1 Cloud Code Generation Resource",
    },
    "nim/mistral-large": {
        "candidates": ["mistralai/mistral-large-4-1t-a52b"],
        "cloud": "nvidia-nim",
        "context_length": 131072,
        "max_output_tokens": 16384,
        "tools": True,
        "vision": False,
        "thinking": None,
        "description": "NVIDIA NIM Mistral Large 4 (1T-A52B MoE) Frontier Cloud Resource",
    },
    "nim/kimi": {
        "candidates": ["moonshotai/kimi-k3.1-pro"],
        "cloud": "nvidia-nim",
        "context_length": 131072,
        "max_output_tokens": 16384,
        "tools": True,
        "vision": False,
        "thinking": True,
        "description": "NVIDIA NIM Moonshot Kimi K3.1 Pro Long-Context Reasoning & Coding",
    },
    "nim/step": {
        "candidates": ["stepfun-ai/step-5-preview"],
        "cloud": "nvidia-nim",
        "context_length": 131072,
        "max_output_tokens": 16384,
        "tools": True,
        "vision": False,
        "thinking": True,
        "description": "NVIDIA NIM StepFun Step-5 Preview Frontier Reasoning Resource",
    },
    "nim/glm-flash": {
        "candidates": ["z-ai/glm-5.3-flash"],
        "cloud": "nvidia-nim",
        "context_length": 131072,
        "max_output_tokens": 8192,
        "tools": True,
        "vision": False,
        "thinking": False,
        "description": "NVIDIA NIM GLM-5.3 Flash High-Speed Coding & Tool-Calling Resource",
    },
    "nim/minimax-flash": {
        "candidates": ["minimaxai/minimax-m3.1-flash"],
        "cloud": "nvidia-nim",
        "context_length": 131072,
        "max_output_tokens": 8192,
        "tools": True,
        "vision": False,
        "thinking": False,
        "description": "NVIDIA NIM MiniMax M3.1 Flash High-Speed Long-Context Assistant",
    },
    "nim/gemma-31b": {
        "candidates": ["google/gemma-4-31b-it"],
        "cloud": "nvidia-nim",
        "context_length": 131072,
        "max_output_tokens": 8192,
        "tools": True,
        "vision": False,
        "thinking": False,
        "description": "NVIDIA NIM Google Gemma-4 31B Dense Instruction & Code Resource",
    },
    "nim/embed": {
        "candidates": ["nvidia/nemotron-3.5-embed-8b"],
        "cloud": "nvidia-nim",
        "context_length": 8192,
        "max_output_tokens": 0,
        "tools": False,
        "vision": False,
        "thinking": None,
        "embedding": True,
        "description": "NVIDIA NIM Nemotron-3.5 8B Cloud Semantic Embeddings",
    },
}

NIM_DIRECT_MODEL_IDS = {spec["candidates"][0]: alias for alias, spec in NIM_ALIAS_SPECS.items()}

GEMINI_ALIAS_SPECS = {
    "gemini/orchestrator": {
        "candidates": ["gemini-pro-orchestrator-neon"],
        "cloud": "gemini-pro",
        "context_length": 1000000,
        "max_output_tokens": 65536,
        "tools": True,
        "vision": False,
        "thinking": True,
        "orchestrator": True,
        "description": "Gemini Pro Flagship Autonomous Cloud Orchestrator (Antigravity subscription, 0 VRAM)",
    },
    "gemini/pro": {
        "candidates": ["gemini-4-neon-medium"],
        "cloud": "gemini-pro",
        "context_length": 1000000,
        "max_output_tokens": 65536,
        "tools": True,
        "vision": False,
        "thinking": True,
        "orchestrator": False,
        "description": "Gemini Pro Flagship Architect, Code & Deep Reasoning (Antigravity subscription, 0 VRAM)",
    },
    "gemini/flash": {
        "candidates": ["gemini-3.8-flash-medium"],
        "cloud": "gemini-pro",
        "context_length": 1000000,
        "max_output_tokens": 65536,
        "tools": True,
        "vision": False,
        "thinking": False,
        "orchestrator": False,
        "description": "Gemini Pro Fast Cloud Assistant & Edits (Antigravity subscription, 0 VRAM)",
    },
}

GEMINI_DIRECT_MODEL_IDS = {spec["candidates"][0]: alias for alias, spec in GEMINI_ALIAS_SPECS.items()}

# Curated 13-model client catalog (5 local cluster/* + 5 nim/* + 3 gemini/*)
CLIENT_MODELS = [
    {"id": "cluster/code", "name": "Cluster Code (30B MoE -> 14B)", "ctx": 16384, "out": 4096, "tools": True, "vision": False},
    {"id": "cluster/fast-agent", "name": "Cluster Fast Agent (8B 130 tok/s)", "ctx": 16384, "out": 4096, "tools": True, "vision": False},
    {"id": "cluster/vision-heavy", "name": "Cluster Vision Heavy (26B 32K)", "ctx": 32768, "out": 4096, "tools": True, "vision": True},
    {"id": "cluster/reasoning", "name": "Cluster Reasoning (R1-14B Think)", "ctx": 8192, "out": 4096, "tools": True, "vision": False},
    {"id": "cluster/math", "name": "Cluster Math (Phi-4 14B)", "ctx": 8192, "out": 4096, "tools": False, "vision": False},
]

NIM_CLIENT_MODELS = [
    {"id": "nim/ultra", "upstream_id": NIM_ALIAS_SPECS["nim/ultra"]["candidates"][0], "name": "NIM Ultra (550B Cloud Orchestrator)", "ctx": 131072, "out": 16384, "tools": True, "vision": False},
    {"id": "nim/fast", "upstream_id": NIM_ALIAS_SPECS["nim/fast"]["candidates"][0], "name": "NIM Fast (30B Cloud Lightning)", "ctx": 131072, "out": 8192, "tools": True, "vision": False},
    {"id": "nim/code", "upstream_id": NIM_ALIAS_SPECS["nim/code"]["candidates"][0], "name": "NIM Code (Laguna-XS Cloud Coder)", "ctx": 65536, "out": 8192, "tools": True, "vision": False},
    {"id": "nim/vision", "upstream_id": NIM_ALIAS_SPECS["nim/vision"]["candidates"][0], "name": "NIM Vision (120B Cloud VL)", "ctx": 131072, "out": 8192, "tools": True, "vision": True},
    {"id": "nim/reasoning", "upstream_id": NIM_ALIAS_SPECS["nim/reasoning"]["candidates"][0], "name": "NIM Reasoning (30B Cloud CoT)", "ctx": 131072, "out": 8192, "tools": True, "vision": False},
]

GEMINI_CLIENT_MODELS = [
    {"id": "gemini/orchestrator", "upstream_id": GEMINI_ALIAS_SPECS["gemini/orchestrator"]["candidates"][0], "name": "Gemini Pro Orchestrator (Autonomous Cloud)", "ctx": 1000000, "out": 65536, "tools": True, "vision": False},
    {"id": "gemini/pro", "upstream_id": GEMINI_ALIAS_SPECS["gemini/pro"]["candidates"][0], "name": "Gemini Pro (Flagship Cloud Architect)", "ctx": 1000000, "out": 65536, "tools": True, "vision": False},
    {"id": "gemini/flash", "upstream_id": GEMINI_ALIAS_SPECS["gemini/flash"]["candidates"][0], "name": "Gemini Flash (Fast Cloud Assistant)", "ctx": 1000000, "out": 65536, "tools": True, "vision": False},
]


def _build_continue_model_specs():
    specs = {}
    for m in CLIENT_MODELS:
        caps = []
        if m.get("tools"):
            caps.append("tool_use")
        if m.get("vision"):
            caps.append("image_input")
        roles = ["chat"] if m["id"] in ("cluster/vision-heavy", "cluster/reasoning", "cluster/math") else ["chat", "edit", "apply"]
        specs[m["id"]] = {"name": m["name"], "ctx": m["ctx"], "roles": roles, "capabilities": caps}
    for m in NIM_CLIENT_MODELS:
        caps = []
        if m.get("tools"):
            caps.append("tool_use")
        if m.get("vision"):
            caps.append("image_input")
        roles = ["chat"] if m["id"] in ("nim/vision", "nim/reasoning") else ["chat", "edit", "apply"]
        specs[m["id"]] = {"name": m["name"], "ctx": m["ctx"], "roles": roles, "capabilities": caps}
    for m in GEMINI_CLIENT_MODELS:
        specs[m["id"]] = {"name": m["name"], "ctx": m["ctx"], "roles": ["chat", "edit", "apply"], "capabilities": ["tool_use"]}
    specs["cluster/autocomplete"] = {
        "name": "Cluster Tab Autocomplete",
        "ctx": 4096,
        "roles": ["autocomplete"],
        "capabilities": [],
    }
    specs["cluster/embed"] = {
        "name": "Cluster Embeddings",
        "ctx": 2048,
        "roles": ["embed"],
        "capabilities": [],
    }
    return specs


CONTINUE_MODEL_SPECS = _build_continue_model_specs()
CURATED_ORDER = [
    "cluster/code",
    "cluster/fast-agent",
    "cluster/vision-heavy",
    "cluster/reasoning",
    "cluster/math",
    "nim/ultra",
    "nim/fast",
    "nim/code",
    "nim/vision",
    "nim/reasoning",
    "gemini/orchestrator",
    "gemini/pro",
    "gemini/flash",
    "cluster/autocomplete",
    "cluster/embed",
]


def openclaw_model_entries(include_nim=True, include_gemini=True):
    """Build OpenClaw provider model entries from the canonical catalog."""
    entries = [
        {
            "id": m["id"],
            "name": m["name"],
            "api": "openai-completions",
            "contextWindow": m["ctx"],
            "maxTokens": m["out"],
        }
        for m in CLIENT_MODELS
    ]
    # Also include cluster/code-fast for interactive low-latency Telegram fallback
    entries.insert(
        2,
        {
            "id": "cluster/code-fast",
            "name": "Cluster Code Fast (14B 72 tok/s)",
            "api": "openai-completions",
            "contextWindow": 12288,
            "maxTokens": 4096,
        },
    )
    if include_nim:
        for m in NIM_CLIENT_MODELS:
            entries.append(
                {
                    "id": m["id"],
                    "name": m["name"],
                    "api": "openai-completions",
                    "contextWindow": m["ctx"],
                    "maxTokens": m["out"],
                }
            )
    if include_gemini:
        for m in GEMINI_CLIENT_MODELS:
            entries.append(
                {
                    "id": m["id"],
                    "name": m["name"],
                    "api": "openai-completions",
                    "contextWindow": m["ctx"],
                    "maxTokens": m["out"],
                }
            )
    return entries


CLUSTER_TOOL_SCHEMAS = [
    {
        "name": "hw_node_profile",
        "description": "Inspect hardware configuration, motherboard/BIOS, RAM DIMMs, GPUs (NVIDIA/Intel Arc/AMD), chipset/platform drivers, power plan (MinCPU/ASPM), VBS/HVCI/HAGS, storage, Event Log stability, and optional HWiNFO64 JSON export for a cluster node.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Cluster node name from cluster.local.json (defaults to local_node)."},
                "days": {"type": "integer", "minimum": 1, "maximum": 90, "default": 14},
                "hwinfo_path": {"type": "string", "description": "Optional path to a HWiNFO64 JSON export file."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "hw_gpu_telemetry",
        "description": "Read-only GPU telemetry (nvidia-smi clocks/VRAM/power/temp/throttle/PCIe/BAR1 ReBAR plus active MSI Afterburner V/F curve flatness summary when available).",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "hw_event_log_check",
        "description": "Audit Windows System Event Log for WHEA-Logger 18/19 (with APIC IDs), nvlddmkm 153, igdkmdn, amdkmdag, Display, BugCheck, or Kernel-Power 41 events.",
        "inputSchema": {
            "type": "object",
            "properties": {"hours": {"type": "integer", "minimum": 1, "maximum": 336, "default": 24}},
            "additionalProperties": False,
        },
    },
    {
        "name": "llm_compute_ceiling",
        "description": "Compute theoretical decode (c1/cN), prefill, and KV-cache ceilings for a model on a target node's hardware using tools/llm/ceiling.py.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "params": {
                    "type": "object",
                    "description": "Hardware and model byte/FLOP parameters matching ceiling JSON schema (bandwidth_gbs, peak_tflops, dense_gb, lm_head_gb, active_params_b, ...).",
                },
                "profile": {
                    "type": "string",
                    "description": "Optional profile folder name under profiles/<profile>/ceiling-<profile>.json (e.g. 'torre' or 'portatil') when params is omitted.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "node_llm_status",
        "description": "Check live LM Studio / OpenAI server reachability, listed models, and currently loaded models on one or all configured cluster nodes.",
        "inputSchema": {
            "type": "object",
            "properties": {"node": {"type": "string", "description": "Optional node name filter from cluster.local.json."}},
            "additionalProperties": False,
        },
    },
    {
        "name": "node_best_configs",
        "description": "Read verified tuned per-model serving configurations and before/after benchmarks from profiles/best-config.json.",
        "inputSchema": {
            "type": "object",
            "properties": {"node": {"type": "string", "description": "Optional node name filter (e.g. 'torre' or 'portatil')."}},
            "additionalProperties": False,
        },
    },
    {
        "name": "node_llm_control",
        "description": "Safely load (default-inheriting) or unload (prefix-scoped) a model on a target cluster node for cold-load verification, benchmarking, or freeing VRAM.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["load", "unload"]},
                "node": {"type": "string", "description": "Target cluster node name."},
                "model_key": {"type": "string", "description": "Model identifier (required for load; optional filter for unload)."},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
    {
        "name": "cluster_models",
        "description": "List unified cluster/* (local), nim/* (NVIDIA NIM cloud), and gemini/* (Gemini Pro subscription) aliases on the Cluster Router Gateway (:1235), their live resolved model & node, context length, loaded status, and active failover/anti-thrash routing.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "cluster_chat",
        "description": "Delegate a prompt or follow-up turn to a local cluster/* alias, NVIDIA NIM nim/* alias, or Gemini Pro subscription gemini/* alias (e.g. cluster/code, nim/ultra, nim/fast, gemini/pro, gemini/flash, gemini/orchestrator) via the Cluster Router Gateway /api/v1/chat endpoint.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "User prompt to send to the selected cluster model."},
                "model": {"type": "string", "default": "cluster/code", "description": "Virtual alias (cluster/*, nim/*, or gemini/*) or concrete model id (default: cluster/code)."},
                "system_prompt": {"type": "string", "description": "Optional system instruction."},
                "reasoning": {"type": "string", "enum": ["off", "low", "medium", "high", "on"], "description": "Optional reasoning mode override."},
                "previous_response_id": {"type": "string", "description": "Optional response_id from a prior turn to continue stateful chat pinned to the same node."},
                "integrations": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional LM Studio MCP integrations (e.g. ['mcp/browse'] or ['browse']).",
                },
                "store": {"type": "boolean", "default": True, "description": "Whether LM Studio should store conversation state for previous_response_id chaining."},
                "timeout_s": {"type": "integer", "minimum": 10, "maximum": 600, "default": 180},
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "cluster_orchestrate",
        "description": "Autonomous multi-step cloud orchestrator (zero local VRAM) supporting both Gemini Pro subscription (`gemini/orchestrator`, full native Antigravity tools + web search + cluster MCP) and NVIDIA NIM (`nim/ultra`, `nim/fast`) across torre, portatil, raspberrypi, and arduq. Returns the orchestrator's final text plus step-by-step tool_trace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Goal the orchestrator plans and executes with the available tools."},
                "model": {"type": "string", "default": "nim/ultra", "description": "Cloud orchestrator model ('gemini/orchestrator', 'nim/ultra', 'nim/fast', 'gemini/pro', 'gemini/flash')."},
                "max_steps": {"type": "integer", "minimum": 1, "maximum": 12, "default": 6, "description": "Max tool executions before the orchestrator summarizes (for NIM loop)."},
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "peer_status",
        "description": "Check live reachability of the local and sibling Peer Order Daemons (:1236), agy CLI availability, offline Drive queue counts, and OpenClaw HA leader status.",
        "inputSchema": {
            "type": "object",
            "properties": {"node": {"type": "string", "description": "Optional node filter ('torre', 'portatil', 'raspberrypi', or 'arduq')."}},
            "additionalProperties": False,
        },
    },
    {
        "name": "peer_send_order",
        "description": "Send an Antigravity order (agy -p) bidirectionally to torre, portatil, raspberrypi, or arduq across WSL, Windows, or auto via Tailscale :1236 (with automatic Google Drive offline queue fallback).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Target cluster node ('torre', 'portatil', 'raspberrypi', or 'arduq')."},
                "prompt": {"type": "string", "description": "Task or order prompt to execute on the target machine."},
                "target_os": {"type": "string", "enum": ["auto", "wsl", "windows"], "default": "auto"},
                "mode": {"type": "string", "enum": ["auto", "accept-edits", "plan"], "default": "auto"},
                "agent": {"type": "string", "description": "Optional Antigravity agent name."},
                "model": {"type": "string", "description": "Optional model override."},
                "conversation_id": {"type": "string", "description": "Optional conversation ID to resume a multi-turn thread."},
                "cwd": {"type": "string", "description": "Optional working directory on the target node."},
                "wait_s": {"type": "number", "minimum": 0, "maximum": 600, "default": 0},
                "queue_if_offline": {"type": "boolean", "default": True},
                "reply_telegram": {"type": "boolean", "default": False},
            },
            "required": ["node", "prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "peer_order_status",
        "description": "Fetch status, output text, and conversation_id for a peer_send_order or peer_exec job_id (online HTTP or Google Drive completed queue).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "node": {"type": "string", "description": "Target node where the job ran ('torre', 'portatil', 'raspberrypi', or 'arduq')."},
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "peer_exec",
        "description": "Run a shell (WSL/Linux bash) or PowerShell (Windows) command on torre, portatil, raspberrypi, or arduq via Tailscale :1236 (with offline queue fallback).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Target node ('torre', 'portatil', 'raspberrypi', or 'arduq')."},
                "command": {"type": "string", "description": "Shell or PowerShell command to execute."},
                "target_os": {"type": "string", "enum": ["auto", "wsl", "windows"], "default": "auto"},
                "cwd": {"type": "string"},
                "wait_s": {"type": "number", "minimum": 0, "maximum": 300, "default": 30},
                "timeout_s": {"type": "integer", "minimum": 5, "maximum": 600, "default": 120},
                "queue_if_offline": {"type": "boolean", "default": True},
            },
            "required": ["node", "command"],
            "additionalProperties": False,
        },
    },
    {
        "name": "openclaw_control",
        "description": "Inspect, start, stop, cleanly transfer Active-Standby leadership (takeover without Telegram 409 Conflict), sync Telegram secrets, or send a Telegram reply via the Antigravity-first Telegram HA responder (or optional OpenClaw gateway).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["status", "start", "stop", "takeover", "telegram_reply", "sync_secrets"]},
                "node": {"type": "string", "description": "Optional node to run the action on (defaults to local_node)."},
                "message": {"type": "string", "description": "Message text when action='telegram_reply'."},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
]

ORCHESTRATOR_ALLOWED_TOOL_NAMES = (
    "peer_status",
    "peer_exec",
    "peer_send_order",
    "peer_order_status",
    "cluster_models",
    "cluster_chat",
    "hw_node_profile",
    "node_llm_status",
    "node_best_configs",
    "openclaw_control",
)


def orchestrator_openai_tools(names=ORCHESTRATOR_ALLOWED_TOOL_NAMES):
    """Build OpenAI function-calling tool specs directly from canonical CLUSTER_TOOL_SCHEMAS."""
    by_name = {t["name"]: t for t in CLUSTER_TOOL_SCHEMAS}
    out = []
    for n in names:
        t = by_name.get(n)
        if not t:
            continue
        out.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["inputSchema"],
            },
        })
    return out
