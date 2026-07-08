# Deterministic build + deploy of the desktop agent.
# Run from anywhere: output always lands in <repo>\dist (never a CWD-relative
# dist/, which is what created the stray agent\dist earlier), then deploys to the
# copy the app serves for download. The served exe may be running and file-locked,
# so we stop it, copy, and restart.
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

Write-Host "Building DevActivityAgent from $root ..."
python -m PyInstaller "$root\agent\agent.spec" `
  --distpath "$root\dist" --workpath "$root\build" --noconfirm

$built = "$root\dist\DevActivityAgent.exe"
if (-not (Test-Path $built)) { throw "Build produced no exe at $built" }

$served  = "$root\app\static\downloads\DevActivityAgent.exe"
$running = Get-Process -Name DevActivityAgent -ErrorAction SilentlyContinue
if ($running) { Stop-Process -Name DevActivityAgent -Force; Start-Sleep -Milliseconds 800 }
Copy-Item $built $served -Force
Write-Host "Deployed -> $served"
if ($running) { Start-Process -FilePath $served; Write-Host "Agent restarted." }
