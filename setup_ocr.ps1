$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$Wheelhouse = Join-Path $PSScriptRoot "wheelhouse"
$Venv = Join-Path $PSScriptRoot ".venv"
$Python = Join-Path $Venv "Scripts\python.exe"

if (-not (Test-Path $Wheelhouse)) {
    throw "The Field Crafter offline dependency wheelhouse is missing. Re-download the complete Field_Crafter_1.16_Python.zip release asset."
}
$Wheels = @(Get-ChildItem $Wheelhouse -File -Filter "*.whl")
if ($Wheels.Count -lt 1) {
    throw "The Field Crafter offline dependency wheelhouse is empty. Re-download the complete Field_Crafter_1.16_Python.zip release asset."
}

function Find-Python313 {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $Exe = & py -3.13 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $Exe) {
            return $Exe.Trim()
        }
    }

    if (Get-Command python -ErrorAction SilentlyContinue) {
        $Exe = & python -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1); print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $Exe) {
            return $Exe.Trim()
        }
    }

    throw "Field Crafter 1.16 Python requires 64-bit Python 3.13. Install Python 3.13 for Windows, then run this setup again."
}

$SystemPython = Find-Python313

$NeedsRecreate = -not (Test-Path $Python)
if (-not $NeedsRecreate) {
    & $Python -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)" *> $null
    if ($LASTEXITCODE -ne 0) {
        $NeedsRecreate = $true
    }
}

if ($NeedsRecreate) {
    if (Test-Path $Venv) {
        Remove-Item $Venv -Recurse -Force
    }
    & $SystemPython -m venv $Venv
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create the Field Crafter Python 3.13 private environment."
    }
}

$OfflineArgs = @(
    "-m", "pip", "install",
    "--no-index",
    "--find-links", $Wheelhouse
)

& $Python @OfflineArgs -r (Join-Path $PSScriptRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Could not install Field Crafter runtime dependencies from the bundled offline wheelhouse."
}

& $Python @OfflineArgs -r (Join-Path $PSScriptRoot "requirements-ocr.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Could not install Field Crafter OCR dependencies from the bundled offline wheelhouse."
}

& $Python -c "import rapidocr, onnxruntime, PIL, tkinterdnd2, requests, bs4; print('Field Crafter Python runtime ready')"
if ($LASTEXITCODE -ne 0) {
    throw "Field Crafter's private Python environment did not pass its runtime import check."
}
