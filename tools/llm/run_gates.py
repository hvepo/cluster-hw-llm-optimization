#!/usr/bin/env python3
"""Unified verification gates for Torre and Portátil (uses persisted defaults by default)."""
import argparse
import concurrent.futures
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.request

LLM_DIR = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.abspath(os.path.join(LLM_DIR, ".."))
sys.path.insert(0, LLM_DIR)
sys.path.insert(0, TOOLS_DIR)

import bench  # noqa: E402
from cluster_lib import api_request, get_api_models, load_cluster_config, node_url, resolve_lms_cli  # noqa: E402


def chat_stream(base_url, model, prompt, max_tokens=256, temperature=0.0, extra_body=None, seed=42):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "seed": seed,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if extra_body:
        body.update(extra_body)
    req = urllib.request.Request(
        base_url + "/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.monotonic()
    t_first = None
    chunks = []
    usage = {}
    with urllib.request.urlopen(req, timeout=420) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if payload == "[DONE]":
                break
            evt = json.loads(payload)
            if evt.get("usage"):
                usage = evt["usage"]
            choices = evt.get("choices") or []
            if choices:
                delta = choices[0].get("delta") or {}
                c = delta.get("content") or delta.get("reasoning_content") or ""
                if c:
                    if t_first is None:
                        t_first = time.monotonic()
                    chunks.append(c)
    t_end = time.monotonic()
    if t_first is None:
        t_first = t_end
    text = "".join(chunks)
    comp_toks = usage.get("completion_tokens") or 0
    prompt_toks = usage.get("prompt_tokens") or 0
    ttft = t_first - t0
    decode_tps = (comp_toks - 1) / (t_end - t_first) if comp_toks > 1 and (t_end - t_first) > 0 else 0.0
    return {
        "text": text,
        "ttft_ms": round(ttft * 1000, 1),
        "total_ms": round((t_end - t0) * 1000, 1),
        "decode_tps": round(decode_tps, 2),
        "prefill_tps": round(prompt_toks / ttft, 1) if prompt_toks and ttft > 0 else 0.0,
        "prompt_tokens": prompt_toks,
        "completion_tokens": comp_toks,
        "loop": bench.looping(text),
    }


def unload_node_prefix(cfg, node_name):
    url = node_url(node_name, cfg)
    prefixes = tuple(cfg["nodes"][node_name]["model_prefixes"])
    for m in get_api_models(url, timeout=10):
        k = m.get("key", "")
        if not k.startswith(prefixes):
            continue
        for inst in m.get("loaded_instances") or []:
            inst_id = inst.get("id") or k
            try:
                api_request(url, "POST", "/api/v1/models/unload", {"instance_id": inst_id}, timeout=60)
            except Exception:
                pass
    time.sleep(1.0)


def load_node_default(cfg, node_name, model_key):
    unload_node_prefix(cfg, node_name)
    url = node_url(node_name, cfg)
    if node_name == cfg.get("local_node", "torre"):
        lms = resolve_lms_cli(cfg, node=node_name)
        subprocess.run([lms, "load", model_key, "-y"], stdin=subprocess.DEVNULL, check=True, capture_output=True, text=True, timeout=180)
    else:
        api_request(url, "POST", "/api/v1/models/load", {"model": model_key, "echo_load_config": True}, timeout=240)
    return url


def run_node_gates(cfg, node_name):
    specs = {
        "torre": [
            ("t-vision-context", 16384, {"reasoning_effort": "none"}),
            ("t-code-chat", 8192, None),
            ("t-general-agent", 12288, {"reasoning_effort": "none"}),
            ("t-fast-agent", 16384, {"reasoning_effort": "none"}),
            ("t-math-physics", 8192, None),
            ("t-deep-reasoning", 8192, None),
        ],
        "portatil": [
            ("p-battery-chat", 16384, {"reasoning_effort": "none"}),
            ("p-agent-moe-alt", 16384, {"reasoning_effort": "none"}),
            ("p-code-moe", 16384, None),
            ("p-agent-moe", 32768, {"reasoning_effort": "none"}),
        ],
    }[node_name]

    out = {"node": node_name, "needle_gates": {}}
    url = node_url(node_name, cfg)

    auto_model = "t-autocomplete-tab" if node_name == "torre" else "p-autocomplete-tab"
    fim_prompts = [
        "<|fim_prefix|>def merge_intervals(intervals):\n    intervals.sort()\n    merged = [intervals[0]]\n    for s, e in intervals[1:]:\n        <|fim_suffix|>\n    return merged<|fim_middle|>",
        "<|fim_prefix|>class LRUCache:\n    def get(self, key: int) -> int:\n        <|fim_suffix|>\n        return self.cache[key]<|fim_middle|>",
    ]
    load_node_default(cfg, node_name, auto_model)
    chat_stream(url, auto_model, fim_prompts[0], 16, 0.0)
    ttfts, totals = [], []
    for i in range(6):
        r = chat_stream(url, auto_model, fim_prompts[i % 2], 32, 0.0)
        ttfts.append(r["ttft_ms"])
        totals.append(r["total_ms"])
    out["fim_autocomplete"] = {
        "model": auto_model,
        "median_ttft_ms": round(statistics.median(ttfts), 1),
        "median_total_32tok_ms": round(statistics.median(totals), 1),
    }
    unload_node_prefix(cfg, node_name)

    for model_key, ctx, eb in specs:
        load_node_default(cfg, node_name, model_key)
        m_res = {}
        try:
            for label, nominal in [("half_ctx", ctx // 2), ("full_ctx", ctx)]:
                prompt, codes = bench.document(nominal, seed=7)
                r = chat_stream(url, model_key, prompt, 256, 0.0, eb, seed=42)
                norm = r["text"].replace(" ", "")
                hits = sum(c in norm for c in codes)
                m_res[label] = {
                    "prompt_tokens": r["prompt_tokens"],
                    "prefill_tps": r["prefill_tps"],
                    "decode_tps": r["decode_tps"],
                    "retrieval": f"{hits}/{len(codes)}",
                    "loop": r["loop"],
                }
        finally:
            unload_node_prefix(cfg, node_name)
        out["needle_gates"][model_key] = m_res
        print(f"[{node_name}] {model_key}: {json.dumps(m_res)}", flush=True)
    return out


def main():
    ap = argparse.ArgumentParser(description="Run Cluster Verification Gates")
    ap.add_argument("--node", default="torre", help="Cluster node name or 'all'")
    ap.add_argument("--out", help="Optional JSON output path")
    args = ap.parse_args()
    cfg = load_cluster_config()

    targets = list(cfg.get("nodes", {}).keys()) if args.node in ("both", "all") else [args.node]
    all_res = {n: run_node_gates(cfg, n) for n in targets}
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(all_res, f, indent=2)
    print(json.dumps(all_res, indent=2))


if __name__ == "__main__":
    main()
