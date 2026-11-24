#!/usr/bin/env python3
"""Zero-dependency Python 3 Stdio MCP server for cluster-hw-llm-optimization.

Focused on:
1. Hardware, BIOS, Driver, and Stability inspection of any cluster device.
2. Per-node LLM ceiling computation, benchmark inspection, and default-safe load/unload for tuning.
3. Listing and delegating prompts to the 13-model catalog (`cluster/*`, `nim/*`, `gemini/*`) via the Cluster Router Gateway (:1235).
4. Autonomous multi-turn tool-calling orchestration (`gemini/orchestrator`, `nim/ultra`, `nim/fast`) across torre, portatil, raspberrypi, and arduq.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TOOLS_DIR = os.path.join(REPO_ROOT, "tools")
sys.path.insert(0, TOOLS_DIR)
sys.path.insert(0, os.path.join(TOOLS_DIR, "llm"))
sys.path.insert(0, os.path.join(TOOLS_DIR, "hw"))
sys.path.insert(0, os.path.join(TOOLS_DIR, "peer"))

import catalog  # noqa: E402
import ceiling  # noqa: E402
import cluster_ctl  # noqa: E402
import cluster_lib  # noqa: E402
import gpu_oc_verify  # noqa: E402
import hardware_probe  # noqa: E402
import nim_orchestrator  # noqa: E402
import peer_ctl  # noqa: E402

SERVER_INFO = {"name": "cluster-hw-llm", "version": "1.4.0"}

TOOLS = catalog.CLUSTER_TOOL_SCHEMAS

RESOURCES = [
    {"uri": "cluster://best-config", "name": "Node Best LLM Configs", "mimeType": "application/json", "description": "Verified tuned model configurations and benchmarks across cluster nodes."},
    {"uri": "cluster://benchmarks", "name": "Hardware & LLM Benchmarks", "mimeType": "text/markdown", "description": "Measured before/after LLM hill-climb, GPU UV/OC, and stability results."},
    {"uri": "cluster://architecture", "name": "Hardware Profiler & LLM Fine-Tuner Architecture", "mimeType": "text/markdown", "description": "Two-phase hardware profiling + per-node LLM fine-tuning design."},
    {"uri": "cluster://peer-telegram", "name": "Bidirectional Peer Bridge & Telegram HA", "mimeType": "text/markdown", "description": "Cross-OS Tailscale :1236 / Drive queue bridge and Antigravity-first Telegram HA responder guide."},
    {"uri": "cluster://agent-collaboration", "name": "Cluster Subagent Collaboration Guide", "mimeType": "text/markdown", "description": "Patterns for delegating work across Antigravity and local cluster/* agents."},
    {"uri": "cluster://user-guide", "name": "Cluster Mode User Guide", "mimeType": "text/markdown", "description": "Operator handbook for the 13-model unified Cluster Mode, orchestrators per surface, and cloud vs local working modes."},
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
        hdrs = cluster_lib.gateway_auth_headers(cfg, repo_root=REPO_ROOT)
        req = urllib.request.Request(gw_base + "/cluster/status", headers=hdrs, method="GET")
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
        hdrs = {"Content-Type": "application/json"}
        hdrs.update(cluster_lib.gateway_auth_headers(cfg, repo_root=REPO_ROOT))
        req = urllib.request.Request(
            gw_base + "/api/v1/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers=hdrs,
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


def _orchestrator_executor(name, args):
    """Tool executor for the NIM orchestrator loop; recursion into cluster_orchestrate is blocked."""
    if name == "cluster_orchestrate":
        return {"ok": False, "error": "cluster_orchestrate is not available to the orchestrator itself (recursion guard)"}
    return handle_tool(name, args)


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
            with open(p, encoding="utf-8") as f:
                params = json.load(f)
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
    if name == "cluster_orchestrate":
        return nim_orchestrator.run_orchestrator(
            prompt=args["prompt"],
            model=args.get("model") or nim_orchestrator.ORCH_DEFAULT_MODEL,
            max_steps=args.get("max_steps") or nim_orchestrator.ORCH_DEFAULT_MAX_STEPS,
            tool_executor=_orchestrator_executor,
        )
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
        "cluster://peer-telegram": ("text/markdown", os.path.join(REPO_ROOT, "docs", "PEER_AND_OPENCLAW.md")),
        "cluster://agent-collaboration": ("text/markdown", os.path.join(REPO_ROOT, "docs", "AGENT_COLLABORATION.md")),
        "cluster://user-guide": ("text/markdown", os.path.join(REPO_ROOT, "docs", "USER_GUIDE.md")),
    }
    if uri not in mapping:
        raise ValueError(f"Unknown resource URI {uri!r}")
    mime, path = mapping[uri]
    with open(path, "r", encoding="utf-8") as f:
        return {"contents": [{"uri": uri, "mimeType": mime, "text": f.read()}]}


def read_message(buf_in):
    while True:
        line = buf_in.readline()
        if not line:
            return None, None
        s = line.decode("utf-8", "replace")
        if not s.strip():
            continue
        if ":" in s and not s.lstrip().startswith("{"):
            # LSP header block in arbitrary order (Content-Type, Content-Length, etc.)
            headers = {}
            cur_line = s
            while True:
                if not cur_line.strip():
                    break
                if ":" in cur_line:
                    k, v = cur_line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
                nxt = buf_in.readline()
                if not nxt:
                    return None, None
                cur_line = nxt.decode("utf-8", "replace")
            if "content-length" not in headers:
                continue
            try:
                length = int(headers["content-length"])
            except ValueError:
                continue
            if length <= 0:
                continue
            body = buf_in.read(length)
            if not body:
                return None, None
            try:
                return json.loads(body.decode("utf-8")), "lsp"
            except Exception as exc:
                return {"_parse_error": str(exc)}, "lsp"
        try:
            return json.loads(s), "ndjson"
        except Exception as exc:
            return {"_parse_error": str(exc)}, "ndjson"


def send_message(buf_out, obj, framing, write_lock=None):
    raw = json.dumps(obj).encode("utf-8")
    if write_lock is not None:
        with write_lock:
            _write_framed(buf_out, raw, framing)
    else:
        _write_framed(buf_out, raw, framing)


def _write_framed(buf_out, raw, framing):
    if framing == "lsp":
        buf_out.write(f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii") + raw)
    else:
        buf_out.write(raw + b"\n")
    buf_out.flush()


def _dispatch_request(msg, framing, buf_out, write_lock):
    if "_parse_error" in msg:
        send_message(
            buf_out,
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"Parse error: {msg['_parse_error']}"}},
            framing,
            write_lock=write_lock,
        )
        return
    req_id = msg.get("id")
    method = msg.get("method", "")
    params = msg.get("params") or {}
    if req_id is None:
        return
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
            try:
                payload = handle_tool(params["name"], params.get("arguments") or {})
                is_err = isinstance(payload, dict) and payload.get("ok") is False and bool(payload.get("error"))
                result = {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}]}
                if is_err:
                    result["isError"] = True
            except Exception as tool_exc:
                result = {
                    "content": [{"type": "text", "text": json.dumps({"ok": False, "error": str(tool_exc)}, indent=2)}],
                    "isError": True,
                }
        elif method == "resources/list":
            result = {"resources": RESOURCES}
        elif method == "resources/read":
            result = read_resource(params["uri"])
        elif method == "resources/templates/list":
            result = {"resourceTemplates": []}
        elif method == "prompts/list":
            result = {"prompts": []}
        else:
            send_message(
                buf_out,
                {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}},
                framing,
                write_lock=write_lock,
            )
            return
        send_message(buf_out, {"jsonrpc": "2.0", "id": req_id, "result": result}, framing, write_lock=write_lock)
    except Exception as exc:
        send_message(
            buf_out,
            {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": str(exc)}},
            framing,
            write_lock=write_lock,
        )


def main():
    buf_in = sys.stdin.buffer
    buf_out = sys.stdout.buffer
    write_lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="mcp-worker") as pool:
        futures = []
        while True:
            msg, framing = read_message(buf_in)
            if msg is None:
                break
            futures = [f for f in futures if not f.done()]
            futures.append(pool.submit(_dispatch_request, msg, framing, buf_out, write_lock))
        for f in futures:
            try:
                f.result()
            except Exception:
                pass


if __name__ == "__main__":
    main()
