# Verify the shell exe actually has the frontend assets embedded.
#
# Why this exists: `cargo build --release` happily produces an exe that looks
# fine -- tray icon works, bridge listens, the overlay window shows and hides,
# logs look clean -- but contains no frontend assets at all. Its WebView points
# at the dev server on localhost:6021, which normally has nothing listening, so
# the overlay is a blank page: recording and typing work, the banner never
# appears.
#
# That failure mode survived three rounds of debugging. It logs no error, does
# not crash, and the transparent overlay's pixels cannot be read reliably via
# GDI BitBlt or PrintWindow on this machine -- so short of looking at the
# screen, it is very hard to confirm. Whether the asset manifest got embedded
# is the one hard check that needs no running process.
#
# ASCII-only on purpose: Windows PowerShell 5.1 fails to parse a BOM-less
# UTF-8 file containing non-ASCII text.
#
# Usage:
#   .\verify-build.ps1                 # check the default build output
#   .\verify-build.ps1 -Path <exe>     # check a specific exe (e.g. deployed copy)
[CmdletBinding()]
param(
    [string]$Path
)

$ErrorActionPreference = 'Stop'

if (-not $Path) {
    # cargo's target-dir may be redirected by .cargo/config.toml, so ask cargo
    # rather than hardcoding a path.
    $targetDir = $null
    try {
        Push-Location (Join-Path $PSScriptRoot 'src-tauri')
        # Pull target_directory out with a regex rather than ConvertFrom-Json:
        # cargo's output contains the crate description in Chinese, and Windows
        # PowerShell 5.1 corrupts it badly enough that JSON parsing throws.
        $raw = (& cargo metadata --no-deps --format-version 1 2>$null) -join ''
        $m = [regex]::Match($raw, '"target_directory"\s*:\s*"([^"]+)"')
        if ($m.Success) { $targetDir = $m.Groups[1].Value -replace '\\\\', '\' }
    } catch {
        # fall through to the in-repo default
    } finally {
        Pop-Location
    }
    if (-not $targetDir) { $targetDir = Join-Path $PSScriptRoot 'src-tauri\target' }
    $Path = Join-Path $targetDir 'release\capswriter-shell.exe'
}

if (-not (Test-Path $Path)) {
    Write-Host "exe not found: $Path" -ForegroundColor Red
    Write-Host "Build it first:  cd shell; npx tauri build --no-bundle"
    exit 1
}

$item = Get-Item $Path
Write-Host "Checking: $($item.FullName)"
Write-Host "Size: $([math]::Round($item.Length/1MB,2)) MB   Modified: $($item.LastWriteTime)"
Write-Host ""

# Look for traces of the frontend asset manifest. A correct build (tauri build,
# i.e. with the custom-protocol feature) embeds dist/ along with its paths.
$bytes = [System.IO.File]::ReadAllBytes($Path)
$text = [System.Text.Encoding]::ASCII.GetString($bytes)

$assetHits = ([regex]::Matches($text, 'assets/overlay')).Count
$devUrlHits = ([regex]::Matches($text, 'localhost:6021')).Count

Write-Host "frontend asset path 'assets/overlay' occurrences: $assetHits"
Write-Host "dev server 'localhost:6021' occurrences:          $devUrlHits"
Write-Host ""

if ($assetHits -eq 0) {
    Write-Host "FAIL: no frontend assets embedded -- the overlay will render blank." -ForegroundColor Red
    Write-Host "      The banner will never show, yet recording/recognition/typing" -ForegroundColor Red
    Write-Host "      all work normally, so this is easy to mistake for a good build." -ForegroundColor Red
    Write-Host ""
    Write-Host "      Cause: a bare 'cargo build'. Tauri CLI must build and embed the frontend."
    Write-Host "      Fix:   cd shell; npx tauri build --no-bundle"
    exit 1
}

Write-Host "OK: frontend assets are embedded; the overlay can render." -ForegroundColor Green
exit 0
