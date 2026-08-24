FIELD CRAFTER 1.15
==================

Field Crafter is a City of Heroes / Homecoming crafting helper. It can read recipe
and invention-salvage inventory directly from a running City of Heroes client on
Windows, or use local screenshot OCR as a fallback. After review, it calculates
what salvage to buy, what surplus is safe to dispose of under the selected policy,
and whether enough inventory room is available before crafting.

WHAT IS NEW IN 1.15
-------------------

Public-release hardening:
- Added prepare_release.py as the canonical release-data preparation command.
- Added strict release validation that blocks packaging unless the full recipe
  database and complete game-memory recipe map pass validation.
- Added release_database_info.json plus SHA-256 binding between release metadata,
  the factory SQLite database, and the bundled memory-recipe map.
- Added a one-command Windows build pipeline that produces both the portable EXE
  and the Python ZIP only after release validation succeeds.
- Added individual checksum files, SHA256SUMS.txt, and RELEASE_MANIFEST.json.
- Added verify_release_artifacts.ps1 for a final EXE/ZIP/hash/manifest/ZIP-structure check.
- Added a core runtime smoke test that runs before public packaging.
- First launch remains crafting-data offline-capable. It never silently refreshes
  Homecoming Wiki data.

UI cleanup:
- Review & Edit now places Recipes and Salvage Inventory side-by-side, making better
  use of horizontal space and allowing a shorter default application window.
- The old 1220x820 and early-1.15 1220x700 factory-default saved geometries are
  migrated to the compact 1220x600 default while deliberately resized windows are preserved.
- Inventory Input now presents live game-memory reading as the primary workflow.
  The screenshot/OCR fallback remains visible by default, with a Hide/Show control
  available when the extra workspace is not needed.
- Inventory Input shows compact recipe/salvage read totals and database status.
- The footer progress bar is hidden while idle. It appears only during real work;
  OCR uses determinate screenshot progress while database/memory operations use an
  indeterminate activity indicator.
- Database update output no longer consumes most of the Database tab while empty;
  it opens automatically when an update/map operation begins.
- Review summary and source labels were tightened for easier scanning.

NORMAL USE - PYTHON VERSION
---------------------------

1. Extract the complete Field_Crafter_1.15_Python.zip folder.
2. Double-click:

       Field Crafter.pyw

   or run:

       .\launch_gui.ps1

3. On first launch, Field Crafter creates a private .venv and installs its Python
   dependencies. Dependency installation can require internet access. The bundled
   crafting database and game-memory recipe map do NOT require a Wiki connection.
4. Choose a running City of Heroes character and click Read inventory.
5. Review recipes/salvage in Review & Edit.
6. Confirm the detected inventory and click Calculate shopping list.

SCREENSHOT / OCR FALLBACK
-------------------------

On Inventory Input, the screenshot/OCR fallback is visible by default and can be
hidden when not needed. Recipe and salvage screenshots can be added by file picker,
drag/drop, or clipboard paste. OCR runs
locally. Nothing is uploaded by the OCR workflow.

DATA UPDATES INSIDE FIELD CRAFTER
---------------------------------

The Database tab can scan Homecoming Wiki for a candidate recipe database, validate
it, and let the user explicitly accept or reject the candidate. It can also refresh
the user-level game-memory recipe map. These are user-initiated maintenance tools;
normal first launch never performs a crafting-data refresh.

MAINTAINER: BUILDING A PUBLIC RELEASE
-------------------------------------

Use the Build Source package, not an already-generated public ZIP.

Required on the Windows build PC:
- Windows 10/11
- Python 3
- Internet access to Homecoming Wiki
- PowerShell

For a normal public build, run from the extracted Build Source folder:

       .\build_release.ps1

The script will:
1. Create/use a private .release_venv.
2. Install runtime, OCR, and PyInstaller build dependencies.
3. Compile the Python sources and run core smoke tests.
4. Live-refresh the complete recipe/salvage database from Homecoming Wiki.
5. Rebuild the complete game-memory recipe map.
6. Validate SQLite integrity and all database validation checks.
7. Validate every canonical set family/member plus all common invention mappings.
8. Stamp release metadata only after those checks pass.
9. Re-validate file hashes and redistribution metadata.
10. Build the portable one-file Windows EXE.
11. Stage and independently validate the public Python package.
12. Create the Python ZIP and SHA-256 manifests.
13. Re-open and verify the final artifacts, checksums, manifest, and Python ZIP structure.

If ANY strict release-data check fails, packaging stops. Do not manually bypass this
for a public release.

OUTPUTS
-------

After a successful build, dist\ contains at least:

       Field_Crafter_1.15.exe
       Field_Crafter_1.15.exe.sha256
       Field_Crafter_1.15_Python.zip
       Field_Crafter_1.15_Python.zip.sha256
       SHA256SUMS.txt
       RELEASE_MANIFEST.json

The EXE is a single-file portable application and does not require Python or a
sibling _internal folder. The Python ZIP contains one enclosing
Field_Crafter_1.15_Python folder with the complete runtime source and validated
factory data.

For the short maintainer workflow, see LOCAL_RELEASE_STEPS.txt.

DATA-ONLY RELEASE PREFLIGHT
---------------------------

To refresh and fully validate the release data without building packages:

       .\prepare_redistribution.ps1

This runs the same live refresh and strict release-data gates used by the full build.

REPACKAGING WITHOUT ANOTHER LIVE REFRESH
----------------------------------------

If you have ALREADY run prepare_release.py/build_release.ps1 successfully and only
need to rebuild packaging from the exact same validated source tree, you can run:

       .\build_release.ps1 -SkipRefresh

-SkipRefresh never skips strict validation; it only skips the network refresh step.
For a new public release snapshot, use the normal command without -SkipRefresh.

DIRECT PYTHON MAINTAINER COMMANDS
---------------------------------

With PYTHONPATH set to .\src and dependencies installed:

       python prepare_release.py
       python validate_release_data.py
       python release_self_test.py

prepare_release.py replaces factory data only after the refreshed candidate passes
all required checks. If preparation fails, a concise failure report is written to:

       data\last_release_prepare_failure.txt

and the existing known-good factory data is not intentionally replaced by a partial
candidate.

RELEASE METADATA
----------------

The release-data contract is recorded in:

       data\release_data_summary.json
       data\release_database_info.json

A distributable release must report:

       "release_data_ready": true
       "redistribution_ready": true
       "validation_status": "passed"

and validate_release_data.py must exit successfully. Metadata also records SHA-256
hashes of homecoming_recipes.sqlite and memory_recipe_aliases.json so an accidentally
mixed or modified factory-data pair fails validation.

USER DATA AND SETTINGS
----------------------

Writable application state is stored under:

       %LOCALAPPDATA%\FieldCrafter\

This includes the active recipe database, window state, update cache/backups, and
user-saved memory recipe aliases. Factory data remains bundled with the application.
A newer validated factory database can replace an older active copy on application
upgrade; the prior active database is backed up first.

INTEGRITY
---------

To verify a generated artifact on Windows:

       Get-FileHash ".\Field_Crafter_1.15.exe" -Algorithm SHA256
       Get-FileHash ".\Field_Crafter_1.15_Python.zip" -Algorithm SHA256

Compare the results with the corresponding .sha256 files or SHA256SUMS.txt.

You can also re-run the complete artifact verification gate with:

       .\verify_release_artifacts.ps1

IMPORTANT BUILD-SOURCE STATUS
-----------------------------

A Build Source or testing archive may intentionally contain:

       "redistribution_ready": false

when it was assembled in an environment that could not complete the live Homecoming
Wiki refresh. That archive is suitable for development/testing but is NOT the final
public release. Run build_release.ps1 on your normal Windows PC; only artifacts
created after its green RELEASE BUILD PASSED result should be redistributed.

VERSION
-------

Field Crafter 1.15
