Field Crafter release data

Public/runtime files:
- homecoming_recipes.sqlite: bundled Homecoming recipe/salvage database.
- validation_report.txt / validation_report.json: database validation results.
- memory_recipe_aliases.json: game-internal recipe ID -> canonical recipe mappings.
- release_data_summary.json: strict release status, coverage, counts, and hashes.
- release_database_info.json: compact release/database metadata for inspection.

For a public Field Crafter 1.15 release, prepare_release.py builds a fresh candidate
from Homecoming Wiki, rebuilds the full memory recipe map, validates complete mapping
coverage and database integrity, and only then installs the candidate as factory
data. build_release.ps1 performs that process automatically before creating the EXE
or Python ZIP.

A source/testing archive can intentionally have redistribution_ready=false. Do not
redistribute that archive as the final end-user package; run build_release.ps1 on a
network-capable Windows build machine first.
