#!/usr/bin/env python3
"""Apply and optionally verify permanent per-model LM Studio default configs on Torre.

Portable across Windows and WSL using tools/cluster_lib.py path resolution (no hardcoded usernames).
"""
import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../tools")))
from cluster_lib import (  # noqa: E402
    api_request,
    get_api_models,
    load_cluster_config,
    node_url,
    resolve_lms_cli,
    resolve_local_lmstudio_home,
)

TORRE_DEFAULTS = [
    {"key": "t-code-chat", "context_length": 12288, "parallel": 2, "flash_attention": True, "k_quant": "q8_0", "v_quant": "q8_0", "eval_batch_size": 2048, "physical_batch_size": 512, "context_checkpoints": 32, "enable_thinking": None, "mtp_sidecar": False},
    {"key": "t-general-agent", "context_length": 12288, "parallel": 2, "flash_attention": True, "k_quant": "q8_0", "v_quant": "q8_0", "eval_batch_size": 2048, "physical_batch_size": 512, "context_checkpoints": 32, "enable_thinking": False, "mtp_sidecar": False},
    {"key": "t-fast-agent", "context_length": 16384, "parallel": 4, "flash_attention": True, "k_quant": "f16", "v_quant": "f16", "eval_batch_size": 2048, "physical_batch_size": 512, "context_checkpoints": 32, "enable_thinking": False, "mtp_sidecar": False},
    {"key": "t-vision-context", "context_length": 16384, "parallel": 2, "flash_attention": True, "k_quant": "q8_0", "v_quant": "q8_0", "eval_batch_size": 2048, "physical_batch_size": 512, "context_checkpoints": 0, "enable_thinking": False, "mtp_sidecar": True, "draft_model": "bartowski/T-draft-gemma-mtp/T-draft-gemma-mtp.gguf", "draft_max_tokens": 4},
    {"key": "t-math-physics", "context_length": 8192, "parallel": 2, "flash_attention": True, "k_quant": "q8_0", "v_quant": "f16", "eval_batch_size": 2048, "physical_batch_size": 512, "context_checkpoints": 32, "enable_thinking": None, "mtp_sidecar": False},
    {"key": "t-deep-reasoning", "context_length": 8192, "parallel": 2, "flash_attention": True, "k_quant": "q8_0", "v_quant": "q8_0", "eval_batch_size": 2048, "physical_batch_size": 512, "context_checkpoints": 32, "enable_thinking": True, "mtp_sidecar": False},
    {"key": "t-autocomplete-tab", "context_length": 4096, "parallel": 4, "flash_attention": True, "k_quant": "f16", "v_quant": "f16", "eval_batch_size": 2048, "physical_batch_size": 512, "context_checkpoints": 32, "enable_thinking": None, "mtp_sidecar": False},
]


def resolve_indexed_id(index_cache, model_key):
    data = json.load(open(index_cache, encoding="utf-8"))
    for m in data.get("models", []):
        if m.get("defaultIdentifier") == model_key or model_key in (m.get("autoIdentifiers") or []):
            return m["indexedModelIdentifier"]
    raise KeyError(f"Model key {model_key!r} not found in {index_cache}")


def kv_field(q):
    return {"checked": False, "value": "f16"} if q == "f16" else {"checked": True, "value": q}


def write_default_cfg(default_root, index_cache, spec):
    rel_id = resolve_indexed_id(index_cache, spec["key"])
    cfg_path = os.path.join(default_root, rel_id + ".json")
    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
    load_fields = [
        {"key": "llm.load.contextLength", "value": spec["context_length"]},
        {"key": "llm.load.numParallelSessions", "value": spec["parallel"]},
        {"key": "llm.load.offloadKVCacheToGpu", "value": True},
        {"key": "llm.load.gpuStrictVramCap", "value": False},
        {"key": "llm.load.llama.autoFit", "value": False},
        {"key": "llm.load.llama.acceleration.offloadRatio", "value": "max"},
        {"key": "llm.load.llama.flashAttention", "value": spec["flash_attention"]},
        {"key": "llm.load.llama.kCacheQuantizationType", "value": kv_field(spec["k_quant"])},
        {"key": "llm.load.llama.vCacheQuantizationType", "value": kv_field(spec["v_quant"])},
        {"key": "llm.load.llama.evalBatchSize", "value": spec["eval_batch_size"]},
        {"key": "llm.load.llama.physicalBatchSize", "value": spec["physical_batch_size"]},
        {"key": "llm.load.llama.contextCheckpoints", "value": spec["context_checkpoints"]},
    ]
    if spec.get("mtp_sidecar"):
        load_fields.extend([
            {"key": "llm.load.speculativeDecoding.draftMtpSidecar", "value": True},
            {"key": "llm.load.speculativeDecoding.draftModel", "value": spec["draft_model"]},
            {"key": "llm.load.speculativeDecoding.draftMaxTokens", "value": spec["draft_max_tokens"]},
        ])
    if spec.get("draft_simple"):
        load_fields.extend([
            {"key": "llm.load.speculativeDecoding.draftSimple", "value": True},
            {"key": "llm.load.speculativeDecoding.draftModel", "value": spec["draft_model"]},
        ])
        if spec.get("draft_max_tokens") is not None:
            load_fields.append({"key": "llm.load.speculativeDecoding.draftMaxTokens", "value": spec["draft_max_tokens"]})
        if spec.get("draft_min_tokens") is not None:
            load_fields.append({"key": "llm.load.speculativeDecoding.draftMinTokens", "value": spec["draft_min_tokens"]})
        if spec.get("draft_min_continue_prob") is not None:
            load_fields.append({"key": "llm.load.speculativeDecoding.draftMinContinueProbability", "value": spec["draft_min_continue_prob"]})
    op_fields = []
    if spec.get("enable_thinking") is not None:
        op_fields.append({"key": "llm.prediction.reasoning.enableThinking", "value": spec["enable_thinking"]})
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"preset": "", "operation": {"fields": op_fields}, "load": {"fields": load_fields}}, f, indent=2)
    return cfg_path


def unload_torre(url, prefixes):
    for m in get_api_models(url, timeout=10):
        k = m.get("key", "")
        if not k.startswith(prefixes):
            continue
        for inst in m.get("loaded_instances") or []:
            try:
                api_request(url, "POST", "/api/v1/models/unload", {"instance_id": inst.get("id") or k}, timeout=60)
            except Exception:
                pass
    time.sleep(1.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true", help="Cold-load each model via lms load <key> -y to verify inheritance")
    args = ap.parse_args()

    cfg = load_cluster_config()
    lms_home = resolve_local_lmstudio_home(cfg, node="torre")
    internal = os.path.join(lms_home, ".internal")
    default_root = os.path.join(internal, "user-concrete-model-default-config")
    index_cache = os.path.join(internal, "model-index-cache.json")

    for spec in TORRE_DEFAULTS:
        path = write_default_cfg(default_root, index_cache, spec)
        print(f"Configured {spec['key']} -> {path}")

    if args.verify:
        url = node_url("torre", cfg)
        prefixes = tuple(cfg["nodes"]["torre"]["model_prefixes"])
        lms_cli = resolve_lms_cli(cfg, node="torre")
        unload_torre(url, prefixes)
        for spec in TORRE_DEFAULTS:
            k = spec["key"]
            subprocess.run([lms_cli, "load", k, "-y"], stdin=subprocess.DEVNULL, check=True, capture_output=True, text=True, timeout=180)
            cfg_echo = next((m["loaded_instances"][0].get("config", {}) for m in get_api_models(url) if m.get("key") == k and m.get("loaded_instances")), None)
            assert cfg_echo and cfg_echo.get("context_length") == spec["context_length"], f"Mismatch on {k}: {cfg_echo}"
            print(f"  VERIFIED {k}: ctx={cfg_echo['context_length']} p={cfg_echo['parallel']} fa={cfg_echo['flash_attention']}")
            unload_torre(url, prefixes)


if __name__ == "__main__":
    main()
