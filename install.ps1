<#
.SYNOPSIS
    Universal Windows + WSL installer for cluster-hw-llm-optimization (Antigravity + VS Code + OpenCode + cluster.local.json).
.DESCRIPTION
    Delegates to scripts/install.py using native Windows python.exe (or wsl.exe -e python3 fallback if
    no Windows Python is installed). Supports any combination of OS (--os: auto, windows, wsl, all)
    and client targets (--targets: agy, vscode, opencode, all).
#>
[CmdletBinding()]
param(
    [string]$Node = "",
    [string]$Targets = "agy,opencode,vscode",
    [string]$Os = "auto",
    [string]$Scope = "all",
    [string]$TorreUrl = "",
    [string]$PortatilUrl = "",
    [switch]$WinUseWslPython,
    [switch]$Serving,
    [string]$Clients = "all",
    [switch]$Peer,
    [switch]$OpenClaw,
    [switch]$DryRun,
    [switch]$Uninstall,
    [switch]$Doctor
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path $PSScriptRoot).ProviderPath

function Find-WinPython {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Python\bin\python.exe")
    )
    if ($env:LOCALAPPDATA) {
        $progPy = Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA "Programs\Python\Python3*\python.exe") -ErrorAction SilentlyContinue | Sort-Object -Descending | Select-Object -First 1
        if ($progPy) { $candidates += $progPy.FullName }
    }
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
    return $null
}

$pyArgs = @(
    "--targets", $Targets,
    "--os", $Os,
    "--scope", $Scope
)
if ($Node) { $pyArgs += @("--node", $Node) }
if ($TorreUrl) { $pyArgs += @("--torre-url", $TorreUrl) }
if ($PortatilUrl) { $pyArgs += @("--portatil-url", $PortatilUrl) }
if ($WinUseWslPython) { $pyArgs += "--win-use-wsl-python" }
if ($Serving) { $pyArgs += @("--serving", "--clients", $Clients) }
if ($Peer) { $pyArgs += "--peer" }
if ($OpenClaw) { $pyArgs += "--openclaw" }
if ($DryRun) { $pyArgs += "--dry-run" }
if ($Uninstall) { $pyArgs += "--uninstall" }
if ($Doctor) { $pyArgs += "--doctor" }

$WinPy = Find-WinPython
if ($WinPy) {
    $InstallScript = Join-Path $RepoRoot "scripts\install.py"
    & $WinPy $InstallScript @pyArgs
    exit $LASTEXITCODE
}

$WslCmd = Get-Command wsl.exe -ErrorAction SilentlyContinue
if ($WslCmd) {
    $WslRepo = (& wsl.exe -e wslpath -u ($RepoRoot -replace '\\','/') 2>$null)
    if ($LASTEXITCODE -eq 0 -and $WslRepo) {
        $WslScript = ($WslRepo.Trim().TrimEnd('/')) + "/scripts/install.py"
        & wsl.exe -e python3 $WslScript @pyArgs
        exit $LASTEXITCODE
    }
}

throw "Could not find a non-store Windows python.exe or WSL python3. Install Python 3."
