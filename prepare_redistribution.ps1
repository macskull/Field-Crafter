$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
& "$PSScriptRoot\build_release.ps1" -DataOnly
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
