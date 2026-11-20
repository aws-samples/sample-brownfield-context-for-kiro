# Fallback install: copy the three brownfield-context skills into ~/.kiro/skills/.
# Use when importing the Kiro Power is not possible (older Kiro CLI, blocked Power import).
# Usage: powershell -ExecutionPolicy Bypass -File tools\install-skills.ps1 [-Package <dist\brownfield-context>]
param(
    [string]$Package = (Join-Path (Split-Path -Parent $PSScriptRoot) "dist\brownfield-context")
)
$ErrorActionPreference = "Stop"
$skillsSrc = Join-Path $Package "skills"
if (-not (Test-Path $skillsSrc)) {
    Write-Error "Skills not found under '$skillsSrc'. Run: python tools\build_power.py (or unzip brownfield-context-1.4.3.zip)."
}
$dest = Join-Path $HOME ".kiro\skills"
New-Item -ItemType Directory -Force -Path $dest | Out-Null
foreach ($skill in Get-ChildItem -Directory $skillsSrc) {
    $target = Join-Path $dest $skill.Name
    if (Test-Path $target) { Remove-Item -Recurse -Force $target }
    Copy-Item -Recurse $skill.FullName $target
    Write-Host "installed $($skill.Name) -> $target"
}
$cli = Join-Path $dest "brownfield-context-setup\scripts\bcp_cli.py"
Write-Host ""
Write-Host "Run the tool directly with Python 3.12+:"
Write-Host "  python `"$cli`" --help"
Write-Host "  python `"$cli`" generate --bre <bre-folder> --target <repo> --review-dir <review-folder>"
