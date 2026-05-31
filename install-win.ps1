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
.PARAMETER SkipTokenSetup
  Skip the long-lived OAuth token setup phase. The daemon will fall back
  to .credentials.json (subject to the documented refresh issues on Windows).
.EXAMPLE
  .\install-win.ps1
.EXAMPLE
  .\install-win.ps1 -SkipPrimeRun
.EXAMPLE
  .\install-win.ps1 -SkipTokenSetup
#>
[CmdletBinding()]
param(
    [switch]$SkipPrimeRun,
    [switch]$SkipTokenSetup
)
$ErrorActionPreference = 'Stop'

$RepoDir    = $PSScriptRoot
$TaskName   = 'Clawdmeter Daemon'
$VenvDir    = Join-Path $RepoDir 'daemon\.venv'
$DaemonPy   = Join-Path $RepoDir 'daemon\claude_usage_daemon.py'
$LogDir     = Join-Path $env:LOCALAPPDATA 'Clawdmeter\logs'
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
            <Interval>PT1M</Interval>
            <Count>999</Count>
        </RestartOnFailure>
        <Enabled>true</Enabled>
        <Hidden>false</Hidden>
    </Settings>
    <Actions Context="Author">
        <Exec>
            <Command>__PYTHONW__</Command>
            <Arguments>&quot;__DAEMON__&quot; &quot;__LOGDIR__&quot;</Arguments>
            <WorkingDirectory>__REPODIR__</WorkingDirectory>
        </Exec>
    </Actions>
</Task>
'@

$xml = $xmlTemplate
$xml = $xml.Replace('__USERID__',   $userId)
$xml = $xml.Replace('__PYTHONW__',  $PythonwBin)
$xml = $xml.Replace('__DAEMON__',   $DaemonPy)
$xml = $xml.Replace('__LOGDIR__',   $LogDir)
$xml = $xml.Replace('__REPODIR__',  $RepoDir)

try {
    Register-ScheduledTask -TaskName $TaskName -Xml $xml -Force -ErrorAction Stop | Out-Null
} catch {
    # Separate our message from any partial output Register-ScheduledTask may have written.
    Write-Host ""
    Write-Host "Error: Failed to register Scheduled Task."
    Write-Host "  $($_.Exception.Message)"
    exit 1
}
if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
    Write-Host "Error: Task '$TaskName' was not registered despite no error."
    exit 1
}

Write-Host "  Registered: $TaskName"
Write-Host ""

# ── Phase 3.5/5: Long-lived OAuth token setup ───────────────────────────────
Write-Host "[3.5/5] Long-lived OAuth token"

$tokenPath = Join-Path $env:USERPROFILE '.claude\.clawdmeter-oauth-token'
$tokenAlreadySet = ($env:CLAUDE_CODE_OAUTH_TOKEN -and $env:CLAUDE_CODE_OAUTH_TOKEN.Trim()) -or (Test-Path $tokenPath)

if ($tokenAlreadySet) {
    Write-Host "  Long-lived token already configured."
} elseif ($SkipTokenSetup) {
    Write-Host "  Skipping token setup (-SkipTokenSetup). Daemon will fall back to .credentials.json."
    Write-Host "  Note: on Windows, .credentials.json may contain a stale refresh token that returns"
    Write-Host "  400 invalid_grant. Run .\install-win.ps1 again (without -SkipTokenSetup) to set up a token."
} else {
    Write-Host "  Configure a long-lived OAuth token now? (recommended) [Y/n] " -NoNewline
    $ans = Read-Host
    if ($ans -match '^[Nn]') {
        Write-Host "  Skipped. The daemon will fall back to .credentials.json."
        Write-Host "  Note: on Windows, .credentials.json may contain a stale refresh token - see"
        Write-Host "  https://code.claude.com/docs/en/authentication#generate-a-long-lived-token"
    } else {
        Write-Host ""
        Write-Host "  Steps:"
        Write-Host "    1. In a separate PowerShell window, run:  claude setup-token"
        Write-Host "    2. Sign in via the browser when prompted."
        Write-Host "    3. Copy the printed token (it begins with `"sk-ant-oat01-`")."
        Write-Host "    4. Paste it here."
        Write-Host ""

        $tok = $null
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            $tok = Read-Host "  Token"
            if ($tok -match '^sk-ant-oat01-') {
                break
            }
            if ($attempt -lt 3) {
                Write-Host "  Invalid token format (expected prefix sk-ant-oat01-). Try again ($attempt/3)."
            } else {
                Write-Host "  Invalid token format (expected prefix sk-ant-oat01-) ($attempt/3); aborting."
            }
            $tok = $null
        }

        if ($null -eq $tok) {
            Write-Host "Error: No valid token provided after 3 attempts."
            exit 1
        }

        New-Item -ItemType Directory -Force (Split-Path $tokenPath -Parent) | Out-Null
        # PowerShell 5.1's `Set-Content -Encoding utf8` writes a UTF-8 BOM,
        # which the daemon would otherwise prepend to the Bearer header.
        # Write BOM-less UTF-8 via .NET so both PS 5.1 and PS 7+ behave the same.
        [IO.File]::WriteAllText($tokenPath, $tok, (New-Object Text.UTF8Encoding $false))
        Write-Host "  Token saved to $tokenPath"
        Write-Host "  This token lasts ~1 year. To rotate it, delete the file and re-run the installer."
    }
}

Write-Host ""

# ── Phase 4/5: Bluetooth pairing primer ─────────────────────────────────────
Write-Host "[4/5] Bluetooth pairing"
Write-Host "  Open Settings -> Bluetooth & devices -> Add device -> Bluetooth"
Write-Host '  and pick "Claude Controller". No PIN required (JustWorks pairing).'
Write-Host "  Pairing once in Settings is required for reliable WinRT GATT access."
Write-Host ""

if (-not $SkipPrimeRun) {
    Write-Host "Run a one-time connection test now? It shows progress, then hands off to the background task. [Y/n] " -NoNewline
    $ans = Read-Host
    if ($ans -notmatch '^[Nn]') {
        # The background task may already be running (re-install, or a prior
        # session). The ESP32 accepts only one BLE connection at a time, so a
        # running daemon and this foreground scan fight over the slot -- the
        # scan ends up connected but unable to see any GATT characteristic.
        # Stop the task and wait for the daemon to release the link first;
        # phase 5 restarts it afterward.
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        $running = @()
        for ($i = 0; $i -lt 20; $i++) {
            $running = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -in @('python.exe', 'pythonw.exe') -and $_.CommandLine -like '*claude_usage_daemon*' })
            if ($running.Count -eq 0) { break }
            Start-Sleep -Milliseconds 300
        }
        if ($running.Count -gt 0) {
            # Force-kill stragglers so the scan gets exclusive device access.
            $running | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
            Start-Sleep -Milliseconds 500
        }

        Write-Host "  Connecting to the device (shows progress, then exits automatically once the first update is sent)..."
        try {
            Start-Process -FilePath $PythonBin -ArgumentList "`"$DaemonPy`"", "--once" -NoNewWindow -Wait
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
