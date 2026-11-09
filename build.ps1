# Call Companion - build script (Windows PowerShell 5.1 or PowerShell 7). Run from the repository folder:
#
#   .\build.ps1                 # exe + installer       -> dist\installer\CallCompanion-Setup-<version>.exe
#   .\build.ps1 -Publish        # ... and create / update the GitHub release v<version> (installed copies update to it)
#   .\build.ps1 -SkipInstaller  # only the PyInstaller folder dist\CallCompanion\ (no Inno Setup needed)
#
# Needs on the BUILD machine only: 64-bit Python 3.10+ (py launcher), Inno Setup 6.3+ (winget install JRSoftware.InnoSetup),
# and for -Publish the GitHub CLI logged in (winget install GitHub.cli; gh auth login).
# The target PC needs nothing: the setup .exe contains the Python runtime, Qt and all assets.
#
# If PowerShell refuses to run scripts:  powershell -ExecutionPolicy Bypass -File .\build.ps1
[CmdletBinding()]
param(
    [switch]$Publish,
    [switch]$SkipInstaller,
    [switch]$Force          # -Publish even with uncommitted changes
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Step($msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }

# ---- version (single source: version.py) ---------------------------------------------------------------------
$verLine = Select-String -Path version.py -Pattern '__version__\s*=\s*"([^"]+)"' | Select-Object -First 1
if (-not $verLine) { throw "__version__ not found in version.py" }
$Version = $verLine.Matches[0].Groups[1].Value
if (-not ($Version -match '^(\d+)\.(\d+)\.(\d+)')) { throw "version.py: '$Version' is not SemVer" }
$NumVersion = "$($Matches[1]).$($Matches[2]).$($Matches[3]).0"
$IsPrerelease = $Version.Contains("-")
Write-Host "Call Companion $Version  (file version $NumVersion)" -ForegroundColor Green

if ($Publish) {
    if ($SkipInstaller) { throw "-Publish needs the installer; drop -SkipInstaller" }
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { throw "GitHub CLI 'gh' not found (winget install GitHub.cli; gh auth login)" }
    $dirty = git status --porcelain
    if ($dirty -and -not $Force) { throw "Uncommitted changes - commit first (the release is built from HEAD) or use -Force" }
}

# ---- Python environment -----------------------------------------------------------------------------------------
Step "Python virtual environment (.venv)"
if (-not (Test-Path ".venv\Scripts\python.exe")) { py -3 -m venv .venv }
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$bits = & $py -c "import struct; print(struct.calcsize('P') * 8)"
if ($bits -ne "64") { throw "The venv Python is $bits-bit; install 64-bit Python (the installer targets x64)" }
& $py -m pip install --disable-pip-version-check --upgrade pip | Out-Host
& $py -m pip install --disable-pip-version-check -r requirements.txt | Out-Host
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# ---- PyInstaller ------------------------------------------------------------------------------------------------
Step "PyInstaller -> dist\CallCompanion\"
Get-Process CallCompanion -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.StartsWith((Join-Path $PSScriptRoot "dist"), "OrdinalIgnoreCase") } |
    Stop-Process -Force
if (Test-Path "dist\CallCompanion") { Remove-Item "dist\CallCompanion" -Recurse -Force }
& $py -m PyInstaller --noconfirm --clean callcompanion.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$out = "dist\CallCompanion"
if (-not (Test-Path "$out\CallCompanion.exe")) { throw "$out\CallCompanion.exe was not produced" }
Copy-Item config.json "$out\config.json" -Force
Copy-Item extension "$out\extension" -Recurse -Force
if (-not (Test-Path "$out\_internal\assets\app.ico")) { throw "assets were not bundled ($out\_internal\assets\app.ico missing)" }
Write-Host "OK: $out\CallCompanion.exe"

if ($SkipInstaller) { Write-Host "`n-SkipInstaller: done."; return }

# ---- Inno Setup -------------------------------------------------------------------------------------------------
Step "Inno Setup -> dist\installer\"
$iscc = $null
$candidates = @(
    $env:ISCC,
    (Get-Command iscc.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
)
foreach ($c in $candidates) { if ($c -and (Test-Path $c)) { $iscc = $c; break } }
if (-not $iscc) { throw "Inno Setup 6 (ISCC.exe) not found. Install it:  winget install JRSoftware.InnoSetup   (or set `$env:ISCC)" }

New-Item -ItemType Directory -Force "dist\installer" | Out-Null
& $iscc /Qp "/DAppVersion=$Version" "/DAppNumVersion=$NumVersion" installer.iss
if ($LASTEXITCODE -ne 0) { throw "ISCC failed ($LASTEXITCODE)" }

$setupName = "CallCompanion-Setup-$Version.exe"
$setup = Join-Path $PSScriptRoot "dist\installer\$setupName"
if (-not (Test-Path $setup)) { throw "$setup was not produced" }

# Checksum file: the auto-updater refuses an installer whose SHA-256 it cannot verify.
$hash = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()
$shaFile = "$setup.sha256"
[System.IO.File]::WriteAllText($shaFile, "$hash  $setupName`n", (New-Object System.Text.UTF8Encoding($false)))
$mb = [math]::Round((Get-Item $setup).Length / 1MB, 1)
Write-Host "OK: $setup  ($mb MB)"
Write-Host "    sha256 $hash"

if (-not $Publish) {
    Write-Host ""
    Write-Host "Test install on this PC: double-click the setup, or silently:" -ForegroundColor Green
    Write-Host "    & '$setup' /VERYSILENT /SUPPRESSMSGBOXES"
    Write-Host "Publish for auto-update:  .\build.ps1 -Publish"
    return
}

# ---- GitHub release ---------------------------------------------------------------------------------------------
Step "GitHub release v$Version"
$tag = "v$Version"
$notesFile = Join-Path $env:TEMP "callcompanion-notes-$Version.md"
$changelog = Get-Content CHANGELOG.md -Raw -Encoding UTF8
$pattern = '(?s)## \[' + [regex]::Escape($Version) + '\][^\n]*\n(.*?)(?=\n## \[|\z)'
$m = [regex]::Match($changelog, $pattern)
$notes = if ($m.Success) { $m.Groups[1].Value.Trim() } else { "Call Companion $Version" }
$notes += "`n`n---`nInstall: download **$setupName** and run it (no admin rights needed). Installed copies update themselves."
[System.IO.File]::WriteAllText($notesFile, $notes, (New-Object System.Text.UTF8Encoding($false)))

$commit = (git rev-parse HEAD).Trim()
# Windows PowerShell 5.1 turns redirected stderr of a native command into an error -> relax "Stop" for this probe.
$ErrorActionPreference = "Continue"
gh release view $tag *> $null
$exists = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = "Stop"
if ($exists) {
    Write-Host "Release $tag exists - replacing its assets"
    gh release upload $tag $setup $shaFile --clobber
} else {
    $ghArgs = @("release", "create", $tag, $setup, $shaFile, "--title", "Call Companion $Version",
                "--notes-file", $notesFile, "--target", $commit)
    if ($IsPrerelease) { $ghArgs += "--prerelease" }
    & gh @ghArgs
}
if ($LASTEXITCODE -ne 0) { throw "gh release failed" }
Write-Host "Published $tag. Installed copies pick it up within check_interval_h (or tray > Check for updates)." -ForegroundColor Green
