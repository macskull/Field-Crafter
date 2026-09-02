Field Crafter 1.16 - Python Distribution
========================================

Field Crafter is a crafting inventory and shopping-list utility for
City of Heroes: Homecoming.

Quick start
-----------
1. Extract the entire Field_Crafter_1.16_Python folder.
2. Keep the extracted folder together.
3. Launch:
       Field Crafter.pyw
   or:
       .\launch_gui.ps1

Requirements
------------
- Windows 10 or Windows 11
- 64-bit Python 3.13
- City of Heroes: Homecoming for direct game-memory inventory reading

First launch
------------
The prepared Python distribution includes an offline dependency wheelhouse.
On first launch, Field Crafter creates its private Python environment using
those bundled wheels.

The validated crafting database, complete game-memory recipe map, bundled
memory definitions, and memory-update configuration are included with the
release.

Inventory input
---------------
Field Crafter can:
- read recipes and invention salvage directly from a running Homecoming client;
- select among multiple running characters;
- use screenshot, drag-and-drop, or clipboard OCR as a fallback.

Review detected recipes and salvage before calculating the shopping list,
especially items highlighted for manual review.

Updates
-------
Crafting database updates:
- Use the Database tab to check Homecoming Wiki for crafting-data changes.
- Candidate crafting data is validated before it is accepted.

Game-memory definition updates:
- Use Check for memory updates to query Field Crafter's signed memory-definition
  channel.
- Downloaded definitions are checked for signature, hash, schema, compatibility,
  and live validity before activation.

Writable application data
--------------------------
Field Crafter stores user-writable state under:

    %LOCALAPPDATA%\FieldCrafter\

This includes settings, database update state, memory-definition updates, and
diagnostics.

Diagnostics
-----------
Game-memory troubleshooting diagnostics are stored under:

    %LOCALAPPDATA%\FieldCrafter\diagnostics\

Privacy
-------
Game-memory reading and screenshot OCR are processed locally. Internet access is
used when you explicitly check for crafting-data or memory-definition updates.

Release verification
--------------------
Use the SHA-256 values published with the GitHub v1.16 release to verify the
downloaded Python ZIP before extracting it.

Project page:
    https://github.com/macskull/Field-Crafter

Release page:
    https://github.com/macskull/Field-Crafter/releases/tag/v1.16
