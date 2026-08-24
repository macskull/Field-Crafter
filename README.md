# Field Crafter

**Field Crafter** is a crafting inventory and shopping-list utility for **City of Heroes: Homecoming**.

It can read recipes and invention salvage directly from a running City of Heroes client on Windows, or use screenshots and local OCR as a fallback. After reviewing the detected inventory, Field Crafter calculates the salvage needed for your selected recipes, identifies surplus salvage, and helps determine whether enough inventory space is available before crafting.

## Download

For normal use, download Field Crafter from the repository's **Releases** page.

Two versions are available:

- **Field_Crafter_1.15.exe** - Recommended for most Windows users. Portable and does not require Python.
- **Field_Crafter_1.15_Python.zip** - Python version for users who prefer to run the application from source.

The automatically generated **Source code** ZIP/TAR files shown by GitHub are not the prepared Field Crafter Python distribution. Use `Field_Crafter_1.15_Python.zip` from the release assets instead.

## Features

- Read recipe and invention-salvage inventory directly from a running City of Heroes client.
- Select from multiple running City of Heroes characters.
- Screenshot and clipboard OCR fallback when memory reading is unavailable or undesired.
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
- User-initiated database update and game-memory mapping tools.
- Offline-capable normal operation with a bundled validated crafting database.

## Requirements

### EXE version

- Windows 10 or Windows 11.
- City of Heroes: Homecoming for direct game-memory inventory reading.

The portable EXE does not require a Python installation.

### Python version

- Windows 10 or Windows 11.
- Python 3.
- Internet access may be required the first time the Python version installs its Python dependencies.

The crafting database itself is bundled with Field Crafter and does not need to be downloaded on first launch.

## Getting Started

### Portable EXE

Download:

`Field_Crafter_1.15.exe`

Place it anywhere you like and run it.

No installation is required.

Windows SmartScreen may warn about an unknown publisher because the application is not currently code-signed. You can verify the downloaded file against the published SHA-256 hashes as described below.

### Python version

Download and extract:

`Field_Crafter_1.15_Python.zip`

Keep the extracted folder together.

Launch Field Crafter by double-clicking:

`Field Crafter.pyw`

or by running:

```powershell
.\launch_gui.ps1
```

On first launch, the Python distribution creates a private Python environment and installs its required packages.

## Using Field Crafter

Field Crafter uses a three-step workflow.

### 1. Inventory Input

For direct game-memory reading:

1. Start City of Heroes and log into the character whose inventory you want to read.
2. Open Field Crafter.
3. Select the appropriate character and server.
4. Click **Read inventory**.

Field Crafter reads the character's recipe and invention-salvage inventory and sends the results to **Review & Edit**.

### 2. Review & Edit

Review the recipes and salvage detected by Field Crafter.

Recipes selected under **Craft?** are included in the shopping-list calculation.

Entries requiring manual review are highlighted in red. Selecting a recipe or salvage row displays additional details about how that item was detected.

When the inventory looks correct, confirm the review and continue to the shopping list.

### 3. Shopping List

Field Crafter calculates the selected crafting requirements and separates the results into:

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

Detected OCR confidence and related audit information are shown in the selected item's Details area when applicable.

Always review OCR-derived inventory before calculating a shopping list.

## Database Updates

Field Crafter ships with a validated Homecoming crafting database so a database download is not required during normal first launch.

The **Database** tab provides user-initiated maintenance tools for:

- Checking Homecoming Wiki for crafting-data changes.
- Reviewing and accepting a validated candidate database.
- Refreshing game-memory recipe mappings.

Field Crafter does not silently replace its crafting database during normal startup.

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

Removing the application EXE or extracted Python folder does not automatically remove this user data.

## Privacy

Field Crafter is designed to perform its normal inventory processing locally.

- Game-memory inventory reading occurs locally.
- Screenshot OCR occurs locally.
- Screenshots are not uploaded as part of the OCR workflow.
- Internet access is used when you explicitly check for Homecoming crafting-data updates and, for the Python distribution, when required Python packages need to be installed.

## Verify Your Download

Each public release includes SHA-256 hashes in:

`SHA256SUMS.txt`

To verify the portable EXE in PowerShell:

```powershell
Get-FileHash ".\Field_Crafter_1.15.exe" -Algorithm SHA256
```

To verify the Python ZIP:

```powershell
Get-FileHash ".\Field_Crafter_1.15_Python.zip" -Algorithm SHA256
```

Compare the returned hash with the corresponding value in `SHA256SUMS.txt`.

The values must match exactly. Letter case does not matter.

A matching hash confirms that your downloaded file is byte-for-byte identical to the published release artifact.

## Reporting Problems

Field Crafter 1.15 is currently being made available for public testing and feedback.

If you encounter a problem, please open an **Issue** in this GitHub repository and include as much relevant information as possible, such as:

- Field Crafter version.
- EXE or Python version.
- Whether the inventory came from game memory or OCR.
- What you expected to happen.
- What actually happened.
- Any displayed error message.
- A screenshot when useful.

For recipe-mapping problems, including the internal recipe/mapping information displayed in the selected recipe's Details section can be especially helpful.

## Version

**Field Crafter 1.15 - Public Test Release**

Field Crafter is an independent community utility for City of Heroes: Homecoming.
