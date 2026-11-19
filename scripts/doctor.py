#!/usr/bin/env python3
"""Self-test suite for cluster-hw-llm-optimization:
- Compiles all Python files (py_compile)
- Validates PowerShell AST on all .ps1 files (when powershell.exe/pwsh is available)
- Validates all JSON configs and Antigravity plugin schema rules (3 focused skills, 0 router leftovers)
- Regression-tests fixed linear multi-stream scaling in tools/llm/ceiling.py
- Executes end-to-end stdio JSON-RPC handshake + tool calls on mcp/cluster_mcp_server.py (7 pure HW/LLM tools)
- Executes contract tests on hooks/pre_tool_guard.py & hooks/session_start.py
- --check-privacy: scans non-ignored/tracked files for personal users, hostnames, Tailscale CGNAT IPs, or secrets
- --live: runs live hw_node_profile, hw_gpu_telemetry, hw_event_log_check, and node_llm_status via MCP
"""
import argparse
import json
import os
import py_compile
import re
import socket
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
EXPECTED_SKILLS = {"hw-node-profiler", "llm-node-finetune", "cluster-install-migrate"}
EXPECTED_MCP_TOOLS = {
    "hw_node_profile",
    "hw_gpu_telemetry",
    "hw_event_log_check",
    "llm_compute_ceiling",
    "node_llm_status",
    "node_best_configs",
    "node_llm_control",
}


def iter_repo_files(include_untracked=True):
    try:
        args = ["git", "-C", REPO_ROOT, "ls-files"]
        if include_untracked:
            args += ["--cached", "--others", "--exclude-standard"]
        out = subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL)
        files = [os.path.join(REPO_ROOT, p) for p in out.splitlines() if p.strip()]
        if files:
            return [f for f in files if os.path.isfile(f)]
    except Exception:
        pass
    out = []
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "backup")]
        for fn in files:
            out.append(os.path.join(root, fn))
    return out


def check_python_compile(files):
    py_files = [f for f in files if f.endswith(".py")]
    for p in py_files:
        py_compile.compile(p, doraise=True)
    print(f"[PASS] py_compile verified {len(py_files)} Python files")


def check_ps1_ast(files):
    ps1_files = [f for f in files if f.endswith(".ps1")]
    if not ps1_files:
        return
    ps_bin = None
    for cand in ("powershell.exe", "pwsh"):
        try:
            subprocess.run([cand, "-NoProfile", "-Command", "exit 0"], stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
            ps_bin = cand
            break
        except Exception:
            pass
    if not ps_bin:
        print(f"[SKIP] No PowerShell binary to AST-check {len(ps1_files)} .ps1 files")
        return
    for p in ps1_files:
        target = p
        if ps_bin == "powershell.exe" and not sys.platform.startswith("win"):
            target = subprocess.check_output(["wslpath", "-w", p], text=True).strip()
        cmd = (
            f"$errs=$null; [void][System.Management.Automation.Language.Parser]::ParseFile('{target}', [ref]$null, [ref]$errs); "
            "if ($errs -and $errs.Count -gt 0) { $errs | ForEach-Object { Write-Error $_.Message }; exit 1 } else { exit 0 }"
        )
        r = subprocess.run([ps_bin, "-NoProfile", "-Command", cmd], stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=15)
        if r.returncode != 0:
            raise AssertionError(f"PowerShell AST error in {p}: {r.stderr or r.stdout}")
    print(f"[PASS] PowerShell AST verified {len(ps1_files)} .ps1 files")


def check_json_and_plugin_schema(files):
    json_files = [f for f in files if f.endswith(".json")]
    for p in json_files:
        json.load(open(p, encoding="utf-8"))
    allowed_plugin_keys = {
        "name", "displayName", "description", "version", "author",
        "homepage", "repository", "suggestedPrompts", "disabled", "logo",
    }
    pj_path = os.path.join(REPO_ROOT, "plugins", "cluster-hw-llm-optimization", "plugin.json")
    pj = json.load(open(pj_path, encoding="utf-8"))
    extra = set(pj.keys()) - allowed_plugin_keys
    assert not extra, f"Disallowed top-level keys in plugin.json: {extra}"
    assert os.path.isfile(os.path.join(REPO_ROOT, "plugins", "cluster-hw-llm-optimization", "hooks.json")), "Missing hooks.json at plugin root"
    rules_path = os.path.join(REPO_ROOT, "plugins", "cluster-hw-llm-optimization", "rules", "AGENTS.md")
    with open(rules_path, encoding="utf-8") as f:
        assert not f.read().lstrip().startswith("---"), "Plugin rules/AGENTS.md must not have YAML frontmatter"

    for sroot in (
        os.path.join(REPO_ROOT, "skills"),
        os.path.join(REPO_ROOT, ".github", "skills"),
        os.path.join(REPO_ROOT, "plugins", "cluster-hw-llm-optimization", "skills"),
    ):
        found = {d for d in os.listdir(sroot) if os.path.isdir(os.path.join(sroot, d))}
        assert found == EXPECTED_SKILLS, f"Skill mismatch in {sroot}: expected {EXPECTED_SKILLS}, got {found}"
    print(f"[PASS] Validated {len(json_files)} JSON files + Antigravity plugin schema + 3 focused skills")


def check_ceiling_math():
    sys.path.insert(0, os.path.join(REPO_ROOT, "tools", "llm"))
    import ceiling
    c_path = os.path.join(REPO_ROOT, "profiles", "torre", "ceiling-torre.json")
    m = json.load(open(c_path, encoding="utf-8"))
    c1 = ceiling.decode_ceiling(m, 0, 1.0, 8192, n=1)
    c2 = ceiling.decode_ceiling(m, 0, 1.0, 8192, n=2)
    c4 = ceiling.decode_ceiling(m, 0, 1.0, 8192, n=4)
    assert c1 < c2 < 2.0 * c1, f"c2 linear check failed: c1={c1}, c2={c2}"
    assert c2 < c4 < 2.0 * c2, f"c4 linear check failed: c2={c2}, c4={c4}"
    print(f"[PASS] ceiling.py linear multi-stream math verified (c1={c1:.1f}, c2={c2:.1f}, c4={c4:.1f})")


def mcp_roundtrip(framing="ndjson", live=False):
    server = os.path.join(REPO_ROOT, "mcp", "cluster_mcp_server.py")
    proc = subprocess.Popen(
        [sys.executable, server],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    def write_msg(obj):
        raw = json.dumps(obj).encode("utf-8")
        if framing == "lsp":
            proc.stdin.write(f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii") + raw)
        else:
            proc.stdin.write(raw + b"\n")
        proc.stdin.flush()

    def read_msg():
        if framing == "lsp":
            hdr = proc.stdout.readline().decode("utf-8")
            assert hdr.lower().startswith("content-length:"), f"Expected Content-Length, got {hdr!r}"
            n = int(hdr.split(":", 1)[1].strip())
            while proc.stdout.readline() not in (b"\r\n", b"\n"):
                pass
            return json.loads(proc.stdout.read(n).decode("utf-8"))
        return json.loads(proc.stdout.readline().decode("utf-8"))

    try:
        write_msg({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}})
        init = read_msg()
        assert init["result"]["serverInfo"]["name"] == "cluster-hw-llm"
        write_msg({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        write_msg({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = {t["name"] for t in read_msg()["result"]["tools"]}
        assert tools == EXPECTED_MCP_TOOLS, f"Expected MCP tools {EXPECTED_MCP_TOOLS}, got {tools}"
        write_msg({"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}})
        res_uris = [r["uri"] for r in read_msg()["result"]["resources"]]
        assert len(res_uris) == 3, f"Expected 3 resources, got {res_uris}"
        write_msg({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "node_best_configs", "arguments": {"node": "torre"}}})
        cat_txt = read_msg()["result"]["content"][0]["text"]
        assert "t-code-chat" in cat_txt
        write_msg({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "llm_compute_ceiling", "arguments": {"profile": "torre"}}})
        ceil_txt = json.loads(read_msg()["result"]["content"][0]["text"])
        assert "decode" in ceil_txt and "prefill" in ceil_txt
        if live:
            for idx, (tname, targs) in enumerate(
                [("hw_node_profile", {"days": 1}), ("hw_gpu_telemetry", {}), ("hw_event_log_check", {"hours": 1}), ("node_llm_status", {})],
                start=10,
            ):
                write_msg({"jsonrpc": "2.0", "id": idx, "method": "tools/call", "params": {"name": tname, "arguments": targs}})
                r = read_msg()
                assert "result" in r, f"Live tool {tname} failed: {r}"
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)
        stderr_out = proc.stderr.read().decode("utf-8", "replace").strip()
        assert proc.returncode == 0, f"MCP server exited {proc.returncode}: {stderr_out}"
    print(f"[PASS] MCP server ({framing}, live={live}) passed initialize + 7 pure HW/LLM tools")


def check_hooks():
    pre = os.path.join(REPO_ROOT, "hooks", "pre_tool_guard.py")
    ses = os.path.join(REPO_ROOT, "hooks", "session_start.py")

    def run_hook(script, payload):
        r = subprocess.run([sys.executable, script], input=json.dumps(payload), text=True, capture_output=True, timeout=10)
        assert r.returncode == 0, f"Hook {script} failed: {r.stderr}"
        return json.loads(r.stdout)

    d1 = run_hook(pre, {"toolCall": {"name": "run_command", "args": {"CommandLine": "rm -rf ~/.lmstudio/.internal/user-concrete-model-default-config"}}})
    assert d1["decision"] == "deny", f"Expected deny, got {d1}"
    d2 = run_hook(pre, {"toolCall": {"name": "run_command", "args": {"CommandLine": "lms.exe unload --all < /dev/null"}}})
    assert d2["decision"] == "ask", f"Expected ask, got {d2}"
    d3 = run_hook(pre, {"toolCall": {"name": "run_command", "args": {"CommandLine": "python3 tools/cluster_ctl.py status"}}})
    assert d3["decision"] == "allow", f"Expected allow, got {d3}"
    s1 = run_hook(ses, {"hookEventName": "SessionStart"})
    assert "injectSteps" in s1 and s1["injectSteps"], f"Expected injectSteps, got {s1}"
    print("[PASS] Hook contracts verified (PreToolUse deny/ask/allow + SessionStart)")


def check_install_matrix():
    sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))
    import cluster_lib

    # 1. Cross-OS path normalization tests
    assert cluster_lib.normalize_local_path(r"C:\Users\Demo\repo", "wsl") == "/mnt/c/Users/Demo/repo"
    assert cluster_lib.normalize_local_path(r"\\wsl.localhost\Ubuntu\home\demo\repo", "wsl") == "/home/demo/repo"
    assert cluster_lib.normalize_local_path("/mnt/d/work/repo", "windows") == r"D:\work\repo"

    install_script = os.path.join(REPO_ROOT, "scripts", "install.py")
    with tempfile.TemporaryDirectory() as tmp:
        posix_home = os.path.join(tmp, "posix_home")
        win_home = os.path.join(tmp, "win_home")
        win_appdata = os.path.join(win_home, "AppData", "Roaming")
        os.makedirs(posix_home, exist_ok=True)
        os.makedirs(win_appdata, exist_ok=True)

        # Pre-seed existing third-party MCP servers to ensure non-destructive merge
        for mcp_p in (
            os.path.join(posix_home, ".config", "Code", "User", "mcp.json"),
            os.path.join(posix_home, ".vscode-server", "data", "User", "mcp.json"),
            os.path.join(win_appdata, "Code", "User", "mcp.json"),
        ):
            os.makedirs(os.path.dirname(mcp_p), exist_ok=True)
            with open(mcp_p, "w", encoding="utf-8") as f:
                json.dump({"inputs": ["keep"], "servers": {"other-srv": {"type": "stdio", "command": "node"}}}, f)

        for oc_p in (
            os.path.join(posix_home, ".config", "opencode", "opencode.json"),
            os.path.join(win_home, ".config", "opencode", "opencode.json"),
        ):
            os.makedirs(os.path.dirname(oc_p), exist_ok=True)
            with open(oc_p, "w", encoding="utf-8") as f:
                json.dump({"theme": "dark", "mcp": {"servers": {"other-oc": {"type": "local", "command": ["node"]}}}}, f)

        env = dict(os.environ)
        env["CLUSTER_INSTALL_POSIX_HOME"] = posix_home
        env["CLUSTER_INSTALL_WIN_HOME"] = win_home
        env["CLUSTER_INSTALL_WIN_APPDATA"] = win_appdata
        env["CLUSTER_INSTALL_SKIP_CONFIG"] = "1"

        # 2. Install across both Windows and WSL for all 3 targets (agy, vscode, opencode)
        r = subprocess.run(
            [sys.executable, install_script, "--os", "windows,wsl", "--targets", "agy,vscode,opencode", "--scope", "user"],
            stdin=subprocess.DEVNULL,
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
        )
        assert r.returncode == 0, f"Matrix install failed: {r.stderr or r.stdout}"

        for home_root in (posix_home, win_home):
            agy_dir = os.path.join(home_root, ".gemini", "config", "plugins", "cluster-hw-llm-optimization")
            assert os.path.isfile(os.path.join(agy_dir, "plugin.json"))
            assert os.path.isfile(os.path.join(agy_dir, "mcp_config.json"))
            assert os.path.isfile(os.path.join(agy_dir, "hooks.json"))
            assert os.path.isfile(os.path.join(agy_dir, "repo_root.txt"))
            assert set(os.listdir(os.path.join(agy_dir, "skills"))) == EXPECTED_SKILLS
            assert set(os.listdir(os.path.join(home_root, ".copilot", "skills"))) == EXPECTED_SKILLS
            assert set(os.listdir(os.path.join(home_root, ".config", "opencode", "skills"))) == EXPECTED_SKILLS
            oc_data = json.load(open(os.path.join(home_root, ".config", "opencode", "opencode.json"), encoding="utf-8"))
            assert oc_data.get("theme") == "dark"
            assert "other-oc" in oc_data["mcp"]["servers"]
            assert "cluster-hw-llm" in oc_data["mcp"]["servers"]

        for mcp_p in (
            os.path.join(posix_home, ".config", "Code", "User", "mcp.json"),
            os.path.join(posix_home, ".vscode-server", "data", "User", "mcp.json"),
            os.path.join(win_appdata, "Code", "User", "mcp.json"),
        ):
            vd = json.load(open(mcp_p, encoding="utf-8"))
            assert vd.get("inputs") == ["keep"]
            assert "other-srv" in vd["servers"]
            assert "cluster-hw-llm" in vd["servers"]

        # 3. Test Windows WSL-Python fallback mode (--win-use-wsl-python)
        r_fb = subprocess.run(
            [sys.executable, install_script, "--os", "windows", "--targets", "all", "--scope", "user", "--win-use-wsl-python"],
            stdin=subprocess.DEVNULL,
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
        )
        assert r_fb.returncode == 0, f"WSL-Python fallback install failed: {r_fb.stderr}"
        win_vsc = json.load(open(os.path.join(win_appdata, "Code", "User", "mcp.json"), encoding="utf-8"))
        assert win_vsc["servers"]["cluster-hw-llm"]["command"] == "wsl.exe"
        win_oc = json.load(open(os.path.join(win_home, ".config", "opencode", "opencode.json"), encoding="utf-8"))
        assert win_oc["mcp"]["servers"]["cluster-hw-llm"]["command"][0] == "wsl.exe"

        # 4. Test clean uninstall across both Windows and WSL
        r_un = subprocess.run(
            [sys.executable, install_script, "--os", "windows,wsl", "--targets", "all", "--scope", "user", "--uninstall"],
            stdin=subprocess.DEVNULL,
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
        )
        assert r_un.returncode == 0, f"Matrix uninstall failed: {r_un.stderr}"
        for home_root in (posix_home, win_home):
            assert not os.path.exists(os.path.join(home_root, ".gemini", "config", "plugins", "cluster-hw-llm-optimization"))
            oc_data = json.load(open(os.path.join(home_root, ".config", "opencode", "opencode.json"), encoding="utf-8"))
            assert "other-oc" in oc_data["mcp"]["servers"]
            assert "cluster-hw-llm" not in oc_data["mcp"]["servers"]
        for mcp_p in (
            os.path.join(posix_home, ".config", "Code", "User", "mcp.json"),
            os.path.join(win_appdata, "Code", "User", "mcp.json"),
        ):
            vd = json.load(open(mcp_p, encoding="utf-8"))
            assert "other-srv" in vd["servers"]
            assert "cluster-hw-llm" not in vd["servers"]

    print("[PASS] Install/Uninstall matrix verified across {windows, wsl} x {agy, vscode, opencode} + wsl.exe fallback")


def check_privacy(files):
    generic_patterns = [
        ("tailscale_cgnat_ip", re.compile(r"\b100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b")),
        ("hardcoded_user_path", re.compile(r"(?:/home/|C:\\\\?Users\\\\?)(?!<USER>)[A-Za-z0-9_.-]{2,}")),
        ("github_token", re.compile(r"\b(?:ghp|gho|github_pat)_[A-Za-z0-9_]{12,}\b")),
        ("openai_token", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
        ("bearer_token", re.compile(r"Bearer\s+[A-Za-z0-9._-]{16,}")),
    ]

    dynamic_terms = set()
    for term in (os.environ.get("USER"), os.environ.get("USERNAME"), socket.gethostname()):
        if term and len(term) >= 3 and term.lower() not in ("root", "user", "localhost", "ubuntu", "pc", "laptop"):
            dynamic_terms.add(term)
    loc_cfg = os.path.join(REPO_ROOT, "cluster.local.json")
    if os.path.isfile(loc_cfg):
        raw_loc = open(loc_cfg, encoding="utf-8", errors="replace").read()
        for m in re.findall(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+\b", raw_loc):
            dynamic_terms.add(m)
        for m in re.findall(r"(?:/home/|/Users/|\\\\Users\\\\|C:\\\\Users\\\\)([A-Za-z0-9_.-]+)", raw_loc):
            if "<" not in m:
                dynamic_terms.add(m)

    hits = []
    for fpath in files:
        try:
            txt = open(fpath, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        rel = os.path.relpath(fpath, REPO_ROOT)
        for label, pat in generic_patterns:
            m = pat.search(txt)
            if m:
                matched = m.group(0)
                if rel == "scripts/doctor.py" or "<" in matched:
                    continue
                hits.append(f"{rel}: matched {label} -> {matched!r}")
        for term in dynamic_terms:
            if term in txt:
                hits.append(f"{rel}: matched local machine identifier {term!r}")
    assert not hits, "Privacy scan failed:\n  " + "\n  ".join(hits)
    print(f"[PASS] Privacy scan clean across {len(files)} repo files (checked {len(dynamic_terms)} dynamic identifiers)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--check-privacy", action="store_true")
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()

    files = iter_repo_files(include_untracked=True)
    if args.all or not (args.check_privacy or args.live):
        check_python_compile(files)
        check_ps1_ast(files)
        check_json_and_plugin_schema(files)
        check_ceiling_math()
        mcp_roundtrip("ndjson", live=False)
        mcp_roundtrip("lsp", live=False)
        check_hooks()
        check_install_matrix()
    if args.check_privacy:
        check_privacy(files)
    if args.live:
        mcp_roundtrip("ndjson", live=True)
    print("DOCTOR_ALL_GREEN")


if __name__ == "__main__":
    main()
