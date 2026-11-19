#!/usr/bin/env python3
"""Measure NVIDIA CUDA device-to-device memory bandwidth using nvcuda.dll via ctypes.

Run from Windows Python (so nvcuda.dll is directly accessible):
  python.exe bw_test.py
"""
import ctypes
import subprocess
import time

c_int = ctypes.c_int
c_uint = ctypes.c_uint
c_uint64 = ctypes.c_uint64
c_size_t = ctypes.c_size_t
c_void_p = ctypes.c_void_p
c_float = ctypes.c_float
c_ubyte = ctypes.c_ubyte


def load_cuda():
    cuda = ctypes.WinDLL("nvcuda.dll")

    def sym(names):
        for n in names:
            if hasattr(cuda, n):
                return getattr(cuda, n)
        raise AttributeError(f"None of {names} found in nvcuda.dll")

    api = {
        "cuInit": sym(["cuInit"]),
        "cuDeviceGet": sym(["cuDeviceGet"]),
        "cuDeviceGetName": sym(["cuDeviceGetName"]),
        "cuCtxCreate": sym(["cuCtxCreate_v2", "cuCtxCreate"]),
        "cuCtxDestroy": sym(["cuCtxDestroy_v2", "cuCtxDestroy"]),
        "cuCtxSynchronize": sym(["cuCtxSynchronize"]),
        "cuMemAlloc": sym(["cuMemAlloc_v2", "cuMemAlloc"]),
        "cuMemFree": sym(["cuMemFree_v2", "cuMemFree"]),
        "cuMemsetD8": sym(["cuMemsetD8_v2", "cuMemsetD8"]),
        "cuMemcpyDtoDAsync": sym(["cuMemcpyDtoDAsync_v2", "cuMemcpyDtoDAsync"]),
        "cuEventCreate": sym(["cuEventCreate"]),
        "cuEventRecord": sym(["cuEventRecord"]),
        "cuEventSynchronize": sym(["cuEventSynchronize"]),
        "cuEventElapsedTime": sym(["cuEventElapsedTime"]),
        "cuEventDestroy": sym(["cuEventDestroy_v2", "cuEventDestroy"]),
    }

    api["cuInit"].argtypes = [c_uint]
    api["cuDeviceGet"].argtypes = [ctypes.POINTER(c_int), c_int]
    api["cuDeviceGetName"].argtypes = [ctypes.c_char_p, c_int, c_int]
    api["cuCtxCreate"].argtypes = [ctypes.POINTER(c_void_p), c_uint, c_int]
    api["cuCtxDestroy"].argtypes = [c_void_p]
    api["cuCtxSynchronize"].argtypes = []
    api["cuMemAlloc"].argtypes = [ctypes.POINTER(c_uint64), c_size_t]
    api["cuMemFree"].argtypes = [c_uint64]
    api["cuMemsetD8"].argtypes = [c_uint64, c_ubyte, c_size_t]
    api["cuMemcpyDtoDAsync"].argtypes = [c_uint64, c_uint64, c_size_t, c_void_p]
    api["cuEventCreate"].argtypes = [ctypes.POINTER(c_void_p), c_uint]
    api["cuEventRecord"].argtypes = [c_void_p, c_void_p]
    api["cuEventSynchronize"].argtypes = [c_void_p]
    api["cuEventElapsedTime"].argtypes = [ctypes.POINTER(c_float), c_void_p, c_void_p]
    api["cuEventDestroy"].argtypes = [c_void_p]
    return api


def check(rc, label):
    if rc != 0:
        raise RuntimeError(f"{label} failed with CUDA error code {rc}")


def nvidia_smi_snapshot():
    cmd = [
        "nvidia-smi",
        "--query-gpu=clocks.current.memory,clocks.current.graphics,temperature.gpu,power.draw,clocks_event_reasons.active",
        "--format=csv,noheader",
    ]
    return subprocess.check_output(cmd, text=True).strip()


def main():
    api = load_cuda()
    check(api["cuInit"](0), "cuInit")
    dev = c_int()
    check(api["cuDeviceGet"](ctypes.byref(dev), 0), "cuDeviceGet")
    name_buf = ctypes.create_string_buffer(256)
    check(api["cuDeviceGetName"](name_buf, 256, dev), "cuDeviceGetName")
    print("Device:", name_buf.value.decode("utf-8", "replace"))

    ctx = c_void_p()
    check(api["cuCtxCreate"](ctypes.byref(ctx), 0, dev), "cuCtxCreate")

    size = 512 * 1024 * 1024
    warmup = 5
    iters = 30
    passes = 3

    d_src = c_uint64()
    d_dst = c_uint64()
    ev_start = c_void_p()
    ev_end = c_void_p()

    try:
        check(api["cuMemAlloc"](ctypes.byref(d_src), size), "cuMemAlloc src")
        check(api["cuMemAlloc"](ctypes.byref(d_dst), size), "cuMemAlloc dst")
        check(api["cuMemsetD8"](d_src, 0xA5, size), "cuMemsetD8 src")
        check(api["cuMemsetD8"](d_dst, 0x5A, size), "cuMemsetD8 dst")
        check(api["cuEventCreate"](ctypes.byref(ev_start), 0), "cuEventCreate start")
        check(api["cuEventCreate"](ctypes.byref(ev_end), 0), "cuEventCreate end")
        check(api["cuCtxSynchronize"](), "cuCtxSynchronize init")

        print("Idle snapshot:", nvidia_smi_snapshot())

        results = []
        for p in range(1, passes + 1):
            for _ in range(warmup):
                check(api["cuMemcpyDtoDAsync"](d_dst, d_src, size, None), "warmup memcpy")
            check(api["cuCtxSynchronize"](), "warmup sync")

            check(api["cuEventRecord"](ev_start, None), "event record start")
            for _ in range(iters):
                check(api["cuMemcpyDtoDAsync"](d_dst, d_src, size, None), "timed memcpy")
            check(api["cuEventRecord"](ev_end, None), "event record end")
            check(api["cuEventSynchronize"](ev_end), "event sync end")

            ms = c_float()
            check(api["cuEventElapsedTime"](ctypes.byref(ms), ev_start, ev_end), "event elapsed")
            elapsed_s = ms.value / 1000.0
            gb_per_s = (2.0 * size * iters) / elapsed_s / 1e9
            snap = nvidia_smi_snapshot()
            results.append(gb_per_s)
            print(f"Pass {p}: {gb_per_s:.2f} GB/s ({ms.value / iters:.3f} ms/copy) | smi: {snap}")
            time.sleep(0.2)

        avg = sum(results) / len(results)
        spread = (max(results) - min(results)) / avg * 100.0
        print(f"Summary: min={min(results):.2f} median={sorted(results)[1]:.2f} max={max(results):.2f} GB/s (spread={spread:.2f}%)")
    finally:
        if ev_start.value:
            api["cuEventDestroy"](ev_start)
        if ev_end.value:
            api["cuEventDestroy"](ev_end)
        if d_src.value:
            api["cuMemFree"](d_src)
        if d_dst.value:
            api["cuMemFree"](d_dst)
        if ctx.value:
            api["cuCtxDestroy"](ctx)


if __name__ == "__main__":
    main()
