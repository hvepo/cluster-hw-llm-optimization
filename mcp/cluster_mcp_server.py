#!/usr/bin/env python3
"""Zero-dependency Python 3 Stdio MCP server for cluster-hw-llm-optimization.

Focused on:
1. Hardware, BIOS, Driver, and Stability inspection of any cluster device.
2. Per-node LLM ceiling computation, benchmark inspection, and default-safe load/unload for tuning.
3. Listing and delegating prompts to unified `cluster/*` role aliases via the Cluster Router Gateway (:1235).
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TOOLS_DIR = os.path.join(REPO_ROOT, "tools")
sys.path.insert(0, TOOLS_DIR)
sys.path.insert(0, os.path.join(TOOLS_DIR, "llm"))
sys.path.insert(0, os.path.join(TOOLS_DIR, "hw"))
sys.path.insert(0, os.path.join(TOOLS_DIR, "peer"))

import ceiling  # noqa: E402
import cluster_ctl  # noqa: E402
import cluster_lib  # noqa: E402
import gpu_oc_verify  # noqa: E402
import hardware_probe  # noqa: E402
import peer_ctl  # noqa: E402

SERVER_INFO = {"name": "cluster-hw-llm", "version": "1.2.0"}

TOOLS = [
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
        "description": "List unified cluster/* virtual role aliases on the Cluster Router Gateway (:1235), their live resolved model & node (torre vs portatil), context length, loaded status, and active failover/anti-thrash routing.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "cluster_chat",
        "description": "Delegate a prompt or follow-up turn to a local cluster/* model alias (e.g. cluster/code, cluster/fast-agent, cluster/reasoning, cluster/vision-heavy) via the Cluster Router Gateway /api/v1/chat endpoint.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "User prompt to send to the selected cluster model."},
                "model": {"type": "string", "default": "cluster/code", "description": "Virtual alias (cluster/*) or concrete t-*/p-* model ID (default: cluster/code)."},
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
        "name": "peer_status",
        "description": "Check live reachability of the local and sibling Peer Order Daemons (:1236), agy CLI availability, offline Drive queue counts, and OpenClaw HA leader status.",
        "inputSchema": {
            "type": "object",
            "properties": {"node": {"type": "string", "description": "Optional node filter (e.g. 'torre' or 'portatil')."}},
            "additionalProperties": False,
        },
    },
    {
        "name": "peer_send_order",
        "description": "Send an Antigravity order (agy -p) bidirectionally to torre or portatil across WSL, Windows, or auto via Tailscale :1236 (with automatic Google Drive offline queue fallback).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Target cluster node ('torre' or 'portatil')."},
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
                "node": {"type": "string", "description": "Target node where the job ran ('torre' or 'portatil')."},
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "peer_exec",
        "description": "Run a shell (WSL/Linux bash) or PowerShell (Windows) command on torre or portatil via Tailscale :1236 (with offline queue fallback).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Target node ('torre' or 'portatil')."},
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
        "description": "Inspect, start, stop, cleanly transfer Active-Standby leadership (takeover without Telegram 409 Conflict), or send a Telegram reply via OpenClaw.",
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

RESOURCES = [
    {"uri": "cluster://best-config", "name": "Node Best LLM Configs", "mimeType": "application/json", "description": "Verified tuned model configurations and benchmarks across cluster nodes."},
    {"uri": "cluster://benchmarks", "name": "Hardware & LLM Benchmarks", "mimeType": "text/markdown", "description": "Measured before/after LLM hill-climb, GPU UV/OC, and stability results."},
    {"uri": "cluster://architecture", "name": "Hardware Profiler & LLM Fine-Tuner Architecture", "mimeType": "text/markdown", "description": "Two-phase hardware profiling + per-node LLM fine-tuning design."},
]


def check_events_hours(hours=24):
    ps = (
        f"$t = (Get-Date).AddHours(-{int(hours)}); "
        "$ev = @(Get-WinEvent -FilterHashtable @{LogName='System'; StartTime=$t} -ErrorAction SilentlyContinue | "
        "Where-Object { ($_.ProviderName -eq 'Microsoft-Windows-WHEA-Logger' -and ($_.Id -eq 18 -or $_.Id -eq 19)) -or "
        "($_.ProviderName -match 'nvlddmkm|igdkmdn|amdkmdag|Display|BugCheck') -or ($_.ProviderName -eq 'Microsoft-Windows-Kernel-Power' -and $_.Id -eq 41) }); "
        "$items = $ev | Select-Object @{n='time';e={$_.TimeCreated.ToString('o')}}, Id, LevelDisplayName, ProviderName, Message; "
        "@{count=$ev.Count; events=@($items)} | ConvertTo-Json -Depth 4 -Compress; exit 0"
    )
    ps_bin = cluster_lib.resolve_host_binary("powershell.exe")
    r = subprocess.run([ps_bin, "-NoProfile", "-Command", ps], stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=30)
    if r.returncode != 0 or not r.stdout.strip():
        return {"error": (r.stderr or r.stdout).strip()}
    return json.loads(r.stdout.strip().splitlines()[-1])


def _gateway_base_url(cfg):
    env_url = os.environ.get("CLUSTER_GATEWAY_URL")
    if env_url:
        return env_url.rstrip("/")
    gw = cfg.get("gateway") or {}
    host = gw.get("host") or "127.0.0.1"
    port = int(gw.get("port") or 1235)
    return f"http://{host}:{port}"


def _extract_output_text(output_items, target_type):
    parts = []
    for item in output_items or []:
        if not isinstance(item, dict) or item.get("type") != target_type:
            continue
        content = item.get("content")
        if isinstance(content, str) and content:
            parts.append(content)
        elif isinstance(content, list):
            for chunk in content:
                if isinstance(chunk, dict):
                    txt = chunk.get("text") or chunk.get("content") or ""
                    if txt:
                        parts.append(txt)
                elif isinstance(chunk, str) and chunk:
                    parts.append(chunk)
    return "".join(parts)


def _call_cluster_models(cfg):
    gw_base = _gateway_base_url(cfg)
    try:
        req = urllib.request.Request(gw_base + "/cluster/status", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        return {
            "ok": False,
            "gateway_url": gw_base,
            "error": f"HTTP {exc.code}: {body or exc.reason}",
            "hint": "Start the Cluster Router Gateway with: python3 tools/gateway/gateway_ctl.py start",
        }
    except Exception as exc:
        return {
            "ok": False,
            "gateway_url": gw_base,
            "error": str(exc),
            "hint": "Start the Cluster Router Gateway with: python3 tools/gateway/gateway_ctl.py start",
        }


def _call_cluster_chat(cfg, args):
    gw_base = _gateway_base_url(cfg)
    model = args.get("model") or "cluster/code"
    prompt = args["prompt"]
    payload = {
        "model": model,
        "input": prompt,
        "stream": False,
        "store": args.get("store", True),
    }
    if args.get("system_prompt"):
        payload["system_prompt"] = args["system_prompt"]
    if args.get("reasoning"):
        payload["reasoning"] = args["reasoning"]
    if args.get("previous_response_id"):
        payload["previous_response_id"] = args["previous_response_id"]
    if args.get("integrations"):
        payload["integrations"] = [
            m if (not isinstance(m, str) or m.startswith(("mcp/", "ephemeral_mcp"))) else f"mcp/{m}"
            for m in args["integrations"]
        ]

    timeout_s = int(args.get("timeout_s") or 180)
    try:
        req = urllib.request.Request(
            gw_base + "/api/v1/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", "replace")
            data = json.loads(raw) if raw.strip() else {}
            resolved_model = resp.headers.get("X-Cluster-Resolved-Model") or data.get("model_instance_id") or model
            resolved_node = resp.headers.get("X-Cluster-Node") or cluster_lib.node_for_model(resolved_model, cfg) or ""
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        return {
            "ok": False,
            "gateway_url": gw_base,
            "model_requested": model,
            "error": f"HTTP {exc.code}: {body or exc.reason}",
            "hint": "Start the Cluster Router Gateway with: python3 tools/gateway/gateway_ctl.py start",
        }
    except Exception as exc:
        return {
            "ok": False,
            "gateway_url": gw_base,
            "model_requested": model,
            "error": str(exc),
            "hint": "Start the Cluster Router Gateway with: python3 tools/gateway/gateway_ctl.py start",
        }

    out_items = data.get("output") or []
    text = _extract_output_text(out_items, "message")
    reasoning_txt = _extract_output_text(out_items, "reasoning")
    result = {
        "ok": True,
        "model_requested": model,
        "resolved_model": resolved_model,
        "resolved_node": resolved_node,
        "response_id": data.get("response_id"),
        "text": text,
        "stats": data.get("stats") or {},
    }
    if data.get("model_instance_id"):
        result["model_instance_id"] = data["model_instance_id"]
    if reasoning_txt:
        result["reasoning"] = reasoning_txt
    return result


def handle_tool(name, args):
    cfg = cluster_lib.load_cluster_config(REPO_ROOT)
    if name == "hw_node_profile":
        return hardware_probe.collect_node_hardware_profile(
            node=args.get("node"),
            days=args.get("days", 14),
            hwinfo_path=args.get("hwinfo_path"),
        )
    if name == "hw_gpu_telemetry":
        ab = cluster_lib.resolve_afterburner_cfg(cfg)
        return {
            "gpu": hardware_probe.query_nvidia_gpu(),
            "afterburner_vf": gpu_oc_verify.decode_afterburner_vf(ab) if ab else None,
        }
    if name == "hw_event_log_check":
        if cluster_lib.detect_platform() not in ("windows", "wsl"):
            return {"note": "Windows Event Log only available on Windows/WSL hosts"}
        return check_events_hours(args.get("hours", 24))
    if name == "llm_compute_ceiling":
        params = args.get("params")
        if not params:
            prof = args.get("profile") or cfg.get("local_node", "torre")
            p = os.path.join(REPO_ROOT, "profiles", prof, f"ceiling-{prof}.json")
            params = json.load(open(p, encoding="utf-8"))
        return ceiling.compute_ceilings(params)
    if name == "node_llm_status":
        return cluster_ctl.collect_status(cfg, node_filter=args.get("node"))
    if name == "node_best_configs":
        bc = cluster_lib.load_best_config(REPO_ROOT)
        node = args.get("node")
        nodes = bc.get("nodes", {})
        return {n: nd for n, nd in nodes.items() if not node or n == node}
    if name == "node_llm_control":
        act = args["action"]
        if act == "load":
            if not args.get("model_key"):
                raise ValueError("model_key is required when action='load'")
            cluster_ctl.cmd_load(cfg, args["model_key"], node_override=args.get("node"))
            return {"ok": True, "action": "load", "loaded": args["model_key"]}
        unloaded = cluster_ctl.cmd_unload(cfg, node_override=args.get("node"), model_key=args.get("model_key"), quiet=True)
        return {"ok": True, "action": "unload", "unloaded": unloaded}
    if name == "cluster_models":
        return _call_cluster_models(cfg)
    if name == "cluster_chat":
        return _call_cluster_chat(cfg, args)
    if name == "peer_status":
        return peer_ctl.collect_status(cfg, node_filter=args.get("node"))
    if name == "peer_send_order":
        return peer_ctl.send_order(
            cfg,
            target_node=args["node"],
            prompt=args["prompt"],
            target_os=args.get("target_os") or "auto",
            mode=args.get("mode") or "auto",
            agent=args.get("agent"),
            model=args.get("model"),
            conversation_id=args.get("conversation_id"),
            cwd=args.get("cwd"),
            wait_s=args.get("wait_s", 0.0),
            queue_if_offline=args.get("queue_if_offline", True),
            reply_telegram=args.get("reply_telegram", False),
        )
    if name == "peer_order_status":
        return peer_ctl.get_job_status(cfg, args["job_id"], target_node=args.get("node"))
    if name == "peer_exec":
        return peer_ctl.exec_command(
            cfg,
            target_node=args["node"],
            command=args["command"],
            target_os=args.get("target_os") or "auto",
            cwd=args.get("cwd"),
            wait_s=args.get("wait_s", 30.0),
            timeout_s=args.get("timeout_s", 120),
            queue_if_offline=args.get("queue_if_offline", True),
        )
    if name == "openclaw_control":
        return peer_ctl.remote_openclaw_control(
            cfg,
            action=args["action"],
            target_node=args.get("node"),
            message=args.get("message"),
        )
    raise ValueError(f"Unknown tool {name!r}")


def read_resource(uri):
    mapping = {
        "cluster://best-config": ("application/json", os.path.join(REPO_ROOT, "profiles", "best-config.json")),
        "cluster://benchmarks": ("text/markdown", os.path.join(REPO_ROOT, "docs", "BENCHMARKS.md")),
        "cluster://architecture": ("text/markdown", os.path.join(REPO_ROOT, "docs", "ARCHITECTURE.md")),
    }
    if uri not in mapping:
        raise ValueError(f"Unknown resource URI {uri!r}")
    mime, path = mapping[uri]
    with open(path, "r", encoding="utf-8") as f:
        return {"contents": [{"uri": uri, "mimeType": mime, "text": f.read()}]}


def read_message(buf_in):
    line = buf_in.readline()
    if not line:
        return None, None
    s = line.decode("utf-8", "replace")
    if s.lower().startswith("content-length:"):
        length = int(s.split(":", 1)[1].strip())
        while True:
            hdr = buf_in.readline()
            if not hdr or hdr in (b"\r\n", b"\n"):
                break
        body = buf_in.read(length)
        return json.loads(body.decode("utf-8")), "lsp"
    while not s.strip():
        line = buf_in.readline()
        if not line:
            return None, None
        s = line.decode("utf-8", "replace")
    return json.loads(s), "ndjson"


def send_message(buf_out, obj, framing):
    raw = json.dumps(obj).encode("utf-8")
    if framing == "lsp":
        buf_out.write(f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii") + raw)
    else:
        buf_out.write(raw + b"\n")
    buf_out.flush()


def main():
    buf_in = sys.stdin.buffer
    buf_out = sys.stdout.buffer
    while True:
        msg, framing = read_message(buf_in)
        if msg is None:
            break
        req_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params") or {}
        if req_id is None:
            continue
        try:
            if method == "initialize":
                proto = params.get("protocolVersion") or "2024-11-05"
                result = {
                    "protocolVersion": proto,
                    "capabilities": {"tools": {}, "resources": {}},
                    "serverInfo": SERVER_INFO,
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                payload = handle_tool(params["name"], params.get("arguments") or {})
                result = {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}]}
            elif method == "resources/list":
                result = {"resources": RESOURCES}
            elif method == "resources/read":
                result = read_resource(params["uri"])
            elif method == "resources/templates/list":
                result = {"resourceTemplates": []}
            elif method == "prompts/list":
                result = {"prompts": []}
            else:
                send_message(buf_out, {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}, framing)
                continue
            send_message(buf_out, {"jsonrpc": "2.0", "id": req_id, "result": result}, framing)
        except Exception as exc:
            send_message(
                buf_out,
                {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": str(exc)}},
                framing,
            )


if __name__ == "__main__":
    main()
