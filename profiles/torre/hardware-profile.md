# Torre Hardware & OS Tuning Profile (Workstation Node)

- **CPU:** AMD Ryzen 9 5950X (16C/32T, `VMR-B2` stepping)
- **Motherboard:** ASUS ROG STRIX X570-E GAMING (UEFI, Above 4G Decoding + Re-Size BAR enabled, CSM disabled)
- **RAM:** 32 GiB `DDR4-3600 CL14-15-15-35 1T` (`4x8 GiB` Samsung B-Die, `FCLK=1800`, `UCLK==MEMCLK`)
- **GPU:** NVIDIA GeForce RTX 3080 Ti FE (12 GiB GDDR6X, PCIe Gen4 x16, `16,384 MiB` BAR1)

## Verified BIOS Settings
- `D.O.C.P.`, `FCLK 1800 MHz`, `UCLK==MEMCLK`, `Performance Enhancer: Default`, `SVM: Enabled`, `Global C-state: Enabled`, `CPPC + CPPC Preferred Cores: Enabled`.
- `PBO: Advanced`, Scalar `Auto`, Max Boost `+0 MHz`, Thermal Limit `85-90 C`.
- **Per-Core Curve Optimizer:** Do **NOT** blindly use aggressive all-core `-15` if your fastest boost core throws idle/light-load `WHEA 18 Cache Hierarchy Error` reboots (`APIC ID 6/7 = Core 3`). Identify crashing cores in Windows Event Log or HWiNFO64 (`tools/hw/system_audit.py --hwinfo <export>`), set the culprit star core to `0`, remaining top CPPC cores to `-5`, and other cores to `-10`.

## Verified RTX 3080 Ti Undervolt + VRAM OC
- **Power Limit:** `100%` (`350 W`)
- **VRAM Offset:** `+500 MHz` (`10,001 MHz` effective GDDR6X)
- **Voltage/Frequency Curve:** `1,695 MHz @ 850 mV` -> `1,770 MHz @ 875 mV` -> **flat `1,785 MHz` ceiling across `937.5-1,237.5 mV`** (shift-select and drag the entire right tail below the target frequency so Boost never overshoots into `1,900+ MHz` during LLM prefill).
- **Measured Result:** `831.81 GB/s` CUDA D2D (`0` throttle flags `0x1`/`0x0`), `t-code-chat` `+17.9%` `c1` (`71.12 tok/s`), `t-vision-context` `+10.0%` `c1` (`107.44 tok/s`), `0 nvlddmkm 153` resets.

## Windows 11 OS & LM Studio Host Settings
- **Power Plan:** `Balanced` (after installing current AMD X570 Chipset Software + Ryzen Balanced PPM driver) with `PCI Express -> Link State Power Management = Off`.
- **NVIDIA Control Panel:** `CUDA - Sysmem Fallback Policy = Prefer No Sysmem Fallback` (prevents silent 10x PCIe spill near the 12 GiB ceiling).
- **Defender Exclusions:** `%USERPROFILE%\.lmstudio\models` and `%USERPROFILE%\.lmstudio\extensions\backends`.
- **LM Studio `hardware-config.json`:** `load.gpuStrictVramCap = false` on `cuda`, `cuda12`, `cuda13`.
