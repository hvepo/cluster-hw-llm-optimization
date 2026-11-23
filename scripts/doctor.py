#!/usr/bin/env python3
"""Self-test suite for cluster-hw-llm-optimization:
- Compiles all Python files (py_compile)
- Validates PowerShell AST on all .ps1 files (when powershell.exe/pwsh is available)
- Validates all JSON configs and Antigravity plugin schema rules (3 focused skills, 0 router leftovers)
- Regression-tests fixed linear multi-stream scaling in tools/llm/ceiling.py
- Executes end-to-end stdio JSON-RPC handshake + tool calls on mcp/cluster_mcp_server.py (9 HW/LLM/Cluster tools)
- Executes contract tests on hooks/pre_tool_guard.py & hooks/session_start.py
- --check-privacy: scans non-ignored/tracked files for personal users, hostnames, Tailscale CGNAT IPs, or secrets
- --live: runs live hw_node_profile, hw_gpu_telemetry, hw_event_log_check, and node_llm_status via MCP
"""
import argparse
import json
import os
import py_compile
import re
import shutil
import socket
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
EXPECTED_SKILLS = {"hw-node-profiler", "llm-node-finetune", "cluster-install-migrate"}
EXPECTED_AGENTS = {"cluster-coder.md", "cluster-fast-agent.md", "cluster-reasoning.md", "cluster-vision.md"}
EXPECTED_MCP_TOOLS = {
    "hw_node_profile",
    "hw_gpu_telemetry",
    "hw_event_log_check",
    "llm_compute_ceiling",
    "node_llm_status",
    "node_best_configs",
    "node_llm_control",
    "cluster_models",
    "cluster_chat",
    "peer_status",
    "peer_send_order",
    "peer_order_status",
    "peer_exec",
    "openclaw_control",
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
    assert len(pj.get("suggestedPrompts", [])) <= 3, "plugin.json suggestedPrompts must have at most 3 entries"
    assert os.path.isfile(os.path.join(REPO_ROOT, "plugins", "cluster-hw-llm-optimization", "hooks.json")), "Missing hooks.json at plugin root"
    rules_path = os.path.join(REPO_ROOT, "plugins", "cluster-hw-llm-optimization", "rules", "AGENTS.md")
    with open(rules_path, encoding="utf-8") as f:
        assert not f.read().lstrip().startswith("---"), "Plugin rules/AGENTS.md must not have YAML frontmatter"

    agents_dir = os.path.join(REPO_ROOT, "plugins", "cluster-hw-llm-optimization", "agents")
    assert os.path.isdir(agents_dir), f"Missing plugin agents directory: {agents_dir}"
    found_agents = {fn for fn in os.listdir(agents_dir) if fn.endswith(".md") and os.path.isfile(os.path.join(agents_dir, fn))}
    assert found_agents == EXPECTED_AGENTS, f"Agent mismatch in {agents_dir}: expected {EXPECTED_AGENTS}, got {found_agents}"
    for af in sorted(EXPECTED_AGENTS):
        with open(os.path.join(agents_dir, af), encoding="utf-8") as f:
            txt = f.read()
        assert txt.startswith("---\n"), f"Agent {af} must start with YAML frontmatter"
        parts = txt.split("---\n", 2)
        assert len(parts) >= 3, f"Agent {af} missing closing YAML frontmatter delimiter"
        fm = parts[1]
        assert "name:" in fm and "description:" in fm, f"Agent {af} frontmatter must declare name and description"

    for sroot in (
        os.path.join(REPO_ROOT, "skills"),
        os.path.join(REPO_ROOT, ".github", "skills"),
        os.path.join(REPO_ROOT, "plugins", "cluster-hw-llm-optimization", "skills"),
    ):
        found = {d for d in os.listdir(sroot) if os.path.isdir(os.path.join(sroot, d))}
        assert found == EXPECTED_SKILLS, f"Skill mismatch in {sroot}: expected {EXPECTED_SKILLS}, got {found}"
    print(f"[PASS] Validated {len(json_files)} JSON files + Antigravity plugin schema + 3 focused skills")

    for node_name in ("torre", "portatil"):
        c_path = os.path.join(REPO_ROOT, "profiles", node_name, "continue-config.yaml")
        assert os.path.isfile(c_path), f"Missing Continue template: {c_path}"
        c_txt = open(c_path, encoding="utf-8").read()
        for tok in ("schema: v1", "cluster/code", "cluster/autocomplete", "cluster/embed", "tool_use", "image_input", "mcpServers:", "cluster-hw-llm"):
            assert tok in c_txt, f"Continue template {c_path} missing {tok!r}"
    print("[PASS] Verified Continue node templates (torre & portatil)")


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
        write_msg({"jsonrpc": "2.0", "id": 31, "method": "resources/templates/list", "params": {}})
        assert read_msg()["result"] == {"resourceTemplates": []}
        write_msg({"jsonrpc": "2.0", "id": 32, "method": "prompts/list", "params": {}})
        assert read_msg()["result"] == {"prompts": []}
        write_msg({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "node_best_configs", "arguments": {"node": "torre"}}})
        cat_txt = read_msg()["result"]["content"][0]["text"]
        assert "t-code-chat" in cat_txt
        write_msg({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "llm_compute_ceiling", "arguments": {"profile": "torre"}}})
        ceil_txt = json.loads(read_msg()["result"]["content"][0]["text"])
        assert "decode" in ceil_txt and "prefill" in ceil_txt
        if live:
            for idx, (tname, targs) in enumerate(
                [
                    ("hw_node_profile", {"days": 1}),
                    ("hw_gpu_telemetry", {}),
                    ("hw_event_log_check", {"hours": 1}),
                    ("node_llm_status", {}),
                    ("cluster_models", {}),
                ],
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
    print(f"[PASS] MCP server ({framing}, live={live}) passed initialize + {len(EXPECTED_MCP_TOOLS)} tools")


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

        win_settings_p = os.path.join(win_appdata, "Code", "User", "settings.json")
        wsl_machine_settings_p = os.path.join(posix_home, ".vscode-server", "data", "Machine", "settings.json")
        os.makedirs(os.path.dirname(win_settings_p), exist_ok=True)
        os.makedirs(os.path.dirname(wsl_machine_settings_p), exist_ok=True)
        with open(win_settings_p, "w", encoding="utf-8") as f:
            json.dump({"workbench.colorTheme": "Dark+", "chat.disableAIFeatures": True}, f)
        with open(wsl_machine_settings_p, "w", encoding="utf-8") as f:
            json.dump({"editor.tabSize": 4}, f)
        cline_gs = os.path.join(posix_home, ".vscode-server", "data", "User", "globalStorage", "saoudrizwan.claude-dev")
        os.makedirs(cline_gs, exist_ok=True)

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
            assert set(os.listdir(os.path.join(agy_dir, "agents"))) == EXPECTED_AGENTS
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

        ws_d = json.load(open(win_settings_p, encoding="utf-8"))
        assert ws_d.get("workbench.colorTheme") == "Dark+"
        assert ws_d.get("chat.disableAIFeatures") is False
        assert "cluster-hw-llm" in ws_d.get("twinny.chatToolsMcpServers", {})
        wms_d = json.load(open(wsl_machine_settings_p, encoding="utf-8"))
        assert wms_d.get("editor.tabSize") == 4
        assert "cluster-hw-llm" in wms_d.get("twinny.chatToolsMcpServers", {})
        cline_cfg = json.load(open(os.path.join(cline_gs, "settings", "cline_mcp_settings.json"), encoding="utf-8"))
        assert "cluster-hw-llm" in cline_cfg.get("mcpServers", {})

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
        ws_d2 = json.load(open(win_settings_p, encoding="utf-8"))
        assert ws_d2.get("workbench.colorTheme") == "Dark+"
        assert "cluster-hw-llm" not in ws_d2.get("twinny.chatToolsMcpServers", {})
        wms_d2 = json.load(open(wsl_machine_settings_p, encoding="utf-8"))
        assert wms_d2.get("editor.tabSize") == 4
        assert "cluster-hw-llm" not in wms_d2.get("twinny.chatToolsMcpServers", {})
        cline_cfg2 = json.load(open(os.path.join(cline_gs, "settings", "cline_mcp_settings.json"), encoding="utf-8"))
        assert "cluster-hw-llm" not in cline_cfg2.get("mcpServers", {})

    print("[PASS] Install/Uninstall matrix verified across {windows, wsl} x {agy, vscode, opencode} + wsl.exe fallback")


def check_gateway_and_clients():
    import threading
    import urllib.error
    import urllib.request
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    sys.path.insert(0, os.path.join(REPO_ROOT, "tools", "gateway"))
    import cluster_gateway  # noqa: E402

    mock_state = {
        "torre_online": True,
        "portatil_online": True,
        "loaded": set(),
        "calls": [],
        "resp_counter": 0,
    }

    def current_models():
        ms = []
        if mock_state["torre_online"]:
            for k, ctx in (
                ("t-code-chat", 32768),
                ("t-general-agent", 32768),
                ("t-fast-agent", 32768),
                ("t-vision-context", 131072),
                ("t-math-physics", 16384),
                ("t-deep-reasoning", 131072),
                ("t-autocomplete-tab", 32768),
                ("t-draft-coder", 32768),
                ("text-embedding-t-embed-index", 2048),
                ("text-embedding-nomic-embed-text-v1.5", 2048),
            ):
                ms.append({
                    "type": "embedding" if "embedding" in k else "llm",
                    "key": k,
                    "architecture": "qwen2",
                    "max_context_length": ctx,
                    "loaded_instances": [{"id": k, "config": {"context_length": ctx}}] if k in mock_state["loaded"] else [],
                })
        if mock_state["portatil_online"]:
            for k, ctx in (
                ("p-code-moe", 262144),
                ("p-agent-moe-alt", 32768),
                ("p-agent-moe", 262144),
                ("p-battery-chat", 32768),
                ("p-autocomplete-tab", 32768),
                ("p-draft-gemma-moe-mtp", 131072),
                ("text-embedding-p-embed-index", 2048),
            ):
                ms.append({
                    "type": "embedding" if "embedding" in k else "llm",
                    "key": k,
                    "architecture": "qwen3moe",
                    "max_context_length": ctx,
                    "loaded_instances": [{"id": k, "config": {"context_length": ctx}}] if k in mock_state["loaded"] else [],
                })
        return ms

    class MockUpstream(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            return

        def do_GET(self):
            if self.path == "/api/v1/models":
                raw = json.dumps({"models": current_models()}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or "0")
            body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            mock_state["calls"].append((self.path, body))
            if self.path == "/api/v1/models/load":
                mk = body["model"]
                mock_state["loaded"].add(mk)
                raw = json.dumps({"instance_id": mk, "status": "loaded", "load_config": body}).encode("utf-8")
            elif self.path == "/api/v1/models/unload":
                iid = body["instance_id"]
                mock_state["loaded"].discard(iid)
                raw = json.dumps({"instance_id": iid}).encode("utf-8")
            elif self.path == "/api/v1/chat":
                mock_state["resp_counter"] += 1
                rid = f"resp_lms_{mock_state['resp_counter']}"
                if body.get("stream"):
                    chunks = [
                        b'event: message.delta\ndata: {"type":"message.delta","content":"ok"}\n\n',
                        f'event: chat.end\ndata: {{"type":"chat.end","result":{{"response_id":"{rid}","stats":{{"tokens_per_second":50.0}}}}}}\n\n'.encode("utf-8"),
                    ]
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    for ch in chunks:
                        self.wfile.write(ch)
                        self.wfile.flush()
                    return
                raw = json.dumps({"response_id": rid, "model_instance_id": body["model"], "output": [{"type": "message", "content": "ok"}]}).encode("utf-8")
            elif self.path == "/v1/responses":
                mock_state["resp_counter"] += 1
                rid = f"resp_oai_{mock_state['resp_counter']}"
                raw = json.dumps({"id": rid, "model": body["model"], "output": []}).encode("utf-8")
            elif self.path == "/v1/messages":
                raw = json.dumps({"id": "msg_1", "type": "message", "model": body["model"], "content": [{"type": "text", "text": "ok"}]}).encode("utf-8")
            elif self.path == "/v1/embeddings":
                raw = json.dumps({"object": "list", "model": body["model"], "data": [{"embedding": [0.1, 0.2], "index": 0}]}).encode("utf-8")
            elif self.path == "/v1/chat/completions":
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    return
                raw = json.dumps({"id": "chatcmpl_1", "model": body["model"], "choices": [{"message": {"role": "assistant", "content": "ok"}}]}).encode("utf-8")
            else:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    up_srv = ThreadingHTTPServer(("127.0.0.1", 0), MockUpstream)
    up_port = up_srv.server_address[1]
    t_up = threading.Thread(target=up_srv.serve_forever, daemon=True)
    t_up.start()

    old_env = os.environ.get("CLUSTER_GATEWAY_LOAD_VIA_REST")
    os.environ["CLUSTER_GATEWAY_LOAD_VIA_REST"] = "1"
    router = cluster_gateway.ClusterRouter(upstream_url=f"http://127.0.0.1:{up_port}", repo_root=REPO_ROOT, cache_ttl=0.0)
    gw_srv = cluster_gateway.ClusterGatewayServer(("127.0.0.1", 0), router)
    gw_port = gw_srv.server_address[1]
    t_gw = threading.Thread(target=gw_srv.serve_forever, daemon=True)
    t_gw.start()
    gw_base = f"http://127.0.0.1:{gw_port}"

    def req_json(method, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        hdrs = {"Content-Type": "application/json"} if payload is not None else {}
        rq = urllib.request.Request(gw_base + path, data=data, headers=hdrs, method=method)
        with urllib.request.urlopen(rq, timeout=5) as r:
            return r.status, dict(r.headers), json.loads(r.read().decode("utf-8"))

    try:
        # 1. Discovery + draft filtering + Option B tuned contexts
        _, _, h = req_json("GET", "/health")
        assert h["ok"] and h["upstream_reachable"]
        _, _, oai_m = req_json("GET", "/v1/models")
        oai_ids = {m["id"]: m for m in oai_m["data"]}
        assert "cluster/code" in oai_ids and oai_ids["cluster/code"]["resolved_model"] == "p-code-moe"
        assert oai_ids["cluster/code"]["context_length"] == 16384
        assert oai_ids["cluster/code-fast"]["resolved_model"] == "t-code-chat"
        assert oai_ids["cluster/code-fast"]["context_length"] == 12288
        assert "t-draft-coder" not in oai_ids and "p-draft-gemma-moe-mtp" not in oai_ids
        assert "text-embedding-nomic-embed-text-v1.5" not in oai_ids

        _, _, lms_m = req_json("GET", "/api/v1/models")
        lms_by_k = {m["key"]: m for m in lms_m["models"]}
        assert lms_by_k["t-code-chat"]["max_context_length"] == 12288
        assert lms_by_k["p-agent-moe"]["max_context_length"] == 32768
        _, _, v0_m = req_json("GET", "/api/v0/models")
        assert any(m["id"] == "cluster/code" for m in v0_m["data"])

        # 2. All 5 protocol roundtrips + reasoning injection + SSE + stateful previous_response_id pin
        _, hdr1, d1 = req_json("POST", "/v1/chat/completions", {"model": "cluster/fast-agent", "messages": [{"role": "user", "content": "hi"}]})
        assert hdr1["X-Cluster-Resolved-Model"] == "t-fast-agent"
        assert mock_state["calls"][-1][1].get("reasoning_effort") == "none"

        _, hdr_m, _ = req_json("POST", "/v1/messages", {"model": "cluster/code", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]})
        assert hdr_m["X-Cluster-Resolved-Model"] == "p-code-moe"

        _, hdr_r, resp_oai = req_json("POST", "/v1/responses", {"model": "cluster/code-fast", "input": "hi"})
        assert hdr_r["X-Cluster-Resolved-Model"] == "t-code-chat"
        assert resp_oai["id"].startswith("resp_oai_")

        # Streaming native /api/v1/chat with MCP integration + response_id pin
        rq_sse = urllib.request.Request(
            gw_base + "/api/v1/chat",
            data=json.dumps({"model": "cluster/code", "input": "hi", "stream": True, "integrations": ["mcp/browse"]}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(rq_sse, timeout=5) as rs:
            assert rs.headers.get("X-Cluster-Resolved-Model") == "p-code-moe"
            sse_txt = rs.read().decode("utf-8")
            assert "chat.end" in sse_txt
        assert mock_state["calls"][-1][1]["integrations"] == ["mcp/browse"]
        m_pin = re.search(r'"response_id":"([^"]+)"', sse_txt)
        assert m_pin, f"Missing response_id in SSE: {sse_txt}"
        pinned_id = m_pin.group(1)

        # 3. Anti-thrash diversion (on torre) + local-first (on portatil) + Failover + Session Pin preservation
        mock_state["loaded"].add("t-code-chat")
        orig_local = router.local_node
        try:
            router.local_node = "torre"
            _, _, st_div = req_json("GET", "/cluster/status")
            assert st_div["aliases"]["cluster/autocomplete"]["resolved"] == "p-autocomplete-tab"
            assert st_div["aliases"]["cluster/autocomplete"]["anti_thrash_diverted"] is True
            assert st_div["aliases"]["cluster/embed"]["resolved"] == "text-embedding-p-embed-index"
            assert st_div["aliases"]["cluster/embed"]["anti_thrash_diverted"] is True

            router.local_node = "portatil"
            _, _, st_port = req_json("GET", "/cluster/status")
            assert st_port["aliases"]["cluster/autocomplete"]["resolved"] == "p-autocomplete-tab"
            assert st_port["aliases"]["cluster/autocomplete"]["anti_thrash_diverted"] is False
            assert st_port["aliases"]["cluster/embed"]["resolved"] == "text-embedding-p-embed-index"
            assert st_port["aliases"]["cluster/embed"]["anti_thrash_diverted"] is False
        finally:
            router.local_node = orig_local

        # Even if portatil goes offline, previous_response_id remains pinned if still listed or fails over when requested fresh
        mock_state["portatil_online"] = False
        _, hdr_fb, _ = req_json("POST", "/v1/chat/completions", {"model": "cluster/code", "messages": [{"role": "user", "content": "hi"}]})
        assert hdr_fb["X-Cluster-Resolved-Model"] == "t-code-chat"

        mock_state["portatil_online"] = True
        _, hdr_pin, _ = req_json("POST", "/api/v1/chat", {"model": "cluster/code-fast", "input": "follow-up", "previous_response_id": pinned_id})
        assert hdr_pin["X-Cluster-Resolved-Model"] == "p-code-moe"

        mock_state["torre_online"] = False
        _, hdr_tfb, _ = req_json("POST", "/v1/chat/completions", {"model": "cluster/fast-agent", "messages": [{"role": "user", "content": "hi"}]})
        assert hdr_tfb["X-Cluster-Resolved-Model"] == "p-battery-chat"
        mock_state["torre_online"] = True

        # 4. Safe /api/v1/models/load guardrails + alias unload
        try:
            req_json("POST", "/api/v1/models/load", {"model": "t-code-chat", "k_cache_quant": "f16"})
            raise AssertionError("Expected 400 unsafe VRAM config rejection")
        except urllib.error.HTTPError as e:
            assert e.code == 400
        try:
            req_json("POST", "/api/v1/models/load", {"model": "t-math-physics", "v_cache_quant": "q8_0"})
            raise AssertionError("Expected 400 phi3 V=q8_0 rejection")
        except urllib.error.HTTPError as e:
            assert e.code == 400

        _, _, ld_ok = req_json("POST", "/api/v1/models/load", {"model": "cluster/fast-agent", "parallel": 2})
        assert ld_ok["instance_id"] == "t-fast-agent"
        assert ld_ok["load_config"]["flash_attention"] is True
        _, _, un_ok = req_json("POST", "/api/v1/models/unload", {"instance_id": "cluster/fast-agent"})
        assert un_ok["instance_id"] == "t-fast-agent"

        # 4b. MCP cluster_models & cluster_chat end-to-end against mocked Cluster Gateway
        mcp_srv = os.path.join(REPO_ROOT, "mcp", "cluster_mcp_server.py")
        mcp_env = dict(os.environ)
        mcp_env["CLUSTER_GATEWAY_URL"] = gw_base
        m_proc = subprocess.Popen(
            [sys.executable, mcp_srv],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=mcp_env,
        )
        try:
            def m_send(obj):
                m_proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
                m_proc.stdin.flush()

            def m_recv():
                return json.loads(m_proc.stdout.readline().decode("utf-8"))

            m_send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}})
            assert m_recv()["result"]["serverInfo"]["name"] == "cluster-hw-llm"
            m_send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
            m_send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "cluster_models", "arguments": {}}})
            cm_res = json.loads(m_recv()["result"]["content"][0]["text"])
            assert cm_res["ok"] is True
            assert cm_res["aliases"]["cluster/code"]["resolved"] == "p-code-moe"
            m_send({
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "cluster_chat", "arguments": {"prompt": "hi", "model": "cluster/code"}},
            })
            cc_res = json.loads(m_recv()["result"]["content"][0]["text"])
            assert cc_res["ok"] is True
            assert cc_res["resolved_model"] == "p-code-moe"
            assert cc_res["resolved_node"] == "portatil"
            assert cc_res["text"] == "ok"
            assert str(cc_res["response_id"]).startswith("resp_lms_")
        finally:
            m_proc.stdin.close()
            m_proc.wait(timeout=5)
            assert m_proc.returncode == 0, f"MCP gateway test failed: {m_proc.stderr.read().decode('utf-8', 'replace')}"
    finally:
        gw_srv.shutdown()
        gw_srv.server_close()
        up_srv.shutdown()
        up_srv.server_close()
        if old_env is None:
            os.environ.pop("CLUSTER_GATEWAY_LOAD_VIA_REST", None)
        else:
            os.environ["CLUSTER_GATEWAY_LOAD_VIA_REST"] = old_env

    # 5. Sandboxed setup_clients.py non-destructive install & uninstall test
    setup_script = os.path.join(REPO_ROOT, "scripts", "setup_clients.py")
    with tempfile.TemporaryDirectory() as tmp:
        posix_home = os.path.join(tmp, "posix_home")
        win_home = os.path.join(tmp, "win_home")
        win_app = os.path.join(win_home, "AppData", "Roaming")
        os.makedirs(os.path.join(posix_home, ".config", "opencode"), exist_ok=True)
        os.makedirs(os.path.join(posix_home, ".continue"), exist_ok=True)
        os.makedirs(os.path.join(posix_home, ".lmstudio"), exist_ok=True)
        os.makedirs(os.path.join(win_app, "Code", "User"), exist_ok=True)
        os.makedirs(os.path.join(win_home, ".lmstudio"), exist_ok=True)

        with open(os.path.join(posix_home, ".config", "opencode", "opencode.json"), "w", encoding="utf-8") as f:
            json.dump({"mcp": {"servers": {"other-oc": {"type": "local", "command": ["node"]}}}}, f)
        with open(os.path.join(win_app, "Code", "User", "chatLanguageModels.json"), "w", encoding="utf-8") as f:
            json.dump([
                {"name": "Google", "vendor": "gemini", "apiKey": "${input:keep}"},
                {"name": "lmstudio", "vendor": "customendpoint", "models": [{"id": "", "name": "", "url": ""}]},
            ], f)
        with open(os.path.join(win_home, ".lmstudio", "mcp.json"), "w", encoding="utf-8") as f:
            json.dump({"mcpServers": {"browse": {"command": "browse.exe", "args": ["mcp"]}}}, f)

        env = dict(os.environ)
        env["CLUSTER_INSTALL_POSIX_HOME"] = posix_home
        env["CLUSTER_INSTALL_WIN_HOME"] = win_home
        env["CLUSTER_INSTALL_WIN_APPDATA"] = win_app

        r1 = subprocess.run(
            [sys.executable, setup_script, "--os", "windows,wsl", "--clients", "all"],
            stdin=subprocess.DEVNULL, env=env, text=True, capture_output=True, timeout=20,
        )
        assert r1.returncode == 0, f"setup_clients failed: {r1.stderr or r1.stdout}"

        oc_d = json.load(open(os.path.join(posix_home, ".config", "opencode", "opencode.json"), encoding="utf-8"))
        assert "other-oc" in oc_d["mcp"]["servers"]
        assert oc_d["provider"]["cluster"]["options"]["baseURL"] == "http://127.0.0.1:1235/v1"
        assert "code" in oc_d["provider"]["cluster"]["models"]
        assert oc_d["provider"]["cluster"]["models"]["code"]["id"] == "cluster/code"

        byok = json.load(open(os.path.join(win_app, "Code", "User", "chatLanguageModels.json"), encoding="utf-8"))
        names = [x.get("name") for x in byok]
        assert "Google" in names and "Local Cluster (LM Link)" in names and "lmstudio" not in names

        lms_mcp = json.load(open(os.path.join(win_home, ".lmstudio", "mcp.json"), encoding="utf-8"))
        assert "browse" in lms_mcp["mcpServers"] and "cluster-hw-llm" in lms_mcp["mcpServers"]
        assert os.path.isfile(os.path.join(posix_home, ".config", "cluster-llm", "env.sh"))
        assert os.path.isfile(os.path.join(win_home, ".config", "cluster-llm", "env.ps1"))
        for cont_p in (
            os.path.join(win_home, ".continue", "config.yaml"),
            os.path.join(posix_home, ".continue", "config.yaml"),
        ):
            cont_txt = open(cont_p, encoding="utf-8").read()
            assert "cluster/autocomplete" in cont_txt
            assert "tool_use" in cont_txt and "image_input" in cont_txt
            assert "mcpServers:" in cont_txt and "cluster-hw-llm" in cont_txt

        # Verify standalone install_continue.py creates missing ~/.continue on WSL and tailors for portatil
        shutil.rmtree(os.path.join(posix_home, ".continue"))
        ic_script = os.path.join(REPO_ROOT, "scripts", "install_continue.py")
        r_ic = subprocess.run(
            [sys.executable, ic_script, "--node", "portatil", "--os", "windows,wsl"],
            stdin=subprocess.DEVNULL, env=env, text=True, capture_output=True, timeout=20,
        )
        assert r_ic.returncode == 0, f"install_continue failed: {r_ic.stderr or r_ic.stdout}"
        for cont_p in (
            os.path.join(win_home, ".continue", "config.yaml"),
            os.path.join(posix_home, ".continue", "config.yaml"),
        ):
            ptxt = open(cont_p, encoding="utf-8").read()
            assert "Portatil" in ptxt and "cluster/code-heavy" in ptxt and "mcpServers:" in ptxt

        r2 = subprocess.run(
            [sys.executable, setup_script, "--os", "windows,wsl", "--clients", "all", "--uninstall"],
            stdin=subprocess.DEVNULL, env=env, text=True, capture_output=True, timeout=20,
        )
        assert r2.returncode == 0, f"setup_clients uninstall failed: {r2.stderr}"
        oc_d2 = json.load(open(os.path.join(posix_home, ".config", "opencode", "opencode.json"), encoding="utf-8"))
        assert "other-oc" in oc_d2["mcp"]["servers"] and "provider" not in oc_d2
        byok2 = json.load(open(os.path.join(win_app, "Code", "User", "chatLanguageModels.json"), encoding="utf-8"))
        assert [x.get("name") for x in byok2] == ["Google"]
        lms_mcp2 = json.load(open(os.path.join(win_home, ".lmstudio", "mcp.json"), encoding="utf-8"))
        assert list(lms_mcp2["mcpServers"].keys()) == ["browse"]

    # 6. CLI smoke checks for Ollama cleaner (--check --json) and Antigravity SDK launcher (--help)
    cln = subprocess.run(
        [sys.executable, os.path.join(REPO_ROOT, "scripts", "cleanup_ollama.py"), "--check", "--json"],
        stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=25,
    )
    assert cln.returncode == 0, f"cleanup_ollama --check --json failed: {cln.stderr}"
    cln_data = json.loads(cln.stdout)
    assert "api_11434_live" in cln_data and "wsl_root_cleanup_pending" in cln_data

    agy_h = subprocess.run(
        [sys.executable, os.path.join(REPO_ROOT, "tools", "gateway", "agy_local_agent.py"), "--help"],
        stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=10,
    )
    assert agy_h.returncode == 0, f"agy_local_agent --help failed: {agy_h.stderr}"

    print("[PASS] 5-protocol Cluster Gateway (failover, anti-thrash, SSE, /api/v1/chat pin) + MCP + client provisioner verified")


def check_peer_and_openclaw():
    import time
    import urllib.error
    import urllib.request

    sys.path.insert(0, os.path.join(REPO_ROOT, "tools", "peer"))
    import openclaw_ctl  # noqa: E402
    import peer_daemon  # noqa: E402

    with tempfile.TemporaryDirectory() as tmp:
        sync_dir = os.path.join(tmp, "sync")
        os.makedirs(sync_dir, exist_ok=True)
        tok = "test-peer-token-1234567890abcdef"
        rt = peer_daemon.PeerRuntime(repo_root=REPO_ROOT, token=tok, sync_dir=sync_dir)
        srvs = peer_daemon.start_servers(rt, ["127.0.0.1"], port=0)
        port = srvs[0].server_address[1]
        base = f"http://127.0.0.1:{port}"
        try:
            # 1. /health works unauthenticated; /status requires valid Bearer token
            with urllib.request.urlopen(base + "/health", timeout=3) as r:
                hl = json.loads(r.read().decode("utf-8"))
                assert hl["ok"] is True
            for hdrs in ({}, {"Authorization": "Bearer wrong"}):
                try:
                    urllib.request.urlopen(urllib.request.Request(base + "/status", headers=hdrs), timeout=3)
                    raise AssertionError("Expected 401 on unauthorized /status")
                except urllib.error.HTTPError as e:
                    assert e.code == 401
            with urllib.request.urlopen(urllib.request.Request(base + "/status", headers={"Authorization": f"Bearer {tok}"}), timeout=3) as r:
                st = json.loads(r.read().decode("utf-8"))
                assert st["ok"] is True

            # 2. POST /exec + GET /jobs/<id>
            req = urllib.request.Request(
                base + "/exec",
                data=json.dumps({"command": "echo peer-ok", "target_os": "wsl", "wait_s": 10}).encode("utf-8"),
                headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                ex = json.loads(r.read().decode("utf-8"))
                assert ex["ok"] is True and ex["status"] == "completed" and "peer-ok" in ex["stdout"]
            with urllib.request.urlopen(urllib.request.Request(base + f"/jobs/{ex['job_id']}", headers={"Authorization": f"Bearer {tok}"}), timeout=3) as r:
                jb = json.loads(r.read().decode("utf-8"))
                assert jb["job"]["job_id"] == ex["job_id"]

            # 3. Offline queue poller fallback
            inbox = os.path.join(sync_dir, "peer-queue", f"to_{rt.local_node}")
            os.makedirs(inbox, exist_ok=True)
            q_id = "job_offline_test"
            with open(os.path.join(inbox, f"{q_id}.json"), "w", encoding="utf-8") as f:
                json.dump({"job_id": q_id, "kind": "exec", "command": "echo drive-ok", "target_os": "wsl"}, f)
            assert rt.poll_offline_queue_once() == 1
            comp_path = os.path.join(sync_dir, "peer-queue", "completed", f"{q_id}.json")
            deadline = time.time() + 8.0
            while time.time() < deadline and not os.path.isfile(comp_path):
                time.sleep(0.1)
            assert os.path.isfile(comp_path), "Offline queue did not write completed result"
            comp = json.load(open(comp_path, encoding="utf-8"))
            assert comp["status"] == "completed" and "drive-ok" in comp["stdout"]

            # 4. OpenClaw HA Lease conflict/expiry/takeover + openclaw.json migration
            store = openclaw_ctl.LeaseStore(sync_dir)
            ok1, _ = store.acquire_or_renew("torre", pid=111, ttl_s=30, now=1000.0)
            assert ok1 is True
            ok_conflict, _ = store.acquire_or_renew("portatil", pid=222, ttl_s=30, now=1010.0)
            assert ok_conflict is False
            ok_failover, l2 = store.acquire_or_renew("portatil", pid=222, ttl_s=30, now=1035.0)
            assert ok_failover is True and l2["holder_node"] == "portatil" and l2["epoch"] == 2
            ok_takeover, l3 = store.acquire_or_renew("torre", pid=333, ttl_s=30, force=True, manual_leader="torre", now=1040.0)
            assert ok_takeover is True and l3["holder_node"] == "torre" and l3["epoch"] == 3

            oc_home = os.path.join(tmp, "oc_home")
            os.makedirs(oc_home, exist_ok=True)
            if os.name != "nt":
                os.chmod(oc_home, 0o775)
            oc_cfg_p = os.path.join(oc_home, "openclaw.json")
            with open(oc_cfg_p, "w", encoding="utf-8") as f:
                json.dump({
                    "models": {"providers": {"cluster-local": {"baseUrl": "http://127.0.0.1:1238/v1"}}},
                    "channels": {"telegram": {"enabled": True, "botToken": "123:ABC", "allowFrom": ["999"]}},
                    "agents": {"defaults": {"workspace": os.path.join(tmp, "ws"), "model": {"primary": "cluster-local/old", "fallbacks": ["google/gemini-3.8-flash"]}}},
                }, f)
            openclaw_ctl.configure_openclaw(config_path=oc_cfg_p)
            migrated = json.load(open(oc_cfg_p, encoding="utf-8"))
            assert migrated["channels"]["telegram"]["botToken"] == "123:ABC"
            assert migrated["models"]["providers"]["cluster-local"]["baseUrl"] == "http://127.0.0.1:1235/v1"
            assert migrated["agents"]["defaults"]["model"]["primary"] == "cluster-local/cluster/vision-heavy"
            assert "cluster-local/cluster/code" in migrated["agents"]["defaults"]["model"]["fallbacks"]
            assert "google/gemini-3.8-flash" in migrated["agents"]["defaults"]["model"]["fallbacks"]
            assert os.path.isfile(os.path.join(tmp, "ws", "AGENTS.md"))
            if os.name != "nt":
                assert (os.stat(oc_home).st_mode & 0o022) == 0, "configure_openclaw did not strip group/world write bits"

            # 4b. OpenClaw Node v24 preference, writable npm prefix fallback, and zombie/PID checks
            fake_node_prefix = os.path.join(tmp, "fake_node_prefix")
            os.makedirs(os.path.join(fake_node_prefix, "bin"), exist_ok=True)
            fake_node_bin = os.path.join(fake_node_prefix, "bin", "node")
            open(fake_node_bin, "w", encoding="utf-8").close()
            os.chmod(fake_node_bin, 0o755)
            assert openclaw_ctl._resolve_npm_prefix(fake_node_bin) == fake_node_prefix
            if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() != 0 and os.path.exists("/usr/bin/node"):
                fb_prefix = openclaw_ctl._resolve_npm_prefix("/usr/bin/node")
                assert fb_prefix != "/usr" and os.access(fb_prefix, os.W_OK)

            managed_root = os.path.join(tmp, "managed_nodejs")
            managed_bin_dir = os.path.join(managed_root, "current", "bin")
            os.makedirs(managed_bin_dir, exist_ok=True)
            managed_node = os.path.join(managed_bin_dir, "node")
            open(managed_node, "w", encoding="utf-8").close()
            os.chmod(managed_node, 0o755)
            orig_node_base_dir = openclaw_ctl._node_base_dir
            try:
                openclaw_ctl._node_base_dir = lambda: managed_root
                assert openclaw_ctl.find_linux_node_bin() == managed_node
            finally:
                openclaw_ctl._node_base_dir = orig_node_base_dir

            assert openclaw_ctl._pid_alive(os.getpid()) is True
            assert openclaw_ctl._is_proc_zombie(os.getpid()) is False

            # 4c. OpenClaw Node v24 wrapper + authenticated POST /openclaw/sync-secrets
            wrap_path = openclaw_ctl.write_openclaw_wrapper(
                managed_node,
                os.path.join(managed_bin_dir, "openclaw"),
                wrapper_path=os.path.join(tmp, "bin", "openclaw"),
            )
            wrap_txt = open(wrap_path, encoding="utf-8").read()
            assert wrap_txt.startswith("#!/usr/bin/env sh\n")
            assert managed_bin_dir in wrap_txt and f"exec {managed_node}" in wrap_txt
            if os.name != "nt":
                assert os.access(wrap_path, os.X_OK)

            oc_sync_dir = os.path.join(tmp, "oc_sync_target")
            os.makedirs(oc_sync_dir, exist_ok=True)
            with open(os.path.join(oc_sync_dir, "openclaw.json"), "w", encoding="utf-8") as f:
                json.dump({"models": {"providers": {"cluster-local": {"baseUrl": "http://127.0.0.1:1235/v1"}}}}, f)
            old_oc_home = os.environ.get("OPENCLAW_HOME")
            os.environ["OPENCLAW_HOME"] = oc_sync_dir
            try:
                try:
                    urllib.request.urlopen(
                        urllib.request.Request(
                            base + "/openclaw/sync-secrets",
                            data=json.dumps({"secrets": {"telegram": {"enabled": True, "botToken": "secret_tok", "allowFrom": ["42"]}}}).encode("utf-8"),
                            headers={"Content-Type": "application/json"},
                            method="POST",
                        ),
                        timeout=3,
                    )
                    raise AssertionError("Expected 401 on unauthorized /openclaw/sync-secrets")
                except urllib.error.HTTPError as e:
                    assert e.code == 401

                sync_req = urllib.request.Request(
                    base + "/openclaw/sync-secrets",
                    data=json.dumps({"secrets": {"telegram": {"enabled": True, "botToken": "secret_tok", "allowFrom": ["42"]}}}).encode("utf-8"),
                    headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(sync_req, timeout=3) as r:
                    raw_resp = r.read().decode("utf-8")
                    assert "secret_tok" not in raw_resp, "Secret leaked in sync-secrets HTTP response!"
                    sync_res = json.loads(raw_resp)
                    assert sync_res["ok"] is True and sync_res["telegram_configured"] is True
                merged_oc = json.load(open(os.path.join(oc_sync_dir, "openclaw.json"), encoding="utf-8"))
                assert merged_oc["channels"]["telegram"]["botToken"] == "secret_tok"
                assert merged_oc["models"]["providers"]["cluster-local"]["baseUrl"] == "http://127.0.0.1:1235/v1"
            finally:
                if old_oc_home is None:
                    os.environ.pop("OPENCLAW_HOME", None)
                else:
                    os.environ["OPENCLAW_HOME"] = old_oc_home

            # 5. Stdio MCP roundtrip for peer_status & openclaw_control(status)
            mcp_env = dict(os.environ)
            mcp_env["CLUSTER_PEER_TOKEN"] = tok
            mcp_env["CLUSTER_PEER_SYNC_DIR"] = sync_dir
            m_proc = subprocess.Popen(
                [sys.executable, os.path.join(REPO_ROOT, "mcp", "cluster_mcp_server.py")],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=mcp_env,
            )
            try:
                def _ms(o):
                    m_proc.stdin.write((json.dumps(o) + "\n").encode("utf-8"))
                    m_proc.stdin.flush()

                def _mr():
                    return json.loads(m_proc.stdout.readline().decode("utf-8"))

                _ms({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}})
                assert _mr()["result"]["serverInfo"]["name"] == "cluster-hw-llm"
                _ms({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
                _ms({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "peer_status", "arguments": {}}})
                ps_out = json.loads(_mr()["result"]["content"][0]["text"])
                assert ps_out["token_configured"] is True and "nodes" in ps_out
                _ms({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "openclaw_control", "arguments": {"action": "status"}}})
                oc_out = json.loads(_mr()["result"]["content"][0]["text"])
                assert "local_node" in oc_out and "lease" in oc_out
            finally:
                m_proc.stdin.close()
                m_proc.wait(timeout=5)
                assert m_proc.returncode == 0
        finally:
            rt.stop()
            for s in srvs:
                s.shutdown()
                s.server_close()

    print("[PASS] Peer daemon (:1236 auth, /exec, Drive offline queue) + OpenClaw HA lease & config migration verified")


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
        check_gateway_and_clients()
        check_peer_and_openclaw()
    if args.check_privacy:
        check_privacy(files)
    if args.live:
        mcp_roundtrip("ndjson", live=True)
    print("DOCTOR_ALL_GREEN")


if __name__ == "__main__":
    main()
