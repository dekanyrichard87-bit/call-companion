# Installs Call Companion for the current user and enables autostart at logon (no admin needed).
# Run from the folder that contains CallCompanion\ (i.e. dist\).
$ErrorActionPreference = "Stop"
$src = Join-Path $PSScriptRoot "CallCompanion"
$dst = Join-Path $env:LOCALAPPDATA "Programs\CallCompanion"
if (-not (Test-Path (Join-Path $src "CallCompanion.exe"))) { throw "CallCompanion\CallCompanion.exe not found next to this script" }

Get-Process CallCompanion -ErrorAction SilentlyContinue | Stop-Process -Force
New-Item -ItemType Directory -Force $dst | Out-Null

# /MIR updates the app; /XF keeps the user's edited config.json
robocopy $src $dst /MIR /XF config.json /NFL /NDL /NJH /NJS | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE)" }
if (-not (Test-Path (Join-Path $dst "config.json"))) { Copy-Item (Join-Path $src "config.json") $dst }

& (Join-Path $dst "CallCompanion.exe") --install-autostart
Start-Process (Join-Path $dst "CallCompanion.exe")
Write-Host "Installed to $dst - autostart enabled, app started (tray icon)."
Write-Host "Chrome extension folder: $dst\extension  (chrome://extensions -> Developer mode -> Load unpacked)"
