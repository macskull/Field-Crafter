Field Crafter release data

Public/runtime files:
- homecoming_recipes.sqlite: bundled Homecoming recipe/salvage database.
- validation_report.txt / validation_report.json: database validation results.
- memory_recipe_aliases.json: game-internal recipe ID -> canonical recipe mappings.
- memory_profiles.json: bundled signed-update-compatible game-memory definition pack.
- memory_update_config.json: public memory-update channel and signature-verification configuration.
- release_data_summary.json: strict release status, coverage, counts, and hashes.
- release_database_info.json: compact release/database metadata for inspection.

For a public Field Crafter 1.16 release, prepare_release.py builds a fresh candidate
from Homecoming Wiki, rebuilds the complete memory recipe map, validates complete
mapping coverage and database integrity, validates the bundled memory definition
pack/update configuration, and only then installs the candidate as factory data.

Release metadata hash-binds:
- homecoming_recipes.sqlite
- memory_recipe_aliases.json
- memory_profiles.json
- memory_update_config.json

build_release.ps1 performs the release-data validation again before creating the
portable EXE and prepared Python ZIP.

A source/testing archive can intentionally have redistribution_ready=false. The
public end-user artifacts are created from release data marked redistribution-ready.
