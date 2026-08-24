from __future__ import annotations

from dataclasses import dataclass

from .client import MediaWikiClient
from .extractors import extract_recipe_drop_pool_titles, extract_temp_power_titles


@dataclass(frozen=True, slots=True)
class DiscoveredPage:
    title: str
    recipe_type: str
    discovered_from: str


# The historical Recipe Drop Pools article is the primary index for classic
# invention recipe drops. Common IOs, costumes, temporary powers and newer
# supplemental recipe types also have dedicated sources; these categories are
# an independent cross-check for set-recipe links discovered from that index.
SET_CROSSCHECK_CATEGORIES: tuple[str, ...] = (
    "Invention Set Enhancements",
    "Common Recipe Drops",
    "Uncommon Recipe Drops",
    "Rare Recipe Drops",
    "Very Rare Recipe Drops",
    "Mission Recipe Drops",
    "Random Rare Recipe Roll",
    "PvP Recipe Drops",
    "Special Recipe Drops",
    "Unknown Recipe Drops",
)

# These enhancement families intentionally have enhancement objects but no
# invention recipes. Homecoming documents Archetype and Winter Origin
# enhancements as non-Inventions/non-craftable; Cupid's Crush and Overwhelming
# Force are likewise acquired directly as attuned event enhancements. Their
# individual enhancement pages still live under Invention Set Enhancements,
# so treating that category as a recipe index creates hundreds of false
# candidates. Query these categories dynamically instead of hard-coding set
# names so new event/ATO/Winter pieces are handled automatically.
NON_RECIPE_ENHANCEMENT_CATEGORIES: tuple[str, ...] = (
    "Archetype Enhancements",
    "Winter Origin Enhancements",
    "Spring Event Wedding Enhancements",
    "Summer Blockbuster Enhancements",
)

# Useful as a fallback if the temporary-power index ever changes link markup.
AUXILIARY_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("Invention Costume Pieces", "costume"),
    ("Invention Temp Powers", "temporary_power"),
)


def _classify_reverse_title(title: str) -> str:
    lower = title.casefold()
    if lower.startswith("temporary power:"):
        return "temporary_power"
    if lower.startswith("costume piece:"):
        return "costume"
    if lower.startswith("invention:"):
        return "common_io"
    # Most remaining colon-delimited reverse-index entries are set recipes.
    # Unknown pages are harmless: parse_generic_recipe_page is the final gate.
    if ":" in title:
        return "set_enhancement"
    return "supplemental"


def discover_recipe_pages(
    client: MediaWikiClient,
    *,
    recipe_drop_pool_html: str,
    temp_power_index_html: str,
    salvage_reverse_titles: list[str] | None = None,
    refresh: bool = False,
) -> tuple[list[DiscoveredPage], dict[str, list[str]], dict[str, list[str]]]:
    """Discover individual recipe pages from canonical indexes and cross-checks.

    Returns ``(pages, category_members, discrepancies)``. Recipe Drop Pools and
    the salvage ``Recipes Used In`` reverse index are both treated as positive
    recipe evidence. The broad Invention Set Enhancements category is only a
    cross-check because it also contains ATO, Winter, and event enhancements
    that intentionally have no recipe form.
    """
    found: dict[str, DiscoveredPage] = {}

    pool_titles = extract_recipe_drop_pool_titles(recipe_drop_pool_html)
    pool_set = set(pool_titles)
    for title in pool_titles:
        found.setdefault(title, DiscoveredPage(title, "set_enhancement", "Recipe Drop Pools"))

    temp_titles = extract_temp_power_titles(temp_power_index_html)
    temp_set = set(temp_titles)
    for title in temp_titles:
        found.setdefault(title, DiscoveredPage(title, "temporary_power", "Invention Temporary Powers"))

    reverse_titles = sorted(set(salvage_reverse_titles or []), key=str.casefold)
    reverse_set = set(reverse_titles)
    for title in reverse_titles:
        recipe_type = _classify_reverse_title(title)
        # Common IOs and invention-made costumes have dedicated canonical index
        # parsers. Their individual pages are useful as reverse-index evidence
        # but must not overwrite the richer canonical records.
        if recipe_type in {"common_io", "costume"}:
            continue
        found.setdefault(title, DiscoveredPage(title, recipe_type, "Salvage Recipes Used In"))

    category_members: dict[str, list[str]] = {}
    category_query_errors: list[str] = []

    # First learn the pages that belong to known enhancement-only families.
    known_non_recipe_titles: set[str] = set()
    for category in NON_RECIPE_ENHANCEMENT_CATEGORIES:
        try:
            members = list(client.category_members(category, refresh=refresh))
        except Exception as exc:
            members = []
            category_query_errors.append(f"{category}: {exc!r}")
        category_members[category] = members
        known_non_recipe_titles.update(members)

    category_set_titles: set[str] = set()
    ignored_known_non_recipe: set[str] = set()
    for category in SET_CROSSCHECK_CATEGORIES:
        try:
            members = list(client.category_members(category, refresh=refresh))
        except Exception as exc:
            members = []
            category_query_errors.append(f"{category}: {exc!r}")
        category_members[category] = members
        category_set_titles.update(members)
        for title in members:
            # If another independent source explicitly identifies the page as a
            # recipe, positive evidence wins. Otherwise known ATO/Winter/Event
            # enhancement pages are intentionally not crawled as recipes.
            if title in known_non_recipe_titles and title not in pool_set and title not in reverse_set:
                ignored_known_non_recipe.add(title)
                continue
            found.setdefault(title, DiscoveredPage(title, "set_enhancement", f"Category:{category}"))

    for category, recipe_type in AUXILIARY_CATEGORIES:
        try:
            members = list(client.category_members(category, refresh=refresh))
        except Exception as exc:
            members = []
            category_query_errors.append(f"{category}: {exc!r}")
        category_members[category] = members
        for title in members:
            found.setdefault(title, DiscoveredPage(title, recipe_type, f"Category:{category}"))

    primary_individual = pool_set | temp_set
    discrepancies = {
        # These are useful coverage facts, not necessarily defects. A recipe
        # recovered through the salvage reverse index can legitimately be absent
        # from the older Recipe Drop Pools article.
        "category_recipe_evidence_not_in_recipe_drop_pools": sorted(
            (category_set_titles & reverse_set) - pool_set, key=str.casefold
        ),
        "recipe_drop_pools_not_in_categories": sorted(pool_set - category_set_titles, key=str.casefold),
        "salvage_reverse_not_in_primary_indexes": sorted(reverse_set - primary_individual, key=str.casefold),
        "primary_indexes_not_in_salvage_reverse": sorted(primary_individual - reverse_set, key=str.casefold),
        "known_non_recipe_enhancement_pages": sorted(ignored_known_non_recipe, key=str.casefold),
        "temporary_power_index_titles": temp_titles,
        "category_query_errors": category_query_errors,
    }
    return sorted(found.values(), key=lambda x: x.title.casefold()), category_members, discrepancies
