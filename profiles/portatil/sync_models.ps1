# sync-laptop-models.ps1
# Run once on Portatil in PowerShell before Phase 1/2 laptop benchmarking.
# - Prints laptop hardware, GPU shared memory pool, disk space, and LM Studio runtime diagnostics.
# - Safely archives (moves, does NOT delete) 3 dense models replaced by faster MoE/PC models:
#     p-vision-context, p-code-chat, p-math-physics -> %USERPROFILE%\.lmstudio\_archived_models\
# - Downloads 3 new p-* models with curl.exe (-C - resumable):
#     p-battery-chat         (Qwen3-4B Q4_K_M, ~2.33 GiB)
#     p-code-moe             (Qwen3-Coder-30B-A3B-Instruct Q4_K_M, ~17.28 GiB)
#     p-draft-gemma-moe-mtp  (mtp-gemma-4-26B-A4B-it Q8_0, ~0.43 GiB, in its own drafter folder)

$ErrorActionPreference = "Stop"

Write-Host "=========================================================" -ForegroundColor Cyan
Write-Host " Portatil Diagnostics & p-* Model Sync" -ForegroundColor Cyan
Write-Host "=========================================================" -ForegroundColor Cyan

# 1. System & GPU diagnostics
$os  = Get-CimInstance Win32_OperatingSystem
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
$gpu = Get-CimInstance Win32_VideoController
$ramGiB = [math]::Round(($os.TotalVisibleMemorySize * 1KB) / 1GB, 2)
$freeRamGiB = [math]::Round(($os.FreePhysicalMemory * 1KB) / 1GB, 2)
$cDrive = Get-PSDrive -Name C
$freeDiskGiB = [math]::Round($cDrive.Free / 1GB, 2)

Write-Host "`n[1/5] Hardware & Power Diagnostics" -ForegroundColor Yellow
Write-Host "OS:          $($os.Caption) ($($os.Version))"
Write-Host "CPU:         $($cpu.Name) ($($cpu.NumberOfCores)C/$($cpu.NumberOfLogicalProcessors)T)"
Write-Host "System RAM:  $ramGiB GiB total ($freeRamGiB GiB currently free)"
Write-Host "C: Disk:     $freeDiskGiB GiB free"
foreach ($g in $gpu) {
    Write-Host "GPU:         $($g.Name) | Driver: $($g.DriverVersion) ($($g.DriverDate))"
}
try {
    $powerPlan = (powercfg /getactivescheme) 2>&1
    Write-Host "Power Plan:  $powerPlan"
} catch {}
try {
    $sharedBytes = (Get-Counter '\GPU Adapter Memory(*)\Shared Usage' -ErrorAction SilentlyContinue).CounterSamples |
        Measure-Object -Property CookedValue -Sum | Select-Object -ExpandProperty Sum
    $sharedTotal = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\0*' -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty 'HardwareInformation.qwMemorySize' -ErrorAction SilentlyContinue | Select-Object -First 1)
    Write-Host "GPU Shared In-Use: $([math]::Round($sharedBytes / 1GB, 2)) GiB"
    if ($sharedTotal) {
        Write-Host "GPU Reported Dedicated/Adapter Mem: $([math]::Round($sharedTotal / 1MB, 0)) MiB"
    }
} catch {}

# 2. Check free space
$lmsRoot = Join-Path $env:USERPROFILE ".lmstudio"
$modelsRoot = Join-Path $lmsRoot "models"
$archiveRoot = Join-Path $lmsRoot "_archived_models"
$lmsExe = Join-Path $lmsRoot "bin\lms.exe"

if (-not (Test-Path $modelsRoot)) {
    throw "LM Studio models folder not found at $modelsRoot"
}

# Estimate space after archiving vs downloading (~20.1 GiB new downloads, ~24.0 GiB archived on same drive)
Write-Host "`n[2/5] Disk Check & Existing Models" -ForegroundColor Yellow
if ($freeDiskGiB -lt 25) {
    throw "Less than 25 GiB free on C: ($freeDiskGiB GiB). Free space before continuing."
}

$allDirs = Get-ChildItem -Path $modelsRoot -Directory -Recurse -Depth 2 -ErrorAction SilentlyContinue
Write-Host "Found model folders:"
Get-ChildItem -Path $modelsRoot -Filter "*.gguf" -Recurse | ForEach-Object {
    Write-Host ("  {0,7:N2} GiB  {1}" -f ($_.Length / 1GB), $_.FullName.Substring($modelsRoot.Length + 1))
}

# 3. Safely archive redundant dense copies (move to _archived_models, never delete)
Write-Host "`n[3/5] Archiving redundant slow dense copies -> $archiveRoot" -ForegroundColor Yellow
New-Item -ItemType Directory -Path $archiveRoot -Force | Out-Null

$archivePatterns = @("P-vision-context", "p-vision-context", "P-code-chat", "p-code-chat", "P-math-physics", "p-math-physics")
$archivedCount = 0
foreach ($pubDir in Get-ChildItem -Path $modelsRoot -Directory) {
    foreach ($modelDir in Get-ChildItem -Path $pubDir.FullName -Directory) {
        $match = $false
        foreach ($pat in $archivePatterns) {
            if ($modelDir.Name -ieq $pat -or $modelDir.Name -like "*$pat*") {
                $match = $true; break
            }
        }
        if ($match) {
            $destParent = Join-Path $archiveRoot $pubDir.Name
            New-Item -ItemType Directory -Path $destParent -Force | Out-Null
            $destPath = Join-Path $destParent $modelDir.Name
            if (Test-Path $destPath) {
                Write-Host "  Already archived: $($pubDir.Name)\$($modelDir.Name)" -ForegroundColor DarkGray
            } else {
                Move-Item -Path $modelDir.FullName -Destination $destPath
                Write-Host "  Moved to archive: $($pubDir.Name)\$($modelDir.Name)" -ForegroundColor Green
                $archivedCount++
            }
        }
    }
}
if ($archivedCount -eq 0) {
    Write-Host "  No unarchived dense copies found (already archived or renamed differently)." -ForegroundColor DarkGray
}

# Verify core p-agent-moe is still untouched
$moeFound = Get-ChildItem -Path $modelsRoot -Filter "*agent-moe*.gguf" -Recurse -ErrorAction SilentlyContinue
Write-Host "  Active MoE files kept:"
foreach ($f in $moeFound) {
    Write-Host ("    {0,7:N2} GiB  {1}" -f ($f.Length / 1GB), $f.FullName.Substring($modelsRoot.Length + 1))
}

# 4. Download new p-* models via curl.exe (resumable, clean p-* directory names)
Write-Host "`n[4/5] Downloading new p-* models" -ForegroundColor Yellow

$downloads = @(
    @{
        Name = "p-battery-chat (Qwen3-4B Q4_K_M, ~2.33 GiB)"
        Dest = Join-Path $modelsRoot "bartowski\P-battery-chat\P-battery-chat.gguf"
        Url  = "https://huggingface.co/bartowski/Qwen_Qwen3-4B-GGUF/resolve/main/Qwen_Qwen3-4B-Q4_K_M.gguf"
    },
    @{
        Name = "p-draft-gemma-moe-mtp (Gemma-4-26B-A4B MTP Q8_0, ~0.43 GiB)"
        Dest = Join-Path $modelsRoot "unsloth\P-draft-gemma-moe-mtp\P-draft-gemma-moe-mtp.gguf"
        Url  = "https://huggingface.co/unsloth/gemma-4-26b-a4b-it-GGUF/resolve/main/MTP/mtp-gemma-4-26B-A4B-it-Q8_0.gguf"
    },
    @{
        Name = "p-code-moe (Qwen3-Coder-30B-A3B-Instruct Q4_K_M, ~17.28 GiB)"
        Dest = Join-Path $modelsRoot "unsloth\P-code-moe\P-code-moe.gguf"
        Url  = "https://huggingface.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF/resolve/main/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf"
    }
)

foreach ($item in $downloads) {
    $dir = Split-Path -Parent $item.Dest
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
    Write-Host "`n--> Downloading $($item.Name)" -ForegroundColor Cyan
    Write-Host "    -> $($item.Dest)"
    & curl.exe -fL -C - --progress-bar -o $item.Dest $item.Url
    if ($LASTEXITCODE -ne 0) {
        throw "curl.exe failed ($LASTEXITCODE) for $($item.Name)"
    }
    $sz = (Get-Item $item.Dest).Length
    Write-Host ("    Done: {0:N2} GiB" -f ($sz / 1GB)) -ForegroundColor Green
}

# 5. Summary & LM Studio CLI inspection
Write-Host "`n[5/5] Summary & LM Studio Status" -ForegroundColor Yellow
$freeAfterGiB = [math]::Round((Get-PSDrive -Name C).Free / 1GB, 2)
Write-Host "C: Disk Free After Sync: $freeAfterGiB GiB"

if (Test-Path $lmsExe) {
    Write-Host "`n--- lms runtime ls ---" -ForegroundColor Cyan
    & $lmsExe runtime ls
    Write-Host "`n--- lms ls ---" -ForegroundColor Cyan
    & $lmsExe ls
} else {
    Write-Host "lms.exe not found at $lmsExe (restart or refresh LM Studio UI to index new models)."
}

Write-Host "`n=========================================================" -ForegroundColor Green
Write-Host " Sync complete! Please paste this output back to the agent." -ForegroundColor Green
Write-Host "=========================================================" -ForegroundColor Green
