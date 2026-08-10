# Deterministic build + deploy of the desktop agent AND its installer.
# Run from anywhere: output always lands in <repo>\dist (never a CWD-relative
# dist/, which is what created the stray agent\dist earlier), then deploys both
# artifacts to the copy the app serves for download. The served exe may be
# running and file-locked, so we stop it, copy, and restart.
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

Write-Host "Building DevActivityAgent from $root ..."
python -m PyInstaller "$root\agent\agent.spec" `
  --distpath "$root\dist" --workpath "$root\build" --noconfirm

$built = "$root\dist\DevActivityAgent.exe"
if (-not (Test-Path $built)) { throw "Build produced no exe at $built" }

# Build the installer (it bundles the exe from dist\, so this must run after PyInstaller).
$iscc = (Get-Command iscc -ErrorAction SilentlyContinue).Source
if (-not $iscc) {
  $iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
            "$env:ProgramFiles\Inno Setup 6\ISCC.exe") |
          Where-Object { Test-Path $_ } | Select-Object -First 1
}
if ($iscc) {
  & $iscc "$root\installer.iss" | Out-Null
  Write-Host "Installer built -> $root\dist\DevActivitySetup.exe"
} else {
  Write-Warning "Inno Setup (ISCC) not found - skipping installer. Install: winget install JRSoftware.InnoSetup"
}

# Deploy exe + installer to the served downloads dir. The served exe is the
# autostart/Start-menu target and may be running (file-locked): stop, copy, restart.
$downloads = "$root\app\static\downloads"
$install   = "$env:LOCALAPPDATA\Developer Activity"   # stable autostart copy (if installed)
$running = Get-Process -Name DevActivityAgent -ErrorAction SilentlyContinue
if ($running) { Stop-Process -Name DevActivityAgent -Force; Start-Sleep -Milliseconds 800 }
Copy-Item $built "$downloads\DevActivityAgent.exe" -Force
if (Test-Path "$root\dist\DevActivitySetup.exe") {
  Copy-Item "$root\dist\DevActivitySetup.exe" "$downloads\DevActivitySetup.exe" -Force
}
# Keep the installed autostart copy (%LocalAppData%) fresh, only if this machine uses it.
$installedExe = "$install\DevActivityAgent.exe"
if (Test-Path $install) { Copy-Item $built $installedExe -Force; Write-Host "Updated install copy -> $installedExe" }
Write-Host "Deployed -> $downloads"
# Restart from the installed copy if present (matches autostart), else the served copy.
if ($running) {
  $launch = if (Test-Path $installedExe) { $installedExe } else { "$downloads\DevActivityAgent.exe" }
  Start-Process -FilePath $launch
  Write-Host "Agent restarted from $launch"
}
