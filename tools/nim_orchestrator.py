#!/usr/bin/env python3
"""Zero-dependency NVIDIA NIM autonomous tool-calling orchestrator loop.

Drives `nim/*` cloud models (e.g. nim/ultra, nim/fast) as multi-turn
tool-calling agents over the OpenAI-compatible /v1/chat/completions endpoint.
The caller supplies a `tool_executor(name, args)` callback (e.g. the MCP
server's handle_tool or a CLI dispatcher); the loop feeds tool results back
to the orchestrator until it produces a final text answer or the step budget
is exhausted.

Return contract (mirrors the `cluster_orchestrate` MCP tool):
    {"ok": bool, "orchestrator_model": str, "resolved_model": str,
     "steps_executed": int, "tool_trace": [{"step", "tool", "args", "result"}],
     "text": str, "error"?: str}
"""
import http.client
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cluster_lib  # noqa: E402

NIM_ORCH_HOST = "integrate.api.nvidia.com"
NIM_ORCH_PORT = 443
NIM_ORCH_PATH = "/v1/chat/completions"
ORCH_CALL_TIMEOUT_S = 300
ORCH_TOOL_RESULT_LIMIT = 6000
ORCH_DEFAULT_MODEL = "nim/ultra"
ORCH_DEFAULT_MAX_STEPS = 6


ORCHESTRATOR_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "peer_status",
            "description": "Check live reachability of the local and sibling Peer Order Daemons (:1236), agy CLI availability, offline Drive queue counts, and OpenClaw HA leader status.",
            "parameters": {
                "type": "object",
                "properties": {"node": {"type": "string", "description": "Optional node filter (e.g. 'torre' or 'portatil')."}},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "peer_exec",
            "description": "Run a shell (WSL/Linux bash) or PowerShell (Windows) command on torre or portatil via Tailscale :1236 (with offline queue fallback).",
            "parameters": {
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
    },
    {
        "type": "function",
        "function": {
            "name": "peer_send_order",
            "description": "Send an Antigravity order (agy -p) bidirectionally to torre or portatil across WSL, Windows, or auto via Tailscale :1236 (with automatic Google Drive offline queue fallback).",
            "parameters": {
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
    },
    {
        "type": "function",
        "function": {
            "name": "peer_order_status",
            "description": "Fetch status, output text, and conversation_id for a peer_send_order or peer_exec job_id (online HTTP or Google Drive completed queue).",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "node": {"type": "string", "description": "Target node where the job ran ('torre' or 'portatil')."},
                },
                "required": ["job_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cluster_models",
            "description": "List unified cluster/* (local) and nim/* (NVIDIA NIM cloud) aliases on the Cluster Router Gateway (:1235), their live resolved model & node (torre vs portatil), context length, loaded status, and active failover/anti-thrash routing.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cluster_chat",
            "description": "Delegate a prompt or follow-up turn to a local cluster/* model alias or a cloud nim/* alias (e.g. cluster/code, nim/ultra, nim/fast) via the Cluster Router Gateway /api/v1/chat endpoint. nim/* requires the NVIDIA NIM API key; store=true and previous_response_id work for both.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "User prompt to send to the selected model."},
                    "model": {"type": "string", "default": "cluster/code", "description": "Virtual alias (cluster/* or nim/*) or concrete t-*/p-*/nvidia/id (default: cluster/code)."},
                    "system_prompt": {"type": "string", "description": "Optional system instruction."},
                    "reasoning": {"type": "string", "enum": ["off", "low", "medium", "high", "on"], "description": "Optional reasoning mode override."},
                    "previous_response_id": {"type": "string", "description": "Optional response_id from a prior turn to continue stateful chat."},
                    "integrations": {"type": "array", "items": {"type": "string"}, "description": "Optional MCP integrations (e.g. ['mcp/browse'])."},
                    "store": {"type": "boolean", "default": True, "description": "Whether to store conversation state for previous_response_id chaining."},
                    "timeout_s": {"type": "integer", "minimum": 10, "maximum": 600, "default": 180},
                },
                "required": ["prompt"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hw_node_profile",
            "description": "Inspect hardware configuration, motherboard/BIOS, RAM DIMMs, GPUs (NVIDIA/Intel Arc/AMD), chipset/platform drivers, power plan (MinCPU/ASPM), VBS/HVCI/HAGS, storage, Event Log stability, and optional HWiNFO64 JSON export for a cluster node.",
            "parameters": {
                "type": "object",
                "properties": {
                    "node": {"type": "string", "description": "Cluster node name from cluster.local.json (defaults to local_node)."},
                    "days": {"type": "integer", "minimum": 1, "maximum": 90, "default": 14},
                    "hwinfo_path": {"type": "string", "description": "Optional path to a HWiNFO64 JSON export file."},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "node_llm_status",
            "description": "Check live LM Studio / OpenAI server reachability, listed models, and currently loaded models on one or all configured cluster nodes.",
            "parameters": {
                "type": "object",
                "properties": {"node": {"type": "string", "description": "Optional node name filter from cluster.local.json."}},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "node_best_configs",
            "description": "Read verified tuned per-model serving configurations and before/after benchmarks from profiles/best-config.json.",
            "parameters": {
                "type": "object",
                "properties": {"node": {"type": "string", "description": "Optional node name filter (e.g. 'torre' or 'portatil')."}},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "openclaw_control",
            "description": "Inspect, start, stop, cleanly transfer Active-Standby leadership (takeover without Telegram 409 Conflict), sync Telegram secrets, or send a Telegram reply via the Antigravity-first Telegram HA responder (or optional OpenClaw gateway).",
            "parameters": {
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
    },
]

ORCHESTRATOR_TOOL_NAMES = {t["function"]["name"] for t in ORCHESTRATOR_TOOLS}


def _nim_chat_completion(api_key, model, messages, tools=None, timeout_s=ORCH_CALL_TIMEOUT_S, max_tokens=8192):
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    raw = json.dumps(payload).encode("utf-8")
    conn = http.client.HTTPSConnection(NIM_ORCH_HOST, NIM_ORCH_PORT, timeout=timeout_s)
    try:
        conn.request(
            "POST",
            NIM_ORCH_PATH,
            body=raw,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Content-Length": str(len(raw)),
            },
        )
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", "replace")
    finally:
        conn.close()
    if resp.status >= 400:
        raise RuntimeError(f"NVIDIA NIM HTTP {resp.status}: {body[:500]}")
    return json.loads(body)


def _truncate(text, limit=ORCH_TOOL_RESULT_LIMIT):
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]"


def _normalize_tool_calls(msg):
    out = []
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        out.append(
            {
                "id": tc.get("id") or f"call_{i}",
                "type": tc.get("type") or "function",
                "function": {"name": fn.get("name") or "", "arguments": fn.get("arguments") or ""},
            }
        )
    return out


def _parse_tool_args(raw_args):
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str) and raw_args.strip():
        try:
            parsed = json.loads(raw_args)
            return parsed if isinstance(parsed, dict) else {"raw": parsed}
        except Exception:
            return {"raw": raw_args}
    return {}


def _resolve_nim_model(model):
    """Map a `nim/*` alias to the concrete NVIDIA model id (falls back to the input)."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "gateway"))
        import cluster_gateway

        spec = cluster_gateway.NIM_ALIAS_SPECS.get(model)
        if spec:
            return spec["candidates"][0]
        if model in cluster_gateway.NIM_DIRECT_MODEL_IDS:
            return model
    except Exception:
        pass
    return model


def run_orchestrator(prompt, model=ORCH_DEFAULT_MODEL, max_steps=ORCH_DEFAULT_MAX_STEPS,
                     tool_executor=None, timeout_s=ORCH_CALL_TIMEOUT_S):
    """Run the NIM multi-turn tool-calling loop.

    `tool_executor(name, args)` must return a JSON-serializable dict (or raise).
    """
    alias = str(model or ORCH_DEFAULT_MODEL)
    try:
        max_steps = int(max_steps)
    except (TypeError, ValueError):
        max_steps = ORCH_DEFAULT_MAX_STEPS
    if max_steps < 1:
        max_steps = ORCH_DEFAULT_MAX_STEPS

    result = {
        "ok": False,
        "orchestrator_model": alias,
        "resolved_model": alias,
        "steps_executed": 0,
        "tool_trace": [],
        "text": "",
    }

    api_key = cluster_lib.resolve_nvidia_api_key()
    if not api_key:
        result["error"] = "NVIDIA NIM API key not configured (~/.config/nvidia_api_key or NVIDIA_API_KEY)."
        return result

    nim_model = _resolve_nim_model(alias)
    trace = []
    executed = 0
    messages = [{"role": "user", "content": str(prompt or "")}]

    while executed < max_steps:
        try:
            obj = _nim_chat_completion(api_key, nim_model, messages, tools=ORCHESTRATOR_TOOLS, timeout_s=timeout_s)
        except Exception as exc:
            result["error"] = f"NVIDIA NIM orchestrator call failed: {exc}"
            result["steps_executed"] = executed
            result["tool_trace"] = trace
            return result
        resolved = obj.get("model") or nim_model

        choices = obj.get("choices") or []
        msg = choices[0].get("message") or {} if choices else {}
        tool_calls = _normalize_tool_calls(msg)
        if not tool_calls:
            result["ok"] = True
            result["text"] = (msg.get("content") or "").strip()
            result["steps_executed"] = executed
            result["tool_trace"] = trace
            result["resolved_model"] = resolved
            return result

        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": tool_calls})
        for tc in tool_calls:
            if executed >= max_steps:
                messages.append(
                    {"role": "tool", "tool_call_id": tc["id"], "content": "Step budget reached; provide your final summary."}
                )
                break
            name = tc["function"]["name"]
            targs = _parse_tool_args(tc["function"]["arguments"])
            if name not in ORCHESTRATOR_TOOL_NAMES:
                res = {
                    "ok": False,
                    "error": f"unknown tool for orchestrator: {name!r} (available: {sorted(ORCHESTRATOR_TOOL_NAMES)})",
                }
            elif tool_executor is None:
                res = {"ok": False, "error": "no tool executor provided for orchestrator"}
            else:
                try:
                    res = tool_executor(name, targs)
                except Exception as exc:
                    res = {"ok": False, "error": f"tool {name!r} raised: {exc}"}
            executed += 1
            trace.append({"step": executed, "tool": name, "args": targs, "result": _truncate(json.dumps(res, default=str))})
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": trace[-1]["result"]})

    # Budget exhausted while tools were still requested: ask for a final summary.
    text = ""
    try:
        obj = _nim_chat_completion(api_key, nim_model, messages, tools=None, max_tokens=4096)
        choices = obj.get("choices") or []
        msg = choices[0].get("message") or {} if choices else {}
        text = (msg.get("content") or "").strip()
        resolved = obj.get("model") or resolved
    except Exception:
        pass

    result.update(
        {
            "ok": True,
            "text": text,
            "steps_executed": executed,
            "tool_trace": trace,
            "resolved_model": resolved,
        }
    )
    return result


if __name__ == "__main__":
    _res = run_orchestrator("Say NIM_ORCH_SMOKE_OK and stop.", model=ORCH_DEFAULT_MODEL, max_steps=1)
    print(json.dumps(_res, indent=2)[:2000])
    raise SystemExit(0 if _res.get("ok") else 1)
