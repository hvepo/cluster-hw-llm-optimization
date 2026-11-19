<#
.SYNOPSIS
    Windows installer for cluster-hw-llm-optimization (Antigravity + VS Code + cluster.local.json).
.DESCRIPTION
    Safely installs the Antigravity plugin (~/.gemini/config/plugins/cluster-hw-llm-optimization)
    and registers `cluster-hw-llm` in VS Code user mcp.json for any cluster node name without
    clobbering existing entries or touching Antigravity config.json.
#>
[CmdletBinding()]
param(
    [string]$Node = "torre",
    [string]$Targets = "agy,vscode",
    [string]$TorreUrl = "",
    [string]$PortatilUrl = "",
    [switch]$DryRun,
    [switch]$Uninstall,
    [switch]$Doctor
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path $PSScriptRoot).ProviderPath
$targetSet = @($Targets.Split(",") | ForEach-Object { $_.Trim().ToLower() } | Where-Object { $_ })

function Find-WinPython {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Python\bin\python.exe")
    )
    $cmdPy = Get-Command python -ErrorAction SilentlyContinue
    if ($cmdPy -and $cmdPy.Source -notmatch "WindowsApps\\python\.exe$") {
        $candidates += $cmdPy.Source
    }
    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c)) {
            return $c
        }
    }
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        $exe = (& $pyLauncher.Source -3 -c "import sys; print(sys.executable)" 2>$null)
        if ($LASTEXITCODE -eq 0 -and $exe) { return $exe.Trim() }
    }
    throw "Could not find a non-store Windows python.exe. Install Python 3 or pass PATH."
}

$AgyDest = Join-Path $HOME ".gemini\config\plugins\cluster-hw-llm-optimization"
$VsCodeMcp = Join-Path $env:APPDATA "Code\User\mcp.json"

if ($Uninstall) {
    if ($targetSet -contains "agy" -and (Test-Path $AgyDest)) {
        Write-Host "[uninstall] Remove $AgyDest"
        if (-not $DryRun) { Remove-Item -Recurse -Force $AgyDest }
    }
    if ($targetSet -contains "vscode" -and (Test-Path $VsCodeMcp)) {
        $raw = Get-Content -Raw -Encoding UTF8 $VsCodeMcp | ConvertFrom-Json
        if ($raw.servers -and $raw.servers.PSObject.Properties["cluster-hw-llm"]) {
            Write-Host "[uninstall] Remove servers.cluster-hw-llm from $VsCodeMcp"
            if (-not $DryRun) {
                $raw.servers.PSObject.Properties.Remove("cluster-hw-llm")
                [System.IO.File]::WriteAllText($VsCodeMcp, ($raw | ConvertTo-Json -Depth 10), [System.Text.UTF8Encoding]::new($false))
            }
        }
    }
    exit 0
}

$WinPy = Find-WinPython
$McpScript = Join-Path $RepoRoot "mcp\cluster_mcp_server.py"
Write-Host "RepoRoot : $RepoRoot"
Write-Host "WinPython: $WinPy"

# 1. Create/update gitignored cluster.local.json
$LocalCfgPath = Join-Path $RepoRoot "cluster.local.json"
$ExCfgPath = Join-Path $RepoRoot "cluster.example.json"
if (Test-Path $LocalCfgPath) {
    $cfg = Get-Content -Raw -Encoding UTF8 $LocalCfgPath | ConvertFrom-Json
} else {
    $cfg = Get-Content -Raw -Encoding UTF8 $ExCfgPath | ConvertFrom-Json
}
if ($cfg.PSObject.Properties["routing_presets"]) {
    $cfg.PSObject.Properties.Remove("routing_presets")
}
$cfg.local_node = $Node
if (-not $cfg.nodes.PSObject.Properties[$Node]) {
    $newNode = [PSCustomObject]@{
        label          = $Node
        role           = "custom-cluster-node"
        os             = "windows"
        base_url       = "http://127.0.0.1:1234"
        model_prefixes = @()
        hardware       = [PSCustomObject]@{}
        paths          = [PSCustomObject]@{}
    }
    $cfg.nodes | Add-Member -Force -NotePropertyName $Node -NotePropertyValue $newNode
}
if ($TorreUrl) { $cfg.nodes.torre.base_url = $TorreUrl }
if ($PortatilUrl) { $cfg.nodes.portatil.base_url = $PortatilUrl }
Write-Host "[config] $LocalCfgPath (local_node=$Node)"
if (-not $DryRun) {
    [System.IO.File]::WriteAllText($LocalCfgPath, ($cfg | ConvertTo-Json -Depth 10), [System.Text.UTF8Encoding]::new($false))
}

# 2. Windows Antigravity plugin (never touches ~/.gemini/config/config.json)
if ($targetSet -contains "agy") {
    $SrcPlugin = Join-Path $RepoRoot "plugins\cluster-hw-llm-optimization"
    Write-Host "[agy] Install plugin -> $AgyDest"
    if (-not $DryRun) {
        if (Test-Path $AgyDest) { Remove-Item -Recurse -Force $AgyDest }
        Copy-Item -Recurse -Force $SrcPlugin $AgyDest
        [System.IO.File]::WriteAllText((Join-Path $AgyDest "repo_root.txt"), $RepoRoot + "`n", [System.Text.UTF8Encoding]::new($false))

        $mcpObj = [ordered]@{
            mcpServers = [ordered]@{
                "cluster-hw-llm" = [ordered]@{
                    command = $WinPy
                    args    = @($McpScript)
                    env     = @{ CLUSTER_REPO_ROOT = $RepoRoot }
                }
            }
        }
        [System.IO.File]::WriteAllText((Join-Path $AgyDest "mcp_config.json"), ($mcpObj | ConvertTo-Json -Depth 8), [System.Text.UTF8Encoding]::new($false))

        $hooksObj = [ordered]@{
            "cluster-safety-guard" = [ordered]@{
                SessionStart = @(
                    [ordered]@{ type = "command"; command = "`"$WinPy`" ./hooks/session_start.py"; timeout = 10 }
                )
                PreToolUse   = @(
                    [ordered]@{
                        matcher = "run_command"
                        hooks   = @(
                            [ordered]@{ type = "command"; command = "`"$WinPy`" ./hooks/pre_tool_guard.py"; timeout = 10 }
                        )
                    }
                )
            }
        }
        [System.IO.File]::WriteAllText((Join-Path $AgyDest "hooks.json"), ($hooksObj | ConvertTo-Json -Depth 10), [System.Text.UTF8Encoding]::new($false))
    }
}

# 3. VS Code User mcp.json (preserves existing inputs & other servers)
if ($targetSet -contains "vscode") {
    Write-Host "[vscode] Merge servers.cluster-hw-llm -> $VsCodeMcp"
    if (-not $DryRun) {
        $vsDir = Split-Path -Parent $VsCodeMcp
        New-Item -ItemType Directory -Force -Path $vsDir | Out-Null
        $vsData = $null
        if (Test-Path $VsCodeMcp) {
            try { $vsData = Get-Content -Raw -Encoding UTF8 $VsCodeMcp | ConvertFrom-Json } catch { $vsData = $null }
        }
        if (-not $vsData) { $vsData = [PSCustomObject]@{} }
        if (-not $vsData.PSObject.Properties["servers"] -or -not $vsData.servers) {
            $vsData | Add-Member -Force -NotePropertyName "servers" -NotePropertyValue ([PSCustomObject]@{})
        }
        $srvEntry = [ordered]@{
            type    = "stdio"
            command = $WinPy
            args    = @($McpScript)
            env     = @{ CLUSTER_REPO_ROOT = $RepoRoot }
        }
        $vsData.servers | Add-Member -Force -NotePropertyName "cluster-hw-llm" -NotePropertyValue ([PSCustomObject]$srvEntry)
        [System.IO.File]::WriteAllText($VsCodeMcp, ($vsData | ConvertTo-Json -Depth 10), [System.Text.UTF8Encoding]::new($false))
    }
}

if ($Doctor -and -not $DryRun) {
    & $WinPy (Join-Path $RepoRoot "scripts\doctor.py") --all
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
