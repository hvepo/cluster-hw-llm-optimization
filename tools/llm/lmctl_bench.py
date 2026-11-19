#!/usr/bin/env python3
"""Safe LM Studio benchmark controller for temporary override experiments.

Unlike older helpers that deleted permanent defaults after temporary tests,
`lmctl_bench.py` snapshots any pre-existing per-model default JSON before loading
an override config and restores it cleanly when unloading.

Always passes stdin=subprocess.DEVNULL to Windows/WSL CLI calls so background jobs never hang.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from cluster_lib import (  # noqa: E402
    api_request,
    get_api_models,
    load_cluster_config,
    node_for_model,
    node_url,
    resolve_lms_cli,
    resolve_local_lmstudio_home,
)

BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".lmctl_backups")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".lmctl_temp_configs.json")


def parse_bool(val):
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    raise argparse.ArgumentTypeError(f"Invalid boolean: {val}")


def parse_kv_quant(val):
    parts = val.split(":")
    valid = ("f16", "q8_0", "q4_0")
    if len(parts) not in (1, 2) or any(p not in valid for p in parts):
        raise argparse.ArgumentTypeError(f"Invalid kv-quant {val!r}; expected f16|q8_0|q4_0 or k:v")
    return val


def resolve_indexed_id(lms_home, model_key):
    cache_path = os.path.join(lms_home, ".internal", "model-index-cache.json")
    data = json.load(open(cache_path, encoding="utf-8"))
    for m in data.get("models", []):
        if m.get("defaultIdentifier") == model_key or model_key in (m.get("autoIdentifiers") or []):
            return m["indexedModelIdentifier"]
    raise KeyError(f"Model key {model_key!r} not found in {cache_path}")


def set_strict_vram_cap(lms_home, runtime_name, enabled=False):
    hw_path = os.path.join(lms_home, ".internal", "hardware-config.json")
    if not os.path.exists(hw_path):
        return
    data = json.load(open(hw_path, encoding="utf-8"))
    entries = data.get("json", [])
    updated = False
    for name, body in entries:
        if name == runtime_name:
            fields = body.setdefault("fields", [])
            for f in fields:
                if f.get("key") == "load.gpuStrictVramCap":
                    f["value"] = enabled
                    updated = True
            if not updated:
                fields.append({"key": "load.gpuStrictVramCap", "value": enabled})
                updated = True
    if not updated:
        entries.append([runtime_name, {"fields": [{"key": "load.gpuStrictVramCap", "value": enabled}]}])
    with open(hw_path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def snapshot_before_override(cfg_path, persist=False):
    tracked = {}
    if os.path.exists(STATE_FILE):
        try:
            tracked = json.load(open(STATE_FILE, encoding="utf-8"))
            if isinstance(tracked, list):
                tracked = {p: None for p in tracked}
        except Exception:
            tracked = {}
    if persist:
        tracked.pop(cfg_path, None)
    elif cfg_path not in tracked:
        bak = None
        if os.path.exists(cfg_path):
            os.makedirs(BACKUP_DIR, exist_ok=True)
            bak = os.path.join(BACKUP_DIR, os.path.basename(cfg_path) + f".{int(time.time())}.bak")
            shutil.copy2(cfg_path, bak)
        tracked[cfg_path] = bak
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(tracked, f, indent=2)


def restore_temp_cfgs(default_cfg_root):
    if not os.path.exists(STATE_FILE):
        return
    try:
        tracked = json.load(open(STATE_FILE, encoding="utf-8"))
        if isinstance(tracked, list):
            tracked = {p: None for p in tracked}
    except Exception:
        tracked = {}

    for cfg_path, bak_path in tracked.items():
        try:
            if bak_path and os.path.exists(bak_path):
                os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
                shutil.move(bak_path, cfg_path)
            else:
                if os.path.exists(cfg_path):
                    os.remove(cfg_path)
                parent = os.path.dirname(cfg_path)
                while parent.startswith(default_cfg_root) and parent != default_cfg_root:
                    if os.path.isdir(parent) and not os.listdir(parent):
                        os.rmdir(parent)
                        parent = os.path.dirname(parent)
                    else:
                        break
        except Exception as e:
            print(f"[warn] restore {cfg_path}: {e}", file=sys.stderr)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({}, f)


def unload_node(cfg, node_name, drain_s=2.0):
    if drain_s > 0:
        time.sleep(drain_s)
    url = node_url(node_name, cfg)
    prefixes = tuple(cfg["nodes"][node_name]["model_prefixes"])
    for m in get_api_models(url, timeout=10):
        k = m.get("key", "")
        if not k.startswith(prefixes):
            continue
        for inst in m.get("loaded_instances") or []:
            inst_id = inst.get("id") or inst.get("instance_id") or k
            try:
                _, resp = api_request(url, "POST", "/api/v1/models/unload", {"instance_id": inst_id}, timeout=60)
                print(f"[{node_name}] Unloaded {k} ({inst_id}): {json.dumps(resp)}")
            except RuntimeError as e:
                if "model_not_found" not in str(e):
                    raise


def cmd_load(args, cfg):
    owner = args.node or node_for_model(args.model_key, cfg) or cfg.get("local_node", "torre")
    local_node = cfg.get("local_node", "torre")
    url = node_url(owner, cfg)
    prefixes = tuple(cfg["nodes"][owner]["model_prefixes"])
    if not args.model_key.startswith(prefixes):
        raise ValueError(f"Refusing to load {args.model_key!r}: expected prefixes {prefixes} for node {owner!r}")

    if not args.keep_loaded:
        unload_node(cfg, owner, drain_s=1.0)

    if owner != local_node:
        payload = {"model": args.model_key, "echo_load_config": True}
        if args.context_length is not None:
            payload["context_length"] = args.context_length
        if args.eval_batch_size is not None:
            payload["eval_batch_size"] = args.eval_batch_size
        if args.flash_attention is not None:
            payload["flash_attention"] = args.flash_attention
        if args.offload_kv_cache_to_gpu is not None:
            payload["offload_kv_cache_to_gpu"] = args.offload_kv_cache_to_gpu
        _, resp = api_request(url, "POST", "/api/v1/models/load", payload, timeout=args.timeout)
        print(f"Loaded {args.model_key} via REST on {owner}: {json.dumps(resp)}")
        return

    lms_home = resolve_local_lmstudio_home(cfg, node=owner)
    default_root = os.path.join(lms_home, ".internal", "user-concrete-model-default-config")
    lms_cli = resolve_lms_cli(cfg, node=owner)

    if args.runtime:
        rt_base = args.runtime.split("@")[0]
        set_strict_vram_cap(lms_home, rt_base, enabled=False)
        sel_args = [lms_cli, "runtime", "select", args.runtime] if "@" in args.runtime else [lms_cli, "runtime", "select", args.runtime, "--latest"]
        r = subprocess.run(sel_args, stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f"lms runtime select failed: {r.stderr or r.stdout}")

    if not args.use_defaults:
        rel_id = resolve_indexed_id(lms_home, args.model_key)
        cfg_path = os.path.join(default_root, rel_id + ".json")
        os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
        snapshot_before_override(cfg_path, persist=args.persist)

        load_fields = [
            {"key": "llm.load.llama.autoFit", "value": False},
            {"key": "llm.load.llama.acceleration.offloadRatio", "value": "max"},
            {"key": "llm.load.gpuStrictVramCap", "value": False},
        ]
        if args.flash_attention is not None:
            load_fields.append({"key": "llm.load.llama.flashAttention", "value": args.flash_attention})
        if args.kv_quant is not None:
            k_q, v_q = args.kv_quant.split(":", 1) if ":" in args.kv_quant else (args.kv_quant, args.kv_quant)
            load_fields.append({"key": "llm.load.llama.kCacheQuantizationType", "value": {"checked": k_q != "f16", "value": k_q}})
            load_fields.append({"key": "llm.load.llama.vCacheQuantizationType", "value": {"checked": v_q != "f16", "value": v_q}})
        if args.eval_batch_size is not None:
            load_fields.append({"key": "llm.load.llama.evalBatchSize", "value": args.eval_batch_size})
        if args.physical_batch_size is not None:
            load_fields.append({"key": "llm.load.llama.physicalBatchSize", "value": args.physical_batch_size})
        if args.context_checkpoints is not None:
            load_fields.append({"key": "llm.load.llama.contextCheckpoints", "value": args.context_checkpoints})
        if args.draft_mtp_sidecar:
            load_fields.append({"key": "llm.load.speculativeDecoding.draftMtpSidecar", "value": True})
            if args.draft_model:
                draft_rel = resolve_indexed_id(lms_home, args.draft_model) if "/" not in args.draft_model else args.draft_model
                load_fields.append({"key": "llm.load.speculativeDecoding.draftModel", "value": draft_rel})
            if args.draft_max_tokens is not None:
                load_fields.append({"key": "llm.load.speculativeDecoding.draftMaxTokens", "value": args.draft_max_tokens})

        op_fields = []
        if args.enable_thinking is not None:
            op_fields.append({"key": "llm.prediction.reasoning.enableThinking", "value": args.enable_thinking})
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"preset": "", "operation": {"fields": op_fields}, "load": {"fields": load_fields}}, f, indent=2)

    lms_args = [lms_cli, "load", args.model_key]
    if not args.use_defaults:
        lms_args += ["--gpu", "max"]
        if args.context_length is not None:
            lms_args += ["-c", str(args.context_length)]
        if args.parallel is not None:
            lms_args += ["--parallel", str(args.parallel)]
    lms_args.append("-y")

    t0 = time.monotonic()
    r = subprocess.run(lms_args, stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=args.timeout)
    if r.returncode != 0:
        raise RuntimeError(f"lms load failed ({r.returncode}): {r.stderr or r.stdout}")
    deadline = t0 + 30
    while time.monotonic() < deadline:
        for m in get_api_models(url, timeout=8):
            if m.get("key") == args.model_key and m.get("loaded_instances"):
                print(f"Loaded {args.model_key} in {time.monotonic()-t0:.2f}s | instances={json.dumps(m['loaded_instances'])}")
                return
        time.sleep(0.5)
    raise TimeoutError(f"{args.model_key} not visible in /api/v1/models")


def main():
    ap = argparse.ArgumentParser(description="Safe temporary benchmark controller for LM Studio")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ps = sub.add_parser("status")
    ps.add_argument("--node", default="torre")

    pl = sub.add_parser("load")
    pl.add_argument("model_key")
    pl.add_argument("--node", help="Cluster node name from cluster.local.json")
    pl.add_argument("--use-defaults", action="store_true", help="Load inheriting existing permanent defaults")
    pl.add_argument("--context-length", type=int)
    pl.add_argument("--parallel", type=int)
    pl.add_argument("--flash-attention", type=parse_bool)
    pl.add_argument("--kv-quant", type=parse_kv_quant)
    pl.add_argument("--offload-kv-cache-to-gpu", type=parse_bool)
    pl.add_argument("--eval-batch-size", type=int)
    pl.add_argument("--physical-batch-size", type=int)
    pl.add_argument("--context-checkpoints", type=int)
    pl.add_argument("--draft-mtp-sidecar", action="store_true")
    pl.add_argument("--draft-model")
    pl.add_argument("--draft-max-tokens", type=int)
    pl.add_argument("--enable-thinking", type=parse_bool)
    pl.add_argument("--runtime")
    pl.add_argument("--keep-loaded", action="store_true")
    pl.add_argument("--persist", action="store_true")
    pl.add_argument("--timeout", type=int, default=300)

    pu = sub.add_parser("unload")
    pu.add_argument("--node", default="torre")
    pu.add_argument("--drain", type=float, default=2.0)

    args = ap.parse_args()
    cfg = load_cluster_config()
    if args.cmd == "status":
        url = node_url(args.node, cfg)
        prefixes = tuple(cfg["nodes"][args.node]["model_prefixes"])
        models = get_api_models(url)
        loaded = [(m["key"], inst) for m in models if m["key"].startswith(prefixes) for inst in (m.get("loaded_instances") or [])]
        print(f"Node={args.node} ({url}) loaded={len(loaded)}")
        for k, inst in loaded:
            print(f"  {k}: {json.dumps(inst.get('config'))}")
    elif args.cmd == "load":
        cmd_load(args, cfg)
    elif args.cmd == "unload":
        unload_node(cfg, args.node, drain_s=args.drain)
        local_node = cfg.get("local_node", "torre")
        if args.node == local_node:
            lms_home = resolve_local_lmstudio_home(cfg, node=local_node)
            restore_temp_cfgs(os.path.join(lms_home, ".internal", "user-concrete-model-default-config"))


if __name__ == "__main__":
    main()
