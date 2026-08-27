# One command to build, verify, deploy and restart the Tauri shell.
#
# Why this script exists: hand-typing the build command three times produced a
# broken exe three times. `cargo build --release` compiles fine but skips the
# custom-protocol feature, so the frontend is never embedded and the overlay
# renders a blank page -- with no error anywhere. This script always uses the
# correct command and refuses to deploy an exe that failed verification.
#
# ASCII-only on purpose: Windows PowerShell 5.1 cannot parse a BOM-less UTF-8
# file containing non-ASCII text.
#
# Usage:
#   .\build.ps1                 # build + verify + deploy + restart
#   .\build.ps1 -NoDeploy       # build + verify only
#   .\build.ps1 -NoRestart      # build + verify + deploy, leave processes alone
#   .\build.ps1 -Target <dir>   # deploy somewhere else
[CmdletBinding()]
param(
    [string]$Target = 'C:\AI_Program\CapsWriter-Offline',
    [switch]$NoDeploy,
    [switch]$NoRestart
)

$ErrorActionPreference = 'Stop'
$shellDir = $PSScriptRoot

function Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "  OK: $msg" -ForegroundColor Green }

# `exit` inside a function only leaves the function in some hosts, which would
# let a failure fall through and still report success. Throw instead, and let
# the trap below turn it into a real non-zero exit.
function Die($msg)  { throw "BUILD-FAIL: $msg" }

trap {
    if ("$_" -match 'BUILD-FAIL: (.*)') {
        Write-Host "  FAIL: $($Matches[1])" -ForegroundColor Red
    } else {
        Write-Host "  FAIL: $_" -ForegroundColor Red
    }
    exit 1
}

# ---------------------------------------------------------------
# 1. Build
# ---------------------------------------------------------------
Step 'Build (tauri build --no-bundle)'

Push-Location $shellDir
try {
    # Must go through the Tauri CLI: it builds the frontend AND enables the
    # custom-protocol feature that embeds it. A bare `cargo build` does neither.
    #
    # The Tauri CLI writes progress to stderr, which PowerShell surfaces as
    # NativeCommandError. Under $ErrorActionPreference='Stop' that aborts the
    # script mid-build, so relax it here and judge success by exit code alone.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & npx tauri build --no-bundle 2>&1 | ForEach-Object { "  $_" }
    $rc = $LASTEXITCODE
    $ErrorActionPreference = $prev
    if ($rc -ne 0) { Die "tauri build exited $rc" }
} finally {
    Pop-Location
}
Ok 'compiled'

# ---------------------------------------------------------------
# 2. Locate the produced exe
# ---------------------------------------------------------------
Step 'Locate output'

# target/ is redirected by src-tauri/.cargo/config.toml, so ask cargo instead
# of hardcoding a path. Parse with regex, not ConvertFrom-Json: cargo prints the
# crate description in Chinese and PS 5.1 mangles it enough to break the parser.
$targetDir = $null
Push-Location (Join-Path $shellDir 'src-tauri')
try {
    $raw = (& cargo metadata --no-deps --format-version 1 2>$null) -join ''
    $m = [regex]::Match($raw, '"target_directory"\s*:\s*"([^"]+)"')
    if ($m.Success) { $targetDir = $m.Groups[1].Value -replace '\\\\', '\' }
} finally {
    Pop-Location
}
if (-not $targetDir) { $targetDir = Join-Path $shellDir 'src-tauri\target' }

$exe = Join-Path $targetDir 'release\capswriter-shell.exe'
if (-not (Test-Path $exe)) { Die "exe not found at $exe" }
$exeItem = Get-Item $exe
Ok "$exe  ($([math]::Round($exeItem.Length/1MB,2)) MB)"

# ---------------------------------------------------------------
# 3. Verify the frontend really is embedded
# ---------------------------------------------------------------
Step 'Verify embedded frontend'

& (Join-Path $shellDir 'verify-build.ps1') -Path $exe | ForEach-Object { "  $_" }
if ($LASTEXITCODE -ne 0) { Die 'verification failed -- refusing to deploy' }

if ($NoDeploy) {
    Write-Host "`nDone (build + verify only)." -ForegroundColor Green
    exit 0
}

# ---------------------------------------------------------------
# 4. Deploy
# ---------------------------------------------------------------
Step "Deploy to $Target"

if (-not (Test-Path $Target)) { Die "target not found: $Target" }

$stopped = @()
if (-not $NoRestart) {
    # The exe cannot be overwritten while running.
    Get-Process capswriter-shell -ErrorAction SilentlyContinue | ForEach-Object {
        $stopped += 'shell'
        Stop-Process -Id $_.Id -Force
    }
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -match 'start_client|core_client' } |
        ForEach-Object {
            $stopped += 'client'
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
    if ($stopped.Count) { Start-Sleep -Seconds 4 }
}

Copy-Item $exe (Join-Path $Target 'shell\capswriter-shell.exe') -Force
Ok 'exe copied'

# Replace dist/ wholesale rather than copying over it: repeated
# `Copy-Item dist\* -Recurse` piles up stale hashed assets in the target,
# which then sit around shadowing the current build.
$dstDist = Join-Path $Target 'shell\dist'
if (Test-Path $dstDist) { [System.IO.Directory]::Delete($dstDist, $true) }
Copy-Item (Join-Path $shellDir 'dist') $dstDist -Recurse -Force
Ok "dist replaced ($(@(Get-ChildItem "$dstDist\assets").Count) assets)"

# The deployed copy is what actually runs, so verify that one too.
& (Join-Path $shellDir 'verify-build.ps1') -Path (Join-Path $Target 'shell\capswriter-shell.exe') |
    ForEach-Object { "  $_" }
if ($LASTEXITCODE -ne 0) { Die 'deployed copy failed verification' }

if ($NoRestart) {
    Write-Host "`nDone (not restarted; run the shell yourself)." -ForegroundColor Green
    exit 0
}

# ---------------------------------------------------------------
# 5. Restart
# ---------------------------------------------------------------
Step 'Restart'

Start-Process -FilePath (Join-Path $Target 'shell\capswriter-shell.exe') -WorkingDirectory $Target
Start-Sleep -Seconds 7

if ($stopped -contains 'client') {
    $py = 'C:\Users\AriaP\AppData\Local\Programs\Python\Python311\pythonw.exe'
    if (Test-Path $py) {
        Start-Process -FilePath $py -ArgumentList "`"$Target\start_client.py`"" -WorkingDirectory $Target
        Start-Sleep -Seconds 12
    }
}

$nShell = @(Get-Process capswriter-shell -ErrorAction SilentlyContinue).Count
$nClient = @(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
             Where-Object { $_.CommandLine -match 'start_client' }).Count
"  shell processes : $nShell"
"  client processes: $nClient"

# An Established connection on 6020 means the client reached the shell before
# any hotkey press -- the ordering that a earlier bug got wrong.
$est = @(Get-NetTCPConnection -LocalPort 6020 -ErrorAction SilentlyContinue |
         Where-Object { $_.State -eq 'Established' }).Count
"  bridge established: $est"

if ($nShell -lt 1) { Die 'shell did not start' }
Ok 'running'

Write-Host "`nDone. Hold CapsLock to confirm the banner appears." -ForegroundColor Green
