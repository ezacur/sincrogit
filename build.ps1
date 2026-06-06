# Build SincroGit.exe (GUI + CLI) with PyInstaller.
#
#   .\build.ps1            -> release: single-file dist\SincroGit.exe (slower build, slower start)
#   .\build.ps1 -Fast      -> dev: dist\SincroGit\SincroGit.exe folder (fast build, instant start)
#   .\build.ps1 -Clean     -> wipe the cache first (reproducible, slowest)
#
# TIP: while developing you usually DON'T need to build at all — just run
#      `python -m sincrogit`. Only build to test/ship the packaged artifact.

param(
    [switch]$Fast,    # --onedir: much faster to build and to start; for iteration
    [switch]$Clean    # --clean: wipe PyInstaller cache (only for a clean release build)
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if (-not (Test-Path app.ico)) {
    Write-Host "==> Generating app.ico from the vector icon..."
    python tools\make_icon.py app.ico
}

$pyiArgs = @(
    "--noconsole",
    "--name", "SincroGit",
    "--icon", "app.ico",
    "--collect-submodules", "watchdog",
    "--noconfirm"
)
if ($Fast) { $pyiArgs += "--onedir" } else { $pyiArgs += "--onefile" }
if ($Clean) { $pyiArgs += "--clean" }
$pyiArgs += "app.py"

$sw = [System.Diagnostics.Stopwatch]::StartNew()
Write-Host "==> Running PyInstaller ($(if ($Fast) {'onedir/fast'} else {'onefile/release'})$(if ($Clean) {' +clean'}))..."
python -m PyInstaller @pyiArgs
$sw.Stop()

$out = if ($Fast) { "dist\SincroGit\SincroGit.exe" } else { "dist\SincroGit.exe" }
Write-Host ""
Write-Host ("==> Done in {0:N1}s. Executable: {1}" -f $sw.Elapsed.TotalSeconds, $out)
