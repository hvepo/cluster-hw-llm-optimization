#!/usr/bin/env python3
"""GPU Undervolt + VRAM OC verification suite:
1. Decodes active MSI Afterburner V/F curve (verifies flattened right tail).
2. Runs bw_test.py via Windows Python to measure CUDA D2D bandwidth & spread.
3. Runs default-inheriting LLM decode + cold prefill stress (t-code-chat & t-vision-context)
   with live 0.25s nvidia-smi telemetry sampling and MemWatch protection.
4. Audits Windows System Event Log for nvlddmkm 153 / Display / WHEA errors.
"""
import argparse
import configparser
import datetime
import json
import os
import statistics
import struct
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

HW_DIR = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.abspath(os.path.join(HW_DIR, ".."))
LLM_DIR = os.path.join(TOOLS_DIR, "llm")
sys.path.insert(0, LLM_DIR)
sys.path.insert(0, TOOLS_DIR)

import bench  # noqa: E402
from cluster_lib import (  # noqa: E402
    api_request,
    detect_platform,
    get_api_models,
    load_cluster_config,
    node_url,
    resolve_afterburner_cfg,
    resolve_host_binary,
    resolve_lms_cli,
    resolve_win_python,
)


def decode_afterburner_vf(cfg_path, section="Startup"):
    if not cfg_path or not os.path.isfile(cfg_path):
        return None
    cp = configparser.ConfigParser(interpolation=None)
    with open(cfg_path, encoding="utf-8", errors="replace") as f:
        cp.read_string(f.read())
    if section not in cp:
        return None
    sec = cp[section]
    res = {
        "cfg_path": cfg_path,
        "section": section,
        "power_limit_pct": sec.get("PowerLimit"),
        "core_clk_boost_khz": sec.get("CoreClkBoost"),
        "mem_clk_boost_khz": sec.get("MemClkBoost"),
    }
    hx = (sec.get("VFCurve") or "").strip()
    if len(hx) >= 24:
        raw = bytes.fromhex(hx)
        _, count, _ = struct.unpack_from("<III", raw, 0)
        pts = [struct.unpack_from("<fff", raw, 12 + i * 12) for i in range(count) if 12 + (i + 1) * 12 <= len(raw)]
        hi = [(round(v, 1), round(f, 1), round(off, 1), round(f + off, 1)) for (v, f, off) in pts if v >= 800.0]
        nz = [p for p in hi if abs(p[2]) > 0.01]
        if hi:
            max_tgt = max(p[3] for p in hi)
            tail_flat = len({p[3] for p in hi if p[0] >= 950.0}) == 1
            res.update({
                "nonzero_points": nz[:8],
                "nonzero_count": len(nz),
                "max_target_mhz_ge800mv": max_tgt,
                "tail_flattened_ge950mv": tail_flat,
            })
    return res


def sample_smi_once():
    smi = resolve_host_binary("nvidia-smi")
    out = subprocess.check_output(
        [
            smi,
            "--query-gpu=clocks.gr,clocks.mem,power.draw,temperature.gpu,clocks_event_reasons.active",
            "--format=csv,noheader,nounits",
        ],
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=5,
    ).strip().splitlines()[0]
    p = [x.strip() for x in out.split(",")]
    return {
        "gr_mhz": int(float(p[0])),
        "mem_mhz": int(float(p[1])),
        "power_w": float(p[2]),
        "temp_c": float(p[3]),
        "throttle": p[4],
    }


class SmiSampler:
    def __init__(self, interval_s=0.25):
        self.interval_s = interval_s
        self.samples = []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            try:
                self.samples.append(sample_smi_once())
            except Exception:
                pass
            if self._stop.wait(self.interval_s):
                break

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        self._t.join(timeout=2)

    def summary(self):
        if not self.samples:
            return None
        gr = [s["gr_mhz"] for s in self.samples]
        mem = [s["mem_mhz"] for s in self.samples]
        pw = [s["power_w"] for s in self.samples]
        tp = [s["temp_c"] for s in self.samples]
        return {
            "n": len(self.samples),
            "gr_med_mhz": round(statistics.median(gr), 1),
            "gr_max_mhz": max(gr),
            "gr_min_mhz": min(gr),
            "mem_mhz": sorted(set(mem)),
            "power_max_w": round(max(pw), 2),
            "power_med_w": round(statistics.median(pw), 2),
            "temp_max_c": round(max(tp), 2),
            "throttle_reasons": sorted({s["throttle"] for s in self.samples}),
        }


def unload_torre(base_url, prefixes):
    for m in get_api_models(base_url, timeout=10):
        k = m.get("key", "")
        if not k.startswith(prefixes):
            continue
        for inst in m.get("loaded_instances") or []:
            inst_id = inst.get("id") or k
            try:
                api_request(base_url, "POST", "/api/v1/models/unload", {"instance_id": inst_id}, timeout=60)
            except Exception:
                pass
    time.sleep(1.2)


def load_torre_default(lms_cli, base_url, prefixes, model_key):
    unload_torre(base_url, prefixes)
    t0 = time.monotonic()
    r = subprocess.run([lms_cli, "load", model_key, "-y"], stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=180)
    if r.returncode != 0:
        raise RuntimeError(f"lms load {model_key} failed: {r.stderr or r.stdout}")
    deadline = t0 + 30
    while time.monotonic() < deadline:
        for m in get_api_models(base_url, timeout=8):
            if m.get("key") == model_key and m.get("loaded_instances"):
                return round(time.monotonic() - t0, 2), m["loaded_instances"][0].get("config", {})
        time.sleep(0.5)
    raise TimeoutError(f"{model_key} did not appear in /api/v1/models")


def check_nvlddmkm(since_iso):
    ps = (
        f"$t = [datetime]::Parse('{since_iso}'); "
        "$ev = @(Get-WinEvent -FilterHashtable @{LogName='System'; StartTime=$t} -ErrorAction SilentlyContinue | "
        "Where-Object { ($_.ProviderName -match 'nvlddmkm|Display|BugCheck') -or ($_.ProviderName -eq 'Microsoft-Windows-WHEA-Logger' -and ($_.Id -eq 18 -or $_.Id -eq 19)) }); "
        "Write-Host ('Matching events: ' + $ev.Count); "
        "$ev | Select-Object TimeCreated,Id,LevelDisplayName,ProviderName | Format-Table -AutoSize; exit 0"
    )
    ps_bin = resolve_host_binary("powershell.exe")
    r = subprocess.run([ps_bin, "-NoProfile", "-Command", ps], stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=30)
    out = (r.stdout or "") + (r.stderr or "")
    cnt = None
    for line in out.splitlines():
        if line.startswith("Matching events:"):
            cnt = int(line.split(":", 1)[1].strip())
    return {"count": cnt, "raw": out.strip()}


def main():
    ap = argparse.ArgumentParser(description="Verify GPU undervolt + VRAM OC under CUDA BW and live LLM prefill")
    ap.add_argument("--node", help="Target cluster node (defaults to local_node)")
    ap.add_argument("--models", help="Optional comma-separated model keys to verify")
    ap.add_argument("--skip-llm", action="store_true", help="Only inspect V/F curve and run CUDA bw_test")
    ap.add_argument("--reps", type=int, default=6)
    args = ap.parse_args()

    cfg = load_cluster_config()
    node = args.node or cfg.get("local_node", "torre")
    node_cfg = cfg["nodes"][node]
    ab_path = resolve_afterburner_cfg(cfg)
    vf = decode_afterburner_vf(ab_path)
    win_py = resolve_win_python(cfg)
    bw_script = os.path.join(HW_DIR, "bw_test.py")
    if detect_platform() == "wsl":
        wslpath_bin = resolve_host_binary("wslpath")
        bw_script = subprocess.check_output([wslpath_bin, "-w", bw_script], stdin=subprocess.DEVNULL, text=True).strip()

    since_iso = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    base_url = node_url(node, cfg)
    prefixes = tuple(node_cfg.get("model_prefixes", []))
    unload_torre(base_url, prefixes)

    bw_res = subprocess.run([win_py, bw_script], stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=60)
    report = {
        "node": node,
        "since": since_iso,
        "afterburner_vf": vf,
        "bw_test_rc": bw_res.returncode,
        "bw_test": (bw_res.stdout or bw_res.stderr).strip(),
        "models": [],
    }

    if not args.skip_llm:
        lms_cli = resolve_lms_cli(cfg, node=node)
        mem_probe = ["bash", os.path.join(LLM_DIR, "mem_nvidia.sh")]
        cap = node_cfg.get("hardware", {}).get("memory_cap_gib", 11.75)
        if args.models:
            specs = [{"key": k.strip(), "prefills": [2048, 4096], "extra_body": {}} for k in args.models.split(",") if k.strip()]
        else:
            specs = node_cfg.get("verify_models") or [
                {"key": "t-code-chat", "prefills": [2048, 4096], "extra_body": {}},
                {"key": "t-vision-context", "prefills": [2048, 8192], "extra_body": {"reasoning_effort": "none"}},
            ]
        for sp in specs:
            key = sp["key"]
            m_res = {"model": key}
            flags = []
            watch = None
            try:
                load_s, _ = load_torre_default(lms_cli, base_url, prefixes, key)
                m_res["load_s"] = load_s
                watch = bench.MemWatch({"cap_gib": cap, "probe": mem_probe, "interval_s": 1}, lambda: unload_torre(base_url, prefixes))
                watch.start()
                srv = bench.Server({"base_url": base_url, "model": key, "temperature": 0.6, "extra_body": sp["extra_body"]})
                srv.chat("Say hello.", 16)
                m_res["ttft_short_ms"] = round(srv.chat("Reply with the single word: ready", 8)["ttft"] * 1000, 1)
                c1 = []
                for i in range(args.reps):
                    r = srv.chat(bench.PROMPTS[i % len(bench.PROMPTS)], 512)
                    if r["decode"]:
                        c1.append(round(r["decode"], 2))
                    if bench.looping(r["text"]):
                        flags.append("LOOP")
                m_res["c1_decode"] = round(statistics.median(c1), 2) if c1 else None
                with SmiSampler(0.25) as smi_c2:
                    with ThreadPoolExecutor(2) as ex:
                        rs = list(ex.map(lambda i: srv.chat(bench.PROMPTS[i % len(bench.PROMPTS)], 512), range(2)))
                toks = sum((x["completion_tokens"] or 0) for x in rs)
                wall = max(x["t_end"] for x in rs) - min(x["t0"] for x in rs)
                m_res["c2_aggregate"] = round(toks / wall, 2) if wall > 0 else None
                m_res["c2_smi"] = smi_c2.summary()
                for L in sp["prefills"]:
                    prompt, _ = bench.document(L, seed=L)
                    with SmiSampler(0.25) as smi_p:
                        r = srv.chat(prompt, 1, temperature=0.0)
                    pt = r["prompt_tokens"]
                    m_res[f"prefill_{L}"] = round(pt / r["ttft"], 1) if pt else None
                    m_res[f"prefill_{L}_smi"] = smi_p.summary()
                    if pt is None:
                        flags.append(f"NULL_PREFILL_{L}")
                m_res["ok"] = not flags
            except Exception as exc:
                m_res["ok"] = False
                m_res["error"] = repr(exc)
                flags.append("REQ_FAIL")
            finally:
                if watch:
                    watch.stop.set()
                    watch.join(timeout=2)
                    m_res["mem_peak_gib"] = round(watch.peak / 2**30, 2)
                m_res["flags"] = sorted(set(flags))
                unload_torre(base_url, prefixes)
            report["models"].append(m_res)

    report["events"] = check_nvlddmkm(since_iso)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
