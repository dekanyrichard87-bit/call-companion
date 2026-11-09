# Builds dist\CallCompanion\ (exe + config.json + extension\).  Run in PowerShell from this folder.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) { py -3 -m venv .venv }
$py = ".\.venv\Scripts\python.exe"
& $py -m pip install --upgrade pip
& $py -m pip install -r requirements.txt
& $py -m PyInstaller --noconfirm --clean callcompanion.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

Copy-Item config.json dist\CallCompanion\config.json -Force
Copy-Item extension dist\CallCompanion\extension -Recurse -Force
Copy-Item install.ps1, uninstall.ps1 dist\ -Force
Write-Host "Build OK -> dist\CallCompanion\CallCompanion.exe"
