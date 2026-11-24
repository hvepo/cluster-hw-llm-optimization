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
import random
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import catalog  # noqa: E402
import cluster_lib  # noqa: E402

NIM_ORCH_HOST = catalog.NIM_HOST
NIM_ORCH_PORT = catalog.NIM_PORT
NIM_ORCH_PATH = "/v1/chat/completions"
ORCH_CALL_TIMEOUT_S = 300
ORCH_TOOL_RESULT_LIMIT = 6000
ORCH_DEFAULT_MODEL = "nim/ultra"
ORCH_DEFAULT_MAX_STEPS = 6

ORCH_SYSTEM_PROMPT = (
    "You are the Cluster Orchestrator across a 4-node hardware & AI mesh: "
    "torre (desktop RTX 3080 Ti), portatil (laptop Arc 140V), raspberrypi (ARM witness), and arduq (QRB2210+STM32 edge). "
    "Use the provided tools concisely to inspect live state, delegate to local cluster/* or peer nodes, and verify results before answering. "
    "Never invent outputs or skip tool verification when asked to inspect or act on the cluster."
)

ORCHESTRATOR_TOOLS = catalog.orchestrator_openai_tools()
ORCHESTRATOR_TOOL_NAMES = {t["function"]["name"] for t in ORCHESTRATOR_TOOLS}


def _nim_chat_completion(api_key, model, messages, tools=None, timeout_s=ORCH_CALL_TIMEOUT_S, max_tokens=8192):
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    raw = json.dumps(payload).encode("utf-8")
    max_attempts = 3
    last_exc = None
    for attempt in range(max_attempts):
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
            if resp.status == 429 or resp.status >= 500:
                last_exc = RuntimeError(f"NVIDIA NIM HTTP {resp.status}: {body[:500]}")
                if attempt + 1 < max_attempts:
                    time.sleep((1.0 * (2 ** attempt)) + random.uniform(0.0, 0.35))
                    continue
                raise last_exc
            if resp.status >= 400:
                raise RuntimeError(f"NVIDIA NIM HTTP {resp.status}: {body[:500]}")
            return json.loads(body)
        except (http.client.HTTPException, OSError, TimeoutError) as exc:
            last_exc = exc
            if attempt + 1 < max_attempts:
                time.sleep((1.0 * (2 ** attempt)) + random.uniform(0.0, 0.35))
                continue
            raise RuntimeError(f"NVIDIA NIM transport error: {exc}") from exc
        finally:
            conn.close()
    raise RuntimeError(str(last_exc or "NVIDIA NIM request failed"))


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
    spec = catalog.NIM_ALIAS_SPECS.get(model)
    if spec:
        return spec["candidates"][0]
    if model in catalog.NIM_DIRECT_MODEL_IDS:
        return model
    return model


GEMINI_FALLBACK_MAP = {
    alias: spec["candidates"][0] for alias, spec in catalog.GEMINI_ALIAS_SPECS.items()
}


def _resolve_gemini_model(model):
    """Map a `gemini/*` alias to the concrete Antigravity (`agy`) model id."""
    if model in GEMINI_FALLBACK_MAP:
        return GEMINI_FALLBACK_MAP[model]
    spec = catalog.GEMINI_ALIAS_SPECS.get(model)
    if spec:
        return spec["candidates"][0]
    if model in catalog.GEMINI_DIRECT_MODEL_IDS:
        return model
    if str(model).startswith("gemini/"):
        return str(model).split("/", 1)[1]
    return model


def _is_gemini_orchestrator_model(model):
    m = str(model or "")
    return m.startswith("gemini/") or m in GEMINI_FALLBACK_MAP.values()


def _run_gemini_orchestrator(prompt, alias, timeout_s=ORCH_CALL_TIMEOUT_S):
    """Execute a goal autonomously via the OAuth-authenticated Antigravity CLI (`agy -p`)."""
    agy_model = _resolve_gemini_model(alias)
    result = {
        "ok": False,
        "orchestrator_model": alias,
        "resolved_model": agy_model,
        "steps_executed": 0,
        "tool_trace": [],
        "text": "",
    }
    agy_cmd = cluster_lib.resolve_agy_cli_cmd()
    if not agy_cmd:
        result["error"] = "Gemini Pro (agy CLI) not configured or unavailable on this host."
        return result

    timeout_s = max(30, min(600, int(timeout_s or ORCH_CALL_TIMEOUT_S)))
    argv = list(agy_cmd) + [
        "-p",
        str(prompt or ""),
        "--model",
        agy_model,
        "--mode",
        "auto",
        "--dangerously-skip-permissions",
        "--disable-slash-commands",
        "--output-format",
        "stream-json",
        "--print-timeout",
        f"{timeout_s}s",
    ]
    work_cwd = None if os.name == "nt" else os.path.expanduser("~")
    try:
        r = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            cwd=work_cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout_s + 15,
        )
    except Exception as exc:
        result["error"] = f"Gemini Pro orchestrator invocation failed: {exc}"
        return result

    conv_id = ""
    result_obj = {}
    fallback_obj = {}
    trace = []
    text_parts = []
    for raw_line in (r.stdout or "").splitlines():
        s = raw_line.strip()
        if not s.startswith("{"):
            continue
        try:
            ev = json.loads(s)
        except Exception:
            continue
        if ev.get("conversation_id"):
            conv_id = str(ev["conversation_id"])
        ev_kind = ev.get("event")
        if ev_kind == "step_update":
            su = ev.get("step_update") or {}
            if su.get("conversation_id"):
                conv_id = str(su["conversation_id"])
            if su.get("text_delta"):
                text_parts.append(str(su["text_delta"]))
            tool_name = su.get("tool_name") or su.get("function_name") or (su.get("tool_call") or {}).get("name")
            if tool_name or su.get("step_type") in ("tool_call", "tool_result"):
                trace.append({
                    "step": len(trace) + 1,
                    "tool": str(tool_name or su.get("step_type") or "tool"),
                    "args": su.get("tool_args") or su.get("args") or {},
                    "result": _truncate(str(su.get("tool_result") or su.get("output") or su.get("state") or "")),
                })
        elif ev_kind == "result":
            result_obj = ev.get("result") or {}
            if result_obj.get("conversation_id"):
                conv_id = str(result_obj["conversation_id"])
        elif "status" in ev or "response" in ev or "error" in ev:
            fallback_obj = ev

    obj = result_obj or fallback_obj
    if not conv_id and obj.get("conversation_id"):
        conv_id = str(obj["conversation_id"])
    status = str(obj.get("status") or ("SUCCESS" if r.returncode == 0 else "ERROR"))
    num_turns = int(obj.get("num_turns") or max(1, len(trace)))
    resp_text = str(obj.get("response") or "".join(text_parts)).strip()

    if not trace:
        trace = [{
            "step": 1,
            "tool": "agy_native_runtime",
            "args": {"model": agy_model},
            "result": f"conversation_id={conv_id} status={status}",
        }]

    if r.returncode != 0 or status != "SUCCESS":
        err_msg = obj.get("error") or (r.stderr or r.stdout or f"agy exited {r.returncode}").strip()
        if trace and trace[-1]["tool"] == "agy_native_runtime":
            trace[-1]["result"] = f"status={status} error={err_msg[:300]}"
        result.update({
            "ok": False,
            "steps_executed": num_turns,
            "conversation_id": conv_id,
            "tool_trace": trace,
            "error": err_msg,
        })
        return result

    result.update({
        "ok": True,
        "steps_executed": num_turns,
        "conversation_id": conv_id,
        "tool_trace": trace,
        "text": resp_text,
    })
    return result


def run_orchestrator(prompt, model=ORCH_DEFAULT_MODEL, max_steps=ORCH_DEFAULT_MAX_STEPS,
                     tool_executor=None, timeout_s=ORCH_CALL_TIMEOUT_S):
    """Run the NIM multi-turn tool-calling loop or Gemini Pro autonomous `agy` runtime.

    `tool_executor(name, args)` must return a JSON-serializable dict (or raise).
    """
    alias = str(model or ORCH_DEFAULT_MODEL)
    if _is_gemini_orchestrator_model(alias):
        return _run_gemini_orchestrator(prompt, alias, timeout_s=timeout_s)
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
    messages = [
        {"role": "system", "content": ORCH_SYSTEM_PROMPT},
        {"role": "user", "content": str(prompt or "")},
    ]

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
            result["text"] = (msg.get("content") or msg.get("reasoning_content") or "").strip()
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
                continue
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
        obj = _nim_chat_completion(api_key, nim_model, messages, tools=None, timeout_s=timeout_s, max_tokens=4096)
        choices = obj.get("choices") or []
        msg = choices[0].get("message") or {} if choices else {}
        text = (msg.get("content") or msg.get("reasoning_content") or "").strip()
        resolved = obj.get("model") or resolved
    except Exception as exc:
        result.update(
            {
                "ok": False,
                "error": f"Final summary failed after step budget: {exc}",
                "steps_executed": executed,
                "tool_trace": trace,
                "resolved_model": resolved,
            }
        )
        return result

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
