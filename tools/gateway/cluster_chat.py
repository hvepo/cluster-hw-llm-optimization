#!/usr/bin/env python3
"""Zero-dependency CLI client for LM Studio's native /api/v1/chat via the Cluster Router Gateway.

Supports stateful conversation chaining (`previous_response_id`), MCP `integrations`
(`mcp/browse`, `mcp/cluster-hw-llm`, etc.), and live SSE streaming with `tok/s` telemetry.

With `--orchestrate`, runs the NVIDIA NIM autonomous multi-turn tool-calling
orchestrator (`cluster_orchestrate`) instead of a plain chat turn.
"""
import argparse
import json
import os
import sys
import urllib.request

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, os.pardir))
for _d in (os.path.join(REPO_ROOT, "tools"), os.path.join(REPO_ROOT, "mcp")):
    if _d not in sys.path:
        sys.path.insert(0, _d)

import cluster_lib  # noqa: E402
import cluster_mcp_server  # noqa: E402
import nim_orchestrator  # noqa: E402


def stream_native_chat(
    gateway_url,
    model,
    prompt,
    system_prompt=None,
    integrations=None,
    reasoning=None,
    previous_response_id=None,
    store=True,
):
    payload = {
        "model": model,
        "input": prompt,
        "stream": True,
        "store": store,
    }
    if system_prompt:
        payload["system_prompt"] = system_prompt
    if integrations:
        payload["integrations"] = integrations
    if reasoning:
        payload["reasoning"] = reasoning
    if previous_response_id:
        payload["previous_response_id"] = previous_response_id

    req = urllib.request.Request(
        gateway_url.rstrip("/") + "/api/v1/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    resolved_model = model
    resolved_node = ""
    response_id = None
    stats = {}
    chunks = []
    event_name = None

    with urllib.request.urlopen(req, timeout=300) as resp:
        resolved_model = resp.headers.get("X-Cluster-Resolved-Model") or model
        resolved_node = resp.headers.get("X-Cluster-Node") or ""
        for raw_line in resp:
            line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith("event:"):
                event_name = line[6:].strip()
                continue
            if not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if not data_str or data_str == "[DONE]":
                continue
            ev = json.loads(data_str)
            ev_type = ev.get("type") or event_name
            if ev_type in ("message.delta", "reasoning.delta"):
                piece = ev.get("content") or ""
                if piece:
                    sys.stdout.write(piece)
                    sys.stdout.flush()
                    if ev_type == "message.delta":
                        chunks.append(piece)
            elif ev_type == "chat.end":
                res = ev.get("result") or {}
                response_id = res.get("response_id") or ev.get("response_id")
                stats = res.get("stats") or {}
    sys.stdout.write("\n")
    sys.stdout.flush()
    return {
        "text": "".join(chunks),
        "resolved_model": resolved_model,
        "resolved_node": resolved_node,
        "response_id": response_id,
        "stats": stats,
    }


def _run_orchestrate_turn(ap, args):
    """Run the NIM multi-turn tool-calling orchestrator loop (see nim_orchestrator.run_orchestrator)."""
    if not args.prompt:
        ap.error("Provide a prompt when using --orchestrate")
    cluster_lib.load_cluster_config(REPO_ROOT)

    def _exec(name, targs):
        if name == "cluster_orchestrate":
            return {"ok": False, "error": "cluster_orchestrate is not available to the orchestrator itself (recursion guard)"}
        return cluster_mcp_server.handle_tool(name, targs)

    res = nim_orchestrator.run_orchestrator(
        prompt=args.prompt,
        model=args.model,
        max_steps=args.max_steps,
        tool_executor=_exec,
    )
    for step in res.get("tool_trace") or []:
        print(f"[orch] step {step['step']}: {step['tool']}({json.dumps(step.get('args') or {})[:160]})", file=sys.stderr)
    if res.get("text"):
        print(res["text"])
    print(f"[orchestrate model={res.get('orchestrator_model')} resolved={res.get('resolved_model')} steps={res.get('steps_executed')}]", file=sys.stderr)
    if not res.get("ok"):
        print(res.get("error") or "orchestration failed", file=sys.stderr)
        return 1
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Native LM Studio /api/v1/chat client via Cluster Router Gateway")
    ap.add_argument("prompt", nargs="?", help="User prompt (optional when --stateful interactive loop is used)")
    ap.add_argument("--model", default=None, help="Model or cluster*/nim*/gemini* alias (default: cluster/code; nim/ultra or gemini/orchestrator with --orchestrate)")
    ap.add_argument("--gateway", default="http://127.0.0.1:1235", help="Gateway base URL")
    ap.add_argument("--system", dest="system_prompt", help="Optional system prompt")
    ap.add_argument("--mcp", action="append", default=[], help="MCP server name (e.g. browse, cluster-hw-llm) or mcp/<name>")
    ap.add_argument("--reasoning", choices=("off", "low", "medium", "high", "on"), help="Reasoning mode")
    ap.add_argument("--previous-response-id", help="Resume stateful conversation from response_id")
    ap.add_argument("--stateful", action="store_true", help="Keep interactive stateful chat loop chaining previous_response_id")
    ap.add_argument("--orchestrate", action="store_true", help="Run as an autonomous cloud orchestrator (via nim/ultra, nim/fast, or gemini/orchestrator)")
    ap.add_argument("--max-steps", type=int, default=nim_orchestrator.ORCH_DEFAULT_MAX_STEPS, help="Max tool executions in --orchestrate mode (default: 6)")
    args = ap.parse_args(argv)

    if args.model is None:
        args.model = "nim/ultra" if args.orchestrate else "cluster/code"
    if args.orchestrate:
        return _run_orchestrate_turn(ap, args)

    integrations = [m if m.startswith(("mcp/", "ephemeral_mcp")) else f"mcp/{m}" for m in args.mcp]
    prev_id = args.previous_response_id

    def run_turn(text):
        nonlocal prev_id
        res = stream_native_chat(
            gateway_url=args.gateway,
            model=args.model,
            prompt=text,
            system_prompt=args.system_prompt,
            integrations=integrations or None,
            reasoning=args.reasoning,
            previous_response_id=prev_id,
            store=True,
        )
        prev_id = res.get("response_id") or prev_id
        st = res.get("stats") or {}
        tps = st.get("tokens_per_second")
        ttft = st.get("time_to_first_token_seconds")
        meta = f"[model={res['resolved_model']} node={res['resolved_node']}"
        if tps is not None:
            meta += f" {tps:.1f} tok/s"
        if ttft is not None:
            meta += f" ttft={ttft:.2f}s"
        if prev_id:
            meta += f" response_id={prev_id}"
        meta += "]"
        print(meta, file=sys.stderr)

    if args.prompt:
        run_turn(args.prompt)
    if args.stateful:
        print(f"Stateful /api/v1/chat session on {args.model} (Ctrl+D or 'exit' to quit)", file=sys.stderr)
        while True:
            try:
                line = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line or line.lower() in ("exit", "quit"):
                break
            run_turn(line)
    elif not args.prompt:
        ap.error("Provide a prompt or --stateful")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
