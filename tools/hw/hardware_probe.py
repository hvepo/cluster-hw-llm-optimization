#!/usr/bin/env python3
"""Universal Hardware, BIOS, Driver, Storage, Power Plan, Event Log & HWiNFO64 Profiler for any cluster node.

Works across Windows, WSL, Linux, and macOS, inspecting AMD/Intel CPUs & chipsets,
NVIDIA/Intel Arc/AMD Radeon GPUs, RAM topology, PCIe/ReBAR, power plans, and stability logs.
"""
import argparse
import json
import os
import platform
import subprocess
import sys

TOOLS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, TOOLS_DIR)
from cluster_lib import (  # noqa: E402
    detect_platform,
    load_cluster_config,
    resolve_afterburner_cfg,
    resolve_host_binary,
    resolve_local_lmstudio_home,
)


def run_ps_probe(days=14):
    ps = f"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$os = Get-CimInstance Win32_OperatingSystem
$cpu = Get-CimInstance Win32_Processor | Select-Object Name, Manufacturer, NumberOfCores, NumberOfLogicalProcessors, MaxClockSpeed
$mb = Get-CimInstance Win32_BaseBoard | Select-Object Manufacturer, Product
$bios = Get-CimInstance Win32_BIOS | Select-Object Manufacturer, SMBIOSBIOSVersion, ReleaseDate
$ram = Get-CimInstance Win32_PhysicalMemory | Select-Object DeviceLocator, Manufacturer, PartNumber, ConfiguredClockSpeed, Speed, Capacity
$gpus = Get-CimInstance Win32_VideoController | Select-Object Name, DriverVersion, DriverDate, AdapterRAM, CurrentHorizontalResolution, CurrentVerticalResolution, CurrentRefreshRate
$disks = Get-PhysicalDisk -ErrorAction SilentlyContinue | Select-Object FriendlyName, MediaType, BusType, HealthStatus, Size
$vols = Get-Volume -ErrorAction SilentlyContinue | Where-Object {{ $_.DriveLetter }} | Select-Object DriveLetter, FileSystemLabel, SizeRemaining, Size
$batt = Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue | Select-Object Name, BatteryStatus, EstimatedChargeRemaining
$dg = Get-CimInstance -ClassName Win32_DeviceGuard -Namespace root\\Microsoft\\Windows\\DeviceGuard -ErrorAction SilentlyContinue | Select-Object SecurityServicesRunning, VirtualizationBasedSecurityStatus
$hags = (Get-ItemProperty 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\GraphicsDrivers' -ErrorAction SilentlyContinue).HwSchMode
$plan = (powercfg /getactivescheme) -join ' '
$aspm = ((powercfg /query SCHEME_CURRENT SUB_PCIEXPRESS ASPM | Select-String 'Current AC Power Setting Index').Line)
if ($aspm) {{ $aspm = $aspm.Trim() }}
$mincpu = ((powercfg /query SCHEME_CURRENT SUB_PROCESSOR PROCTHROTTLEMIN | Select-String 'Current AC Power Setting Index').Line)
if ($mincpu) {{ $mincpu = $mincpu.Trim() }}
$drivers = Get-CimInstance Win32_PnPSignedDriver | Where-Object {{
    ($_.DeviceName -match 'PPM|3D V-Cache|Ryzen|Chipset|PCI|PSP|GPIO|SMBus|Dynamic Tuning|NPU|AI Boost|Arc|NVIDIA|Radeon|NVMe|Management Engine') -and
    ($_.DeviceName -notmatch 'Audio|USB Root')
}} | Select-Object DeviceName, Manufacturer, DriverVersion, DriverDate | Sort-Object DeviceName -Unique
$since = (Get-Date).AddDays(-{int(days)})
$whea = @(Get-WinEvent -FilterHashtable @{{LogName='System'; ProviderName='Microsoft-Windows-WHEA-Logger'; StartTime=$since}} -ErrorAction SilentlyContinue | Where-Object {{ $_.Id -eq 18 -or $_.Id -eq 19 }})
$wheaList = $whea | ForEach-Object {{
    $apic = if ($_.Message -match 'Processor APIC ID:\\s*(\\d+)') {{ [int]$Matches[1] }} else {{ -1 }}
    [PSCustomObject]@{{ Id = $_.Id; Apic = $apic; Time = $_.TimeCreated.ToString('o') }}
}}
$gpuEv = @(Get-WinEvent -FilterHashtable @{{LogName='System'; StartTime=$since}} -ErrorAction SilentlyContinue | Where-Object {{
    $_.ProviderName -match 'nvlddmkm|igdkmdn|amdkmdag|Display' -and $_.Level -le 3
}})
$kp41 = @(Get-WinEvent -FilterHashtable @{{LogName='System'; ProviderName='Microsoft-Windows-Kernel-Power'; Id=41; StartTime=$since}} -ErrorAction SilentlyContinue).Count
$res = [PSCustomObject]@{{
    os = $os.Caption; version = $os.Version; build = $os.BuildNumber; last_boot = $os.LastBootUpTime.ToString('o');
    total_ram_gib = [math]::Round(($os.TotalVisibleMemorySize * 1KB) / 1GB, 2);
    free_ram_gib = [math]::Round(($os.FreePhysicalMemory * 1KB) / 1GB, 2);
    cpu = $cpu; motherboard = $mb; bios = $bios; ram_dimms = $ram;
    video_controllers = $gpus; disks = $disks; volumes = $vols; battery = $batt;
    vbs = $dg; hags = $hags; power_plan = $plan; aspm = $aspm; min_cpu = $mincpu;
    platform_drivers = $drivers;
    stability_events = [PSCustomObject]@{{
        window_days = {int(days)};
        whea_18_19_count = $whea.Count;
        whea_events = @($wheaList);
        gpu_driver_error_count = $gpuEv.Count;
        kernel_power_41_count = $kp41
    }}
}}
$res | ConvertTo-Json -Depth 6 -Compress
exit 0
"""
    ps_bin = resolve_host_binary("powershell.exe")
    r = subprocess.run([ps_bin, "-NoProfile", "-Command", ps], stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=40)
    if r.returncode != 0 or not r.stdout.strip():
        return {"error": (r.stderr or r.stdout).strip()}
    return json.loads(r.stdout.strip().splitlines()[-1])


def query_nvidia_gpu():
    try:
        smi = resolve_host_binary("nvidia-smi")
        out = subprocess.check_output(
            [
                smi,
                "--query-gpu=name,driver_version,pstate,clocks.gr,clocks.mem,clocks.max.mem,memory.total,memory.used,power.draw,power.limit,temperature.gpu,clocks_event_reasons.active,pcie.link.gen.current,pcie.link.gen.max,pcie.link.width.current,pcie.link.width.max",
                "--format=csv,noheader,nounits",
            ],
            stdin=subprocess.DEVNULL,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).strip()
        if not out:
            return None
        p = [x.strip() for x in out.splitlines()[0].split(",")]
        bar1_mib = None
        try:
            q = subprocess.check_output([smi, "-q", "-d", "MEMORY"], stdin=subprocess.DEVNULL, text=True, stderr=subprocess.DEVNULL, timeout=5)
            lines = q.splitlines()
            for idx, ln in enumerate(lines):
                if "BAR1 Memory Usage" in ln and idx + 1 < len(lines):
                    t_ln = lines[idx + 1]
                    if ":" in t_ln:
                        bar1_mib = t_ln.split(":", 1)[1].strip()
                    break
        except Exception:
            pass
        return {
            "name": p[0], "driver_version": p[1], "pstate": p[2],
            "gr_mhz": p[3], "mem_mhz": p[4], "stock_mem_mhz": p[5],
            "vram_total_mib": p[6], "vram_used_mib": p[7],
            "power_w": p[8], "power_limit_w": p[9], "temp_c": p[10], "throttle": p[11],
            "pcie_current": f"Gen{p[12]} x{p[14]}", "pcie_max": f"Gen{p[13]} x{p[15]}",
            "bar1_total": bar1_mib,
        }
    except Exception:
        return None


def analyze_hwinfo(path, whea_apics=None):
    d = json.load(open(path, encoding="utf-8", errors="replace"))
    cpus = d.get("CPU", {}).get("Central Processor(s)", {})
    cpu_key = next((k for k in cpus if k != "CPU Unit Count"), None)
    cpu_info = cpus.get(cpu_key, {}) if cpu_key else {}
    gen = cpu_info.get("General Information", {})
    op = cpu_info.get("Operating Points", {})

    def parse_order(s):
        return [int(x.strip()) for x in str(s).split(",") if x.strip().isdigit()]

    perf_order = parse_order(gen.get("Core Performance Order", ""))
    cppc_order = parse_order(gen.get("Core Performance Order (CPPC)", ""))

    culprit_phys = sorted({a // 2 for a in (whea_apics or []) if a >= 0})
    co_table = {}
    if perf_order or cppc_order:
        top_cppc_0idx = [c - 1 for c in cppc_order[:4]]
        for c0 in range(max(len(perf_order), len(cppc_order))):
            if c0 in culprit_phys:
                rec = "0 (WHEA culprit; +5 only if recurs)"
            elif c0 in top_cppc_0idx:
                rec = "-5 (top star core)"
            else:
                rec = "-10 (test -15 only after CoreCycler)"
            co_table[f"Core {c0} (HWiNFO #{c0+1})"] = rec

    mem_perf = d.get("MEMORY", {}).get("Memory", {}).get("Current Performance Settings", {})
    return {
        "cpu": cpu_key,
        "stepping": gen.get("CPU Stepping"),
        "microcode": gen.get("Microcode Update Revision"),
        "smu": gen.get("SMU Firmware Revision"),
        "fmax": op.get("CPU Boost Max (Fmax)"),
        "ppt": gen.get("CPU Package Power Tracking (PPT)"),
        "tdc": gen.get("CPU Thermal Design Current (TDC)"),
        "edc": gen.get("CPU Electrical Design Current (EDC)"),
        "vddp": gen.get("cLDO VDDP"),
        "vddg_iod": gen.get("cLDO VDDG IOD"),
        "vddg_ccd": gen.get("cLDO VDDG CCD"),
        "perf_order_1idx": perf_order,
        "cppc_order_1idx": cppc_order,
        "recommended_0idx_co": co_table,
        "memory_timings": {
            "clock": mem_perf.get("Current Memory Clock"),
            "primary": mem_perf.get("Current Timing (tCAS-tRCD-tRP-tRAS)"),
            "cr": mem_perf.get("Command Rate (CR)"),
            "tRC": mem_perf.get("Row Cycle Time (tRC)"),
            "tRFC": mem_perf.get("Refresh Cycle Time (tRFC)"),
            "tFAW": mem_perf.get("Four Activate Window (tFAW)"),
        },
    }


def collect_node_hardware_profile(node=None, days=14, hwinfo_path=None):
    cfg = load_cluster_config()
    plat = detect_platform()
    target_node = node or cfg.get("local_node", "torre")
    lms_home = resolve_local_lmstudio_home(cfg, node=target_node)
    default_dir = os.path.join(lms_home, ".internal", "user-concrete-model-default-config")
    default_cfgs = 0
    if os.path.isdir(default_dir):
        for _, _, files in os.walk(default_dir):
            default_cfgs += sum(1 for f in files if f.endswith(".json"))

    out = {
        "node": target_node,
        "platform": plat,
        "host_arch": platform.machine(),
        "nvidia_gpu": query_nvidia_gpu(),
        "lmstudio_default_configs": default_cfgs,
        "configured_node_spec": cfg.get("nodes", {}).get(target_node),
    }

    ab_path = resolve_afterburner_cfg(cfg)
    if ab_path:
        try:
            from gpu_oc_verify import decode_afterburner_vf
            out["afterburner_vf"] = decode_afterburner_vf(ab_path)
        except Exception:
            pass

    if plat in ("windows", "wsl"):
        out["windows_host"] = run_ps_probe(days=days)
        ev = out["windows_host"].get("stability_events") or {}
        whea_apics = [e.get("Apic") for e in (ev.get("whea_events") or []) if isinstance(e, dict)]
    else:
        whea_apics = []

    if hwinfo_path:
        out["hwinfo"] = analyze_hwinfo(hwinfo_path, whea_apics=whea_apics)
    return out


def main():
    ap = argparse.ArgumentParser(description="Universal Hardware, BIOS, Driver, and Stability Profiler for any cluster node")
    ap.add_argument("--node", help="Cluster node name (defaults to local_node in cluster.local.json)")
    ap.add_argument("--days", type=int, default=14, help="Event Log lookback window in days")
    ap.add_argument("--hwinfo", help="Optional path to HWiNFO64 JSON export")
    ap.add_argument("--out", help="Optional path to write node hardware profile JSON")
    args = ap.parse_args()

    profile = collect_node_hardware_profile(node=args.node, days=args.days, hwinfo_path=args.hwinfo)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(profile, f, indent=2)
    print(json.dumps(profile, indent=2))


if __name__ == "__main__":
    main()
