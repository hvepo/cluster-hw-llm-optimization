#!/usr/bin/env python3
"""Native Windows per-core Curve Optimizer stress tool (pulsed cache/SHA/FP).

Execute via Windows Python so SetThreadAffinityMask addresses all logical CPUs directly.
"""
import argparse
import ctypes
import hashlib
import lzma
import math
import os
import threading
import time
import zlib

kernel32 = ctypes.windll.kernel32
GetCurrentThread = kernel32.GetCurrentThread
GetCurrentThread.restype = ctypes.c_void_p
SetThreadAffinityMask = kernel32.SetThreadAffinityMask
SetThreadAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
SetThreadAffinityMask.restype = ctypes.c_size_t
GetCurrentProcessorNumber = kernel32.GetCurrentProcessorNumber
GetCurrentProcessorNumber.restype = ctypes.c_uint

_SHA_BUF = bytes((i * 131 + 17) & 0xFF for i in range(256 * 1024))
_SHA_REF = hashlib.sha256(_SHA_BUF).digest()
_COMP_BUF = (b"Zen3CurveOptimizerStressPattern-L1L2L3CacheStress-0123456789ABCDEF-" * 8192)[: 512 * 1024]
_ZLIB_BUF = bytes(range(256)) * 4096

PULSE_PATTERN = [
    (0.06, 0.03),
    (0.15, 0.08),
    (0.40, 0.15),
    (1.20, 0.20),
]


class LogWriter:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        if path:
            os.makedirs(os.path.dirname(path), exist_ok=True)

    def write(self, msg):
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}"
        with self.lock:
            print(line, flush=True)
            if self.path:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except OSError:
                        pass


def pin_current_thread(apic_id):
    mask = 1 << apic_id
    prev = SetThreadAffinityMask(GetCurrentThread(), mask)
    if prev == 0:
        raise RuntimeError(f"SetThreadAffinityMask failed for APIC {apic_id} (err={ctypes.GetLastError()})")
    time.sleep(0)
    cur = GetCurrentProcessorNumber()
    if cur != apic_id:
        raise RuntimeError(f"Affinity verification mismatch: expected APIC {apic_id}, got {cur}")


def stress_burst(burst_s, op_idx_ref, stop_check):
    t_end = time.perf_counter() + burst_s
    while time.perf_counter() < t_end:
        if stop_check():
            break
        kind = op_idx_ref[0] % 3
        op_idx_ref[0] += 1
        if kind == 0:
            for _ in range(12):
                if hashlib.sha256(_SHA_BUF).digest() != _SHA_REF:
                    raise AssertionError("SHA-256 verification mismatch")
        elif kind == 1:
            z = zlib.compress(_ZLIB_BUF, level=6)
            if zlib.decompress(z) != _ZLIB_BUF:
                raise AssertionError("zlib roundtrip mismatch")
            c = lzma.compress(_COMP_BUF[: 256 * 1024], preset=1)
            if lzma.decompress(c) != _COMP_BUF[: 256 * 1024]:
                raise AssertionError("lzma roundtrip mismatch")
        else:
            acc = 0.0
            for x in range(1, 3000):
                acc += math.sqrt(x * 1.125) * math.sin(x * 0.01) + math.cos(x * 0.02)
            if not math.isfinite(acc):
                raise AssertionError("Non-finite FP result")
            if pow(0x123456789ABCDEF1, 65537, (1 << 127) - 1) == 0:
                raise AssertionError("Unexpected zero pow")


def run_pinned_thread(apic_id, duration_s, stop_file=None, err_list=None):
    try:
        pin_current_thread(apic_id)
        t_done = time.perf_counter() + duration_s
        op_idx = [apic_id]
        pulse_i = 0

        def should_stop():
            return bool(stop_file and os.path.exists(stop_file))

        while time.perf_counter() < t_done and not should_stop():
            rem = t_done - time.perf_counter()
            b_s, sl_s = PULSE_PATTERN[pulse_i % len(PULSE_PATTERN)]
            pulse_i += 1
            stress_burst(min(b_s, rem), op_idx, should_stop)
            rem = t_done - time.perf_counter()
            if rem > 0.005 and not should_stop():
                time.sleep(min(sl_s, rem))
    except Exception as exc:
        if err_list is not None:
            err_list.append((apic_id, repr(exc)))
        else:
            raise


def run_apics(logger, label, phys_core, apics, duration_s, stop_file=None):
    logger.write(f"START {label} phys_core={phys_core} apics={apics} dur={duration_s:.1f}s")
    errs = []
    threads = [
        threading.Thread(target=run_pinned_thread, args=(apic, duration_s, stop_file, errs), daemon=True)
        for apic in apics
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errs:
        raise RuntimeError(f"Worker failure on phys_core={phys_core} apics={apics}: {errs}")
    logger.write(f"DONE  {label} phys_core={phys_core} apics={apics}")


def run_stage1(logger, cores):
    for idx, phys in enumerate(cores):
        t0, t1 = 2 * phys, 2 * phys + 1
        d_each = 25.0 if idx == 0 else 18.0
        run_apics(logger, "stage1", phys, [t0], d_each)
        run_apics(logger, "stage1", phys, [t1], d_each if idx == 0 else 17.0)
        if idx == 0:
            run_apics(logger, "stage1", phys, [t0, t1], 25.0)


def run_stage2(logger, total_cores):
    for phys in range(total_cores):
        t0, t1 = 2 * phys, 2 * phys + 1
        run_apics(logger, "stage2", phys, [t0], 7.0)
        run_apics(logger, "stage2", phys, [t1], 5.0)
        run_apics(logger, "stage2", phys, [t0, t1], 3.0)


def run_continuous(logger, cores, duration_s, stop_file):
    t_end = time.monotonic() + duration_s
    cycle = 0
    while time.monotonic() < t_end:
        if stop_file and os.path.exists(stop_file):
            return
        for phys in cores:
            for apic in (2 * phys, 2 * phys + 1):
                if (stop_file and os.path.exists(stop_file)) or time.monotonic() >= t_end:
                    return
                rem = min(3.0, max(0.2, t_end - time.monotonic()))
                run_apics(logger, f"cont_c{cycle}", phys, [apic], rem, stop_file=stop_file)
        cycle += 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["stage1", "stage2", "continuous"], required=True)
    ap.add_argument("--cores", default="3,0,1,5")
    ap.add_argument("--duration", type=float, default=600.0)
    ap.add_argument("--stop-file", default=None)
    ap.add_argument("--log", default=None)
    args = ap.parse_args()

    logger = LogWriter(args.log)
    logical = os.cpu_count() or 16
    phys_count = max(1, logical // 2)
    cores = [int(x.strip()) for x in args.cores.split(",") if x.strip()]
    logger.write(f"BOOT_STRESS mode={args.mode} logical_cpus={logical}")
    if args.mode == "stage1":
        run_stage1(logger, cores)
    elif args.mode == "stage2":
        run_stage2(logger, phys_count)
    else:
        run_continuous(logger, cores, args.duration, args.stop_file)
    logger.write(f"FINISH mode={args.mode}")


if __name__ == "__main__":
    main()
