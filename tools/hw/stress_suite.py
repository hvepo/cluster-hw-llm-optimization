#!/usr/bin/env python3
"""Combined CPU Per-Core CO + GPU/LLM transient stress orchestrator (Torre).

Reads all endpoints and local CLI paths from cluster_lib / cluster.local.json.
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

HW_DIR = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.abspath(os.path.join(HW_DIR, ".."))
LLM_DIR = os.path.join(TOOLS_DIR, "llm")
sys.path.insert(0, HW_DIR)
sys.path.insert(0, LLM_DIR)
sys.path.insert(0, TOOLS_DIR)

import bench  # noqa: E402
from cluster_lib import (  # noqa: E402
    detect_platform,
    load_cluster_config,
    node_url,
    resolve_lms_cli,
    resolve_win_python,
)
from gpu_oc_verify import SmiSampler, load_torre_default, unload_torre  # noqa: E402


def to_win_path(path):
    if detect_platform() == "wsl":
        return subprocess.check_output(["wslpath", "-w", path], text=True).strip()
    return path


def check_events(since_iso):
    ps = (
        f"$t = [datetime]::Parse('{since_iso}'); "
        "$ev = @(Get-WinEvent -FilterHashtable @{LogName='System'; StartTime=$t} -ErrorAction SilentlyContinue | "
        "Where-Object { ($_.ProviderName -eq 'Microsoft-Windows-WHEA-Logger' -and ($_.Id -eq 18 -or $_.Id -eq 19)) -or "
        "($_.ProviderName -match 'nvlddmkm|Display|BugCheck') -or "
        "($_.ProviderName -eq 'Microsoft-Windows-Kernel-Power' -and $_.Id -eq 41) }); "
        "Write-Host ('Matching events: ' + $ev.Count); "
        "$ev | Select-Object TimeCreated,Id,LevelDisplayName,ProviderName | Format-Table -AutoSize; exit 0"
    )
    r = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps], stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=30)
    out = (r.stdout or "") + (r.stderr or "")
    cnt = None
    for line in out.splitlines():
        if line.startswith("Matching events:"):
            cnt = int(line.split(":", 1)[1].strip())
    return {"count": cnt, "raw": out.strip()}


def run_combined_stage(label, cfg, node, lms_cli, win_py, win_script, win_log, stop_path, win_stop, model_key, extra_body, prefills, cpu_cores, cycles, since_iso):
    base_url = node_url(node, cfg)
    prefixes = tuple(cfg["nodes"][node].get("model_prefixes", []))
    mem_probe = ["bash", os.path.join(LLM_DIR, "mem_nvidia.sh")]
    cap = cfg["nodes"][node].get("hardware", {}).get("memory_cap_gib", 11.75)

    res = {"stage": label, "model": model_key, "cpu_cores": cpu_cores, "cycles": []}
    flags = []
    watch = None
    cpu_proc = None
    if os.path.exists(stop_path):
        os.remove(stop_path)
    try:
        load_s, _ = load_torre_default(lms_cli, base_url, prefixes, model_key)
        res["load_s"] = load_s
        watch = bench.MemWatch({"cap_gib": cap, "probe": mem_probe, "interval_s": 1}, lambda: unload_torre(base_url, prefixes))
        watch.start()
        cpu_proc = subprocess.Popen(
            [win_py, win_script, "--mode", "continuous", "--cores", cpu_cores, "--duration", "600", "--stop-file", win_stop, "--log", win_log],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        srv = bench.Server({"base_url": base_url, "model": model_key, "temperature": 0.6, "extra_body": extra_body})
        srv.chat("Say hello.", 16)
        for cyc in range(1, cycles + 1):
            c_res = {"cycle": cyc}
            with SmiSampler(0.25) as smi_c2:
                with ThreadPoolExecutor(2) as ex:
                    rs = list(ex.map(lambda i: srv.chat(bench.PROMPTS[(i + cyc) % len(bench.PROMPTS)], 512), range(2)))
            toks = sum((x["completion_tokens"] or 0) for x in rs)
            wall = max(x["t_end"] for x in rs) - min(x["t0"] for x in rs)
            c_res["c2_aggregate"] = round(toks / wall, 2) if wall > 0 else None
            c_res["c2_smi"] = smi_c2.summary()
            if any(bench.looping(x["text"]) for x in rs):
                flags.append("LOOP")
            for L in prefills:
                prompt, _ = bench.document(L, seed=L + cyc * 100)
                with SmiSampler(0.25) as smi_p:
                    r = srv.chat(prompt, 1, temperature=0.0)
                pt = r["prompt_tokens"]
                c_res[f"prefill_{L}"] = round(pt / r["ttft"], 1) if pt else None
                c_res[f"prefill_{L}_smi"] = smi_p.summary()
                if pt is None:
                    flags.append(f"NULL_PREFILL_{L}_C{cyc}")
            res["cycles"].append(c_res)
        res["ok"] = not flags
    except Exception as exc:
        res["ok"] = False
        res["error"] = repr(exc)
        flags.append("REQ_FAIL")
    finally:
        try:
            with open(stop_path, "w", encoding="utf-8") as sf:
                sf.write("stop\n")
            if cpu_proc is not None:
                try:
                    cpu_proc.communicate(timeout=12)
                except subprocess.TimeoutExpired:
                    cpu_proc.terminate()
                    cpu_proc.communicate(timeout=5)
        finally:
            if os.path.exists(stop_path):
                os.remove(stop_path)
        if watch:
            watch.stop.set()
            watch.join(timeout=2)
            res["mem_peak_gib"] = round(watch.peak / 2**30, 2)
        unload_torre(base_url, prefixes)
        res["flags"] = sorted(set(flags))
        res["events"] = check_events(since_iso)
    return res


def main():
    ap = argparse.ArgumentParser(description="Combined Per-Core CPU CO + GPU/LLM Transient Stress Suite")
    ap.add_argument("--node", help="Target cluster node (defaults to local_node)")
    ap.add_argument("--cores", default="3,0,1,5", help="Comma-separated physical cores for Stage 1 & 3a")
    ap.add_argument("--ccd1-cores", default="8,9,10,11,12,13,14,15", help="Cores for Stage 3b")
    ap.add_argument("--cycles", type=int, default=3)
    ap.add_argument("--skip-stage2", action="store_true")
    args = ap.parse_args()

    cfg = load_cluster_config()
    node = args.node or cfg.get("local_node", "torre")
    node_cfg = cfg["nodes"][node]
    win_py = resolve_win_python(cfg)
    lms_cli = resolve_lms_cli(cfg, node=node)
    work_dir = tempfile.mkdtemp(prefix="cluster_stress_")
    log_path = os.path.join(work_dir, "stress_progress.log")
    stop_path = os.path.join(work_dir, "cpu_stop.flag")

    win_script = to_win_path(os.path.join(HW_DIR, "cpu_co_stress.py"))
    win_log = to_win_path(log_path)
    win_stop = to_win_path(stop_path)
    bw_script = to_win_path(os.path.join(HW_DIR, "bw_test.py"))

    since_iso = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    unload_torre(node_url(node, cfg), tuple(node_cfg.get("model_prefixes", [])))
    out = {"node": node, "since": since_iso, "log_file": log_path, "stages": {}}

    r1 = subprocess.run([win_py, win_script, "--mode", "stage1", "--cores", args.cores, "--log", win_log], stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=360)
    ev1 = check_events(since_iso)
    out["stages"]["stage1"] = {"rc": r1.returncode, "events": ev1, "ok": r1.returncode == 0 and ev1["count"] == 0}

    if not args.skip_stage2:
        r2 = subprocess.run([win_py, win_script, "--mode", "stage2", "--log", win_log], stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=420)
        ev2 = check_events(since_iso)
        out["stages"]["stage2"] = {"rc": r2.returncode, "events": ev2, "ok": r2.returncode == 0 and ev2["count"] == 0}

    v_models = node_cfg.get("verify_models") or [
        {"key": "t-vision-context", "prefills": [2048, 8192], "extra_body": {"reasoning_effort": "none"}},
        {"key": "t-code-chat", "prefills": [2048, 4096], "extra_body": {}},
    ]
    m0 = v_models[-1] if len(v_models) > 1 else v_models[0]
    m1 = v_models[0]
    out["stages"]["stage3a"] = run_combined_stage(
        "Stage 3a", cfg, node, lms_cli, win_py, win_script, win_log, stop_path, win_stop,
        m0["key"], m0.get("extra_body", {}), m0.get("prefills", [2048, 8192]), args.cores, args.cycles, since_iso,
    )
    out["stages"]["stage3b"] = run_combined_stage(
        "Stage 3b", cfg, node, lms_cli, win_py, win_script, win_log, stop_path, win_stop,
        m1["key"], m1.get("extra_body", {}), m1.get("prefills", [2048, 4096]), args.ccd1_cores, args.cycles, since_iso,
    )

    bw_r = subprocess.run([win_py, bw_script], stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=60)
    out["post_bw_test"] = (bw_r.stdout or bw_r.stderr).strip()
    out["final_events"] = check_events(since_iso)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
