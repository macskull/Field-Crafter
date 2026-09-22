param(
    [Parameter(Mandatory = $true)]
    [string]$PrivateKey,

    [string]$Root = (Get-Location).Path,

    [string]$Summary = "Field Crafter 1.16.2 updates game-memory reading for current Live and Open Beta clients. Portable EXE users updating from 1.16.1: the automatic restart may show a missing python313.dll error. If that specific error appears, close the dialog and launch Field Crafter again manually; 1.16.2 should then start and finalize the update normally."
)

$ErrorActionPreference = "Stop"

$Root = (Resolve-Path $Root).Path
$PrivateKey = (Resolve-Path $PrivateKey).Path
Set-Location $Root

function Fail([string]$Message) {
    throw $Message
}

Write-Host ""
Write-Host "Field Crafter 1.16.2 application-update publication preflight" -ForegroundColor Cyan
Write-Host "Root:        $Root"
Write-Host "Private key: $PrivateKey"

# The private signing key must never be copied into a release/source tree.
$RootPrefix = $Root.TrimEnd('\\') + '\\'
if ($PrivateKey.StartsWith($RootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    Fail "Refusing to use a private signing key stored inside the Field Crafter project tree. Move the key to a separate private location first."
}

$VersionFile = Join-Path $Root "src\hc_recipe_db\version.py"
$Publisher = Join-Path $Root "maintainer\publish_application_update.py"
$ConfigPath = Join-Path $Root "data\application_update_config.json"
$LiveManifestPath = Join-Path $Root "updates\manifest.json"
$Dist = Join-Path $Root "dist"
$ReleaseManifestPath = Join-Path $Dist "RELEASE_MANIFEST.json"
$ReleasePython = Join-Path $Root ".release_venv\Scripts\python.exe"
$OutputDir = Join-Path $Root "application_update_publish"

foreach ($Required in @($VersionFile, $Publisher, $ConfigPath, $LiveManifestPath, $ReleaseManifestPath)) {
    if (-not (Test-Path $Required -PathType Leaf)) {
        Fail "Required file is missing: $Required"
    }
}

if (-not (Test-Path $ReleasePython -PathType Leaf)) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $Candidate = & py -3.13 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $Candidate) {
            $ReleasePython = $Candidate.Trim()
        }
    }
}
if (-not (Test-Path $ReleasePython -PathType Leaf)) {
    Fail "Python 3.13 was not found. Run build_release.ps1 first or install/use Python 3.13."
}

$VersionText = Get-Content $VersionFile -Raw
$VersionMatch = [regex]::Match($VersionText, '(?m)^RELEASE_VERSION\s*=\s*"([^"]+)"\s*$')
if (-not $VersionMatch.Success) {
    Fail "Could not read RELEASE_VERSION from $VersionFile"
}
$Version = $VersionMatch.Groups[1].Value
if ($Version -ne "1.16.2") {
    Fail "This preflight is for Field Crafter 1.16.2, but the source tree reports version $Version."
}

$ReleaseManifest = Get-Content $ReleaseManifestPath -Raw | ConvertFrom-Json
if ([string]$ReleaseManifest.field_crafter_version -ne $Version) {
    Fail "dist\RELEASE_MANIFEST.json reports version $($ReleaseManifest.field_crafter_version), expected $Version."
}

$ExpectedExe = Join-Path $Dist "Field_Crafter_1.16.2.exe"
$ExpectedPython = Join-Path $Dist "Field_Crafter_1.16.2_Python.zip"
foreach ($Artifact in @($ExpectedExe, $ExpectedPython, (Join-Path $Dist "SHA256SUMS.txt"))) {
    if (-not (Test-Path $Artifact -PathType Leaf)) {
        Fail "Validated release artifact is missing: $Artifact"
    }
}

# Accept either the original 1.16.1 predecessor manifest or the already-published
# 1.16.2 manifest. If 1.16.2 is already live, require it to reference the exact
# same release artifacts before allowing a summary/signature-only republication.
$CurrentLiveManifest = Get-Content $LiveManifestPath -Raw | ConvertFrom-Json
$CurrentLiveVersion = [string]$CurrentLiveManifest.version

if ($CurrentLiveVersion -notin @("1.16.1", "1.16.2")) {
    Fail "updates\manifest.json reports unexpected version $CurrentLiveVersion. Refusing to continue automatically."
}

if ($CurrentLiveVersion -eq "1.16.2") {
    $DistExeHash = (Get-FileHash $ExpectedExe -Algorithm SHA256).Hash.ToLowerInvariant()
    $DistPythonHash = (Get-FileHash $ExpectedPython -Algorithm SHA256).Hash.ToLowerInvariant()

    if ([string]$CurrentLiveManifest.artifacts.exe.sha256 -ne $DistExeHash) {
        Fail "Published 1.16.2 EXE hash does not match the validated dist artifact. Refusing to regenerate the manifest."
    }

    if ([string]$CurrentLiveManifest.artifacts.python.sha256 -ne $DistPythonHash) {
        Fail "Published 1.16.2 Python ZIP hash does not match the validated dist artifact. Refusing to regenerate the manifest."
    }
}

$Config = Get-Content $ConfigPath -Raw | ConvertFrom-Json
if ([string]$Config.channel -ne "public-test") {
    Fail "Unexpected application-update channel in bundled config: $($Config.channel)"
}
if ([string]$Config.manifest_url -ne "https://raw.githubusercontent.com/macskull/Field-Crafter/main/updates/manifest.json") {
    Fail "Unexpected application-update manifest URL in bundled config: $($Config.manifest_url)"
}

# Start from a clean staging directory only. This does NOT touch updates\manifest.json.
if (Test-Path $OutputDir) {
    Remove-Item $OutputDir -Recurse -Force
}

Write-Host ""
Write-Host "== Generate signed 1.16.2 application-update manifest ==" -ForegroundColor Cyan
& $ReleasePython $Publisher `
    --private-key $PrivateKey `
    --dist $Dist `
    --output-dir $OutputDir `
    --channel "public-test" `
    --minimum-updater-version "1.16.1" `
    --repository "macskull/Field-Crafter" `
    --summary $Summary
if ($LASTEXITCODE -ne 0) {
    Fail "Application-update manifest publisher failed with exit code $LASTEXITCODE."
}

$GeneratedManifestPath = Join-Path $OutputDir "manifest.json"
$GeneratedPublicKeyPath = Join-Path $OutputDir "application_update_public_key.txt"
foreach ($Generated in @($GeneratedManifestPath, $GeneratedPublicKeyPath)) {
    if (-not (Test-Path $Generated -PathType Leaf)) {
        Fail "Publisher did not create expected output: $Generated"
    }
}

Write-Host ""
Write-Host "== Verify trusted signing key ==" -ForegroundColor Cyan
$GeneratedPublicKey = (Get-Content $GeneratedPublicKeyPath -Raw).Trim()
$ConfiguredPublicKey = ([string]$Config.public_key_ed25519).Trim()
if ($GeneratedPublicKey -ne $ConfiguredPublicKey) {
    Fail "FAIL: private signing key does not match the public key trusted by Field Crafter 1.16.1. DO NOT PUBLISH."
}
Write-Host "PASS: application-update signing key matches the public key already trusted by 1.16.1." -ForegroundColor Green

$GeneratedManifest = Get-Content $GeneratedManifestPath -Raw | ConvertFrom-Json
if ([string]$GeneratedManifest.version -ne "1.16.2") {
    Fail "Generated manifest version is $($GeneratedManifest.version), expected 1.16.2."
}
if ([string]$GeneratedManifest.minimum_updater_version -ne "1.16.1") {
    Fail "Generated manifest minimum_updater_version is $($GeneratedManifest.minimum_updater_version), expected 1.16.1."
}
if ([string]$GeneratedManifest.channel -ne "public-test") {
    Fail "Generated manifest channel is $($GeneratedManifest.channel), expected public-test."
}

$ExpectedExeUrl = "https://github.com/macskull/Field-Crafter/releases/download/v1.16.2/Field_Crafter_1.16.2.exe"
$ExpectedPythonUrl = "https://github.com/macskull/Field-Crafter/releases/download/v1.16.2/Field_Crafter_1.16.2_Python.zip"
if ([string]$GeneratedManifest.artifacts.exe.url -ne $ExpectedExeUrl) {
    Fail "Unexpected EXE URL in generated manifest: $($GeneratedManifest.artifacts.exe.url)"
}
if ([string]$GeneratedManifest.artifacts.python.url -ne $ExpectedPythonUrl) {
    Fail "Unexpected Python URL in generated manifest: $($GeneratedManifest.artifacts.python.url)"
}

Write-Host ""
Write-Host "== Run updater regression tests ==" -ForegroundColor Cyan
$OldPyPath = $env:PYTHONPATH
$env:PYTHONPATH = Join-Path $Root "src"
& $ReleasePython (Join-Path $Root "tools\test_application_updates_v1.py")
$TestExit = $LASTEXITCODE
$env:PYTHONPATH = $OldPyPath
if ($TestExit -ne 0) {
    Fail "Application-updater regression tests failed with exit code $TestExit."
}

$ManifestHash = (Get-FileHash $GeneratedManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "1.16.2 APPLICATION-UPDATE MANIFEST STAGED AND VERIFIED" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host "Staged manifest: $GeneratedManifestPath"
Write-Host "Manifest SHA256: $ManifestHash"
Write-Host ""
Write-Host "IMPORTANT: updates\manifest.json was NOT changed." -ForegroundColor Yellow
Write-Host ""
Write-Host "Next publication order:" -ForegroundColor Cyan
Write-Host "  1. Create/publish GitHub release tag v1.16.2."
Write-Host "  2. Upload these exact dist assets:"
Write-Host "       Field_Crafter_1.16.2.exe"
Write-Host "       Field_Crafter_1.16.2_Python.zip"
Write-Host "       SHA256SUMS.txt"
Write-Host "       RELEASE_MANIFEST.json"
Write-Host "  3. Confirm both release download URLs are publicly reachable."
Write-Host "  4. Only then copy the staged manifest into updates\manifest.json and commit/push it."
Write-Host "  5. Test Check for app updates from a stock 1.16.1 installation."
Write-Host ""
Write-Host "Do not publish a schema-v2 memory manifest until the 1.16.2 application update is live." -ForegroundColor Yellow
