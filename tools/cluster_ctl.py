#!/usr/bin/env python3
"""Cluster node LLM status, catalog, and prefix-safe load/unload helper for benchmarking & verification."""
import argparse
import json
import subprocess
import time

from cluster_lib import (
    api_request,
    detect_platform,
    get_api_models,
    load_best_config,
    load_cluster_config,
    node_for_model,
    node_url,
    resolve_lms_cli,
)


def query_nvidia_gpu():
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=name,clocks.gr,clocks.mem,clocks.max.mem,power.draw,power.limit,temperature.gpu,clocks_event_reasons.active",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).strip()
        if not out:
            return None
        parts = [p.strip() for p in out.splitlines()[0].split(",")]
        return {
            "name": parts[0],
            "gr_mhz": int(float(parts[1])),
            "mem_mhz": int(float(parts[2])),
            "stock_mem_mhz": int(float(parts[3])),
            "power_w": float(parts[4]),
            "power_limit_w": float(parts[5]),
            "temp_c": float(parts[6]),
            "throttle_reasons": parts[7],
        }
    except Exception:
        return None


def collect_status(cfg, node_filter=None):
    plat = detect_platform()
    local_node = cfg.get("local_node", "torre")
    status = {"platform": plat, "local_node": local_node, "nodes": {}, "local_gpu": query_nvidia_gpu()}
    for node_name, node_cfg in cfg.get("nodes", {}).items():
        if node_filter and node_name != node_filter:
            continue
        url = (node_cfg.get("base_url") or "").rstrip("/")
        prefixes = tuple(node_cfg.get("model_prefixes", []))
        info = {"base_url": url, "prefixes": list(prefixes), "reachable": False, "loaded_local": [], "loaded_remote": []}
        if not url or "<" in url:
            info["error"] = "unconfigured base_url (edit cluster.local.json)"
            status["nodes"][node_name] = info
            continue
        try:
            models = get_api_models(url, timeout=6)
            info["reachable"] = True
            info["listed_count"] = len(models)
            for m in models:
                k = m.get("key", "")
                for inst in m.get("loaded_instances") or []:
                    entry = {"key": k, "id": inst.get("id") or k, "config": inst.get("config")}
                    if not prefixes or k.startswith(prefixes):
                        info["loaded_local"].append(entry)
                    else:
                        info["loaded_remote"].append(entry)
        except Exception as e:
            info["error"] = str(e)
        status["nodes"][node_name] = info
    return status


def cmd_status(cfg, node_filter=None, as_json=False):
    st = collect_status(cfg, node_filter=node_filter)
    if as_json:
        print(json.dumps(st, indent=2))
        return
    print(f"Cluster platform={st['platform']} local_node={st['local_node']}")
    if st.get("local_gpu"):
        g = st["local_gpu"]
        print(
            f"  Local GPU: {g['name']} | core={g['gr_mhz']}MHz mem={g['mem_mhz']}MHz "
            f"(stock {g['stock_mem_mhz']}) | {g['power_w']}/{g['power_limit_w']}W | {g['temp_c']}C | throttle={g['throttle_reasons']}"
        )
    for n, info in st["nodes"].items():
        if not info["reachable"]:
            print(f"  [{n}] UNREACHABLE ({info['base_url']}): {info.get('error')}")
            continue
        print(
            f"  [{n}] OK ({info['base_url']}) | listed={info['listed_count']} | "
            f"loaded_local={len(info['loaded_local'])} loaded_lmlink={len(info['loaded_remote'])}"
        )
        for item in info["loaded_local"]:
            print(f"    * {item['key']} (id={item['id']}): {json.dumps(item.get('config'))}")


def cmd_catalog(cfg, node_filter=None, as_json=False):
    bc = load_best_config()
    nodes = bc.get("nodes", {})
    out = {n: nd.get("models", {}) for n, nd in nodes.items() if not node_filter or n == node_filter}
    if as_json:
        print(json.dumps(out, indent=2))
        return
    for n, models in out.items():
        print(f"=== {n} ({len(models)} tuned models) ===")
        for mk, mv in models.items():
            opt = mv.get("optimized_uv_oc") or mv.get("optimized", {})
            c1 = opt.get("c1_tps") or mv.get("throughput_emb_per_s", "-")
            mem = mv.get("optimized", {}).get("mem_gib", "-")
            print(f"  {mk:<30} | {mv.get('underlying_model','')} | c1/tps={c1} | mem={mem} GiB | win={mv.get('winner_config','-')}")


def cmd_load(cfg, model_key, node_override=None, force_remote_rest=False):
    owner = node_override or node_for_model(model_key, cfg) or cfg.get("local_node", "torre")
    if owner not in cfg.get("nodes", {}):
        raise ValueError(f"Unknown node {owner!r}. Configured nodes: {list(cfg.get('nodes', {}))}")
    expected_prefixes = tuple(cfg["nodes"][owner].get("model_prefixes", []))
    if expected_prefixes and not model_key.startswith(expected_prefixes):
        raise ValueError(f"Safety stop: {model_key!r} does not match node {owner!r} prefixes {expected_prefixes}")

    local_node = cfg.get("local_node", "torre")
    url = node_url(owner, cfg)
    cmd_unload(cfg, node_override=owner, quiet=True)

    t0 = time.monotonic()
    if owner == local_node and not force_remote_rest:
        lms = resolve_lms_cli(cfg, node=owner)
        r = subprocess.run([lms, "load", model_key, "-y"], stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"lms load {model_key} failed ({r.returncode}): {r.stderr or r.stdout}")
        deadline = t0 + 30
        while time.monotonic() < deadline:
            for m in get_api_models(url, timeout=8):
                if m.get("key") == model_key and m.get("loaded_instances"):
                    inst = m["loaded_instances"][0]
                    print(f"Loaded {model_key} on {owner} (default-inheriting) in {time.monotonic()-t0:.2f}s | id={inst.get('id')}")
                    return
            time.sleep(0.5)
        raise TimeoutError(f"{model_key} not visible in {url}/api/v1/models after lms load")
    else:
        _, resp = api_request(url, "POST", "/api/v1/models/load", {"model": model_key, "echo_load_config": True}, timeout=300)
        print(f"Loaded {model_key} on {owner} via REST in {time.monotonic()-t0:.2f}s: {json.dumps(resp)}")


def cmd_unload(cfg, node_override=None, model_key=None, quiet=False):
    targets = [node_override] if node_override else ([node_for_model(model_key, cfg)] if model_key else list(cfg.get("nodes", {}).keys()))
    unloaded = []
    for n in targets:
        if not n or n not in cfg.get("nodes", {}):
            continue
        url = (cfg["nodes"][n].get("base_url") or "").rstrip("/")
        if not url or "<" in url:
            continue
        prefixes = tuple(cfg["nodes"][n].get("model_prefixes", []))
        try:
            models = get_api_models(url, timeout=8)
        except Exception as e:
            if not quiet:
                print(f"[{n}] skip unload (unreachable: {e})")
            continue
        for m in models:
            k = m.get("key", "")
            if prefixes and not k.startswith(prefixes):
                continue
            if model_key and k != model_key:
                continue
            for inst in m.get("loaded_instances") or []:
                inst_id = inst.get("id") or inst.get("instance_id") or k
                try:
                    _, resp = api_request(url, "POST", "/api/v1/models/unload", {"instance_id": inst_id}, timeout=60)
                    unloaded.append({"node": n, "key": k, "id": inst_id, "resp": resp})
                    if not quiet:
                        print(f"[{n}] Unloaded {k} ({inst_id})")
                except RuntimeError as e:
                    if "model_not_found" not in str(e):
                        raise
    return unloaded


def main():
    ap = argparse.ArgumentParser(description="Cluster node LLM status, catalog, and benchmark load/unload helper")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("status")
    ps.add_argument("--node")
    ps.add_argument("--json", action="store_true")

    sub.add_parser("health")

    pc = sub.add_parser("catalog")
    pc.add_argument("--node")
    pc.add_argument("--json", action="store_true")

    pl = sub.add_parser("load")
    pl.add_argument("model_key")
    pl.add_argument("--node")
    pl.add_argument("--rest", action="store_true")

    pu = sub.add_parser("unload")
    pu.add_argument("--node")
    pu.add_argument("--model")

    args = ap.parse_args()
    cfg = load_cluster_config()

    if args.cmd in ("status", "health"):
        cmd_status(cfg, node_filter=getattr(args, "node", None), as_json=getattr(args, "json", False))
    elif args.cmd == "catalog":
        cmd_catalog(cfg, node_filter=args.node, as_json=args.json)
    elif args.cmd == "load":
        cmd_load(cfg, args.model_key, node_override=args.node, force_remote_rest=args.rest)
    elif args.cmd == "unload":
        cmd_unload(cfg, node_override=args.node, model_key=args.model)


if __name__ == "__main__":
    main()
