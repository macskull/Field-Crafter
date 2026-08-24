param(
    [switch]$SkipRefresh,
    [switch]$DataOnly
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$Version = "1.15"
$ReleaseVenv = Join-Path $PSScriptRoot ".release_venv"
$ReleasePython = Join-Path $ReleaseVenv "Scripts\python.exe"
$Dist = Join-Path $PSScriptRoot "dist"
$StageRoot = Join-Path $PSScriptRoot "release_stage"
$PythonStage = Join-Path $StageRoot "Field_Crafter_$Version`_Python"

function Invoke-Checked {
    param([scriptblock]$Command, [string]$Label)
    Write-Host ""
    Write-Host "== $Label ==" -ForegroundColor Cyan
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE."
    }
}

function Ensure-ReleasePython {
    if (-not (Test-Path $ReleasePython)) {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            & py -3 -m venv $ReleaseVenv
        } elseif (Get-Command python -ErrorAction SilentlyContinue) {
            & python -m venv $ReleaseVenv
        } else {
            throw "Python 3 was not found. Install Python 3 for Windows, then run build_release.ps1 again."
        }
        if ($LASTEXITCODE -ne 0) { throw "Could not create the release virtual environment." }
    }

    Invoke-Checked { & $ReleasePython -m pip install --upgrade pip } "Update release pip"
    Invoke-Checked { & $ReleasePython -m pip install -r (Join-Path $PSScriptRoot "requirements.txt") } "Install database/runtime dependencies"
    Invoke-Checked { & $ReleasePython -m pip install -r (Join-Path $PSScriptRoot "requirements-ocr.txt") } "Install OCR/runtime dependencies"
    if (-not $DataOnly) {
        Invoke-Checked { & $ReleasePython -m pip install -r (Join-Path $PSScriptRoot "requirements-build.txt") } "Install packaging dependencies"
    }
}

Ensure-ReleasePython
$env:PYTHONPATH = Join-Path $PSScriptRoot "src"

Invoke-Checked { & $ReleasePython -m compileall -q (Join-Path $PSScriptRoot "src") (Join-Path $PSScriptRoot "field_crafter_entry.py") } "Compile Python sources"
Invoke-Checked { & $ReleasePython (Join-Path $PSScriptRoot "release_self_test.py") } "Run core smoke tests"

if (-not $SkipRefresh) {
    Invoke-Checked { & $ReleasePython (Join-Path $PSScriptRoot "prepare_release.py") } "Refresh and prepare release data"
} else {
    Write-Host ""
    Write-Host "Skipping live Wiki refresh by explicit request; strict validation is still mandatory." -ForegroundColor Yellow
}

Invoke-Checked { & $ReleasePython (Join-Path $PSScriptRoot "validate_release_data.py") } "Strict redistribution validation"

if ($DataOnly) {
    Write-Host ""
    Write-Host "Field Crafter $Version release data is validated and ready for packaging." -ForegroundColor Green
    exit 0
}

# A public build must start from a clean packaging area.
Remove-Item -Recurse -Force $Dist -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force $StageRoot -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force (Join-Path $PSScriptRoot "build") -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $Dist | Out-Null
New-Item -ItemType Directory -Force $PythonStage | Out-Null

Invoke-Checked {
    & $ReleasePython -m PyInstaller --noconfirm --clean (Join-Path $PSScriptRoot "field_crafter_1_15.spec")
} "Build portable one-file EXE"

$BuiltExe = Join-Path $Dist "Field_Crafter_$Version.exe"
if (-not (Test-Path $BuiltExe)) {
    throw "PyInstaller completed but the expected EXE was not found: $BuiltExe"
}
if ((Get-Item $BuiltExe).Length -lt 10MB) {
    throw "The generated EXE is unexpectedly small; refusing to package it."
}

# Stage the Python release. Do not include maintainer caches, virtual environments,
# build output, or local logs/state.
$RuntimeItems = @(
    "Field Crafter.pyw",
    "field_crafter_entry.py",
    "launch_gui.ps1",
    "setup_ocr.ps1",
    "requirements.txt",
    "requirements-ocr.txt",
    "README.txt",
    "RELEASE_NOTES_1.15.txt",
    "validate_release_data.py",
    "release_self_test.py",
    "src",
    "assets",
    "data"
)
foreach ($Item in $RuntimeItems) {
    $Source = Join-Path $PSScriptRoot $Item
    if (-not (Test-Path $Source)) { throw "Required Python-release item is missing: $Item" }
    Copy-Item -Recurse -Force $Source $PythonStage
}

# Remove development notes/caches from the public data folder while retaining the
# validated database, memory map, validation reports, and release metadata.
$PublicDataKeep = @(
    "homecoming_recipes.sqlite",
    "memory_recipe_aliases.json",
    "validation_report.txt",
    "validation_report.json",
    "release_data_summary.json",
    "release_database_info.json",
    "README.txt"
)
Get-ChildItem (Join-Path $PythonStage "data") -File | Where-Object {
    $PublicDataKeep -notcontains $_.Name
} | Remove-Item -Force
Get-ChildItem $PythonStage -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# Validate the exact Python staging folder before zipping it.
$OldPyPath = $env:PYTHONPATH
$env:PYTHONPATH = Join-Path $PythonStage "src"
Invoke-Checked { & $ReleasePython (Join-Path $PythonStage "validate_release_data.py") } "Validate staged Python release"
Invoke-Checked { & $ReleasePython (Join-Path $PythonStage "release_self_test.py") } "Smoke-test staged Python release"
$env:PYTHONPATH = $OldPyPath

# Keep only runtime icon assets in the public Python ZIP.
Remove-Item -Recurse -Force (Join-Path $PythonStage "assets\icon_layers") -ErrorAction SilentlyContinue
Remove-Item -Force (Join-Path $PythonStage "assets\field_crafter_icon_master.png") -ErrorAction SilentlyContinue

$PythonZip = Join-Path $Dist "Field_Crafter_$Version`_Python.zip"
# Build the Python ZIP with Python's zipfile module.  This gives us explicit,
# platform-independent archive names and guarantees the required enclosing
# Field_Crafter_<version>_Python/ folder on Windows.  The helper verifies the
# archive structure immediately after writing it.
Invoke-Checked {
    & $ReleasePython (Join-Path $PSScriptRoot "make_release_zip.py") --source $PythonStage --output $PythonZip
} "Build Python release ZIP"
if (-not (Test-Path $PythonZip)) { throw "Python release ZIP was not created." }

# Hash both artifacts and emit individual checksum files plus a machine-readable manifest.
$ExeHash = Get-FileHash $BuiltExe -Algorithm SHA256
$ZipHash = Get-FileHash $PythonZip -Algorithm SHA256
$ExeName = Split-Path $ExeHash.Path -Leaf
$ZipName = Split-Path $ZipHash.Path -Leaf
"$($ExeHash.Hash)  $ExeName" | Set-Content -Encoding ascii "$BuiltExe.sha256"
"$($ZipHash.Hash)  $ZipName" | Set-Content -Encoding ascii "$PythonZip.sha256"
@(
    "$($ExeHash.Hash)  $ExeName",
    "$($ZipHash.Hash)  $ZipName"
) | Set-Content -Encoding ascii (Join-Path $Dist "SHA256SUMS.txt")

$Manifest = [ordered]@{
    field_crafter_version = $Version
    built_at_utc = [DateTime]::UtcNow.ToString("o")
    release_data = "strict validation passed"
    artifacts = @(
        [ordered]@{ file = $ExeName; sha256 = $ExeHash.Hash; bytes = (Get-Item $BuiltExe).Length },
        [ordered]@{ file = $ZipName; sha256 = $ZipHash.Hash; bytes = (Get-Item $PythonZip).Length }
    )
}
$Manifest | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 (Join-Path $Dist "RELEASE_MANIFEST.json")

Invoke-Checked {
    & (Join-Path $PSScriptRoot "verify_release_artifacts.ps1") -DistPath $Dist
} "Verify final release artifacts"

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "FIELD CRAFTER $Version RELEASE BUILD PASSED" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host "Portable EXE: $BuiltExe"
Write-Host "Python ZIP:   $PythonZip"
Write-Host "Checksums:    $(Join-Path $Dist 'SHA256SUMS.txt')"
Write-Host ""
Write-Host "These artifacts were created only after live refresh and strict database/memory-map validation." -ForegroundColor Green
