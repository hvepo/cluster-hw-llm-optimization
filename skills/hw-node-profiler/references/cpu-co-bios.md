# AMD Ryzen Zen 3 Per-Core Curve Optimizer & BIOS Stability

## Diagnosing Sudden Reboots Without Bluescreens (`WHEA 18 / 19`)
Silent reboots without a BSOD dump typically log `Microsoft-Windows-WHEA-Logger` **Event ID `18`** (`Machine Check Exception — Cache Hierarchy Error`) with `Processor APIC ID: N`:
- With SMT enabled on a 16-core Zen 3 (`5950X`): **`Physical Core (0-indexed) = floor(APIC_ID / 2)`**.
  - Example: `APIC ID 6` & `7` -> ASUS BIOS **`Core 3`** -> HWiNFO64 1-indexed `#4`.
- All-core negative CO (`-10` / `-15`) starves the highest-boosting star core during idle/light `CC6 <-> PB2` voltage transitions even if all-core Cinebench passes!

## Safe Per-Core Setup Workflow
1. Run `python3 tools/hw/system_audit.py --days 14 --hwinfo <path_to_hwinfo_json>` to correlate `WHEA 18/19` APIC IDs against `Core Performance Order` & `CPPC` rankings.
2. In BIOS (`Advanced -> AMD Overclocking -> PBO -> Curve Optimizer -> Per Core`):
   - Any core with past `WHEA 18/19`: **`0`** *(use `+5` only if it ever recurs)*.
   - Other top 3 CPPC star cores on CCD0: **`Negative 5`**.
   - Remaining CCD0 & CCD1 cores: **`Negative 10`** (test `-15` only after 24h+ WHEA-free).
3. Keep `D.O.C.P.`, `FCLK=MEMCLK/2` (`1800` for `3600 MT/s`), `UCLK==MEMCLK`, `Performance Enhancer: Default`, `Global C-state: Enabled`, `CPPC + CPPC Preferred Cores: Enabled`, `Above 4G Decoding + Re-Size BAR: Enabled`, `CSM: Disabled`.
4. **Do NOT change RAM subtimings (`tRC`/`tRFC`/`tFAW`) at the same time as CPU CO** — certify CO WHEA-free first using `tools/hw/stress_suite.py` (and optional overnight CoreCycler).
