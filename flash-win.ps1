#Requires -Version 5.1
<#
.SYNOPSIS
  Build and flash Clawdmeter firmware on Windows.
.DESCRIPTION
  Wraps `pio run -e <env> -t upload` with COM port auto-detection.
.EXAMPLE
  .\flash-win.ps1 waveshare_amoled_216           # auto-detects ESP32 COM port
.EXAMPLE
  .\flash-win.ps1 waveshare_amoled_18  COM7      # explicit COM port
#>
[CmdletBinding()]
param(
    [Parameter(Position=0)] [string]$Board,
    [Parameter(Position=1)] [string]$Port
)
$ErrorActionPreference = 'Stop'

# ── No board → print usage and list available envs ──────────────────────────
if (-not $Board) {
    $iniPath = Join-Path $PSScriptRoot 'firmware\platformio.ini'
    $matches = Select-String -Path $iniPath -Pattern '^\[env:(.+)\]'
    Write-Host "Usage: .\flash-win.ps1 <board> [COMport]"
    Write-Host "Available boards:"
    foreach ($m in $matches) {
        $envName = $m.Matches[0].Groups[1].Value
        Write-Host "  $envName"
    }
    exit 1
}

# ── Locate pio.exe ───────────────────────────────────────────────────────────
$pioExe = (Get-Command pio.exe -ErrorAction SilentlyContinue).Source
if (-not $pioExe) {
    $fallback = Join-Path $env:USERPROFILE '.platformio\penv\Scripts\pio.exe'
    if (Test-Path $fallback) {
        $pioExe = $fallback
    }
}
if (-not $pioExe) {
    Write-Host "Error: pio.exe not found. Install PlatformIO with ``pip install platformio`` or via the standalone installer at https://platformio.org/install/cli."
    exit 1
}

# ── Auto-detect COM port ─────────────────────────────────────────────────────
if (-not $Port) {
    # Prefer Espressif native USB (VID 303A — ESP32-S2/S3/C series)
    $espDevices = Get-CimInstance Win32_PnPEntity -Filter "Caption LIKE '%(COM%'" |
        Where-Object { $_.HardwareID -match 'VID_303A' }

    $comNumber = $null
    foreach ($dev in $espDevices) {
        if ($dev.Caption -match '\(COM(\d+)\)') {
            $comNumber = [int]$Matches[1]
            break
        }
    }

    if ($null -eq $comNumber) {
        # Fall back to first USB-serial bridge (CP210x, Silicon Labs, generic USB serial)
        $usbDevices = Get-CimInstance Win32_PnPEntity -Filter "Caption LIKE '%(COM%'" |
            Where-Object { $_.Caption -match '(USB|Silicon Labs|CP210)' }

        foreach ($dev in $usbDevices) {
            if ($dev.Caption -match '\(COM(\d+)\)') {
                $comNumber = [int]$Matches[1]
                break
            }
        }
    }

    if ($null -eq $comNumber) {
        Write-Host "Error: no ESP32 COM port found. Plug the board in via USB-C, or pass an explicit port (e.g. .\flash-win.ps1 $Board COM7)."
        exit 1
    }

    $Port = "COM$comNumber"
    Write-Host "Auto-detected port: $Port"
}

# ── Banner ───────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=== Flashing Clawdmeter ==="
Write-Host "Board: $Board"
Write-Host "Port:  $Port"
Write-Host ""

# ── Invoke pio ───────────────────────────────────────────────────────────────
Push-Location (Join-Path $PSScriptRoot 'firmware')
try {
    & $pioExe run -e $Board -t upload --upload-port $Port
    if ($LASTEXITCODE -ne 0) {
        throw "pio exited with code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}

# ── Done ─────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=== Done! ==="
Write-Host "To watch serial output: pio device monitor -p $Port -b 115200"
