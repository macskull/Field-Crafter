$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path ".venv\Scripts\pythonw.exe")) {
    & "$PSScriptRoot\setup_ocr.ps1"
}
& ".\.venv\Scripts\pythonw.exe" "$PSScriptRoot\Field Crafter.pyw"
