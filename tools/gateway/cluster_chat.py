#!/usr/bin/env python3
"""Zero-dependency CLI client for LM Studio's native /api/v1/chat via the Cluster Router Gateway.

Supports stateful conversation chaining (`previous_response_id`), MCP `integrations`
(`mcp/browse`, `mcp/cluster-hw-llm`, etc.), and live SSE streaming with `tok/s` telemetry.
"""
import argparse
import json
import sys
import urllib.request


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


def main(argv=None):
    ap = argparse.ArgumentParser(description="Native LM Studio /api/v1/chat client via Cluster Router Gateway")
    ap.add_argument("prompt", nargs="?", help="User prompt (optional when --stateful interactive loop is used)")
    ap.add_argument("--model", default="cluster/code", help="Model or cluster/* alias (default: cluster/code)")
    ap.add_argument("--gateway", default="http://127.0.0.1:1235", help="Gateway base URL")
    ap.add_argument("--system", dest="system_prompt", help="Optional system prompt")
    ap.add_argument("--mcp", action="append", default=[], help="MCP server name (e.g. browse, cluster-hw-llm) or mcp/<name>")
    ap.add_argument("--reasoning", choices=("off", "low", "medium", "high", "on"), help="Reasoning mode")
    ap.add_argument("--previous-response-id", help="Resume stateful conversation from response_id")
    ap.add_argument("--stateful", action="store_true", help="Keep interactive stateful chat loop chaining previous_response_id")
    args = ap.parse_args(argv)

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
