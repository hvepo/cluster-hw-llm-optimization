#!/usr/bin/env python3
"""Cross-OS configuration, LM Studio path resolution, and HTTP helpers for cluster-hw-llm-optimization."""
import json
import os
import platform
import re
import shutil
import subprocess
import urllib.error
import urllib.request

_WIN_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")
_WSL_UNC_RE = re.compile(r"^(?:\\\\|//)(?:wsl\.localhost|wsl\$)[\\/][^\\/]+([\\/].*)?$", re.IGNORECASE)
_MNT_DRIVE_RE = re.compile(r"^/mnt/([a-zA-Z])(?:/(.*))?$")


def detect_platform():
    sysname = platform.system().lower()
    if sysname.startswith("windows"):
        return "windows"
    if sysname == "darwin":
        return "macos"
    try:
        with open("/proc/version", "r", encoding="utf-8", errors="ignore") as f:
            v = f.read().lower()
        if "microsoft" in v or "wsl" in v:
            return "wsl"
    except OSError:
        pass
    return "linux"


def resolve_host_binary(name):
    """Locate host binaries (nvidia-smi, powershell.exe, cmd.exe, wsl.exe, wslpath) even under stripped IDE MCP PATHs."""
    found = shutil.which(name)
    if found:
        return found
    plat = detect_platform()
    low = name.lower()
    candidates = []
    if low in ("nvidia-smi", "nvidia-smi.exe"):
        if plat == "wsl":
            candidates = [
                "/usr/lib/wsl/lib/nvidia-smi",
                "/usr/bin/nvidia-smi",
                "/mnt/c/Windows/System32/nvidia-smi.exe",
                "/mnt/c/Program Files/NVIDIA Corporation/NVSMI/nvidia-smi.exe",
            ]
        elif plat == "windows":
            sysroot = os.environ.get("SystemRoot") or r"C:\Windows"
            pfiles = os.environ.get("ProgramFiles") or r"C:\Program Files"
            candidates = [
                os.path.join(sysroot, "System32", "nvidia-smi.exe"),
                os.path.join(pfiles, "NVIDIA Corporation", "NVSMI", "nvidia-smi.exe"),
            ]
        else:
            candidates = ["/usr/bin/nvidia-smi", "/usr/local/bin/nvidia-smi"]
    elif low in ("powershell.exe", "powershell"):
        if plat == "wsl":
            candidates = [
                "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
                "/mnt/c/Program Files/PowerShell/7/pwsh.exe",
            ]
        elif plat == "windows":
            sysroot = os.environ.get("SystemRoot") or r"C:\Windows"
            pfiles = os.environ.get("ProgramFiles") or r"C:\Program Files"
            candidates = [
                os.path.join(sysroot, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
                os.path.join(pfiles, "PowerShell", "7", "pwsh.exe"),
            ]
    elif low in ("cmd.exe", "cmd"):
        if plat == "wsl":
            candidates = ["/mnt/c/Windows/System32/cmd.exe"]
        elif plat == "windows":
            sysroot = os.environ.get("SystemRoot") or r"C:\Windows"
            candidates = [os.path.join(sysroot, "System32", "cmd.exe")]
    elif low in ("wsl.exe", "wsl"):
        if plat == "windows":
            sysroot = os.environ.get("SystemRoot") or r"C:\Windows"
            candidates = [os.path.join(sysroot, "System32", "wsl.exe")]
        elif plat == "wsl":
            candidates = ["/mnt/c/Windows/System32/wsl.exe"]
    elif low == "wslpath":
        candidates = ["/usr/bin/wslpath", "/bin/wslpath"]

    for cand in candidates:
        if os.path.isfile(cand):
            return cand
    return name


def normalize_local_path(raw_path, target_plat=None):
    """Translate a filesystem path across Windows <-> WSL/POSIX boundaries for the current (or target) platform."""
    if not raw_path:
        return raw_path
    p = str(raw_path).strip()
    if not p:
        return p
    plat = target_plat or detect_platform()

    if plat in ("wsl", "linux", "macos"):
        m_unc = _WSL_UNC_RE.match(p)
        if m_unc:
            tail = (m_unc.group(1) or "/").replace("\\", "/")
            return tail if tail.startswith("/") else "/" + tail
        m_drv = _WIN_DRIVE_RE.match(p)
        if m_drv:
            drv = m_drv.group(1).lower()
            rest = m_drv.group(2).replace("\\", "/").lstrip("/")
            return f"/mnt/{drv}/{rest}" if rest else f"/mnt/{drv}"
        return p

    if plat == "windows":
        m_mnt = _MNT_DRIVE_RE.match(p)
        if m_mnt:
            drv = m_mnt.group(1).upper()
            rest = (m_mnt.group(2) or "").replace("/", "\\")
            return f"{drv}:\\{rest}" if rest else f"{drv}:\\"
        if p.startswith("/"):
            distro = os.environ.get("WSL_DISTRO_NAME")
            if not distro:
                try:
                    wsl_bin = resolve_host_binary("wsl.exe")
                    w = subprocess.check_output(
                        [wsl_bin, "-e", "wslpath", "-w", p],
                        stdin=subprocess.DEVNULL,
                        text=True,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                    ).strip()
                    if w:
                        return w
                except Exception:
                    distro = "Ubuntu"
            if distro:
                rel = p.lstrip("/").replace("/", "\\")
                return f"\\\\wsl.localhost\\{distro}\\{rel}"
        return p

    return p


def find_repo_root(start=None):
    env_root = normalize_local_path(os.environ.get("CLUSTER_REPO_ROOT"))
    if env_root and os.path.isdir(env_root):
        return os.path.abspath(env_root)

    here = os.path.abspath(start or os.path.dirname(__file__))
    for candidate in (here, os.path.dirname(here), os.path.dirname(os.path.dirname(here))):
        marker = os.path.join(candidate, "repo_root.txt")
        if os.path.isfile(marker):
            with open(marker, "r", encoding="utf-8") as f:
                p = normalize_local_path(f.read().strip())
            if p and os.path.isdir(p):
                return os.path.abspath(p)
        if os.path.isfile(os.path.join(candidate, "cluster.example.json")):
            return candidate
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _deep_merge(base, over):
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _win_userprofile_from_wsl():
    if os.environ.get("CLUSTER_WIN_HOME"):
        return os.environ["CLUSTER_WIN_HOME"]
    try:
        cmd_bin = resolve_host_binary("cmd.exe")
        out = subprocess.check_output(
            [cmd_bin, "/c", "echo", "%USERPROFILE%"],
            stdin=subprocess.DEVNULL,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).strip()
        if out and "%" not in out:
            return normalize_local_path(out, "wsl")
    except Exception:
        pass
    return None


def resolve_path_token(val, plat=None):
    if not val or (val.startswith("<") and val.endswith(">")):
        return None
    return val


def load_cluster_config(repo_root=None):
    root = find_repo_root(repo_root)
    ex_path = os.path.join(root, "cluster.example.json")
    loc_path = os.path.join(root, "cluster.local.json")
    cfg = json.load(open(ex_path, encoding="utf-8"))
    if os.path.isfile(loc_path):
        cfg = _deep_merge(cfg, json.load(open(loc_path, encoding="utf-8")))

    # Environment variable overrides
    if os.environ.get("CLUSTER_LOCAL_NODE"):
        cfg["local_node"] = os.environ["CLUSTER_LOCAL_NODE"]
    if os.environ.get("CLUSTER_TORRE_URL"):
        cfg["nodes"]["torre"]["base_url"] = os.environ["CLUSTER_TORRE_URL"]
    if os.environ.get("CLUSTER_PORTATIL_URL"):
        cfg["nodes"]["portatil"]["base_url"] = os.environ["CLUSTER_PORTATIL_URL"]
    if os.environ.get("CLUSTER_LMS_HOME"):
        local_node = cfg.get("local_node", "torre")
        if local_node in cfg.get("nodes", {}):
            cfg["nodes"][local_node].setdefault("paths", {})["lmstudio_home"] = os.environ["CLUSTER_LMS_HOME"]
    return cfg


def resolve_local_lmstudio_home(cfg=None, node=None):
    plat = detect_platform()
    if cfg is None:
        cfg = load_cluster_config()
    node = node or cfg.get("local_node", "torre")
    paths = cfg.get("nodes", {}).get(node, {}).get("paths", {})
    explicit = os.environ.get("CLUSTER_LMS_HOME") or resolve_path_token(paths.get("lmstudio_home"), plat)
    if explicit:
        return explicit
    if plat in ("windows", "linux", "macos"):
        return os.path.join(os.path.expanduser("~"), ".lmstudio")
    # WSL: try native Linux first if running there, else Windows host %USERPROFILE%\.lmstudio
    wsl_home = os.path.join(os.path.expanduser("~"), ".lmstudio")
    if os.path.isdir(os.path.join(wsl_home, ".internal")):
        return wsl_home
    win_home = _win_userprofile_from_wsl()
    if win_home:
        return os.path.join(win_home, ".lmstudio")
    return wsl_home


def resolve_lms_cli(cfg=None, node=None):
    plat = detect_platform()
    if cfg is None:
        cfg = load_cluster_config()
    node = node or cfg.get("local_node", "torre")
    paths = cfg.get("nodes", {}).get(node, {}).get("paths", {})
    explicit = os.environ.get("CLUSTER_LMS_CLI") or resolve_path_token(paths.get("lms_cli"), plat)
    if explicit and os.path.exists(explicit):
        return explicit
    lms_home = resolve_local_lmstudio_home(cfg, node=node)
    exe = "lms.exe" if plat in ("windows", "wsl") else "lms"
    cand = os.path.join(lms_home, "bin", exe)
    if os.path.exists(cand):
        return cand
    return exe


def resolve_win_python(cfg=None):
    if os.environ.get("CLUSTER_WIN_PYTHON"):
        return os.environ["CLUSTER_WIN_PYTHON"]
    if cfg is None:
        cfg = load_cluster_config()
    for n in cfg.get("nodes", {}).values():
        p = resolve_path_token(n.get("paths", {}).get("win_python"))
        if p and os.path.exists(p):
            return p
    plat = detect_platform()
    if plat == "windows":
        import sys
        return sys.executable
    if plat == "wsl":
        win_home = _win_userprofile_from_wsl()
        if win_home:
            for rel in (
                "AppData/Local/Python/bin/python.exe",
                "AppData/Local/Programs/Python/Python315/python.exe",
                "AppData/Local/Programs/Python/Python314/python.exe",
                "AppData/Local/Programs/Python/Python313/python.exe",
                "AppData/Local/Programs/Python/Python312/python.exe",
                "AppData/Local/Programs/Python/Python311/python.exe",
            ):
                cand = os.path.join(win_home, rel)
                if os.path.isfile(cand):
                    return cand
        for sys_cand in (
            "/mnt/c/Python315/python.exe",
            "/mnt/c/Python314/python.exe",
            "/mnt/c/Python313/python.exe",
            "/mnt/c/Python312/python.exe",
        ):
            if os.path.isfile(sys_cand):
                return sys_cand
    return "python.exe"


def resolve_afterburner_cfg(cfg=None):
    if os.environ.get("CLUSTER_AFTERBURNER_CFG"):
        return os.environ["CLUSTER_AFTERBURNER_CFG"]
    if cfg is None:
        cfg = load_cluster_config()
    torre_paths = cfg.get("nodes", {}).get("torre", {}).get("paths", {})
    explicit = resolve_path_token(torre_paths.get("afterburner_cfg"))
    if explicit and os.path.isfile(explicit):
        return explicit
    base = (
        r"C:\Program Files (x86)\MSI Afterburner\Profiles"
        if detect_platform() == "windows"
        else "/mnt/c/Program Files (x86)/MSI Afterburner/Profiles"
    )
    if os.path.isdir(base):
        for fn in sorted(os.listdir(base)):
            if fn.startswith("VEN_10DE") and fn.endswith(".cfg"):
                return os.path.join(base, fn)
    return None


def node_for_model(model_key, cfg=None):
    if cfg is None:
        cfg = load_cluster_config()
    for node_name, node_cfg in cfg.get("nodes", {}).items():
        for pfx in node_cfg.get("model_prefixes", []):
            if model_key.startswith(pfx):
                return node_name
    return None


def node_url(node_name, cfg=None):
    if cfg is None:
        cfg = load_cluster_config()
    n = cfg.get("nodes", {}).get(node_name)
    if not n:
        raise KeyError(f"Unknown cluster node {node_name!r}")
    url = (n.get("base_url") or "").rstrip("/")
    if not url or "<" in url:
        raise RuntimeError(
            f"Node {node_name!r} has unconfigured endpoint {url!r}. "
            f"Create cluster.local.json (from cluster.example.json) or set CLUSTER_{node_name.upper()}_URL."
        )
    return url


def api_request(base_url, method, path, payload=None, timeout=30):
    url = base_url.rstrip("/") + path
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {e.code} on {method} {url}: {body}") from e


def get_api_models(base_url, timeout=8):
    _, data = api_request(base_url, "GET", "/api/v1/models", timeout=timeout)
    return data.get("models", [])


def load_best_config(repo_root=None):
    root = find_repo_root(repo_root)
    return json.load(open(os.path.join(root, "profiles", "best-config.json"), encoding="utf-8"))
