# Build SincroGit.exe (GUI + CLI) with PyInstaller.
#
#   .\build.ps1            -> release: single-file dist\SincroGit.exe (slower build, slower start)
#   .\build.ps1 -Fast      -> dev: dist\SincroGit\SincroGit.exe folder (fast build, instant start)
#   .\build.ps1 -Clean     -> wipe the cache first (reproducible, slowest)
#
# If the exe this script is about to overwrite is RUNNING, the script handles it:
# it asks the daemon (via the localhost control port) to flush every repo
# (snapshot + autosnap push) and exit cleanly, waits, builds, and relaunches it.
# A daemon too old to know the command gets a forced kill instead (with a notice).
# A SincroGit running from a DIFFERENT path is left alone.
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

# ---------------------------------------------------------------- running daemon?
# Must match _LOCK_PORT in sincrogit/runtime.py (the daemon's control channel).
$LockPort = 29677

function Request-FlushQuit {
    # Ask the daemon to flush all repos (snapshot + autosnap push) and exit
    # cleanly. True only if a real SincroGit ACKed (then wait for it to vanish).
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        if (-not $client.ConnectAsync("127.0.0.1", $LockPort).Wait(2000)) { return $false }
        $stream = $client.GetStream(); $stream.ReadTimeout = 5000
        $msg = [Text.Encoding]::ASCII.GetBytes("SINCROGIT:flushquit")
        $stream.Write($msg, 0, $msg.Length)
        $buf = New-Object byte[] 64
        $n = $stream.Read($buf, 0, 64)
        $client.Close()
        return ($n -gt 0 -and [Text.Encoding]::ASCII.GetString($buf, 0, $n).StartsWith("SINCROGIT:ok"))
    } catch { return $false }
}

$exePath  = Join-Path $root (& { if ($Fast) { "dist\SincroGit\SincroGit.exe" } else { "dist\SincroGit.exe" } })
$relaunch = $false
$running  = @(Get-Process SincroGit -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exePath })

if ($running.Count -gt 0) {
    $relaunch = $true
    Write-Host "==> The exe to be built is running (PID $($running.Id -join ', ')). Asking it to flush + quit..."
    if (Request-FlushQuit) {
        # Flushing pushes the autosnap mirrors; give it time, poll for exit.
        $deadline = (Get-Date).AddSeconds(180)
        while ((Get-Date) -lt $deadline -and (Get-Process -Id $running.Id -ErrorAction SilentlyContinue)) {
            Start-Sleep -Milliseconds 500
        }
    }
    $left = @(Get-Process SincroGit -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exePath })
    if ($left.Count -gt 0) {
        Write-Host "==> Daemon didn't exit on its own (old build, or flush hung); force-killing it."
        $left | Stop-Process -Force
        Start-Sleep -Seconds 1
    } else {
        Write-Host "==> Daemon flushed and exited cleanly."
    }
} elseif (Get-Process SincroGit -ErrorAction SilentlyContinue) {
    Write-Host "==> A SincroGit from another path is running; leaving it alone (not relaunching it either)."
}

# ------------------------------------------------------------------------- build
if (-not (Test-Path app.ico)) {
    Write-Host "==> Generating app.ico from the vector icon..."
    python tools\make_icon.py app.ico
}

$pyiArgs = @(
    "--noconsole",
    "--name", "SincroGit",
    "--icon", "app.ico",
    "--collect-submodules", "watchdog",
    # Pillow (a python-pptx dependency) optionally hooks numpy, and on an
    # Anaconda Python that drags the whole MKL stack into the bundle
    # (~230 MB of DLLs SincroGit never calls). Keep the scientific stack out.
    "--exclude-module", "numpy",
    "--exclude-module", "pandas",
    "--exclude-module", "scipy",
    "--exclude-module", "matplotlib",
    # Never UPX-compress. PyInstaller's default is upx=True, which silently does
    # NOTHING when upx.exe isn't on PATH — so the setting was a no-op here and an
    # ambush everywhere else: the day upx.exe shows up (a dev box, a CI image) the
    # build starts compressing and the artifact changes character with no repo
    # change behind it. Worse, onefile+UPX is the highest-false-positive shape for
    # antivirus/SmartScreen, and this exe already looks suspicious enough (unsigned,
    # self-registers at logon, opens a localhost port). ~5 MB saved on a 50 MB
    # download is not worth either problem. Explicit beats "depends on your PATH".
    "--noupx",
    "--noconfirm"
)
if ($Fast) { $pyiArgs += "--onedir" } else { $pyiArgs += "--onefile" }
if ($Clean) { $pyiArgs += "--clean" }
$pyiArgs += "app.py"

$sw = [System.Diagnostics.Stopwatch]::StartNew()
Write-Host "==> Running PyInstaller ($(if ($Fast) {'onedir/fast'} else {'onefile/release'})$(if ($Clean) {' +clean'}))..."
python -m PyInstaller @pyiArgs
$sw.Stop()

# A failed build must SAY so and must not pretend — without this check the
# script printed "Done" and relaunched the OLD exe, which then held the file
# lock and made every retry fail with Access denied on dist\SincroGit.exe.
if ($LASTEXITCODE -ne 0) {
    Write-Host ("==> BUILD FAILED (PyInstaller exit {0}) after {1:N1}s." -f $LASTEXITCODE, $sw.Elapsed.TotalSeconds)
    if ($relaunch) {
        Write-Host "==> Relaunching the daemon with the PREVIOUS build (never leave it dead)..."
        Start-Process -FilePath $exePath
    }
    exit $LASTEXITCODE
}

$out = if ($Fast) { "dist\SincroGit\SincroGit.exe" } else { "dist\SincroGit.exe" }
Write-Host ""
Write-Host ("==> Done in {0:N1}s. Executable: {1}" -f $sw.Elapsed.TotalSeconds, $out)

# ---------------------------------------------------------------------- relaunch
if ($relaunch) {
    Write-Host "==> Relaunching the daemon with the fresh build..."
    Start-Process -FilePath (Join-Path $root $out)
}