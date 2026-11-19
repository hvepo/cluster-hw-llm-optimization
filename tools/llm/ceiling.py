#!/usr/bin/env python3
"""Theoretical ceilings for decode / prefill / KV from a JSON of measured HW + model sizes.

Fixes the upstream n^2 cN bug in ceiling.py: aggregate multi-stream decode throughput scales as
  bandwidth_gbs * (accept * n) / (verify + draft)
rather than multiplying by n twice.
"""
import json
import sys


def decode_ceiling(m, drafts, accept, ctx=0, n=1):
    union = 1 - (1 - m["top_k"] / m["n_experts"]) ** ((drafts + 1) * n) if m.get("experts_gb") else 0
    verify = m["dense_gb"] + m["lm_head_gb"] + m.get("experts_gb", 0) * union
    verify += n * m.get("kv_bytes_per_token", 0) * ctx / 1e9 * (drafts + 1 if m.get("kv_per_verify_row") else 1)
    draft = drafts * n * (m.get("draft_layer_gb", 0) + m.get("draft_head_gb", m["lm_head_gb"]))
    step_toks = accept * n
    bytes_read = verify + draft
    return m["bandwidth_gbs"] * step_toks / bytes_read


def prefill_ceiling(m, L):
    linear = 2 * m["active_params_b"] * 1e9
    attn = 4 * m.get("attn_layers", 0) * m.get("attn_heads", 0) * m.get("head_dim", 0) * (L / 2)
    return m["mfu"] * m["peak_tflops"] * 1e12 / (linear + attn + m.get("extra_flops_per_token", 0))


def compute_ceilings(m):
    out = {
        "bandwidth_gbs": m["bandwidth_gbs"],
        "peak_tflops": m["peak_tflops"],
        "mfu": m.get("mfu", 0.5),
        "decode": {},
        "prefill": {},
    }
    if "mfu" not in m:
        m = {**m, "mfu": 0.5}
    for n in m.get("concurrency", [1]):
        rows = []
        for s in m.get("spec", [{"drafts": 0, "accept": 1.0}]):
            ctxs = {
                str(c): round(decode_ceiling(m, s["drafts"], s["accept"], c, n), 1)
                for c in m.get("contexts", [0])
            }
            rows.append({"drafts": s["drafts"], "accept": s["accept"], "by_context": ctxs})
        out["decode"][f"c{n}"] = rows
    for L in m.get("contexts", [4096]):
        out["prefill"][str(L)] = round(prefill_ceiling(m, L), 1)
    if m.get("kv_bytes_per_token"):
        out["kv_gb_per_256k_stream"] = round(m["kv_bytes_per_token"] * 262144 / 1e9, 2)
    return out


def format_report(m):
    lines = [f"bandwidth {m['bandwidth_gbs']} GB/s, peak {m['peak_tflops']} TFLOPS, MFU {m.get('mfu', 0.5)}"]
    mm = {**m, "mfu": m.get("mfu", 0.5)}
    for n in mm.get("concurrency", [1]):
        lines.append(f"\nDecode ceiling, concurrency {n} (aggregate tok/s):")
        lines.append("  drafts accept" + "".join(f"{c // 1024:>8}K" if c >= 1024 else f"{c:>9}" for c in mm.get("contexts", [0])))
        for s in mm.get("spec", [{"drafts": 0, "accept": 1.0}]):
            row = "".join(f"{decode_ceiling(mm, s['drafts'], s['accept'], c, n):9.1f}" for c in mm.get("contexts", [0]))
            lines.append(f"  {s['drafts']:>6} {s['accept']:>6}{row}")
    lines.append("\nPrefill ceiling (tok/s, practical):")
    for L in mm.get("contexts", [4096]):
        label = f"{L // 1024}K" if L >= 1024 else str(L)
        lines.append(f"  {label:>5} {prefill_ceiling(mm, L):8.0f}")
    if mm.get("kv_bytes_per_token"):
        lines.append("\nKV: {:.2f} GB per 256K-token stream".format(mm["kv_bytes_per_token"] * 262144 / 1e9))
    return "\n".join(lines)


def main(path):
    m = json.load(open(path, encoding="utf-8"))
    print(format_report(m))


if __name__ == "__main__":
    if len(sys.argv) > 1:
        main(sys.argv[1])
    else:
        sys.exit("Usage: python3 ceiling.py <ceiling.json>")
