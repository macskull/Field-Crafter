# Field Crafter 1.16.1 - Public Test Hotfix

Field Crafter 1.16.1 fixes a direct game-memory invention salvage failure caused by unrelated `S_*` inventory entries and adds signed full-application updates from GitHub.

## Downloads

### Recommended: Portable EXE

`Field_Crafter_1.16.1.exe`

- Portable single-file Windows application
- No Python installation required
- Recommended for most users

### Python version

`Field_Crafter_1.16.1_Python.zip`

- Prepared Python/source distribution
- Requires 64-bit Python 3.13
- Creates its own private Python environment
- Includes an offline dependency wheelhouse for first-launch setup

> The automatically generated Source code (zip) and Source code (tar.gz) files provided by GitHub are not the prepared Field Crafter Python distribution. Use `Field_Crafter_1.16.1_Python.zip` from the release assets instead.

## What's fixed in 1.16.1

- Fixed invention salvage validation so non-invention `S_*` entries are ignored instead of inflating the character's invention-salvage total.
- Updated salvage reading to stop once canonical invention salvage exactly reproduces the authoritative header total, avoiding reads into unusable allocator/poison slots beyond the live inventory prefix.
- Updated salvage diagnostics to use the same canonical accounting rules as the production reader.

## Application updates

- Added signed full-application update checks from the Database tab.
- Added support for updating both the portable EXE and prepared Python distributions.
- Added signed-manifest verification and exact SHA-256/size verification before installation.
- Added staged replacement and rollback handling so a failed replacement does not strand the existing installation.

Field Crafter 1.16.1 is the bootstrap release for this application-update channel; future compatible releases can normally be installed from inside Field Crafter after 1.16.1 has been installed manually.

## Verify your download

This release includes `SHA256SUMS.txt`.

To verify the EXE in PowerShell:

```powershell
Get-FileHash ".\Field_Crafter_1.16.1.exe" -Algorithm SHA256
```

To verify the Python ZIP:

```powershell
Get-FileHash ".\Field_Crafter_1.16.1_Python.zip" -Algorithm SHA256
```

Compare the returned value with the corresponding entry in `SHA256SUMS.txt`.

## Reporting problems

If you encounter a problem, open an Issue in this repository and include the Field Crafter version, distribution type, input method, expected behavior, actual behavior, and any displayed error.

For game-memory problems, include the relevant diagnostic ZIP from:

`%LOCALAPPDATA%\FieldCrafter\diagnostics\`

when one is available.

## Release assets

- `Field_Crafter_1.16.1.exe`
- `Field_Crafter_1.16.1_Python.zip`
- `SHA256SUMS.txt`
- `RELEASE_MANIFEST.json`
