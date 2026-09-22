# Field Crafter 1.16.2

Field Crafter 1.16.2 updates game-memory reading for the September 2026 Homecoming client changes. It replaces the obsolete identity-gated player path with the direct local-player resolver already validated in Auto-Leveler, supports both current Live and Open Beta inventory layouts, and keeps the signed GitHub updater introduced in 1.16.1.

## Downloads

### Recommended: Portable EXE

`Field_Crafter_1.16.2.exe`

- Portable single-file Windows application
- No Python installation required
- Recommended for most users

### Python version

`Field_Crafter_1.16.2_Python.zip`

- Prepared Python/source distribution
- Requires 64-bit Python 3.13
- Creates its own private Python environment
- Includes an offline dependency wheelhouse for first-launch setup

> The automatically generated Source code archives from GitHub are not the prepared Field Crafter Python distribution. Use `Field_Crafter_1.16.2_Python.zip` from the release assets instead.

## What's fixed in 1.16.2

- Added the direct local-player + live Entity-table resolver validated on current Live and Open Beta clients.
- Added automatic semantic selection between the old inventory layout and the current Beta `+0x10` recipe/salvage layout.
- Fixed inventory validation so capacity is treated as a dynamic slot count rather than an exact constant.
- Fixed stacked inventory validation so summed item quantity may exceed slot capacity.
- Preserved canonical invention-salvage classification so unrelated `S_*` items do not contribute to Field Crafter's salvage total.
- Updated diagnostics to report the authoritative resolver and layout-selection evidence.
- Redacted Windows user-profile path components from user-facing status/error text and shareable diagnostic ZIPs.

## Updating from 1.16.1

Field Crafter 1.16.1 already trusts the signed application-update channel used for this release. Existing 1.16.1 users can use **Check for app updates** on the Database tab to download and install 1.16.2 in place after the signed manifest and release assets are published.

Field Crafter 1.16.2 also expands the signed memory-definition format so future compatible signature/offset changes can normally be distributed without another full application replacement.

## Verify your download

This release includes `SHA256SUMS.txt`.

```powershell
Get-FileHash ".\Field_Crafter_1.16.2.exe" -Algorithm SHA256
Get-FileHash ".\Field_Crafter_1.16.2_Python.zip" -Algorithm SHA256
```

Compare the returned values with `SHA256SUMS.txt`.

## Reporting problems

For game-memory problems, include the diagnostic ZIP from:

`%LOCALAPPDATA%\FieldCrafter\diagnostics\`

## Release assets

- `Field_Crafter_1.16.2.exe`
- `Field_Crafter_1.16.2_Python.zip`
- `SHA256SUMS.txt`
- `RELEASE_MANIFEST.json`
