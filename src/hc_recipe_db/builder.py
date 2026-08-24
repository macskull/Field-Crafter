from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .client import MediaWikiClient, MissingPageError
from .database import RecipeDatabase
from .discovery import discover_recipe_pages
from .extractors import (
    SalvageResolver,
    classify_salvage_categories,
    extract_recipe_drop_pool_titles,
    extract_recipe_titles_from_salvage_page,
    extract_temp_power_titles,
    parse_common_io_page,
    parse_costume_index_page,
    parse_generic_recipe_page,
    parse_salvage_tiers_page,
    parse_vanity_pet_page,
)
from .validation import write_validation_report


SALVAGE_TITLE = "Invention Salvage Tiers"
COMMON_IO_TITLE = "Common Invention Recipes"
COSTUME_TITLE = "Invention Made Costumes"
TEMP_POWER_INDEX_TITLE = "Invention Temporary Powers"
RECIPE_DROP_POOLS_TITLE = "Recipe Drop Pools"
VANITY_PET_TITLE = "Vanity Pet"


def _noop(_: str) -> None:
    pass


def build_database(
    *, db_path: str | Path, cache_dir: str | Path, export_dir: str | Path,
    report_path: str | Path, report_json_path: str | Path | None = None,
    refresh: bool = False, delay_seconds: float = 0.25,
    progress: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict:
    progress = progress or _noop
    cancel_check = cancel_check or (lambda: False)

    def check_cancel() -> None:
        if cancel_check():
            raise RuntimeError("Database update cancelled by user.")
    client = MediaWikiClient(cache_dir=cache_dir, delay_seconds=delay_seconds)
    db = RecipeDatabase(db_path)
    db.reset_data()
    started = datetime.now(timezone.utc).isoformat()
    db.set_meta("schema_version", "3")
    db.set_meta("built_at_utc", started)
    db.set_meta("source", "Homecoming Wiki")
    db.set_meta("wiki_api", client.api_url)

    # 1. Salvage catalog first: all recipe parsing resolves against this exact
    # finite vocabulary.  Refuse to continue if it is incomplete.
    check_cancel()
    progress(f"Fetching {SALVAGE_TITLE}...")
    sp = client.parse_page(SALVAGE_TITLE, refresh=refresh)
    salvage = parse_salvage_tiers_page(sp.html, sp.url)
    db.add_source(title=sp.title, url=sp.url, revision_id=sp.revision_id,
                  timestamp=sp.timestamp, parser_kind="salvage_tiers",
                  discovered_from="canonical_seed", raw_cache_path=sp.raw_json_path)
    if len(salvage) != 108:
        db.audit(sp.title, "canonical_seed", "error", f"Expected 108 salvage, parsed {len(salvage)}")
        db.close()
        raise RuntimeError(f"Refusing to build database: expected 108 invention salvage, parsed {len(salvage)}")
    db.add_salvage(salvage)
    resolver = SalvageResolver.from_salvage(salvage)
    db.audit(sp.title, "canonical_seed", "parsed", f"{len(salvage)} salvage records")

    # 1b. Crawl each salvage article's reverse "Recipes Used In" index.  This
    # is deliberately independent of the recipe indexes/categories and catches
    # salvage-consuming recipe pages that may be omitted from an older index.
    progress("Cross-checking 108 salvage reverse recipe indexes...")
    salvage_reverse_titles: set[str] = set()
    salvage_reverse_failures = 0
    salvage_category_mismatches = 0
    salvage_category_missing = 0
    for idx, item in enumerate(salvage, 1):
        check_cancel()
        if idx == 1 or idx % 18 == 0 or idx == len(salvage):
            progress(f"Reading salvage reverse indexes {idx}/{len(salvage)}: {item.name}")
        try:
            page = client.parse_page(item.wiki_title or item.name, refresh=refresh, fetch_timestamp=False)
            salvage_reverse_titles.update(extract_recipe_titles_from_salvage_page(page.html))
            category_rarity, category_tier, category_origin = classify_salvage_categories(page.categories)
            if None in (category_rarity, category_tier, category_origin):
                salvage_category_missing += 1
                db.audit(page.title, "salvage_category_crosscheck", "warning",
                         f"Could not derive complete salvage taxonomy from categories: {page.categories}")
            elif (category_rarity, category_tier, category_origin) != (item.rarity, item.level_tier, item.origin):
                salvage_category_mismatches += 1
                db.audit(page.title, "salvage_category_crosscheck", "error",
                         f"Tier-table taxonomy={(item.rarity, item.level_tier, item.origin)}; "
                         f"individual-page categories={(category_rarity, category_tier, category_origin)}")
        except Exception as exc:
            salvage_reverse_failures += 1
            db.audit(item.wiki_title or item.name, "salvage_reverse_index", "warning", repr(exc))
    db.set_meta("salvage_reverse_candidate_count", str(len(salvage_reverse_titles)))
    db.set_meta("salvage_reverse_failure_count", str(salvage_reverse_failures))
    db.set_meta("salvage_category_mismatch_count", str(salvage_category_mismatches))
    db.set_meta("salvage_category_missing_count", str(salvage_category_missing))
    db.audit(None, "salvage_reverse_index", "discovered",
             f"{len(salvage_reverse_titles)} unique linked pages; failures={salvage_reverse_failures}; "
             f"taxonomy_mismatches={salvage_category_mismatches}; taxonomy_missing={salvage_category_missing}")

    # 2. Common IOs are all represented on one canonical page.
    check_cancel()
    progress(f"Fetching {COMMON_IO_TITLE}...")
    cp = client.parse_page(COMMON_IO_TITLE, refresh=refresh)
    common_source_id = db.add_source(title=cp.title, url=cp.url, revision_id=cp.revision_id,
                                     timestamp=cp.timestamp, parser_kind="common_io_index",
                                     discovered_from="canonical_seed", raw_cache_path=cp.raw_json_path)
    common_recipes = parse_common_io_page(html=cp.html, source_title=cp.title, source_url=cp.url,
                                          revision_id=cp.revision_id, timestamp=cp.timestamp, resolver=resolver)
    for recipe in common_recipes:
        db.add_recipe(recipe, common_source_id)
    db.audit(cp.title, "canonical_seed", "parsed", f"{len(common_recipes)} common IO recipe types")
    db.set_meta("common_io_recipe_type_count", str(len(common_recipes)))

    # 3. Invention-made costumes are also represented canonically on one page.
    check_cancel()
    progress(f"Fetching {COSTUME_TITLE}...")
    costume_page = client.parse_page(COSTUME_TITLE, refresh=refresh)
    costume_source_id = db.add_source(
        title=costume_page.title, url=costume_page.url, revision_id=costume_page.revision_id,
        timestamp=costume_page.timestamp, parser_kind="costume_index",
        discovered_from="canonical_seed", raw_cache_path=costume_page.raw_json_path,
    )
    costume_recipes = parse_costume_index_page(
        html=costume_page.html, source_title=costume_page.title, source_url=costume_page.url,
        revision_id=costume_page.revision_id, timestamp=costume_page.timestamp, resolver=resolver,
    )
    for recipe in costume_recipes:
        db.add_recipe(recipe, costume_source_id)
    db.audit(costume_page.title, "canonical_seed", "parsed", f"{len(costume_recipes)} costume recipes")
    db.set_meta("costume_index_recipe_count", str(len(costume_recipes)))

    # Newer vanity-pet invention recipes are documented outside the historical
    # drop-pool index.  Retain only inline recipes that explicitly consume one
    # or more of the standard 108 invention salvage items.
    check_cancel()
    progress(f"Fetching {VANITY_PET_TITLE} supplemental recipe source...")
    vanity_page = client.parse_page(VANITY_PET_TITLE, refresh=refresh)
    vanity_source_id = db.add_source(
        title=vanity_page.title, url=vanity_page.url, revision_id=vanity_page.revision_id,
        timestamp=vanity_page.timestamp, parser_kind="vanity_pet_index",
        discovered_from="supplemental_current_source", raw_cache_path=vanity_page.raw_json_path,
    )
    vanity_recipes = parse_vanity_pet_page(
        html=vanity_page.html, source_title=vanity_page.title, source_url=vanity_page.url,
        revision_id=vanity_page.revision_id, timestamp=vanity_page.timestamp, resolver=resolver,
    )
    for recipe in vanity_recipes:
        db.add_recipe(recipe, vanity_source_id)
    db.set_meta("vanity_pet_invention_salvage_recipe_count", str(len(vanity_recipes)))
    db.audit(vanity_page.title, "supplemental_current_source", "parsed",
             f"{len(vanity_recipes)} vanity-pet recipes using standard invention salvage")

    # 4. Recipe Drop Pools is the primary individual set-recipe index; the temp
    # power index is the corresponding canonical list for invention temp powers.
    check_cancel()
    progress(f"Fetching {RECIPE_DROP_POOLS_TITLE} index...")
    pool_page = client.parse_page(RECIPE_DROP_POOLS_TITLE, refresh=refresh)
    db.add_source(title=pool_page.title, url=pool_page.url, revision_id=pool_page.revision_id,
                  timestamp=pool_page.timestamp, parser_kind="recipe_drop_pool_index",
                  discovered_from="canonical_seed", raw_cache_path=pool_page.raw_json_path)
    pool_titles = extract_recipe_drop_pool_titles(pool_page.html)
    db.audit(pool_page.title, "canonical_seed", "discovered", f"{len(pool_titles)} candidate set-recipe pages")

    check_cancel()
    progress(f"Fetching {TEMP_POWER_INDEX_TITLE} index...")
    temp_index = client.parse_page(TEMP_POWER_INDEX_TITLE, refresh=refresh)
    db.add_source(title=temp_index.title, url=temp_index.url, revision_id=temp_index.revision_id,
                  timestamp=temp_index.timestamp, parser_kind="temporary_power_index",
                  discovered_from="canonical_seed", raw_cache_path=temp_index.raw_json_path)
    temp_titles = extract_temp_power_titles(temp_index.html)
    db.audit(temp_index.title, "canonical_seed", "discovered", f"{len(temp_titles)} temporary-power pages")

    # 5. Cross-check primary indexes against recipe-drop categories and crawl the
    # union.  This guards against a future formatting/category maintenance error.
    check_cancel()
    progress("Cross-checking recipe discovery categories...")
    discovered, category_members, discrepancies = discover_recipe_pages(
        client,
        recipe_drop_pool_html=pool_page.html,
        temp_power_index_html=temp_index.html,
        salvage_reverse_titles=sorted(salvage_reverse_titles, key=str.casefold),
        refresh=refresh,
    )
    db.set_meta("discovery_categories_json", json.dumps({k: len(v) for k, v in category_members.items()}, sort_keys=True))
    db.set_meta("discovery_discrepancies_json", json.dumps(discrepancies, ensure_ascii=False, sort_keys=True))
    db.set_meta("recipe_drop_pool_candidate_count", str(len(pool_titles)))
    db.set_meta("temporary_power_candidate_count", str(len(temp_titles)))

    db.set_meta(
        "known_non_recipe_enhancement_page_count",
        str(len(discrepancies.get("known_non_recipe_enhancement_pages", []))),
    )
    for key, values in discrepancies.items():
        if key == "temporary_power_index_titles":
            continue
        if values:
            if key == "category_query_errors":
                db.audit(None, "discovery_crosscheck", "warning",
                         f"Category queries failed: {len(values)}; sample={values[:6]}")
            else:
                # Cross-source differences are coverage/audit information. They
                # become errors only if an evidenced recipe cannot be parsed or
                # a source query itself fails.
                db.audit(None, "discovery_crosscheck", "info",
                         f"{key}: {len(values)} pages; sample={values[:12]}")

    check_cancel()
    progress("Fetching recipe revision metadata in batches...")
    try:
        revision_meta = client.revision_metadata([x.title for x in discovered], refresh=refresh)
    except Exception as exc:
        # action=parse already returns a revision ID for each page.  Revision
        # timestamps are useful provenance, but a transient failure in the
        # optional batched metadata request must not discard an otherwise
        # buildable database.
        revision_meta = {}
        db.audit(None, "revision_metadata", "warning", repr(exc))

    parsed_count = 0
    total = len(discovered)
    reused_cached_pages = 0
    for i, item in enumerate(discovered, 1):
        check_cancel()
        if i == 1 or i % 25 == 0 or i == total:
            progress(f"Parsing recipe pages {i}/{total}: {item.title}")
        try:
            meta_revision, meta_timestamp = revision_meta.get(item.title, (None, None))
            cache_path = client._cache_path("api", f"parse:{item.title}")
            cache_matched = False
            if refresh and meta_revision is not None and cache_path.exists():
                try:
                    cached = json.loads(cache_path.read_text(encoding="utf-8"))
                    cached_revid = (cached.get("parse") or {}).get("revid")
                    cache_matched = cached_revid is not None and int(cached_revid) == int(meta_revision)
                except Exception:
                    cache_matched = False
            page = client.parse_page(
                item.title, refresh=refresh, fetch_timestamp=False, expected_revision_id=meta_revision
            )
            if cache_matched:
                reused_cached_pages += 1
            meta_revision, meta_timestamp = revision_meta.get(page.title, revision_meta.get(item.title, (meta_revision, meta_timestamp)))
            revision_id = page.revision_id if page.revision_id is not None else meta_revision
            sid = db.add_source(title=page.title, url=page.url, revision_id=revision_id,
                                timestamp=meta_timestamp, parser_kind=item.recipe_type,
                                discovered_from=item.discovered_from, raw_cache_path=page.raw_json_path)
            recipe = parse_generic_recipe_page(
                title=page.title, html=page.html, source_url=page.url, revision_id=revision_id,
                timestamp=meta_timestamp, categories=page.categories, recipe_type=item.recipe_type,
                resolver=resolver,
            )
            if recipe is None:
                db.audit(item.title, item.discovered_from, "skipped_no_recipe_table", "No recognized salvage-consuming Recipe crafting table")
                continue
            db.add_recipe(recipe, sid)
            db.audit(item.title, item.discovered_from, "parsed", f"{len(recipe.levels)} levels")
            parsed_count += 1
        except MissingPageError as exc:
            # Stale/red links on old index pages are an index-maintenance fact,
            # not an extractor failure. Keep them visible in the audit without
            # retrying or failing the database build.
            db.audit(item.title, item.discovered_from, "skipped_missing_page", str(exc))
        except Exception as exc:
            db.audit(item.title, item.discovered_from, "error", repr(exc))

    db.set_meta("discovered_page_count", str(len(discovered)))
    db.set_meta("parsed_discovered_recipe_count", str(parsed_count))
    db.set_meta("incremental_cached_recipe_page_count", str(reused_cached_pages))
    db.conn.commit()
    counts = db.counts()
    db.export(export_dir)
    db.close()

    check_cancel()
    progress("Running validation...")
    checks = write_validation_report(db_path, report_path, report_json_path)
    result = {
        "counts": counts,
        "common_io_recipe_types": len(common_recipes),
        "costume_recipes": len(costume_recipes),
        "vanity_pet_invention_salvage_recipes": len(vanity_recipes),
        "recipe_drop_pool_candidates": len(pool_titles),
        "temporary_power_candidates": len(temp_titles),
        "salvage_reverse_candidates": len(salvage_reverse_titles),
        "salvage_reverse_failures": salvage_reverse_failures,
        "salvage_category_mismatches": salvage_category_mismatches,
        "salvage_category_missing": salvage_category_missing,
        "discovered_pages": len(discovered),
        "parsed_discovered_recipes": parsed_count,
        "validation": {
            "pass": sum(x.severity == "PASS" for x in checks),
            "warn": sum(x.severity == "WARN" for x in checks),
            "error": sum(x.severity == "ERROR" for x in checks),
        },
    }
    progress("Build complete.")
    return result
