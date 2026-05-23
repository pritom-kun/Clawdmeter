# Windows installer for Clawdmeter daemon (Python + bleak + Startup shortcut).
# Mirrors install-mac.sh but uses pythonw.exe + a Startup folder .lnk
# instead of LaunchAgents.
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DaemonDir = Join-Path $ScriptDir "daemon"
$VenvDir   = Join-Path $DaemonDir ".venv"
$DaemonPy  = Join-Path $DaemonDir "claude_usage_daemon.py"
$Template  = Join-Path $DaemonDir "start-daemon.cmd.template"
$LauncherCmd = Join-Path $DaemonDir "start-daemon.cmd"
$StartupDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"
$ShortcutPath = Join-Path $StartupDir "Clawdmeter Daemon.lnk"
$LogDir = Join-Path $env:APPDATA "claude-usage-monitor"
$LogFile = Join-Path $LogDir "daemon.log"

Write-Host "=== Clawdmeter Windows install ==="
Write-Host ""

Write-Host "[1/5] Checking prerequisites..."
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) {
    Write-Host "Error: 'python' not found on PATH. Install Python 3.10+ from python.org" -ForegroundColor Red
    Write-Host "  (and tick 'Add python.exe to PATH' in the installer)."
    exit 1
}
$creds = Join-Path $env:USERPROFILE ".claude\.credentials.json"
if (-not (Test-Path $creds)) {
    Write-Host "Warning: $creds not found." -ForegroundColor Yellow
    Write-Host "  Sign in via Claude Code first, then re-run this installer."
    Write-Host "  Continuing anyway - the daemon will retry on each poll."
}
Write-Host "  OK"
Write-Host ""

Write-Host "[2/5] Creating Python virtualenv at daemon\.venv ..."
if (-not (Test-Path $VenvDir)) {
    & $python -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { Write-Host "venv creation failed" -ForegroundColor Red; exit 1 }
}
$VenvPip    = Join-Path $VenvDir "Scripts\pip.exe"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$VenvPythonW = Join-Path $VenvDir "Scripts\pythonw.exe"
& $VenvPython -m pip install --quiet --upgrade pip
& $VenvPip install --quiet "bleak>=0.22" "httpx>=0.27"
if ($LASTEXITCODE -ne 0) { Write-Host "pip install failed" -ForegroundColor Red; exit 1 }
Write-Host "  OK ($VenvPythonW)"
Write-Host ""

Write-Host "[3/5] Rendering launcher wrapper..."
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$tmpl = Get-Content -Raw $Template
$rendered = $tmpl.Replace("__VENV_PYTHONW__", $VenvPythonW).Replace("__DAEMON_PATH__", $DaemonPy)
Set-Content -Path $LauncherCmd -Value $rendered -Encoding ASCII
Write-Host "  Wrote: $LauncherCmd"
Write-Host ""

Write-Host "[4/5] Creating Startup shortcut..."
if (-not (Test-Path $StartupDir)) { New-Item -ItemType Directory -Path $StartupDir -Force | Out-Null }
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($ShortcutPath)
$shortcut.TargetPath = $LauncherCmd
$shortcut.WorkingDirectory = $DaemonDir
$shortcut.WindowStyle = 7  # minimized
$shortcut.Description = "Claude usage tracker BLE daemon"
$shortcut.Save()
Write-Host "  Installed: $ShortcutPath"
Write-Host ""

Write-Host "[5/5] Launching daemon now..."
Write-Host "  (silent - check the log file for output)"
& cmd.exe /c $LauncherCmd
Write-Host ""

Write-Host "=== Done ==="
Write-Host ""
Write-Host "First-time Bluetooth pairing (after firmware is flashed):"
Write-Host "  1. Power on the device."
Write-Host "  2. Open Settings > Bluetooth & devices > Add device > Bluetooth."
Write-Host "  3. Pick 'Claude Controller'."
Write-Host "  4. The daemon will discover it within ~30 s and start polling."
Write-Host ""
Write-Host "Useful commands:"
Write-Host "  Get-Process pythonw                                   # check it's running"
Write-Host "  Get-Content -Wait `"$LogFile`"                          # live logs"
Write-Host "  Stop-Process -Name pythonw -Force                     # stop"
Write-Host "  & `"$LauncherCmd`"                                      # start again"
Write-Host "  Remove-Item `"$ShortcutPath`"                           # disable autostart"
