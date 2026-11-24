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

NIM_BASE_URL = "https://integrate.api.nvidia.com"
NIM_HOST = "integrate.api.nvidia.com"
NIM_PORT = 443

NIM_ALIAS_SPECS = {
    "nim/ultra": {
        "candidates": ["nvidia/nemotron-3-ultra-550b-a55b"],
        "cloud": "nvidia-nim",
        "context_length": 131072,
        "max_output_tokens": 16384,
        "tools": True,
        "vision": False,
        "thinking": None,
        "orchestrator": True,
        "description": "NVIDIA NIM Flagship 550B-A55B MoE Cloud Orchestrator & Coder (0 local VRAM)",
    },
    "nim/fast": {
        "candidates": ["nvidia/nemotron-3.5-lightning-30b-a3b"],
        "cloud": "nvidia-nim",
        "context_length": 131072,
        "max_output_tokens": 8192,
        "tools": True,
        "vision": False,
        "thinking": False,
        "orchestrator": True,
        "description": "NVIDIA NIM Ultra-Low-Latency 30B-A3B Lightning Cloud Agent & Orchestrator",
    },
    "nim/vision": {
        "candidates": ["nvidia/nemotron-3.5-super-vl-120b-a12b"],
        "cloud": "nvidia-nim",
        "context_length": 131072,
        "max_output_tokens": 8192,
        "tools": True,
        "vision": True,
        "thinking": False,
        "description": "NVIDIA NIM 120B-A12B Super-VL Multimodal Vision-Language Cloud Resource",
    },
    "nim/reasoning": {
        "candidates": ["nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"],
        "cloud": "nvidia-nim",
        "context_length": 131072,
        "max_output_tokens": 8192,
        "tools": True,
        "vision": False,
        "thinking": True,
        "description": "NVIDIA NIM 30B-A3B Chain-of-Thought Cloud Reasoning Resource",
    },
    "nim/code": {
        "candidates": ["poolside/laguna-xs-2.1"],
        "cloud": "nvidia-nim",
        "context_length": 65536,
        "max_output_tokens": 8192,
        "tools": True,
        "vision": False,
        "thinking": None,
        "description": "NVIDIA NIM Poolside Laguna-XS 2.1 Cloud Code Generation Resource",
    },
    "nim/mistral-large": {
        "candidates": ["mistralai/mistral-large-4-1t-a52b"],
        "cloud": "nvidia-nim",
        "context_length": 131072,
        "max_output_tokens": 16384,
        "tools": True,
        "vision": False,
        "thinking": None,
        "description": "NVIDIA NIM Mistral Large 4 (1T-A52B MoE) Frontier Cloud Resource",
    },
    "nim/kimi": {
        "candidates": ["moonshotai/kimi-k3.1-pro"],
        "cloud": "nvidia-nim",
        "context_length": 131072,
        "max_output_tokens": 16384,
        "tools": True,
        "vision": False,
        "thinking": True,
        "description": "NVIDIA NIM Moonshot Kimi K3.1 Pro Long-Context Reasoning & Coding",
    },
    "nim/step": {
        "candidates": ["stepfun-ai/step-5-preview"],
        "cloud": "nvidia-nim",
        "context_length": 131072,
        "max_output_tokens": 16384,
        "tools": True,
        "vision": False,
        "thinking": True,
        "description": "NVIDIA NIM StepFun Step-5 Preview Frontier Reasoning Resource",
    },
    "nim/glm-flash": {
        "candidates": ["z-ai/glm-5.3-flash"],
        "cloud": "nvidia-nim",
        "context_length": 131072,
        "max_output_tokens": 8192,
        "tools": True,
        "vision": False,
        "thinking": False,
        "description": "NVIDIA NIM GLM-5.3 Flash High-Speed Coding & Tool-Calling Resource",
    },
    "nim/minimax-flash": {
        "candidates": ["minimaxai/minimax-m3.1-flash"],
        "cloud": "nvidia-nim",
        "context_length": 131072,
        "max_output_tokens": 8192,
        "tools": True,
        "vision": False,
        "thinking": False,
        "description": "NVIDIA NIM MiniMax M3.1 Flash High-Speed Long-Context Assistant",
    },
    "nim/gemma-31b": {
        "candidates": ["google/gemma-4-31b-it"],
        "cloud": "nvidia-nim",
        "context_length": 131072,
        "max_output_tokens": 8192,
        "tools": True,
        "vision": False,
        "thinking": False,
        "description": "NVIDIA NIM Google Gemma-4 31B Dense Instruction & Code Resource",
    },
    "nim/embed": {
        "candidates": ["nvidia/nemotron-3.5-embed-8b"],
        "cloud": "nvidia-nim",
        "context_length": 8192,
        "max_output_tokens": 0,
        "tools": False,
        "vision": False,
        "thinking": None,
        "embedding": True,
        "description": "NVIDIA NIM Nemotron-3.5 8B Cloud Semantic Embeddings",
    },
}

NIM_DIRECT_MODEL_IDS = {spec["candidates"][0]: alias for alias, spec in NIM_ALIAS_SPECS.items()}

GEMINI_ALIAS_SPECS = {
    "gemini/orchestrator": {
        "candidates": ["gemini-pro-orchestrator-neon"],
        "cloud": "gemini-pro",
        "context_length": 1000000,
        "max_output_tokens": 65536,
        "tools": True,
        "vision": False,
        "thinking": True,
        "orchestrator": True,
        "description": "Gemini Pro Flagship Autonomous Cloud Orchestrator (Antigravity subscription, 0 VRAM)",
    },
    "gemini/pro": {
        "candidates": ["gemini-4-neon-medium"],
        "cloud": "gemini-pro",
        "context_length": 1000000,
        "max_output_tokens": 65536,
        "tools": True,
        "vision": False,
        "thinking": True,
        "orchestrator": False,
        "description": "Gemini Pro Flagship Architect, Code & Deep Reasoning (Antigravity subscription, 0 VRAM)",
    },
    "gemini/flash": {
        "candidates": ["gemini-3.8-flash-medium"],
        "cloud": "gemini-pro",
        "context_length": 1000000,
        "max_output_tokens": 65536,
        "tools": True,
        "vision": False,
        "thinking": False,
        "orchestrator": False,
        "description": "Gemini Pro Fast Cloud Assistant & Edits (Antigravity subscription, 0 VRAM)",
    },
}

GEMINI_DIRECT_MODEL_IDS = {spec["candidates"][0]: alias for alias, spec in GEMINI_ALIAS_SPECS.items()}

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
        self._cloud_sessions = OrderedDict()  # response_id -> {"model": str, "alias": str, "messages": list}

    def get_nvidia_api_key(self):
        return cluster_lib.resolve_nvidia_api_key(self.cfg)

    def is_gemini_pro_available(self):
        return cluster_lib.is_gemini_pro_configured(self.cfg)

    def record_cloud_session(self, response_id, concrete_model, alias, messages, cloud="nvidia-nim", conversation_id=""):
        if not response_id or not concrete_model:
            return
        with self._lock:
            self._cloud_sessions[response_id] = {
                "model": concrete_model,
                "alias": alias or "",
                "cloud": cloud or "nvidia-nim",
                "conversation_id": conversation_id or "",
                "messages": list(messages or []),
            }
            self._cloud_sessions.move_to_end(response_id)
            while len(self._cloud_sessions) > 256:
                self._cloud_sessions.popitem(last=False)
        self.record_response_pin(response_id, concrete_model)

    def get_cloud_session(self, response_id):
        if not response_id:
            return None
        with self._lock:
            sess = self._cloud_sessions.get(response_id)
            if sess:
                self._cloud_sessions.move_to_end(response_id)
                return dict(sess)
            return None

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
        for k, v in NIM_ALIAS_SPECS.items():
            specs[k] = dict(v)
        for k, v in GEMINI_ALIAS_SPECS.items():
            specs[k] = dict(v)
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
        for spec in NIM_ALIAS_SPECS.values():
            if model_key in spec.get("candidates", ()):
                return int(spec.get("context_length") or 131072)
        if model_key in NIM_ALIAS_SPECS:
            return int(NIM_ALIAS_SPECS[model_key].get("context_length") or 131072)
        for spec in GEMINI_ALIAS_SPECS.values():
            if model_key in spec.get("candidates", ()):
                return int(spec.get("context_length") or 1000000)
        if model_key in GEMINI_ALIAS_SPECS:
            return int(GEMINI_ALIAS_SPECS[model_key].get("context_length") or 1000000)
        info = self.tuned_models.get(model_key)
        if info and info.get("context_length"):
            return int(info["context_length"])
        if upstream_item and upstream_item.get("max_context_length"):
            return int(upstream_item["max_context_length"])
        return 8192

    def is_nim_model(self, model_key):
        if not model_key:
            return False
        if model_key in NIM_ALIAS_SPECS or model_key in NIM_DIRECT_MODEL_IDS:
            return True
        if model_key.startswith("nim/"):
            return True
        return False

    def is_gemini_model(self, model_key):
        if not model_key:
            return False
        if model_key in GEMINI_ALIAS_SPECS or model_key in GEMINI_DIRECT_MODEL_IDS:
            return True
        if model_key.startswith("gemini/"):
            return True
        return False

    def resolve_model(self, requested_model, previous_response_id=None, state=None):
        if state is None:
            state = self.snapshot_state()

        if previous_response_id:
            cloud_sess = self.get_cloud_session(previous_response_id)
            if cloud_sess:
                c_mod = cloud_sess["model"]
                cloud_kind = cloud_sess.get("cloud") or "nvidia-nim"
                c_alias = cloud_sess.get("alias") or (
                    GEMINI_DIRECT_MODEL_IDS.get(c_mod, "") if cloud_kind == "gemini-pro" else NIM_DIRECT_MODEL_IDS.get(c_mod, "")
                )
                return {
                    "alias": c_alias,
                    "resolved": c_mod,
                    "node": cloud_kind,
                    "cloud": cloud_kind,
                    "diverted": False,
                    "pinned": True,
                    "spec": self.alias_specs.get(c_alias),
                }
            pinned = self.get_pinned_model(previous_response_id)
            if pinned and (pinned in state["by_key"] or not state["reachable"] or self.is_nim_model(pinned) or self.is_gemini_model(pinned)):
                is_nim = self.is_nim_model(pinned)
                is_gem = self.is_gemini_model(pinned)
                cloud_kind = "gemini-pro" if is_gem else ("nvidia-nim" if is_nim else None)
                return {
                    "alias": requested_model if requested_model in self.alias_specs else "",
                    "resolved": pinned,
                    "node": cloud_kind if cloud_kind else (self.node_for_model(pinned) or self.local_node),
                    "cloud": cloud_kind,
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
            if spec.get("cloud") in ("nvidia-nim", "gemini-pro"):
                cloud_kind = spec["cloud"]
                chosen = spec["candidates"][0]
                return {
                    "alias": requested_model,
                    "resolved": chosen,
                    "node": cloud_kind,
                    "cloud": cloud_kind,
                    "diverted": False,
                    "pinned": False,
                    "spec": spec,
                }
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
                "cloud": None,
                "diverted": diverted,
                "pinned": False,
                "spec": spec,
            }

        if requested_model in NIM_DIRECT_MODEL_IDS:
            alias = NIM_DIRECT_MODEL_IDS[requested_model]
            spec = self.alias_specs.get(alias)
            return {
                "alias": alias,
                "resolved": requested_model,
                "node": "nvidia-nim",
                "cloud": "nvidia-nim",
                "diverted": False,
                "pinned": False,
                "spec": spec,
            }

        if requested_model in GEMINI_DIRECT_MODEL_IDS:
            alias = GEMINI_DIRECT_MODEL_IDS[requested_model]
            spec = self.alias_specs.get(alias)
            return {
                "alias": alias,
                "resolved": requested_model,
                "node": "gemini-pro",
                "cloud": "gemini-pro",
                "diverted": False,
                "pinned": False,
                "spec": spec,
            }

        if requested_model.startswith("nim/") and requested_model.count("/") >= 2:
            raw_nim = requested_model.split("/", 1)[1]
            return {
                "alias": requested_model,
                "resolved": raw_nim,
                "node": "nvidia-nim",
                "cloud": "nvidia-nim",
                "diverted": False,
                "pinned": False,
                "spec": {"cloud": "nvidia-nim", "tools": True, "vision": False, "context_length": 131072},
            }

        if requested_model.startswith("gemini/") and len(requested_model) > 7:
            raw_gem = requested_model.split("/", 1)[1]
            return {
                "alias": requested_model,
                "resolved": raw_gem,
                "node": "gemini-pro",
                "cloud": "gemini-pro",
                "diverted": False,
                "pinned": False,
                "spec": {"cloud": "gemini-pro", "tools": True, "vision": False, "context_length": 1000000, "max_output_tokens": 65536},
            }

        return {
            "alias": "",
            "resolved": requested_model,
            "node": self.node_for_model(requested_model) or self.local_node,
            "cloud": None,
            "diverted": False,
            "pinned": False,
            "spec": None,
        }

    def build_status_payload(self):
        state = self.snapshot_state(force=True)
        has_nim = bool(self.get_nvidia_api_key())
        has_gemini = self.is_gemini_pro_available()
        aliases = {}
        for alias, spec in self.alias_specs.items():
            cloud_kind = spec.get("cloud")
            is_nim = cloud_kind == "nvidia-nim"
            is_gem = cloud_kind == "gemini-pro"
            res = self.resolve_model(alias, state=state)
            resolved = res["resolved"]
            online = has_nim if is_nim else (has_gemini if is_gem else (resolved in state["by_key"]))
            loaded = has_nim if is_nim else (has_gemini if is_gem else (resolved in state["loaded_keys"]))
            aliases[alias] = {
                "resolved": resolved,
                "node": res["node"],
                "cloud": cloud_kind,
                "orchestrator": bool(spec.get("orchestrator")),
                "online": online,
                "loaded": loaded,
                "context_length": self.tuned_context_for(resolved, state["by_key"].get(resolved)),
                "anti_thrash_diverted": res["diverted"],
                "description": spec.get("description", ""),
            }
        with self._lock:
            pinned_count = len(self._pin_map)
        online_nodes = list(state["online_nodes"])
        if has_nim and "nvidia-nim" not in online_nodes:
            online_nodes.append("nvidia-nim")
        if has_gemini and "gemini-pro" not in online_nodes:
            online_nodes.append("gemini-pro")
        return {
            "ok": True,
            "gateway": "cluster-hw-llm",
            "upstream": self.upstream_url,
            "upstream_reachable": state["reachable"],
            "nvidia_nim_configured": has_nim,
            "gemini_pro_configured": has_gemini,
            "local_node": self.local_node,
            "online_nodes": online_nodes,
            "loaded_by_node": state["loaded_by_node"],
            "torre_heavy_loaded": state["torre_heavy_loaded"],
            "pinned_sessions": pinned_count,
            "aliases": aliases,
        }

    def build_openai_models_payload(self):
        state = self.snapshot_state()
        has_nim = bool(self.get_nvidia_api_key())
        has_gemini = self.is_gemini_pro_available()
        now_ts = int(time.time())
        items = []
        for alias, spec in self.alias_specs.items():
            cloud_kind = spec.get("cloud")
            if cloud_kind == "nvidia-nim" and not has_nim:
                continue
            if cloud_kind == "gemini-pro" and not has_gemini:
                continue
            res = self.resolve_model(alias, state=state)
            if not cloud_kind and state["reachable"] and res["resolved"] not in state["by_key"]:
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
        has_nim = bool(self.get_nvidia_api_key())
        has_gemini = self.is_gemini_pro_available()
        out = []
        for alias, spec in self.alias_specs.items():
            cloud_kind = spec.get("cloud")
            if cloud_kind == "nvidia-nim" and not has_nim:
                continue
            if cloud_kind == "gemini-pro" and not has_gemini:
                continue
            is_cloud = bool(cloud_kind)
            res = self.resolve_model(alias, state=state)
            resolved = res["resolved"]
            up = state["by_key"].get(resolved)
            if not is_cloud and state["reachable"] and not up:
                continue
            ctx = self.tuned_context_for(resolved, up)
            is_emb = bool(spec.get("embedding"))
            arch = "gemini-pro-subscription" if cloud_kind == "gemini-pro" else ("nvidia-nim-cloud" if cloud_kind == "nvidia-nim" else ((up or {}).get("architecture") or "cluster-alias"))
            quant = "subscription" if cloud_kind == "gemini-pro" else ("cloud-fp8" if cloud_kind == "nvidia-nim" else (up or {}).get("quantization"))
            entry = {
                "type": "embedding" if is_emb else "llm",
                "publisher": cloud_kind if is_cloud else "cluster-gateway",
                "key": alias,
                "display_name": f"{alias} -> {resolved}",
                "architecture": arch,
                "quantization": quant,
                "size_bytes": (up or {}).get("size_bytes") or 0,
                "max_context_length": ctx,
                "loaded_instances": [{"id": resolved, "config": {"context_length": ctx}}] if is_cloud else ((up or {}).get("loaded_instances") or []),
                "resolved_model": resolved,
                "resolved_node": res["node"],
                "format": "cloud" if is_cloud else "gguf",
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
        has_nim = bool(self.get_nvidia_api_key())
        has_gemini = self.is_gemini_pro_available()
        items = []
        for alias, spec in self.alias_specs.items():
            cloud_kind = spec.get("cloud")
            if cloud_kind == "nvidia-nim" and not has_nim:
                continue
            if cloud_kind == "gemini-pro" and not has_gemini:
                continue
            is_cloud = bool(cloud_kind)
            res = self.resolve_model(alias, state=state)
            up = state["by_key"].get(res["resolved"])
            if not is_cloud and state["reachable"] and not up:
                continue
            items.append({
                "id": alias,
                "object": "model",
                "type": "embeddings" if spec.get("embedding") else "llm",
                "publisher": cloud_kind if is_cloud else "cluster-gateway",
                "state": "loaded" if (is_cloud or (up and up.get("loaded_instances"))) else "not-loaded",
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


def _extract_content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") in ("text", "input_text", "output_text") and item.get("text"):
                    parts.append(str(item["text"]))
                elif item.get("content"):
                    parts.append(_extract_content_text(item["content"]))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(p for p in parts if p)
    return str(content) if content is not None else ""


def _format_oai_messages_for_agy(messages, system_prompt=None):
    """Flatten OpenAI-style chat messages into a clean prompt string for `agy -p`."""
    sys_parts = []
    if system_prompt:
        sys_parts.append(str(system_prompt).strip())
    turns = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            turns.append(("user", str(msg)))
            continue
        role = str(msg.get("role") or "user").strip().lower()
        txt = _extract_content_text(msg.get("content")).strip()
        if not txt:
            continue
        if role in ("system", "developer"):
            sys_parts.append(txt)
        else:
            turns.append((role, txt))

    if len(turns) == 1 and turns[0][0] == "user":
        if sys_parts:
            return f"[System Instructions]\n" + "\n\n".join(sys_parts) + f"\n\n{turns[0][1]}"
        return turns[0][1]

    blocks = []
    if sys_parts:
        blocks.append("[System]\n" + "\n\n".join(sys_parts))
    for role, txt in turns:
        label = "Assistant" if role == "assistant" else ("Tool" if role == "tool" else "User")
        blocks.append(f"[{label}]\n{txt}")
    return "\n\n".join(blocks)


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
            online_nodes = list(state["online_nodes"])
            if self.router.get_nvidia_api_key() and "nvidia-nim" not in online_nodes:
                online_nodes.append("nvidia-nim")
            if self.router.is_gemini_pro_available() and "gemini-pro" not in online_nodes:
                online_nodes.append("gemini-pro")
            self._send_json(
                200,
                {
                    "ok": True,
                    "gateway": "cluster-hw-llm",
                    "upstream": self.router.upstream_url,
                    "local_node": self.router.local_node,
                    "upstream_reachable": state["reachable"],
                    "online_nodes": online_nodes,
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
        if res.get("cloud") == "nvidia-nim":
            self._handle_nim_inference(path, body, res, prev_id=prev_id)
            return
        if res.get("cloud") == "gemini-pro":
            self._handle_agy_inference(path, body, res, prev_id=prev_id)
            return
        if prev_id and not res.get("pinned"):
            body.pop("previous_response_id", None)
        body["model"] = res["resolved"]
        _inject_defaults_for_alias(path, body, res)
        self._proxy_request("POST", self.path, json.dumps(body).encode("utf-8"), res)

    def _handle_nim_inference(self, path, body, res, prev_id=None):
        nv_key = self.router.get_nvidia_api_key()
        if not nv_key:
            self._send_json(
                401,
                {
                    "error": {
                        "message": "NVIDIA NIM API key is not configured (~/.config/nvidia_api_key or NVIDIA_API_KEY).",
                        "type": "authentication_error",
                    }
                },
            )
            return

        extra_headers = {
            "X-Cluster-Alias": res.get("alias") or "",
            "X-Cluster-Resolved-Model": res["resolved"],
            "X-Cluster-Node": "nvidia-nim",
        }

        if path == "/api/v1/chat":
            self._handle_nim_native_chat(body, res, nv_key, extra_headers, prev_id=prev_id)
            return

        target_path = path
        out_body = dict(body)
        out_body["model"] = res["resolved"]
        out_body.pop("previous_response_id", None)
        if path == "/v1/embeddings" and "encoding_format" not in out_body:
            out_body["encoding_format"] = "float"
            out_body.setdefault("input_type", "query")

        raw_bytes = json.dumps(out_body).encode("utf-8")
        conn = None
        try:
            conn = http.client.HTTPSConnection(NIM_HOST, NIM_PORT, timeout=300)
            hdrs = {
                "Authorization": f"Bearer {nv_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream" if out_body.get("stream") else "application/json",
                "Content-Length": str(len(raw_bytes)),
            }
            conn.request("POST", target_path, body=raw_bytes, headers=hdrs)
            up_resp = conn.getresponse()
            ctype = up_resp.getheader("Content-Type") or "application/json"
            is_sse = "text/event-stream" in ctype.lower()

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
                    self.wfile.write(f"{len(line):x}\r\n".encode("ascii") + line + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            else:
                payload = up_resp.read()
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
        except Exception as exc:
            self._send_json(502, {"error": {"message": f"NVIDIA NIM upstream error: {exc}", "type": "bad_gateway"}}, extra_headers=extra_headers)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def _handle_nim_native_chat(self, body, res, nv_key, extra_headers, prev_id=None):
        """Translate LM Studio native /api/v1/chat requests into NVIDIA NIM /v1/chat/completions (JSON or SSE)."""
        messages = []
        prior_sess = self.router.get_cloud_session(prev_id) if prev_id else None
        if prior_sess and prior_sess.get("messages"):
            messages.extend(prior_sess["messages"])
        elif body.get("system_prompt"):
            messages.append({"role": "system", "content": str(body["system_prompt"])})

        inp = body.get("input")
        if isinstance(inp, list):
            for item in inp:
                if isinstance(item, dict) and item.get("role") and item.get("content") is not None:
                    messages.append({"role": item["role"], "content": item["content"]})
                elif isinstance(item, str):
                    messages.append({"role": "user", "content": item})
        elif inp is not None:
            messages.append({"role": "user", "content": str(inp)})

        spec = res.get("spec") or {}
        max_out = int(body.get("max_output_tokens") or body.get("max_tokens") or spec.get("max_output_tokens") or 4096)
        stream = bool(body.get("stream"))
        oai_payload = {
            "model": res["resolved"],
            "messages": messages,
            "max_tokens": max_out,
            "stream": stream,
        }
        if "temperature" in body:
            oai_payload["temperature"] = body["temperature"]

        raw_bytes = json.dumps(oai_payload).encode("utf-8")
        t0 = time.monotonic()
        conn = None
        try:
            conn = http.client.HTTPSConnection(NIM_HOST, NIM_PORT, timeout=300)
            hdrs = {
                "Authorization": f"Bearer {nv_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream" if stream else "application/json",
                "Content-Length": str(len(raw_bytes)),
            }
            conn.request("POST", "/v1/chat/completions", body=raw_bytes, headers=hdrs)
            up_resp = conn.getresponse()
            if up_resp.status >= 400:
                err_raw = up_resp.read().decode("utf-8", "replace")
                self._send_json(up_resp.status, {"error": {"message": f"NVIDIA NIM HTTP {up_resp.status}: {err_raw}"}}, extra_headers=extra_headers)
                return

            rid = f"resp_nim_{int(time.time() * 1000)}_{os.urandom(3).hex()}"
            if stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Transfer-Encoding", "chunked")
                self._send_cors_headers()
                for k, v in extra_headers.items():
                    if v:
                        self.send_header(k, v)
                self.end_headers()

                def _write_sse(ev_name, data_obj):
                    chunk = f"event: {ev_name}\ndata: {json.dumps(data_obj)}\n\n".encode("utf-8")
                    self.wfile.write(f"{len(chunk):x}\r\n".encode("ascii") + chunk + b"\r\n")
                    self.wfile.flush()

                text_parts = []
                reason_parts = []
                ttft = None
                while True:
                    raw_line = up_resp.fp.readline(65536)
                    if not raw_line:
                        break
                    s = raw_line.decode("utf-8", "replace").strip()
                    if not s.startswith("data:"):
                        continue
                    d_str = s[5:].strip()
                    if not d_str or d_str == "[DONE]":
                        continue
                    try:
                        ev = json.loads(d_str)
                    except Exception:
                        continue
                    choices = ev.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    r_piece = delta.get("reasoning_content") or ""
                    c_piece = delta.get("content") or ""
                    if (r_piece or c_piece) and ttft is None:
                        ttft = time.monotonic() - t0
                    if r_piece:
                        reason_parts.append(r_piece)
                        _write_sse("reasoning.delta", {"type": "reasoning.delta", "content": r_piece})
                    if c_piece:
                        text_parts.append(c_piece)
                        _write_sse("message.delta", {"type": "message.delta", "content": c_piece})

                full_text = "".join(text_parts)
                full_reason = "".join(reason_parts)
                if not full_text and full_reason:
                    full_text = full_reason
                elapsed = max(0.001, time.monotonic() - t0)
                approx_toks = max(1, len((full_text + full_reason).split()))
                stats = {
                    "tokens_per_second": round(approx_toks / elapsed, 2),
                    "time_to_first_token_seconds": round(ttft or elapsed, 3),
                }
                if body.get("store", True):
                    new_msgs = list(messages) + [{"role": "assistant", "content": full_text}]
                    self.router.record_cloud_session(rid, res["resolved"], res.get("alias"), new_msgs)
                _write_sse(
                    "chat.end",
                    {
                        "type": "chat.end",
                        "result": {
                            "response_id": rid,
                            "model_instance_id": res["resolved"],
                            "stats": stats,
                        },
                    },
                )
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
                return

            raw_resp = up_resp.read().decode("utf-8", "replace")
            elapsed = max(0.001, time.monotonic() - t0)
            data = json.loads(raw_resp) if raw_resp.strip() else {}
            msg = ((data.get("choices") or [{}])[0]).get("message") or {}
            content_txt = str(msg.get("content") or "").strip()
            reason_txt = str(msg.get("reasoning_content") or "").strip()
            if not content_txt and reason_txt:
                content_txt = reason_txt
                reason_txt = ""
            out_items = []
            if reason_txt:
                out_items.append({"type": "reasoning", "content": reason_txt})
            out_items.append({"type": "message", "content": content_txt})
            usage = data.get("usage") or {}
            comp_toks = int(usage.get("completion_tokens") or max(1, len(content_txt.split())))
            if body.get("store", True):
                new_msgs = list(messages) + [{"role": "assistant", "content": content_txt}]
                self.router.record_cloud_session(rid, res["resolved"], res.get("alias"), new_msgs)
            self._send_json(
                200,
                {
                    "response_id": rid,
                    "model_instance_id": res["resolved"],
                    "output": out_items,
                    "stats": {
                        "tokens_per_second": round(comp_toks / elapsed, 2),
                        "completion_tokens": comp_toks,
                        "prompt_tokens": usage.get("prompt_tokens"),
                    },
                },
                extra_headers=extra_headers,
            )
        except Exception as exc:
            self._send_json(502, {"error": {"message": f"NVIDIA NIM /api/v1/chat error: {exc}", "type": "bad_gateway"}}, extra_headers=extra_headers)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def _resolve_agy_cwd(self, body):
        req_cwd = body.get("cwd")
        if os.name == "nt":
            # Windows `agy` cmd wraps via `wsl.exe -e /bin/sh -c 'cd "$HOME" && exec ...'`
            return None
        if isinstance(req_cwd, str) and req_cwd.strip() and os.path.isdir(req_cwd.strip()):
            return req_cwd.strip()
        return os.path.expanduser("~")

    def _write_http_chunk(self, raw_bytes):
        self.wfile.write(f"{len(raw_bytes):x}\r\n".encode("ascii") + raw_bytes + b"\r\n")
        self.wfile.flush()

    def _handle_agy_inference(self, path, body, res, prev_id=None):
        """Bridge `/v1/chat/completions`, `/api/v1/chat`, `/v1/responses`, `/v1/messages` to OAuth `agy -p`."""
        extra_headers = {
            "X-Cluster-Alias": res.get("alias") or "",
            "X-Cluster-Resolved-Model": res["resolved"],
            "X-Cluster-Node": "gemini-pro",
        }
        if path == "/v1/embeddings":
            self._send_json(400, {"error": {"message": "Gemini Pro models do not support /v1/embeddings; use cluster/embed.", "type": "invalid_request"}}, extra_headers=extra_headers)
            return

        agy_cmd = cluster_lib.resolve_agy_cli_cmd(self.router.cfg)
        if not agy_cmd:
            self._send_json(
                503,
                {"error": {"message": "Gemini Pro (agy CLI) is not available or authenticated on this host.", "type": "service_unavailable"}},
                extra_headers=extra_headers,
            )
            return

        conv_id = ""
        prior_sess = self.router.get_cloud_session(prev_id) if prev_id else None
        if prior_sess and prior_sess.get("cloud") == "gemini-pro" and prior_sess.get("conversation_id"):
            conv_id = prior_sess["conversation_id"]

        if path == "/api/v1/chat":
            inp = body.get("input")
            prompt_text = _format_oai_messages_for_agy(
                inp if isinstance(inp, list) else [{"role": "user", "content": inp}],
                system_prompt=None if conv_id else body.get("system_prompt"),
            )
        elif path == "/v1/responses":
            inp = body.get("input")
            prompt_text = _format_oai_messages_for_agy(
                inp if isinstance(inp, list) else [{"role": "user", "content": inp}],
                system_prompt=None if conv_id else body.get("instructions"),
            )
        elif path == "/v1/messages":
            prompt_text = _format_oai_messages_for_agy(
                body.get("messages") or [],
                system_prompt=body.get("system"),
            )
        else:
            prompt_text = _format_oai_messages_for_agy(body.get("messages") or [])

        if not prompt_text.strip():
            self._send_json(400, {"error": {"message": "Empty prompt in request", "type": "invalid_request"}}, extra_headers=extra_headers)
            return

        stream = bool(body.get("stream"))
        timeout_s = max(30, min(600, int(body.get("timeout_s") or 300)))
        req_model_name = body.get("model") or res.get("alias") or res["resolved"]
        argv = list(agy_cmd) + [
            "-p",
            prompt_text,
            "--model",
            res["resolved"],
            "--mode",
            "auto",
            "--dangerously-skip-permissions",
            "--disable-slash-commands",
            "--output-format",
            "stream-json" if stream else "json",
            "--print-timeout",
            f"{timeout_s}s",
        ]
        if conv_id:
            argv.extend(["--conversation", conv_id])

        work_cwd = self._resolve_agy_cwd(body)
        t0 = time.monotonic()

        if stream:
            self._handle_agy_stream(path, argv, work_cwd, body, res, req_model_name, extra_headers, t0)
            return

        try:
            r = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                cwd=work_cwd,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=timeout_s + 15,
            )
        except Exception as exc:
            self._send_json(502, {"error": {"message": f"Gemini Pro agy CLI error: {exc}", "type": "bad_gateway"}}, extra_headers=extra_headers)
            return

        obj = {}
        for line in reversed((r.stdout or "").splitlines()):
            s = line.strip()
            if s.startswith("{"):
                try:
                    obj = json.loads(s)
                    break
                except Exception:
                    continue

        if r.returncode != 0 or obj.get("status") == "ERROR":
            err_msg = obj.get("error") or (r.stderr or r.stdout or f"agy exited {r.returncode}").strip()
            self._send_json(502, {"error": {"message": f"Gemini Pro error: {err_msg}", "type": "bad_gateway"}}, extra_headers=extra_headers)
            return

        elapsed = max(0.001, time.monotonic() - t0)
        new_conv = obj.get("conversation_id") or conv_id
        rid = f"resp_agy_{new_conv}" if new_conv else f"resp_agy_{int(time.time() * 1000)}_{os.urandom(3).hex()}"
        resp_text = str(obj.get("response") or "").strip()
        usage = obj.get("usage") or {}
        in_toks = int(usage.get("input_tokens") or 0)
        out_toks = int(usage.get("output_tokens") or max(1, len(resp_text.split())))
        tot_toks = int(usage.get("total_tokens") or (in_toks + out_toks))

        if body.get("store", True):
            self.router.record_cloud_session(
                rid,
                res["resolved"],
                res.get("alias"),
                [],
                cloud="gemini-pro",
                conversation_id=new_conv,
            )

        if path == "/api/v1/chat":
            self._send_json(
                200,
                {
                    "response_id": rid,
                    "model_instance_id": res["resolved"],
                    "conversation_id": new_conv,
                    "output": [{"type": "message", "content": resp_text}],
                    "stats": {
                        "tokens_per_second": round(out_toks / elapsed, 2),
                        "completion_tokens": out_toks,
                        "prompt_tokens": in_toks,
                    },
                },
                extra_headers=extra_headers,
            )
            return

        if path == "/v1/responses":
            self._send_json(
                200,
                {
                    "id": rid,
                    "object": "response",
                    "created_at": int(time.time()),
                    "model": req_model_name,
                    "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": resp_text}]}],
                    "output_text": resp_text,
                    "usage": {"input_tokens": in_toks, "output_tokens": out_toks, "total_tokens": tot_toks},
                },
                extra_headers=extra_headers,
            )
            return

        if path == "/v1/messages":
            self._send_json(
                200,
                {
                    "id": rid,
                    "type": "message",
                    "role": "assistant",
                    "model": req_model_name,
                    "content": [{"type": "text", "text": resp_text}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": in_toks, "output_tokens": out_toks},
                },
                extra_headers=extra_headers,
            )
            return

        self._send_json(
            200,
            {
                "id": f"chatcmpl_agy_{os.urandom(4).hex()}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": req_model_name,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": resp_text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": in_toks, "completion_tokens": out_toks, "total_tokens": tot_toks},
            },
            extra_headers=extra_headers,
        )

    def _handle_agy_stream(self, path, argv, work_cwd, body, res, req_model_name, extra_headers, t0):
        proc = None
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=work_cwd,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except Exception as exc:
            self._send_json(502, {"error": {"message": f"Gemini Pro agy stream launch failed: {exc}", "type": "bad_gateway"}}, extra_headers=extra_headers)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self._send_cors_headers()
        for k, v in extra_headers.items():
            if v:
                self.send_header(k, v)
        self.end_headers()

        chat_id = f"chatcmpl_agy_{os.urandom(4).hex()}"
        created_ts = int(time.time())
        is_native = (path == "/api/v1/chat")
        sent_role = False
        text_parts = []
        reason_parts = []
        new_conv = ""
        ttft = None
        result_obj = {}

        def _emit_oai_delta(delta_dict, finish_reason=None, usage=None):
            payload = {
                "id": chat_id,
                "object": "chat.completion.chunk",
                "created": created_ts,
                "model": req_model_name,
                "choices": [{"index": 0, "delta": delta_dict, "finish_reason": finish_reason}],
            }
            if usage is not None:
                payload["usage"] = usage
            self._write_http_chunk(f"data: {json.dumps(payload)}\n\n".encode("utf-8"))

        def _emit_native_ev(ev_name, data_obj):
            self._write_http_chunk(f"event: {ev_name}\ndata: {json.dumps(data_obj)}\n\n".encode("utf-8"))

        try:
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                s = raw_line.strip()
                if not s.startswith("{"):
                    continue
                try:
                    ev = json.loads(s)
                except Exception:
                    continue
                ev_kind = ev.get("event")
                if ev.get("conversation_id"):
                    new_conv = ev["conversation_id"]
                if ev_kind == "step_update":
                    su = ev.get("step_update") or {}
                    if su.get("conversation_id"):
                        new_conv = su["conversation_id"]
                    t_delta = su.get("text_delta") or ""
                    r_delta = su.get("thinking_delta") or su.get("reasoning_delta") or ""
                    if (t_delta or r_delta) and ttft is None:
                        ttft = time.monotonic() - t0
                    if r_delta:
                        reason_parts.append(r_delta)
                        if is_native:
                            _emit_native_ev("reasoning.delta", {"type": "reasoning.delta", "content": r_delta})
                        else:
                            d = {"reasoning_content": r_delta}
                            if not sent_role:
                                d["role"] = "assistant"
                                sent_role = True
                            _emit_oai_delta(d)
                    if t_delta:
                        text_parts.append(t_delta)
                        if is_native:
                            _emit_native_ev("message.delta", {"type": "message.delta", "content": t_delta})
                        else:
                            d = {"content": t_delta}
                            if not sent_role:
                                d["role"] = "assistant"
                                sent_role = True
                            _emit_oai_delta(d)
                elif ev_kind == "result":
                    result_obj = ev.get("result") or {}
                    if result_obj.get("conversation_id"):
                        new_conv = result_obj["conversation_id"]

            proc.wait(timeout=10)
            final_resp = str(result_obj.get("response") or "")
            if not text_parts and final_resp:
                text_parts.append(final_resp)
                if is_native:
                    _emit_native_ev("message.delta", {"type": "message.delta", "content": final_resp})
                else:
                    d = {"content": final_resp}
                    if not sent_role:
                        d["role"] = "assistant"
                        sent_role = True
                    _emit_oai_delta(d)
            elif not text_parts and result_obj.get("status") == "ERROR":
                err_txt = f"[Gemini Pro error: {result_obj.get('error') or 'unknown error'}]"
                text_parts.append(err_txt)
                if is_native:
                    _emit_native_ev("message.delta", {"type": "message.delta", "content": err_txt})
                else:
                    _emit_oai_delta({"role": "assistant", "content": err_txt})

            elapsed = max(0.001, time.monotonic() - t0)
            usage = result_obj.get("usage") or {}
            in_toks = int(usage.get("input_tokens") or 0)
            out_toks = int(usage.get("output_tokens") or max(1, len("".join(text_parts).split())))
            tot_toks = int(usage.get("total_tokens") or (in_toks + out_toks))
            rid = f"resp_agy_{new_conv}" if new_conv else f"resp_agy_{int(time.time() * 1000)}_{os.urandom(3).hex()}"

            if body.get("store", True):
                self.router.record_cloud_session(
                    rid,
                    res["resolved"],
                    res.get("alias"),
                    [],
                    cloud="gemini-pro",
                    conversation_id=new_conv,
                )

            if is_native:
                _emit_native_ev(
                    "chat.end",
                    {
                        "type": "chat.end",
                        "result": {
                            "response_id": rid,
                            "conversation_id": new_conv,
                            "model_instance_id": res["resolved"],
                            "stats": {
                                "tokens_per_second": round(out_toks / elapsed, 2),
                                "time_to_first_token_seconds": round(ttft or elapsed, 3),
                            },
                        },
                    },
                )
            else:
                _emit_oai_delta({}, finish_reason="stop", usage={"prompt_tokens": in_toks, "completion_tokens": out_toks, "total_tokens": tot_toks})
                self._write_http_chunk(b"data: [DONE]\n\n")

            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        finally:
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass

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
