#Requires -Version 5.1
<#
.SYNOPSIS
  Install the Clawdmeter daemon on Windows (venv + Scheduled Task).
.DESCRIPTION
  Sets up a Python venv at daemon\.venv\, installs bleak + httpx,
  registers a per-user Scheduled Task that runs at logon and restarts
  on failure, and starts it. Mirrors install-mac.sh.
.PARAMETER SkipPrimeRun
  Skip the optional foreground priming scan after installation.
.EXAMPLE
  .\install-win.ps1
.EXAMPLE
  .\install-win.ps1 -SkipPrimeRun
#>
[CmdletBinding()]
param([switch]$SkipPrimeRun)
$ErrorActionPreference = 'Stop'

$RepoDir    = $PSScriptRoot
$TaskName   = 'Clawdmeter Daemon'
$VenvDir    = Join-Path $RepoDir 'daemon\.venv'
$DaemonPy   = Join-Path $RepoDir 'daemon\claude_usage_daemon.py'
$LogDir     = Join-Path $env:LOCALAPPDATA 'Clawdmeter\logs'
$LogOut     = Join-Path $LogDir 'claude-usage-daemon.out.log'
$LogErr     = Join-Path $LogDir 'claude-usage-daemon.err.log'
$CredsPath  = Join-Path $env:USERPROFILE '.claude\.credentials.json'
$PythonBin  = Join-Path $VenvDir 'Scripts\python.exe'
$PythonwBin = Join-Path $VenvDir 'Scripts\pythonw.exe'

Write-Host "=== Clawdmeter Windows install ==="
Write-Host ""

# ── Phase 1/5: Prerequisites ─────────────────────────────────────────────────
Write-Host "[1/5] Checking prerequisites..."

$pythonLauncher = $null

# Try py -3 first (Python Launcher for Windows), then python, then python3.
# Each candidate is stored as an array so we can splat extra args cleanly.
$candidates = @(
    @('py', '-3'),
    @('python'),
    @('python3')
)

foreach ($candidate in $candidates) {
    $exe      = $candidate[0]
    $extraArgs = @()
    if ($candidate.Length -gt 1) {
        $extraArgs = $candidate[1..($candidate.Length - 1)]
    }
    try {
        $verOutput = & $exe @extraArgs --version 2>&1
        if ($LASTEXITCODE -eq 0 -or ("$verOutput" -match 'Python \d+\.\d+')) {
            $pythonLauncher = $candidate
            break
        }
    } catch {
        # command not found — try next
    }
}

if ($null -eq $pythonLauncher) {
    Write-Host 'Error: Python 3.10+ not found.'
    Write-Host '  Install from https://www.python.org/downloads/ (check "Add to PATH").'
    exit 1
}

# Parse and validate the version string.
$pyExe      = $pythonLauncher[0]
$pyExtraArgs = @()
if ($pythonLauncher.Length -gt 1) {
    $pyExtraArgs = $pythonLauncher[1..($pythonLauncher.Length - 1)]
}
$verString = & $pyExe @pyExtraArgs --version 2>&1
if ("$verString" -match 'Python (\d+)\.(\d+)') {
    $pyMajor = [int]$Matches[1]
    $pyMinor = [int]$Matches[2]
} else {
    Write-Host "Error: Could not parse Python version from: $verString"
    exit 1
}

if ($pyMajor -lt 3 -or ($pyMajor -eq 3 -and $pyMinor -lt 10)) {
    Write-Host "Error: Python $pyMajor.$pyMinor found; this daemon requires Python 3.10 or newer."
    exit 1
}

# Check credentials — warning only, do not exit.
if (-not (Test-Path $CredsPath)) {
    Write-Host 'Warning: Claude Code OAuth token not found at %USERPROFILE%\.claude\.credentials.json'
    Write-Host '  Sign in via Claude Code first, then re-run this installer.'
    Write-Host '  Continuing anyway — the daemon will retry on each poll.'
}

Write-Host "  OK"
Write-Host ""

# ── Phase 2/5: Venv + dependencies ──────────────────────────────────────────
Write-Host "[2/5] Creating Python virtualenv at daemon\.venv ..."

if (-not (Test-Path $VenvDir)) {
    & $pyExe @pyExtraArgs -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Error: Failed to create virtual environment."
        exit 1
    }
}

& $PythonBin -m pip install --quiet --upgrade pip
if ($LASTEXITCODE -ne 0) {
    Write-Host "Error: Failed to upgrade pip."
    exit 1
}

& $PythonBin -m pip install --quiet "bleak>=0.22" "httpx>=0.27"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Error: Failed to install dependencies."
    exit 1
}

Write-Host "  OK ($PythonBin)"
Write-Host ""

# ── Phase 3/5: Register Scheduled Task ──────────────────────────────────────
Write-Host "[3/5] Registering Scheduled Task..."

New-Item -ItemType Directory -Force $LogDir | Out-Null

$userId = "$env:USERDOMAIN\$env:USERNAME"

# Build the Task Scheduler XML.
# Use a single-quoted here-string (no PowerShell expansion) so the &quot; and
# &gt; XML entities are not misread as PS operators, then substitute placeholders.
$xmlTemplate = @'
<?xml version="1.0" encoding="UTF-16"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
    <RegistrationInfo>
        <Description>Clawdmeter daemon -- polls Anthropic API and sends usage data to the ESP32 Claude Controller over BLE.</Description>
        <URI>\Clawdmeter Daemon</URI>
    </RegistrationInfo>
    <Triggers>
        <LogonTrigger>
            <Enabled>true</Enabled>
            <UserId>__USERID__</UserId>
        </LogonTrigger>
    </Triggers>
    <Principals>
        <Principal id="Author">
            <UserId>__USERID__</UserId>
            <LogonType>InteractiveToken</LogonType>
            <RunLevel>LeastPrivilege</RunLevel>
        </Principal>
    </Principals>
    <Settings>
        <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
        <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
        <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
        <AllowHardTerminate>true</AllowHardTerminate>
        <StartWhenAvailable>true</StartWhenAvailable>
        <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
        <Priority>7</Priority>
        <RestartOnFailure>
            <Interval>PT10S</Interval>
            <Count>999</Count>
        </RestartOnFailure>
        <Enabled>true</Enabled>
        <Hidden>false</Hidden>
    </Settings>
    <Actions Context="Author">
        <Exec>
            <Command>cmd.exe</Command>
            <Arguments>/c "&quot;__PYTHONW__&quot; &quot;__DAEMON__&quot; &gt;&gt; &quot;__LOGOUT__&quot; 2&gt;&gt; &quot;__LOGERR__&quot;"</Arguments>
            <WorkingDirectory>__REPODIR__</WorkingDirectory>
        </Exec>
    </Actions>
</Task>
'@

$xml = $xmlTemplate
$xml = $xml.Replace('__USERID__',   $userId)
$xml = $xml.Replace('__PYTHONW__',  $PythonwBin)
$xml = $xml.Replace('__DAEMON__',   $DaemonPy)
$xml = $xml.Replace('__LOGOUT__',   $LogOut)
$xml = $xml.Replace('__LOGERR__',   $LogErr)
$xml = $xml.Replace('__REPODIR__',  $RepoDir)

Register-ScheduledTask -TaskName $TaskName -Xml $xml -Force | Out-Null

Write-Host "  Registered: $TaskName"
Write-Host ""

# ── Phase 4/5: Bluetooth pairing primer ─────────────────────────────────────
Write-Host "[4/5] Bluetooth pairing"
Write-Host "  Open Settings -> Bluetooth & devices -> Add device -> Bluetooth"
Write-Host '  and pick "Claude Controller". No PIN required (JustWorks pairing).'
Write-Host "  Pairing once in Settings is required for reliable WinRT GATT access."
Write-Host ""

if (-not $SkipPrimeRun) {
    Write-Host "Run a 10-second test scan now? [Y/n] " -NoNewline
    $ans = Read-Host
    if ($ans -notmatch '^[Nn]') {
        Write-Host "  Starting foreground scan. Press Ctrl+C when you've seen 'Scanning...' and the device get discovered."
        try {
            Start-Process -FilePath $PythonBin -ArgumentList "`"$DaemonPy`"" -NoNewWindow -Wait
        } catch {
            # Start-Process can throw if the process can't start; carry on.
            Write-Host "  Priming run skipped: $_"
        }
    }
} else {
    Write-Host "  Skipping priming run (-SkipPrimeRun). Use Start-ScheduledTask to test once paired."
}

Write-Host ""

# ── Phase 5/5: Start the task ────────────────────────────────────────────────
Write-Host "[5/5] Starting Scheduled Task..."
Start-ScheduledTask -TaskName $TaskName
Write-Host "  Started."
Write-Host ""

Write-Host "=== Done ==="
Write-Host ""
Write-Host "Useful commands:"
Write-Host "  Get-ScheduledTask     -TaskName 'Clawdmeter Daemon'        # check it's registered"
Write-Host "  Get-ScheduledTaskInfo -TaskName 'Clawdmeter Daemon'        # last run time / result"
Write-Host "  Get-Content `"$env:LOCALAPPDATA\Clawdmeter\logs\claude-usage-daemon.out.log`" -Wait   # live logs"
Write-Host "  Stop-ScheduledTask    -TaskName 'Clawdmeter Daemon'        # stop"
Write-Host "  Start-ScheduledTask   -TaskName 'Clawdmeter Daemon'        # start"
Write-Host "  Unregister-ScheduledTask -TaskName 'Clawdmeter Daemon' -Confirm:`$false   # uninstall"
