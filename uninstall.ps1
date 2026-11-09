# Stops Call Companion, removes autostart and the program folder (keeps %LOCALAPPDATA%\CallCompanion logs/data).
$dst = Join-Path $env:LOCALAPPDATA "Programs\CallCompanion"
$exe = Join-Path $dst "CallCompanion.exe"
if (Test-Path $exe) { & $exe --remove-autostart }
Get-Process CallCompanion -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 500
if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
Write-Host "Removed. Also remove the Chrome extension in chrome://extensions."
