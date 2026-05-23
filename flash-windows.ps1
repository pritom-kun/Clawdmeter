# Build and flash Clawdmeter firmware on Windows.
# Usage:
#   .\flash-windows.ps1                              # auto-detect ESP32-S3 COM port, default env
#   .\flash-windows.ps1 COM7                         # explicit COM port
#   .\flash-windows.ps1 -Env waveshare_amoled_18     # different board, auto-detect port
#   .\flash-windows.ps1 -Env waveshare_amoled_18 COM7
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Port,
    [string]$Env = "waveshare_amoled_216"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# Locate pio: PATH first, then the bundled penv.
$pio = (Get-Command pio -ErrorAction SilentlyContinue).Source
if (-not $pio) {
    $bundled = Join-Path $env:USERPROFILE ".platformio\penv\Scripts\pio.exe"
    if (Test-Path $bundled) { $pio = $bundled }
}
if (-not $pio) {
    Write-Host "Error: 'pio' not found. Install with one of:" -ForegroundColor Red
    Write-Host "  pip install platformio"
    Write-Host "  https://platformio.org/install/cli"
    exit 1
}

# Auto-detect ESP32-S3 USB-JTAG port if none was passed. The ESP32-S3's
# native USB-JTAG enumerates on Windows 10/11 as either "USB JTAG/serial
# debug unit" or "USB Serial Device" — match either FriendlyName.
if (-not $Port) {
    $candidates = Get-PnpDevice -Class Ports -PresentOnly -ErrorAction SilentlyContinue |
        Where-Object { $_.FriendlyName -match "USB JTAG|USB Serial Device" } |
        ForEach-Object {
            if ($_.FriendlyName -match "\((COM\d+)\)") { $matches[1] }
        } | Where-Object { $_ }

    if (-not $candidates -or $candidates.Count -eq 0) {
        Write-Host "Error: no ESP32-S3 COM port found. Plug in via USB-C and try again," -ForegroundColor Red
        Write-Host "or pass the port explicitly: .\flash-windows.ps1 COM7"
        exit 1
    }

    $Port = @($candidates)[0]
    if (@($candidates).Count -gt 1) {
        Write-Host "Multiple candidates found: $($candidates -join ', '). Picking $Port."
        Write-Host "If wrong, re-run with explicit port: .\flash-windows.ps1 <COMx>"
    }
}

Write-Host "=== Flashing Clawdmeter ==="
Write-Host "Env:  $Env"
Write-Host "Port: $Port"
Write-Host ""

& $pio run -d (Join-Path $ScriptDir "firmware") -e $Env -t upload --upload-port $Port
if ($LASTEXITCODE -ne 0) {
    Write-Host "Flash failed (exit $LASTEXITCODE)." -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "=== Done ==="
Write-Host "Monitor with: pio device monitor -p $Port -b 115200"
