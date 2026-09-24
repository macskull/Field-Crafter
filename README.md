# Field Crafter

**Field Crafter** is a crafting inventory and shopping-list utility for **City of Heroes: Homecoming**.

It can read recipes and invention salvage directly from a running City of Heroes client on Windows, with screenshot/OCR input available as a fallback. After reviewing the detected inventory, Field Crafter calculates the salvage needed for selected recipes, identifies surplus salvage, and helps determine whether enough inventory space is available before crafting.

## Download

### Windows EXE

**[Download Field Crafter 1.16.2 for Windows](https://github.com/macskull/Field-Crafter/releases/download/v1.16.2/Field_Crafter_1.16.2.exe)**

Field Crafter is distributed as a portable single-file Windows application. No installation or Python environment is required.

### Release information

- **[View the Field Crafter 1.16.2 release](https://github.com/macskull/Field-Crafter/releases/tag/v1.16.2)**
- **[Download SHA256SUMS.txt](https://github.com/macskull/Field-Crafter/releases/download/v1.16.2/SHA256SUMS.txt)**
- **[Download RELEASE_MANIFEST.json](https://github.com/macskull/Field-Crafter/releases/download/v1.16.2/RELEASE_MANIFEST.json)**

## About this repository

The current branch of this repository is the public distribution and update channel for Field Crafter.

It contains:

- Public release and update metadata.
- Signed application-update metadata.
- Signed game-memory definition updates.
- Public documentation and screenshots.

Application source code and development/build tooling are not distributed from the current branch.

Previously published releases, tags, and repository history remain available as originally published.

## Features

- Read recipe and invention-salvage inventory directly from a running City of Heroes client.
- Select from multiple running City of Heroes characters.
- Screenshot and clipboard OCR fallback when direct memory reading is unavailable or undesired.
- Review and edit detected recipes and salvage before calculating.
- Highlight entries that require manual review.
- Select all recipes, common recipes, or individual recipes for crafting.
- Calculate:
  - Salvage that needs to be purchased.
  - Salvage that can be sold or deleted to make room.
  - Other surplus salvage.
  - Total crafting cost.
  - Whether sufficient inventory space is available.
- Local Homecoming recipe and salvage database.
- User-initiated crafting-database updates.
- Signed game-memory definition updates.
- Signed full-application updates.
- Conservative recovery for compatible game-memory layout changes.
- Offline-capable normal operation with bundled validated crafting and memory data.

## Screenshots

### Inventory Input

Read recipes and invention salvage directly from a running City of Heroes client, with screenshot/OCR input available as a fallback.

![Field Crafter Inventory Input](./docs/screenshots/inventory-input.png)

### Review & Edit

Review detected recipes and salvage, choose which recipes to craft, and inspect additional details when needed.

![Field Crafter Review and Edit](./docs/screenshots/review-edit.png)

### Shopping List

See the salvage to buy, recommended inventory disposals, surplus salvage, crafting cost, and full calculated result.

![Field Crafter Shopping List](./docs/screenshots/shopping-list.png)

## Requirements

- Windows 10 or Windows 11.
- City of Heroes: Homecoming for direct game-memory inventory reading.

The portable EXE does not require a Python installation.

## Getting Started

1. Download `Field_Crafter_1.16.2.exe`.
2. Place it anywhere you like.
3. Run the application.
4. Start City of Heroes and log into the character whose inventory you want to read.
5. Select the appropriate character and server in Field Crafter.
6. Click **Read inventory**.

No installation is required.

Windows SmartScreen may warn about an unknown publisher because the application is not currently code-signed. You can verify the downloaded file against the published SHA-256 hash.

## Using Field Crafter

Field Crafter uses a three-step workflow.

### 1. Inventory Input

Field Crafter can read recipe and invention-salvage inventory directly from a running City of Heroes client.

Screenshot, clipboard, and OCR input are also available as fallback methods.

### 2. Review & Edit

Review the detected recipes and salvage.

Recipes selected under **Craft?** are included in the shopping-list calculation.

Entries requiring manual review are highlighted. Selecting an entry displays additional details about how it was detected.

### 3. Shopping List

Field Crafter separates the calculated result into:

- **BUY** - Salvage still required.
- **SELL / DELETE TO MAKE ROOM** - Surplus salvage that may be removed according to the selected disposal policy.
- **OTHER SURPLUS** - Additional salvage beyond the selected recipes' requirements.

The result also shows estimated crafting cost and whether enough inventory space is available.

## Screenshot / OCR Fallback

If direct game-memory reading is unavailable, recipe and salvage screenshots can be supplied using:

- File selection.
- Drag and drop.
- Clipboard paste.

OCR processing is performed locally on your computer. Screenshots are not uploaded by the OCR workflow.

Always review OCR-derived inventory before calculating a shopping list.

## Crafting Database Updates

Field Crafter ships with a validated Homecoming crafting database, so a database download is not required during normal first launch.

The **Database** tab includes maintenance tools for checking Homecoming Wiki for crafting-data changes and reviewing validated database updates.

Crafting-database maintenance is separate from application updates and game-memory definition updates.

## Game-Memory Definition Updates

Field Crafter supports signed game-memory definition updates so compatible memory-layout changes can be distributed independently of a full application release.

Use **Check for memory updates** to check the configured update channel.

Downloaded definitions are verified before becoming active. Validation includes signed metadata, SHA-256 integrity checks, schema checks, version compatibility, and live validation where required.

## Application Updates

Field Crafter includes a signed application-update channel.

Use **Check for app updates** on the Database tab to check for a newer compatible release.

Downloaded application updates are verified against signed metadata, expected size, and SHA-256 before replacement is attempted.

Application updates are separate from crafting-database updates and game-memory definition updates.

## User Data

Field Crafter stores writable application data under:

```text
%LOCALAPPDATA%\FieldCrafter\
```

This may include:

- Active recipe database.
- Window settings.
- Database update cache and backups.
- User-level game-memory recipe mappings.
- Downloaded memory-definition updates.
- Memory-read diagnostics.

Removing the application EXE does not automatically remove this user data.

## Privacy

Field Crafter performs normal inventory processing locally.

- Game-memory inventory reading occurs locally.
- Screenshot OCR occurs locally.
- Screenshots are not uploaded as part of the OCR workflow.
- Windows user-profile path components are redacted from user-facing error/status text and shareable memory diagnostics.
- Internet access is used for update checks and other explicitly requested online maintenance operations.

## Verify Your Download

Each public release includes SHA-256 hashes in:

**[SHA256SUMS.txt](https://github.com/macskull/Field-Crafter/releases/download/v1.16.2/SHA256SUMS.txt)**

To verify the current portable EXE in PowerShell:

```powershell
Get-FileHash ".\Field_Crafter_1.16.2.exe" -Algorithm SHA256
```

Compare the returned hash with the value in `SHA256SUMS.txt`.

The values must match exactly. Letter case does not matter.

## Reporting Problems

If you encounter a problem, please **[open an Issue](https://github.com/macskull/Field-Crafter/issues)** and include relevant information such as:

- Field Crafter version.
- Whether the inventory came from game memory or OCR.
- What you expected to happen.
- What actually happened.
- Any displayed error message.
- A screenshot when useful.

For game-memory problems, include the relevant diagnostic from:

```text
%LOCALAPPDATA%\FieldCrafter\diagnostics\
```

when one is available.

## Current Release

**Field Crafter 1.16.2**
