#!/usr/bin/env python3
"""Official Antigravity Python SDK local runner backed by the Cluster Router Gateway (:1235).

Uses `google-antigravity`'s `LocalOpenAIAgentConfig` against http://127.0.0.1:1235/v1 so
local Antigravity SDK agents automatically inherit dual-node LM Link failover (`torre` <-> `portatil`)
and all `cluster/*` role aliases (`cluster/code`, `cluster/fast-agent`, `cluster/code-heavy`, etc.).
"""
import argparse
import asyncio
import os
import sys


async def _run_sdk(args):
    from google.antigravity import Agent, LocalOpenAIAgentConfig  # type: ignore

    ws = os.path.abspath(args.workspace)
    kwargs = {
        "model": args.model,
        "base_url": args.gateway.rstrip("/") + "/v1",
        "workspaces": [ws],
    }
    if args.allow_edits:
        from google.antigravity.hooks import policy  # type: ignore

        kwargs["policies"] = [policy.allow_all()]

    cfg = LocalOpenAIAgentConfig(**kwargs)
    if not args.no_lightweight:
        cfg = cfg.lightweight()

    async with Agent(cfg) as agent:
        async def run_one(text):
            resp = await agent.chat(text)
            async for tok in resp:
                sys.stdout.write(tok)
                sys.stdout.flush()
            sys.stdout.write("\n")
            sys.stdout.flush()

        if args.prompt:
            await run_one(args.prompt)
        if args.interactive:
            print(f"Antigravity local SDK session ({args.model} @ {kwargs['base_url']}; 'exit' to quit)", file=sys.stderr)
            while True:
                try:
                    line = input("> ").strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not line or line.lower() in ("exit", "quit"):
                    break
                await run_one(line)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run an Antigravity Python SDK Agent against the Cluster Router Gateway")
    ap.add_argument("prompt", nargs="?", help="Prompt to run (optional when --interactive is set)")
    ap.add_argument("--model", default="cluster/code", help="Cluster alias or model ID (default: cluster/code)")
    ap.add_argument("--gateway", default=os.environ.get("CLUSTER_GATEWAY_URL") or "http://127.0.0.1:1235", help="Cluster Gateway base URL")
    ap.add_argument("--workspace", default=".", help="Workspace directory granted to the local agent (default: .)")
    ap.add_argument("--allow-edits", action="store_true", help="Attach policy.allow_all() so the local agent can edit files and run commands")
    ap.add_argument("--no-lightweight", action="store_true", help="Skip .lightweight() prompt/tool preset")
    ap.add_argument("--interactive", action="store_true", help="Keep interactive chat loop open")
    args = ap.parse_args(argv)

    if not args.prompt and not args.interactive:
        ap.error("Provide a prompt or --interactive")

    try:
        import google.antigravity  # type: ignore  # noqa: F401
    except ImportError:
        print(
            "Antigravity Python SDK (`google-antigravity`) is not installed.\n"
            "Install it with:\n"
            "  pip install google-antigravity\n"
            "Then re-run, or use Antigravity's built-in `cluster_chat` / `cluster_models` MCP tools directly.",
            file=sys.stderr,
        )
        return 2

    asyncio.run(_run_sdk(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
