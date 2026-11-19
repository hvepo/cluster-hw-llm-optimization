<#
.SYNOPSIS
    Applies verified permanent per-model LM Studio default configurations on Portatil
    (HP OmniBook Ultra Flip 14-fh0005ns — Intel Core Ultra 7 258V, Intel Arc 140V iGPU, 32 GB LPDDR5x-8533).

.DESCRIPTION
    1. Backs up existing `$HOME\.lmstudio\.internal\user-concrete-model-default-config` and `hardware-config.json`.
    2. Sets `load.gpuStrictVramCap = false` in `hardware-config.json`.
    3. Resolves each `p-*` model's `indexedModelIdentifier` from `model-index-cache.json` and writes
       the winning Wave 2 default configuration JSON under `user-concrete-model-default-config`.
    4. Pass `-EnableGemmaMtpSidecar` to also enable `P-draft-gemma-moe-mtp.gguf` (`draftMtpSidecar=true`,
       `draftMaxTokens=3`) on `p-agent-moe` (Gemma-4-26B-A4B).
#>
[CmdletBinding()]
param(
    [switch]$EnableGemmaMtpSidecar
)

$ErrorActionPreference = "Stop"

$internalDir = Join-Path $HOME ".lmstudio\.internal"
$indexCachePath = Join-Path $internalDir "model-index-cache.json"
$defaultCfgRoot = Join-Path $internalDir "user-concrete-model-default-config"
$hwCfgPath = Join-Path $internalDir "hardware-config.json"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupDir = Join-Path $internalDir "backup-defaults-$stamp"

if (-not (Test-Path $indexCachePath)) {
    throw "LM Studio model index cache not found at $indexCachePath"
}

New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
if (Test-Path $defaultCfgRoot) {
    Copy-Item -Recurse -Force $defaultCfgRoot (Join-Path $backupDir "user-concrete-model-default-config")
}
if (Test-Path $hwCfgPath) {
    Copy-Item -Force $hwCfgPath (Join-Path $backupDir "hardware-config.json")
}
Write-Host "Backed up previous LM Studio defaults to: $backupDir" -ForegroundColor Cyan

$indexData = Get-Content -Raw -Encoding UTF8 $indexCachePath | ConvertFrom-Json

function Resolve-IndexedModelId {
    param([string]$ModelKey)
    foreach ($m in $indexData.models) {
        if ($m.defaultIdentifier -eq $ModelKey -or ($m.autoIdentifiers -and ($m.autoIdentifiers -contains $ModelKey))) {
            return $m.indexedModelIdentifier
        }
    }
    throw "Model key '$ModelKey' not found in $indexCachePath"
}

$laptopModels = @(
    @{
        Key                 = "p-agent-moe-alt"
        ContextLength       = 16384
        Parallel            = 4
        FlashAttention      = $true
        EvalBatchSize       = 2048
        PhysicalBatchSize   = 512
        ContextCheckpoints  = 0
        EnableThinking      = $false
        MtpSidecar          = $false
    },
    @{
        Key                 = "p-code-moe"
        ContextLength       = 16384
        Parallel            = 4
        FlashAttention      = $true
        EvalBatchSize       = 2048
        PhysicalBatchSize   = 512
        ContextCheckpoints  = 32
        EnableThinking      = $null
        MtpSidecar          = $false
    },
    @{
        Key                 = "p-agent-moe"
        ContextLength       = 32768
        Parallel            = 2
        FlashAttention      = $true
        EvalBatchSize       = 2048
        PhysicalBatchSize   = 512
        ContextCheckpoints  = 32
        EnableThinking      = $false
        MtpSidecar          = [bool]$EnableGemmaMtpSidecar
        DraftModel          = "unsloth/P-draft-gemma-moe-mtp/P-draft-gemma-moe-mtp.gguf"
        DraftMaxTokens      = 3
    },
    @{
        Key                 = "p-battery-chat"
        ContextLength       = 16384
        Parallel            = 2
        FlashAttention      = $true
        EvalBatchSize       = 2048
        PhysicalBatchSize   = 512
        ContextCheckpoints  = 32
        EnableThinking      = $false
        MtpSidecar          = $false
    },
    @{
        Key                 = "p-autocomplete-tab"
        ContextLength       = 4096
        Parallel            = 4
        FlashAttention      = $true
        EvalBatchSize       = 2048
        PhysicalBatchSize   = 512
        ContextCheckpoints  = 32
        EnableThinking      = $null
        MtpSidecar          = $false
    }
)

foreach ($spec in $laptopModels) {
    $relId = Resolve-IndexedModelId -ModelKey $spec.Key
    $targetPath = Join-Path $defaultCfgRoot ($relId.Replace("/", "\") + ".json")
    $targetDir = Split-Path -Parent $targetPath
    New-Item -ItemType Directory -Force -Path $targetDir | Out-Null

    $loadFields = [System.Collections.Generic.List[object]]::new()
    $loadFields.Add(@{ key = "llm.load.contextLength"; value = $spec.ContextLength })
    $loadFields.Add(@{ key = "llm.load.numParallelSessions"; value = $spec.Parallel })
    $loadFields.Add(@{ key = "llm.load.offloadKVCacheToGpu"; value = $true })
    $loadFields.Add(@{ key = "llm.load.gpuStrictVramCap"; value = $false })
    $loadFields.Add(@{ key = "llm.load.llama.autoFit"; value = $false })
    $loadFields.Add(@{ key = "llm.load.llama.acceleration.offloadRatio"; value = "max" })
    $loadFields.Add(@{ key = "llm.load.llama.flashAttention"; value = $spec.FlashAttention })
    $loadFields.Add(@{ key = "llm.load.llama.evalBatchSize"; value = $spec.EvalBatchSize })
    $loadFields.Add(@{ key = "llm.load.llama.physicalBatchSize"; value = $spec.PhysicalBatchSize })
    $loadFields.Add(@{ key = "llm.load.llama.contextCheckpoints"; value = $spec.ContextCheckpoints })

    if ($spec.MtpSidecar) {
        $loadFields.Add(@{ key = "llm.load.speculativeDecoding.draftMtpSidecar"; value = $true })
        $loadFields.Add(@{ key = "llm.load.speculativeDecoding.draftModel"; value = $spec.DraftModel })
        $loadFields.Add(@{ key = "llm.load.speculativeDecoding.draftMaxTokens"; value = $spec.DraftMaxTokens })
    }

    $opFields = [System.Collections.Generic.List[object]]::new()
    if ($null -ne $spec.EnableThinking) {
        $opFields.Add(@{ key = "llm.prediction.reasoning.enableThinking"; value = $spec.EnableThinking })
    }

    $doc = [ordered]@{
        preset    = ""
        operation = @{ fields = $opFields }
        load      = @{ fields = $loadFields }
    }

    $json = $doc | ConvertTo-Json -Depth 10
    [System.IO.File]::WriteAllText($targetPath, $json, [System.Text.UTF8Encoding]::new($false))
    Write-Host ("Configured {0,-22} -> {1} (ctx={2}, p={3}, fa={4}, ckpt={5}, mtp={6})" -f $spec.Key, $relId, $spec.ContextLength, $spec.Parallel, $spec.FlashAttention, $spec.ContextCheckpoints, $spec.MtpSidecar) -ForegroundColor Green
}

Write-Host "`nAll 5 Portatil p-* default configurations applied successfully." -ForegroundColor Cyan
