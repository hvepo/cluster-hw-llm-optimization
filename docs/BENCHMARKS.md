# Verified Cluster Benchmarks (`torre` + `portatil`)

All numbers below were measured directly with `tools/llm/bench.py`, `tools/llm/run_gates.py`, and `tools/hw/` scripts on live LM Studio servers (`usage` token counts from server SSE responses, AC-only where local, `>= 6` reps median for `c1`).

---

## 1. `torre` — RTX 3080 Ti (12 GiB GDDR6X) + Ryzen 9 5950X

### GPU Hardware Profile Comparison

| Metric | Stock (`9,501 MHz`, `350 W`) | Flattened UV+OC (`1,770–1,785 MHz @ 875 mV`, `+500 MHz` VRAM `10,001 MHz`, `350 W`) | Delta vs Stock |
| :--- | :---: | :---: | :---: |
| **CUDA D2D BW** (`bw_test.py`) | `788.48 GB/s` | **`831.81 GB/s`** *(post-stress median; `860.03` max peak)* | **`+5.5%`** |
| **`t-code-chat`** `c1` / `c2` / `pre4K` | `60.34` / `107.30` / `1,970.8` | **`71.12` / `128.53` / `2,209.8` tok/s** | **`+17.9%` / `+19.8%` / `+12.1%`** |
| **`t-vision-context` (MTP-3)** `c1` / `c2` / `pre8K` | `97.69` / `132.84` / `2,240.4` | **`107.44` / `143.76` / `2,505.6` tok/s** | **`+10.0%` / `+8.2%` / `+11.8%`** |
| **Short TTFT (`t-code` / `t-vision`)** | `44.8` / `53.7 ms` | **`36.8` / `40.6 ms`** | **`-17.9%` / `-24.4%`** |
| **Throttle Bitmask / `nvlddmkm 153`** | `0x0` / `0` | **`0x1` only (`0` caps) / `0` errors** | Clean & `3 °C` cooler |

### Full Torre Model Catalog (Stock Clocks Wave 2 Baseline -> Winner + UV/OC Best)

| Model Key | Underlying Model | Before (`c1` / `c2 or c4` / `pre2K`) | After Wave 2 Stock (`c1` / `cN` / `pre2K`) | Post-UV/OC | Peak VRAM | Needle 50%/100% |
|---|---|---:|---:|---:|---:|---:|
| `t-code-chat` | Qwen2.5-Coder-14B `Q4_K_M` (`8K, p=2, q8_0, pbs=2048`) | `18.43` / `34.02` / `188.5` | `60.34` / `107.30` / `1,993.9` | **`71.12` / `128.53` / `2,069.6`** | `11.12 GiB` | `3/3` · `3/3` |
| `t-general-agent` | Qwen3-14B `Q4_K_M` (`12K, p=2, q8_0, think=off`) | `19.34` / `35.82` / `null` | **`60.92` / `110.49` / `2,017.1`** | — | `10.81 GiB` | `3/3` · `3/3` |
| `t-fast-agent` | Qwen3-8B `Q4_K_M` (`16K, p=4, f16, think=off`) | `101.21` / `264.42` / `178.4` | **`127.35` / `323.01` / `3,556.7`** | — | `8.55 GiB` | `3/3` · `3/3` |
| `t-vision-context` | Gemma-4-12B-IT `Q4_K_M` + MTP-3 (`16K, p=2, q8_0, ckpt=0`) | `34.07` / `59.25` / `103.2` | `97.69` / `132.84` / `2,091.0` | **`107.44` / `143.76` / `2,061.2`** | `10.69 GiB` | `3/3` · `3/3` |
| `t-math-physics` | Phi-4 14B `Q4_K_M` (`8K, p=2, K=q8_0:V=f16`) | `19.63` / `37.03` / `null` | **`63.42` / `118.41` / `1,877.6`** | — | `11.20 GiB` | `3/3` · `3/3` |
| `t-deep-reasoning` | R1-Distill-14B `Q4_K_S` (`8K, p=2, q8_0, think=on`) | `27.20` / `49.88` / `186.4` | **`61.17` / `111.61` / `1,942.1`** | — | `10.45 GiB` | `3/3` · `3/3` |
| `t-autocomplete-tab` | Qwen2.5-Coder-1.5B `Q8_0` (`4K, p=4, f16`) | `218.47` / `410.52` / `1,660.5` | **`218.60` / `482.72` / `10,796.6`** | — | `3.55 GiB` | `17.8 ms` FIM TTFT |
| `text-embedding-t-embed-index` | Nomic-Embed v1.5 `Q8_0` (`2K`, batch=32) | — | **`211.2 emb/s`** (`151.5 ms` batch32) | — | co-resident | Pass |

- **Torre 3-model co-residency gate:** `t-fast-agent` (`q8_0 16K`) + `t-autocomplete-tab` (`f16 4K`) + `text-embedding-t-embed-index` ran concurrently in **`10.05 GiB`** VRAM (`t-fast-agent` `119.52 tok/s`, `t-autocomplete-tab` `214.08 tok/s`, embeddings `188.9 emb/s`).
- **Combined CPU + GPU/LLM Stress (`stress_suite.py`):** Stages 1 & 2 (`Core 3` + all 16 cores pulsed cache/SHA/FP) + Stage 3a (`t-vision-context` 3× cycles + CCD0) + Stage 3b (`t-code-chat` 3× cycles + CCD1) completed with **`0` `WHEA 18/19`**, **`0` `nvlddmkm 153`**, and **`0` CUDA errors**.

---

## 2. `portatil` — Core Ultra 7 258V + Intel Arc 140V (24 GiB Shared Override)

| Model Key | Underlying Model | Before (`c1` / `cN` / `pre2K`) | Optimized (`c1` / `cN` / `pre2K`) | TTFT | Needle 50%/100% |
|---|---|---:|---:|---:|---|
| `p-agent-moe-alt` | Qwen3-30B-A3B (`16K, p=4, fa=1, ckpt=0`) | `25.81` / `29.61` / `13.7` | **`26.99` / `38.70` / `332.7`** (`24×` prefill) | `560.3 ms` | `3/3` · `3/3` |
| `p-code-moe` | Qwen3-Coder-30B-A3B `Q4_K_M` (`16K, p=4, fa=1`) | `23.41` / `27.84` / `14.3` | **`26.09` / `36.93` / `308.4`** (`21×` prefill) | `499.5 ms` | `3/3` · `3/3` |
| `p-agent-moe` | Gemma-4-26B-A4B-IT (`32K, p=2, fa=1`) | `17.59` / `19.35` / `11.1` | **`20.57` / `21.27` / `134.2`** (`12×` prefill) | `491.1 ms` | `3/3` · `3/3` (`20.6K` tok) |
| `p-battery-chat` | Qwen3-4B `Q4_K_M` (`16K, p=2, fa=1`) | `24.30` / `37.68` / `18.9` | **`26.05` / `43.86` / `495.1`** (`26×` prefill) | `142.6 ms` | `3/3` · `3/3` |
| `p-autocomplete-tab` | Qwen2.5-Coder-1.5B `Q8_0` (`4K, p=4, fa=1`) | `47.45` / `105.36` / `52.7` | **`45.45` / `141.66` / `1,038.7`** (`20×` prefill) | `50.4 ms` FIM TTFT | Co-resident |
| `text-embedding-p-embed-index` | Nomic-Embed v1.5 `Q8_0` (`2K`, batch=32) | — | **`90.7 emb/s`** (`352.9 ms` batch32) | — | Co-resident |

- **Laptop away co-residency gate:** `p-battery-chat` + `p-autocomplete-tab` + `text-embedding-p-embed-index` ran concurrently cleanly.
- **Cross-machine simultaneous gate:** `torre` (`t-code-chat` 2 streams) + `portatil` (`p-battery-chat` 2 streams) ran concurrently over Tailscale/LAN with `0` errors and `all_loops_false = true`.
