# Windows 11 OS & Chipset Checklist for Zen 3 + CUDA Workstations

1. **AMD X570 Chipset Drivers + `Balanced` Power Plan:**
   - Do not rely on inbox 2017–2021 `AMD PCI` / `GPIO` / `PSP` drivers. Install current `AMD Chipset Software` (including `AMD Ryzen Balanced` provisioning).
   - Use Windows **`Balanced`** (`381b4222-f694-41f0-9685-ff5bb260df2e`), NOT `High performance` (`MinCPU=100%`), so idle Zen 3 cores enter `CC6` sleep and free PPT/thermal headroom for `5.05 GHz` PB2 boost.
   - Keep `PCI Express -> Link State Power Management (ASPM)` = **`Off` (`0x00000000`)** inside `Balanced`.
2. **Virtualization / WSL2 Balance:**
   - WSL2 enables VBS, while **HVCI / Memory Integrity = `Disabled`** preserves native performance.
   - Bound WSL2 via `%USERPROFILE%\.wslconfig` (`memory=6GB`, `autoMemoryReclaim=gradual`) so WSL does not starve host LM Studio weights.
3. **NVIDIA Driver & Defender Exclusions:**
   - Set `CUDA - Sysmem Fallback Policy = Prefer No Sysmem Fallback` in NVIDIA Control Panel.
   - Dual high-refresh monitors (e.g. `4K@144 + 1440p@165`) normally pin Ampere GDDR6X clocks in `P0` (~`95 W` idle) to prevent flicker; keep Power Management Mode on `Normal` globally unless benchmarking.
   - Exclude `%USERPROFILE%\.lmstudio\models` and `%USERPROFILE%\.lmstudio\extensions\backends` in Windows Defender for fast cold GGUF `mmap` loads.
