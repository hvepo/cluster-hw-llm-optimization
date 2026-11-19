#!/usr/bin/env python3
"""Zero-dependency Python 3 Stdio MCP server for cluster-hw-llm-optimization.

Focused strictly on:
1. Hardware, BIOS, Driver, and Stability inspection of any cluster device.
2. Per-node LLM ceiling computation, benchmark inspection, and default-safe load/unload for tuning.
"""
import json
import os
import subprocess
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TOOLS_DIR = os.path.join(REPO_ROOT, "tools")
sys.path.insert(0, TOOLS_DIR)
sys.path.insert(0, os.path.join(TOOLS_DIR, "llm"))
sys.path.insert(0, os.path.join(TOOLS_DIR, "hw"))

import ceiling  # noqa: E402
import cluster_ctl  # noqa: E402
import cluster_lib  # noqa: E402
import gpu_oc_verify  # noqa: E402
import hardware_probe  # noqa: E402

SERVER_INFO = {"name": "cluster-hw-llm", "version": "1.1.0"}

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
    r = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps], stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=30)
    if r.returncode != 0 or not r.stdout.strip():
        return {"error": (r.stderr or r.stdout).strip()}
    return json.loads(r.stdout.strip().splitlines()[-1])


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
