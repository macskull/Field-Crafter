param(
    [string]$DistPath = (Join-Path $PSScriptRoot "dist")
)

$ErrorActionPreference = "Stop"
$Version = "1.15"
$DistPath = (Resolve-Path $DistPath).Path
$SumsPath = Join-Path $DistPath "SHA256SUMS.txt"
$ManifestPath = Join-Path $DistPath "RELEASE_MANIFEST.json"
$ExpectedExe = "Field_Crafter_$Version.exe"
$ExpectedZip = "Field_Crafter_$Version`_Python.zip"

function Fail([string]$Message) {
    throw "Release artifact verification failed: $Message"
}

if (-not (Test-Path $SumsPath)) { Fail "SHA256SUMS.txt is missing." }
if (-not (Test-Path $ManifestPath)) { Fail "RELEASE_MANIFEST.json is missing." }

$ExpectedFiles = @($ExpectedExe, $ExpectedZip)
$ParsedSums = @{}
foreach ($Line in Get-Content $SumsPath) {
    $Trimmed = $Line.Trim()
    if (-not $Trimmed) { continue }
    if ($Trimmed -notmatch '^([0-9A-Fa-f]{64})\s+(.+)$') {
        Fail "Malformed checksum line: $Line"
    }
    $ParsedSums[$Matches[2]] = $Matches[1].ToUpperInvariant()
}

foreach ($Name in $ExpectedFiles) {
    $Path = Join-Path $DistPath $Name
    if (-not (Test-Path $Path)) { Fail "$Name is missing." }
    if (-not $ParsedSums.ContainsKey($Name)) { Fail "No checksum entry exists for $Name." }
    $Actual = (Get-FileHash $Path -Algorithm SHA256).Hash.ToUpperInvariant()
    if ($Actual -ne $ParsedSums[$Name]) { Fail "SHA-256 mismatch for $Name." }
}

$ExePath = Join-Path $DistPath $ExpectedExe
if ((Get-Item $ExePath).Length -lt 10MB) { Fail "The EXE is unexpectedly small." }

$Manifest = Get-Content $ManifestPath -Raw | ConvertFrom-Json
if ([string]$Manifest.field_crafter_version -ne $Version) {
    Fail "Manifest version is '$($Manifest.field_crafter_version)', expected '$Version'."
}
if ([string]$Manifest.release_data -ne "strict validation passed") {
    Fail "Manifest does not record strict release-data validation."
}
foreach ($Name in $ExpectedFiles) {
    $Entry = @($Manifest.artifacts | Where-Object { $_.file -eq $Name })
    if ($Entry.Count -ne 1) { Fail "Manifest must contain exactly one entry for $Name." }
    $Path = Join-Path $DistPath $Name
    if ([int64]$Entry[0].bytes -ne (Get-Item $Path).Length) { Fail "Manifest byte count mismatch for $Name." }
    if ([string]$Entry[0].sha256 -ne $ParsedSums[$Name]) { Fail "Manifest SHA-256 mismatch for $Name." }
}

# Inspect the ZIP structure without extracting it. A release ZIP must contain its
# own top-level directory and the minimum runtime/data files expected by first launch.
Add-Type -AssemblyName System.IO.Compression.FileSystem
$ZipPath = Join-Path $DistPath $ExpectedZip
$Archive = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
try {
    $Names = @($Archive.Entries | ForEach-Object { $_.FullName.Replace('\\','/') })
    $Prefix = "Field_Crafter_$Version`_Python/"
    $RequiredEntries = @(
        "${Prefix}Field Crafter.pyw",
        "${Prefix}field_crafter_entry.py",
        "${Prefix}README.txt",
        "${Prefix}data/homecoming_recipes.sqlite",
        "${Prefix}data/memory_recipe_aliases.json",
        "${Prefix}data/release_data_summary.json",
        "${Prefix}data/release_database_info.json"
    )
    foreach ($Required in $RequiredEntries) {
        if ($Names -notcontains $Required) {
            $Preview = ($Names | Select-Object -First 30) -join "; "
            Fail "Python ZIP is missing $Required. First archive entries: $Preview"
        }
    }

    # The public Python release must not also contain root-level runtime files.
    # One enclosing folder is intentional so extraction stays tidy.
    foreach ($Rootless in @("Field Crafter.pyw", "field_crafter_entry.py", "README.txt")) {
        if ($Names -contains $Rootless) {
            Fail "Python ZIP contains root-level runtime file '$Rootless'; the enclosing $Prefix folder was not preserved."
        }
    }
} finally {
    $Archive.Dispose()
}

Write-Host "PASS: Field Crafter $Version EXE and Python ZIP match their checksums and release manifest." -ForegroundColor Green
