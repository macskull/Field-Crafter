$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$Python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$PythonW = Join-Path $PSScriptRoot ".venv\Scripts\pythonw.exe"

$NeedsSetup = -not (Test-Path $PythonW)
if (-not $NeedsSetup) {
    & $Python -c "import sys, rapidocr, onnxruntime, PIL, tkinterdnd2, requests, bs4; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)" *> $null
    if ($LASTEXITCODE -ne 0) {
        $NeedsSetup = $true
    }
}

if ($NeedsSetup) {
    & "$PSScriptRoot\setup_ocr.ps1"
    if ($LASTEXITCODE -ne 0) {
        throw "Field Crafter Python setup failed."
    }
}

& $PythonW "$PSScriptRoot\Field Crafter.pyw"
