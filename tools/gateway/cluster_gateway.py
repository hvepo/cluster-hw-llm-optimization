#!/usr/bin/env python3
"""Zero-dependency 5-protocol Cluster Router Gateway for LM Studio + LM Link.

Exposes the unified dual-node model pool (e.g. torre + portatil) on http://127.0.0.1:1235
across:
  1. LM Studio native REST API (/api/v1/chat, /api/v1/models, /api/v1/models/load|unload|download)
  2. OpenAI Chat/Completions/Embeddings (/v1/chat/completions, /v1/completions, /v1/embeddings, /v1/models)
  3. OpenAI Responses API (/v1/responses)
  4. Anthropic Messages API (/v1/messages)
  5. Legacy LM Studio API (/api/v0/models)

Features:
  - Machine-independent virtual role aliases (`cluster/*`) with Option B smart-first dual-GPU split
  - Live LM Link failover when either node is offline or asleep
  - Anti-thrash diversion for `cluster/autocomplete` & `cluster/embed` when torre holds a 12B-15B model
  - Draft-model filtering and exact tuned `max_context_length` reporting from profiles/best-config.json
  - Stateful `previous_response_id` session pinning and default-safe `/api/v1/models/load`
"""
import argparse
import http.client
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GATEWAY_DIR = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.abspath(os.path.join(GATEWAY_DIR, ".."))
REPO_ROOT = os.path.abspath(os.path.join(TOOLS_DIR, ".."))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import cluster_lib  # noqa: E402

HEAVY_TORRE_MODELS = {
    "t-code-chat",
    "t-general-agent",
    "t-vision-context",
    "t-math-physics",
    "t-deep-reasoning",
}

DEFAULT_ALIAS_SPECS = {
    "cluster/code": {
        "candidates": ["p-code-moe", "t-code-chat"],
        "tools": True,
        "vision": False,
        "thinking": None,
        "description": "Smart-first coding (Laptop 30B-A3B MoE -> PC 14B fallback)",
    },
    "cluster/code-heavy": {
        "candidates": ["p-code-moe", "t-code-chat"],
        "tools": True,
        "vision": False,
        "thinking": None,
        "description": "Heavy multi-file coding & refactoring (Laptop 30B-A3B MoE -> PC 14B)",
    },
    "cluster/code-fast": {
        "candidates": ["t-code-chat", "p-code-moe"],
        "tools": True,
        "vision": False,
        "thinking": None,
        "description": "Low-latency interactive coding (PC 14B 72 tok/s -> Laptop 30B-A3B)",
    },
    "cluster/agent-heavy": {
        "candidates": ["p-agent-moe-alt", "t-general-agent"],
        "tools": True,
        "vision": False,
        "thinking": False,
        "description": "Deep autonomous multi-step agent (Laptop 30B-A3B -> PC Qwen3-14B)",
    },
    "cluster/fast-agent": {
        "candidates": ["t-fast-agent", "p-battery-chat"],
        "tools": True,
        "vision": False,
        "thinking": False,
        "description": "High-speed subagent & quick tasks (PC 8B 130 tok/s -> Laptop 4B)",
    },
    "cluster/vision": {
        "candidates": ["t-vision-context", "p-agent-moe"],
        "tools": True,
        "vision": True,
        "thinking": False,
        "description": "Fast multimodal & OCR (PC Gemma-4-12B+MTP4 -> Laptop 26B+MTP3)",
    },
    "cluster/vision-heavy": {
        "candidates": ["p-agent-moe", "t-vision-context"],
        "tools": True,
        "vision": True,
        "thinking": False,
        "description": "Long-context 32K multimodal & docs (Laptop 26B+MTP3 -> PC 12B+MTP4)",
    },
    "cluster/reasoning": {
        "candidates": ["t-deep-reasoning", "p-agent-moe-alt"],
        "tools": True,
        "vision": False,
        "thinking": True,
        "description": "Chain-of-thought reasoning (PC R1-14B -> Laptop 30B-A3B)",
    },
    "cluster/math": {
        "candidates": ["t-math-physics", "p-agent-moe-alt"],
        "tools": False,
        "vision": False,
        "thinking": None,
        "description": "Formal math & physics proofs (PC Phi-4 14B -> Laptop 30B-A3B)",
    },
    "cluster/autocomplete": {
        "candidates": ["t-autocomplete-tab", "p-autocomplete-tab"],
        "tools": False,
        "vision": False,
        "thinking": None,
        "fim": True,
        "local_first": True,
        "anti_thrash": True,
        "description": "IDE Tab FIM autocomplete (local-first; anti-thrash diverts when torre holds 12B-15B)",
    },
    "cluster/embed": {
        "candidates": ["text-embedding-t-embed-index", "text-embedding-p-embed-index"],
        "tools": False,
        "vision": False,
        "thinking": None,
        "embedding": True,
        "local_first": True,
        "anti_thrash": True,
        "description": "Codebase & RAG embeddings (local-first; anti-thrash diverts when torre holds 12B-15B)",
    },
}

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "accept-encoding",
}


def is_hidden_internal_model(model_key):
    if not model_key:
        return True
    if model_key.startswith(("t-draft-", "p-draft-")):
        return True
    if model_key == "text-embedding-nomic-embed-text-v1.5":
        return True
    return False


class ClusterRouter:
    """Thread-safe cluster state cache, alias resolver, anti-thrash engine, and session pinner."""

    def __init__(self, upstream_url="http://127.0.0.1:1234", repo_root=REPO_ROOT, cache_ttl=2.0):
        self.repo_root = os.path.abspath(repo_root)
        self.cfg = cluster_lib.load_cluster_config(self.repo_root)
        gw_cfg = self.cfg.get("gateway") or {}
        self.upstream_url = (upstream_url or gw_cfg.get("upstream_url") or "http://127.0.0.1:1234").rstrip("/")
        parsed = urllib.parse.urlparse(self.upstream_url)
        self.upstream_host = parsed.hostname or "127.0.0.1"
        self.upstream_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.upstream_scheme = parsed.scheme or "http"
        self.local_node = self.cfg.get("local_node", "torre")
        self.cache_ttl = cache_ttl

        self.best_cfg = cluster_lib.load_best_config(self.repo_root)
        self.tuned_models = self._build_tuned_model_index()
        self.alias_specs = self._build_alias_specs(gw_cfg.get("aliases") or {})

        self._lock = threading.Lock()
        self._cache_time = 0.0
        self._cached_models = None
        self._pin_map = OrderedDict()  # response_id -> concrete_model

    def _build_tuned_model_index(self):
        out = {}
        for node_name, node_data in (self.best_cfg.get("nodes") or {}).items():
            for mkey, minfo in (node_data.get("models") or {}).items():
                lp = minfo.get("load_params") or {}
                out[mkey] = {
                    "node": node_name,
                    "context_length": lp.get("context_length"),
                    "parallel": lp.get("parallel"),
                    "load_params": lp,
                    "underlying_model": minfo.get("underlying_model"),
                }
        return out

    def _build_alias_specs(self, overrides):
        specs = {k: dict(v) for k, v in DEFAULT_ALIAS_SPECS.items()}
        for alias, cand_list in overrides.items():
            if isinstance(cand_list, list) and cand_list:
                entry = dict(specs.get(alias, {"tools": True, "vision": False, "thinking": None}))
                entry["candidates"] = list(cand_list)
                specs[alias] = entry
        return specs

    def invalidate_cache(self):
        with self._lock:
            self._cache_time = 0.0

    def record_response_pin(self, response_id, concrete_model):
        if not response_id or not concrete_model:
            return
        with self._lock:
            self._pin_map[response_id] = concrete_model
            self._pin_map.move_to_end(response_id)
            while len(self._pin_map) > 512:
                self._pin_map.popitem(last=False)

    def get_pinned_model(self, response_id):
        if not response_id:
            return None
        with self._lock:
            val = self._pin_map.get(response_id)
            if val:
                self._pin_map.move_to_end(response_id)
            return val

    def _fetch_upstream_models(self, force=False):
        now = time.monotonic()
        with self._lock:
            if not force and self._cached_models is not None and (now - self._cache_time) < self.cache_ttl:
                return self._cached_models, True
        conn = None
        try:
            cls = http.client.HTTPSConnection if self.upstream_scheme == "https" else http.client.HTTPConnection
            conn = cls(self.upstream_host, self.upstream_port, timeout=4)
            conn.request("GET", "/api/v1/models")
            resp = conn.getresponse()
            raw = resp.read()
            if resp.status == 200:
                data = json.loads(raw.decode("utf-8", "replace"))
                models = data.get("models") or []
                with self._lock:
                    self._cached_models = models
                    self._cache_time = time.monotonic()
                return models, True
        except Exception:
            pass
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
        with self._lock:
            return (self._cached_models or []), False

    def snapshot_state(self, force=False):
        models, reachable = self._fetch_upstream_models(force=force)
        by_key = {}
        online_nodes = set()
        loaded_by_node = {}
        loaded_keys = set()

        for m in models:
            k = m.get("key") or ""
            if not k:
                continue
            by_key[k] = m
            node = self.node_for_model(k)
            if node:
                online_nodes.add(node)
            insts = m.get("loaded_instances") or []
            if insts:
                loaded_keys.add(k)
                loaded_by_node.setdefault(node or "other", []).append(k)

        torre_heavy_loaded = sorted(k for k in loaded_keys if k in HEAVY_TORRE_MODELS)
        return {
            "reachable": reachable,
            "models": models,
            "by_key": by_key,
            "online_nodes": sorted(online_nodes),
            "loaded_by_node": loaded_by_node,
            "loaded_keys": loaded_keys,
            "torre_heavy_loaded": torre_heavy_loaded,
        }

    def node_for_model(self, model_key):
        node = cluster_lib.node_for_model(model_key, self.cfg)
        if node:
            return node
        return (self.tuned_models.get(model_key) or {}).get("node")

    def ordered_candidates(self, alias, state):
        spec = self.alias_specs[alias]
        cands = list(spec["candidates"])
        if spec.get("local_first") and self.local_node == "portatil":
            p_cands = [c for c in cands if self.node_for_model(c) == "portatil"]
            other_cands = [c for c in cands if self.node_for_model(c) != "portatil"]
            cands = p_cands + other_cands

        diverted = False
        if spec.get("anti_thrash") and state["torre_heavy_loaded"]:
            # Divert FIM/embed from torre to portatil if a 12B-15B model is loaded on torre and portatil is online.
            p_online = [c for c in cands if self.node_for_model(c) == "portatil" and c in state["by_key"]]
            t_first = cands and self.node_for_model(cands[0]) == "torre"
            if t_first and p_online:
                # Only allow local torre if the specific FIM/embed model is already co-loaded on torre.
                if cands[0] not in state["loaded_keys"]:
                    cands = p_online + [c for c in cands if c not in p_online]
                    diverted = True
        return cands, diverted

    def tuned_context_for(self, model_key, upstream_item=None):
        info = self.tuned_models.get(model_key)
        if info and info.get("context_length"):
            return int(info["context_length"])
        if upstream_item and upstream_item.get("max_context_length"):
            return int(upstream_item["max_context_length"])
        return 8192

    def resolve_model(self, requested_model, previous_response_id=None, state=None):
        if state is None:
            state = self.snapshot_state()

        if previous_response_id:
            pinned = self.get_pinned_model(previous_response_id)
            if pinned and (pinned in state["by_key"] or not state["reachable"]):
                return {
                    "alias": requested_model if requested_model in self.alias_specs else "",
                    "resolved": pinned,
                    "node": self.node_for_model(pinned) or self.local_node,
                    "diverted": False,
                    "pinned": True,
                    "spec": self.alias_specs.get(requested_model),
                }

        if requested_model not in self.alias_specs and "/" not in requested_model:
            prefixed = f"cluster/{requested_model}"
            if prefixed in self.alias_specs:
                requested_model = prefixed

        if requested_model in self.alias_specs:
            spec = self.alias_specs[requested_model]
            cands, diverted = self.ordered_candidates(requested_model, state)
            chosen = None
            for c in cands:
                if c in state["by_key"]:
                    chosen = c
                    break
            if chosen is None:
                chosen = cands[0]
            return {
                "alias": requested_model,
                "resolved": chosen,
                "node": self.node_for_model(chosen) or self.local_node,
                "diverted": diverted,
                "pinned": False,
                "spec": spec,
            }

        return {
            "alias": "",
            "resolved": requested_model,
            "node": self.node_for_model(requested_model) or self.local_node,
            "diverted": False,
            "pinned": False,
            "spec": None,
        }

    def build_status_payload(self):
        state = self.snapshot_state(force=True)
        aliases = {}
        for alias, spec in self.alias_specs.items():
            res = self.resolve_model(alias, state=state)
            resolved = res["resolved"]
            aliases[alias] = {
                "resolved": resolved,
                "node": res["node"],
                "online": resolved in state["by_key"],
                "loaded": resolved in state["loaded_keys"],
                "context_length": self.tuned_context_for(resolved, state["by_key"].get(resolved)),
                "anti_thrash_diverted": res["diverted"],
                "description": spec.get("description", ""),
            }
        with self._lock:
            pinned_count = len(self._pin_map)
        return {
            "ok": True,
            "gateway": "cluster-hw-llm",
            "upstream": self.upstream_url,
            "upstream_reachable": state["reachable"],
            "local_node": self.local_node,
            "online_nodes": state["online_nodes"],
            "loaded_by_node": state["loaded_by_node"],
            "torre_heavy_loaded": state["torre_heavy_loaded"],
            "pinned_sessions": pinned_count,
            "aliases": aliases,
        }

    def build_openai_models_payload(self):
        state = self.snapshot_state()
        now_ts = int(time.time())
        items = []
        for alias in self.alias_specs:
            res = self.resolve_model(alias, state=state)
            if state["reachable"] and res["resolved"] not in state["by_key"]:
                continue
            items.append({
                "id": alias,
                "object": "model",
                "created": now_ts,
                "owned_by": f"cluster-gateway/{res['node']}",
                "resolved_model": res["resolved"],
                "context_length": self.tuned_context_for(res["resolved"], state["by_key"].get(res["resolved"])),
            })
        for m in state["models"]:
            k = m.get("key") or ""
            if is_hidden_internal_model(k):
                continue
            items.append({
                "id": k,
                "object": "model",
                "created": now_ts,
                "owned_by": f"{m.get('publisher') or 'lmstudio'}/{self.node_for_model(k) or 'local'}",
                "context_length": self.tuned_context_for(k, m),
            })
        return {"object": "list", "data": items}

    def build_lmstudio_v1_models_payload(self):
        state = self.snapshot_state()
        out = []
        for alias, spec in self.alias_specs.items():
            res = self.resolve_model(alias, state=state)
            resolved = res["resolved"]
            up = state["by_key"].get(resolved)
            if state["reachable"] and not up:
                continue
            ctx = self.tuned_context_for(resolved, up)
            is_emb = bool(spec.get("embedding"))
            entry = {
                "type": "embedding" if is_emb else "llm",
                "publisher": "cluster-gateway",
                "key": alias,
                "display_name": f"{alias} -> {resolved}",
                "architecture": (up or {}).get("architecture") or "cluster-alias",
                "quantization": (up or {}).get("quantization"),
                "size_bytes": (up or {}).get("size_bytes") or 0,
                "max_context_length": ctx,
                "loaded_instances": (up or {}).get("loaded_instances") or [],
                "resolved_model": resolved,
                "resolved_node": res["node"],
                "format": "gguf",
            }
            if not is_emb:
                caps = {
                    "vision": bool(spec.get("vision")),
                    "trained_for_tool_use": bool(spec.get("tools")),
                }
                if spec.get("thinking") is not None:
                    caps["reasoning"] = {"allowed_options": ["off", "on"], "default": "on" if spec["thinking"] else "off"}
                entry["capabilities"] = caps
            out.append(entry)

        for m in state["models"]:
            k = m.get("key") or ""
            if is_hidden_internal_model(k):
                continue
            copy_m = dict(m)
            copy_m["max_context_length"] = self.tuned_context_for(k, m)
            out.append(copy_m)
        return {"models": out}

    def build_lmstudio_v0_models_payload(self):
        state = self.snapshot_state()
        items = []
        for alias in self.alias_specs:
            res = self.resolve_model(alias, state=state)
            up = state["by_key"].get(res["resolved"])
            if state["reachable"] and not up:
                continue
            items.append({
                "id": alias,
                "object": "model",
                "type": "embeddings" if self.alias_specs[alias].get("embedding") else "llm",
                "publisher": "cluster-gateway",
                "state": "loaded" if (up and up.get("loaded_instances")) else "not-loaded",
                "max_context_length": self.tuned_context_for(res["resolved"], up),
                "loaded_context_length": self.tuned_context_for(res["resolved"], up),
            })
        for m in state["models"]:
            k = m.get("key") or ""
            if is_hidden_internal_model(k):
                continue
            items.append({
                "id": k,
                "object": "model",
                "type": "embeddings" if m.get("type") == "embedding" else (m.get("type") or "llm"),
                "publisher": m.get("publisher") or "lmstudio",
                "arch": m.get("architecture"),
                "compatibility_type": m.get("format") or "gguf",
                "state": "loaded" if m.get("loaded_instances") else "not-loaded",
                "max_context_length": self.tuned_context_for(k, m),
            })
        return {"data": items}

    def validate_load_safety(self, model_key, body):
        k_q = body.get("k_cache_quant")
        v_q = body.get("v_cache_quant")
        ctx = body.get("context_length") or self.tuned_context_for(model_key)
        if model_key in HEAVY_TORRE_MODELS and int(ctx) >= 8192:
            if k_q == "f16" or (v_q == "f16" and model_key != "t-math-physics"):
                return (
                    f"Unsafe VRAM config blocked for {model_key} at context={ctx}: "
                    "12B-14B models on torre >= 8K must use q8_0 KV (or K=q8_0:V=f16 on phi3)."
                )
        if model_key == "t-math-physics" and v_q == "q8_0":
            return "Unsafe config blocked for t-math-physics (phi3): V=q8_0 aborts on llama.cpp CUDA; use K=q8_0:V=f16."
        return None


def _inject_defaults_for_alias(path, body, res):
    spec = res.get("spec")
    if not spec or spec.get("thinking") is not False:
        return
    if path == "/v1/chat/completions" and "reasoning_effort" not in body:
        body["reasoning_effort"] = "none"
    elif path == "/api/v1/chat" and "reasoning" not in body:
        body["reasoning"] = "off"


def _extract_response_id_from_obj(obj):
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("response_id"), str) and obj["response_id"]:
        return obj["response_id"]
    res_obj = obj.get("result")
    if isinstance(res_obj, dict) and isinstance(res_obj.get("response_id"), str) and res_obj["response_id"]:
        return res_obj["response_id"]
    if isinstance(obj.get("id"), str) and obj["id"].startswith("resp_"):
        return obj["id"]
    resp = obj.get("response")
    if isinstance(resp, dict) and isinstance(resp.get("id"), str) and resp["id"]:
        return resp["id"]
    return None


def _scan_sse_line_for_pin(line_bytes, router, resolved_model):
    s = line_bytes.decode("utf-8", "replace").strip()
    if not s.startswith("data:"):
        return
    payload = s[5:].strip()
    if not payload or payload == "[DONE]":
        return
    try:
        obj = json.loads(payload)
        rid = _extract_response_id_from_obj(obj)
        if rid:
            router.record_response_pin(rid, resolved_model)
    except Exception:
        pass


class GatewayHTTPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ClusterGateway/1.0"

    def log_message(self, fmt, *args):
        # Quiet unless CLUSTER_GATEWAY_DEBUG=1
        if os.environ.get("CLUSTER_GATEWAY_DEBUG") == "1":
            super().log_message(fmt, *args)

    @property
    def router(self) -> ClusterRouter:
        return self.server.router  # type: ignore[attr-defined]

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Api-Key, Anthropic-Version")

    def _send_json(self, status_code, obj, extra_headers=None):
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self._send_cors_headers()
        for k, v in (extra_headers or {}).items():
            if v:
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)
        self.wfile.flush()

    MAX_BODY_BYTES = 10 * 1024 * 1024

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or "0")
        if length > self.MAX_BODY_BYTES:
            raise ValueError(f"Request body too large ({length} bytes > {self.MAX_BODY_BYTES})")
        return self.rfile.read(length) if length > 0 else b""

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/health":
            state = self.router.snapshot_state()
            self._send_json(
                200,
                {
                    "ok": True,
                    "gateway": "cluster-hw-llm",
                    "upstream": self.router.upstream_url,
                    "local_node": self.router.local_node,
                    "upstream_reachable": state["reachable"],
                    "online_nodes": state["online_nodes"],
                },
            )
            return
        if path == "/cluster/status":
            self._send_json(200, self.router.build_status_payload())
            return
        if path == "/v1/models":
            self._send_json(200, self.router.build_openai_models_payload())
            return
        if path == "/api/v1/models":
            self._send_json(200, self.router.build_lmstudio_v1_models_payload())
            return
        if path == "/api/v0/models":
            self._send_json(200, self.router.build_lmstudio_v0_models_payload())
            return
        if path.startswith("/api/v1/models/download/status/"):
            self._proxy_request("GET", self.path, b"", None)
            return
        self._send_json(404, {"error": {"message": f"Unknown GET path: {path}", "type": "not_found"}})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        raw_body = self._read_body()

        if path == "/api/v1/models/load":
            self._handle_models_load(raw_body)
            return
        if path == "/api/v1/models/unload":
            self._handle_models_unload(raw_body)
            return
        if path == "/api/v1/models/download":
            self._proxy_request("POST", self.path, raw_body, None)
            return
        if path in (
            "/v1/chat/completions",
            "/v1/completions",
            "/v1/embeddings",
            "/v1/responses",
            "/v1/messages",
            "/api/v1/chat",
        ):
            self._handle_inference(path, raw_body)
            return
        self._send_json(404, {"error": {"message": f"Unknown POST path: {path}", "type": "not_found"}})

    def _handle_models_load(self, raw_body):
        try:
            body = json.loads(raw_body.decode("utf-8")) if raw_body.strip() else {}
        except Exception:
            self._send_json(400, {"error": {"message": "Invalid JSON body", "type": "invalid_request"}})
            return
        req_model = body.get("model")
        if not req_model:
            self._send_json(400, {"error": {"message": "Missing required field 'model'", "type": "invalid_request", "param": "model"}})
            return

        res = self.router.resolve_model(req_model)
        resolved = res["resolved"]
        err_msg = self.router.validate_load_safety(resolved, body)
        if err_msg:
            self._send_json(400, {"error": {"message": err_msg, "type": "unsafe_vram_config"}})
            return

        extra = {
            "X-Cluster-Alias": res["alias"],
            "X-Cluster-Resolved-Model": resolved,
            "X-Cluster-Node": res["node"],
        }
        override_keys = set(body.keys()) - {"model", "echo_load_config"}
        via_rest = os.environ.get("CLUSTER_GATEWAY_LOAD_VIA_REST") == "1"

        if not override_keys and res["node"] == self.router.local_node and not via_rest:
            lms = cluster_lib.resolve_lms_cli(self.router.cfg, node=self.router.local_node)
            try:
                r = subprocess.run([lms, "load", resolved, "-y"], stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=300)
                if r.returncode != 0:
                    self._send_json(502, {"error": {"message": (r.stderr or r.stdout or "lms load failed").strip()}}, extra_headers=extra)
                    return
                self.router.invalidate_cache()
                state = self.router.snapshot_state(force=True)
                inst = ((state["by_key"].get(resolved) or {}).get("loaded_instances") or [{}])[0]
                self._send_json(
                    200,
                    {
                        "type": "llm",
                        "instance_id": inst.get("id") or resolved,
                        "status": "loaded",
                        "load_config": inst.get("config") or {},
                        "default_inheriting": True,
                    },
                    extra_headers=extra,
                )
                return
            except Exception as exc:
                self._send_json(502, {"error": {"message": str(exc)}}, extra_headers=extra)
                return

        # Merge safe tuned defaults for explicit REST overrides so flash_attention/context aren't stripped.
        body["model"] = resolved
        if override_keys:
            lp = (self.router.tuned_models.get(resolved) or {}).get("load_params") or {}
            for k in ("context_length", "eval_batch_size", "flash_attention", "offload_kv_cache_to_gpu"):
                if k in lp and k not in body:
                    body[k] = lp[k]
        self._proxy_request("POST", "/api/v1/models/load", json.dumps(body).encode("utf-8"), res, invalidate_after=True)

    def _handle_models_unload(self, raw_body):
        try:
            body = json.loads(raw_body.decode("utf-8")) if raw_body.strip() else {}
        except Exception:
            self._send_json(400, {"error": {"message": "Invalid JSON body", "type": "invalid_request"}})
            return
        target = body.get("instance_id") or body.get("model")
        if not target:
            self._send_json(400, {"error": {"message": "Missing required field 'instance_id'", "type": "invalid_request", "param": "instance_id"}})
            return

        state = self.router.snapshot_state(force=True)
        resolved = target
        res = None
        if target in self.router.alias_specs:
            # Prefer unloading an actually loaded candidate matching the alias.
            cands, _ = self.router.ordered_candidates(target, state)
            chosen = next((c for c in cands if c in state["loaded_keys"]), cands[0])
            up_insts = (state["by_key"].get(chosen) or {}).get("loaded_instances") or []
            inst_id = up_insts[0].get("id") if up_insts else chosen
            resolved = chosen
            body = {"instance_id": inst_id}
            res = {"alias": target, "resolved": resolved, "node": self.router.node_for_model(resolved) or self.router.local_node}
        else:
            body["instance_id"] = target
            res = {"alias": "", "resolved": target, "node": self.router.node_for_model(target) or self.router.local_node}

        self._proxy_request("POST", "/api/v1/models/unload", json.dumps(body).encode("utf-8"), res, invalidate_after=True)

    def _handle_inference(self, path, raw_body):
        try:
            body = json.loads(raw_body.decode("utf-8")) if raw_body.strip() else {}
        except Exception:
            self._send_json(400, {"error": {"message": "Invalid JSON body", "type": "invalid_request"}})
            return

        req_model = body.get("model")
        if not req_model:
            self._send_json(400, {"error": {"message": "Missing required field 'model'", "type": "invalid_request", "param": "model"}})
            return

        prev_id = body.get("previous_response_id")
        res = self.router.resolve_model(req_model, previous_response_id=prev_id)
        if prev_id and not res.get("pinned"):
            body.pop("previous_response_id", None)
        body["model"] = res["resolved"]
        _inject_defaults_for_alias(path, body, res)
        self._proxy_request("POST", self.path, json.dumps(body).encode("utf-8"), res)

    def _proxy_request(self, method, target_path, body_bytes, res, invalidate_after=False):
        cls = http.client.HTTPSConnection if self.router.upstream_scheme == "https" else http.client.HTTPConnection
        conn = None
        try:
            conn = cls(self.router.upstream_host, self.router.upstream_port, timeout=600)
            fwd_headers = {}
            for k, v in self.headers.items():
                if k.lower() not in HOP_BY_HOP_HEADERS:
                    fwd_headers[k] = v
            if body_bytes is not None and method == "POST":
                fwd_headers["Content-Type"] = "application/json"
                fwd_headers["Content-Length"] = str(len(body_bytes))
            conn.request(method, target_path, body=body_bytes if method == "POST" else None, headers=fwd_headers)
            up_resp = conn.getresponse()

            ctype = up_resp.getheader("Content-Type") or "application/json"
            is_sse = "text/event-stream" in ctype.lower()

            extra_headers = {}
            if res:
                if res.get("alias"):
                    extra_headers["X-Cluster-Alias"] = res["alias"]
                if res.get("resolved"):
                    extra_headers["X-Cluster-Resolved-Model"] = res["resolved"]
                if res.get("node"):
                    extra_headers["X-Cluster-Node"] = res["node"]

            if is_sse:
                self.send_response(up_resp.status)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Transfer-Encoding", "chunked")
                self._send_cors_headers()
                for k, v in extra_headers.items():
                    if v:
                        self.send_header(k, v)
                self.end_headers()

                while True:
                    line = up_resp.fp.readline(65536)
                    if not line:
                        break
                    if res and res.get("resolved"):
                        _scan_sse_line_for_pin(line, self.router, res["resolved"])
                    self.wfile.write(f"{len(line):x}\r\n".encode("ascii") + line + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            else:
                payload = up_resp.read()
                if res and res.get("resolved") and "json" in ctype.lower() and payload:
                    try:
                        obj = json.loads(payload.decode("utf-8", "replace"))
                        rid = _extract_response_id_from_obj(obj)
                        if rid:
                            self.router.record_response_pin(rid, res["resolved"])
                    except Exception:
                        pass
                self.send_response(up_resp.status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(payload)))
                self._send_cors_headers()
                for k, v in extra_headers.items():
                    if v:
                        self.send_header(k, v)
                self.end_headers()
                if payload:
                    self.wfile.write(payload)
                    self.wfile.flush()

            if invalidate_after:
                self.router.invalidate_cache()
        except Exception as exc:
            self.router.invalidate_cache()
            self._send_json(502, {"error": {"message": f"Upstream LM Studio error: {exc}", "type": "bad_gateway"}})
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass


class ClusterGatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, router: ClusterRouter):
        self.router = router
        super().__init__(server_address, GatewayHTTPHandler)


def main(argv=None):
    cfg = cluster_lib.load_cluster_config()
    gw_cfg = cfg.get("gateway") or {}
    ap = argparse.ArgumentParser(description="5-protocol Cluster Router Gateway for LM Studio + LM Link")
    ap.add_argument("--host", default=os.environ.get("CLUSTER_GATEWAY_HOST") or gw_cfg.get("host") or "127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("CLUSTER_GATEWAY_PORT") or gw_cfg.get("port") or 1235))
    ap.add_argument("--upstream", default=os.environ.get("CLUSTER_GATEWAY_UPSTREAM") or gw_cfg.get("upstream_url") or "http://127.0.0.1:1234")
    ap.add_argument("--repo-root", default=REPO_ROOT)
    args = ap.parse_args(argv)

    router = ClusterRouter(upstream_url=args.upstream, repo_root=args.repo_root)
    srv = ClusterGatewayServer((args.host, args.port), router)
    print(f"[cluster-gateway] listening on http://{args.host}:{args.port} -> upstream {router.upstream_url} (local_node={router.local_node})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
