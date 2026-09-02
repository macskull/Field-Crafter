# Field Crafter 1.16 - Public Test Release

Field Crafter 1.16 improves game-memory reliability, adds signed memory-definition updates, expands troubleshooting diagnostics, and improves the prepared Python distribution.

## Downloads

### Recommended: Portable EXE

`Field_Crafter_1.16.exe`

- Portable single-file Windows application
- No Python installation required
- Recommended for most users

### Python version

`Field_Crafter_1.16_Python.zip`

- Prepared Python/source distribution
- Requires 64-bit Python 3.13
- Creates its own private Python environment
- Includes an offline dependency wheelhouse for first-launch setup

> The automatically generated Source code (zip) and Source code (tar.gz) files provided by GitHub are not the prepared Field Crafter Python distribution. Use `Field_Crafter_1.16_Python.zip` from the release assets instead.

## What's new in 1.16

- Added automatic game-memory recovery that can attempt to locate compatible memory structures when the current profile stops working.
- Added signed memory-definition updates that can be installed independently of a full Field Crafter release.
- Expanded game-memory diagnostics for troubleshooting memory-reading and recovery problems.
- Added an offline dependency wheelhouse for the prepared 64-bit Python 3.13 distribution.
- Updated release packaging and verification for the new memory-definition files and release metadata.

## Typical usage

1. **Inventory Input**
   Read recipes and salvage directly from the running game, or use screenshots/OCR as a fallback.

2. **Review & Edit**
   Verify detected inventory, choose recipes to craft, and review entries highlighted for manual attention.

3. **Shopping List**
   View salvage to buy, items that can be removed to make room, other surplus salvage, estimated crafting cost, and inventory-space status.

## Verify your download

This release includes `SHA256SUMS.txt`.

To verify the EXE in PowerShell:

```powershell
Get-FileHash ".\Field_Crafter_1.16.exe" -Algorithm SHA256
```

To verify the Python ZIP:

```powershell
Get-FileHash ".\Field_Crafter_1.16_Python.zip" -Algorithm SHA256
```

Compare the returned value with the corresponding entry in `SHA256SUMS.txt`.

## Reporting problems

If you encounter a problem, open an Issue in this repository and include the Field Crafter version, distribution type, input method, expected behavior, actual behavior, and any displayed error.

For game-memory problems, include the relevant diagnostic JSON from:

`%LOCALAPPDATA%\FieldCrafter\diagnostics\`

when one is available.

## Release assets

- `Field_Crafter_1.16.exe`
- `Field_Crafter_1.16_Python.zip`
- `SHA256SUMS.txt`
- `RELEASE_MANIFEST.json`
