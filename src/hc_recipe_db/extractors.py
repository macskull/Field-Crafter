from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup, NavigableString, Tag

from .models import CraftOption, Recipe, RecipeLevel, Requirement, Salvage
from .normalize import canonical_key, clean_text, singularize_salvage_hint

_INT = re.compile(r"\d[\d,]*")
_LEVEL = re.compile(r"^\s*(\d{1,2})\s*$")
_OR = re.compile(r"\bor\b", re.I)


class ParseError(RuntimeError):
    pass


@dataclass(slots=True)
class SalvageResolver:
    canonical_by_key: dict[str, str]

    @classmethod
    def from_salvage(cls, items: Iterable[Salvage]) -> "SalvageResolver":
        mapping: dict[str, str] = {}
        for item in items:
            mapping[canonical_key(item.name)] = item.name
        # Known display-plural aliases used in recipe tables.
        for plural, singular in {
            "Masterwork Weapons": "Masterwork Weapon",
            "Demonic Threat Reports": "Demonic Threat Report",
            "Runes": "Rune",
            "Scientific Theories": "Scientific Theory",
            "Rubies": "Ruby",
        }.items():
            if canonical_key(singular) in mapping:
                mapping[canonical_key(plural)] = mapping[canonical_key(singular)]
        return cls(mapping)

    def resolve(self, raw: str) -> str | None:
        raw = singularize_salvage_hint(clean_text(raw))
        exact = self.canonical_by_key.get(canonical_key(raw))
        if exact:
            return exact

        # Wiki templates occasionally pluralize a salvage display name (for
        # example Boresights or Silvers).  Resolve such forms only when
        # removing the suffix produces an existing canonical salvage name, so
        # naturally-suffixed names such as Temporal Sands remain untouched.
        candidates: list[str] = []
        lower = raw.casefold()
        # Common English plural forms occasionally leak through wiki display
        # templates even though the canonical salvage page is singular.
        if lower.endswith("ies") and len(raw) > 3:
            candidates.append(raw[:-3] + "y")
        if lower.endswith("es") and len(raw) > 2:
            candidates.append(raw[:-2])
        if lower.endswith("s") and len(raw) > 1:
            candidates.append(raw[:-1])
        for candidate in candidates:
            resolved = self.canonical_by_key.get(canonical_key(candidate))
            if resolved:
                return resolved
        return None


def _ints(text: str) -> list[int]:
    return [int(m.group(0).replace(",", "")) for m in _INT.finditer(text)]


def _find_recipe_heading(soup: BeautifulSoup) -> Tag | None:
    for h in soup.find_all(["h2", "h3"]):
        if clean_text(h.get_text(" ", strip=True)).lower() == "recipe":
            return h
        span = h.find("span", id="Recipe")
        if span:
            return h
    return None


def _tables_until_next_same_or_higher_heading(heading: Tag) -> list[Tag]:
    out: list[Tag] = []
    level = int(heading.name[1]) if heading.name and heading.name.startswith("h") else 2
    for el in heading.find_all_next():
        if not isinstance(el, Tag):
            continue
        if el.name and re.fullmatch(r"h[1-6]", el.name):
            if int(el.name[1]) <= level and el is not heading:
                break
        if el.name == "table":
            txt = clean_text(el.get_text(" ", strip=True)).lower()
            if "level" in txt and "invention salvage" in txt:
                out.append(el)
    return out


def _anchor_candidate_name(a: Tag) -> str:
    """Return the linked wiki item name while rejecting media/navigation links."""
    title = clean_text(a.get("title", ""))
    href = clean_text(a.get("href", ""))
    lower_title = title.casefold()
    lower_href = href.casefold()
    rejected_namespaces = ("file:", "image:", "category:", "help:", "special:", "template:", "talk:")
    if lower_title.startswith(rejected_namespaces):
        return ""
    if any(f"/wiki/{ns}" in lower_href for ns in rejected_namespaces):
        return ""

    text = clean_text(a.get_text(" ", strip=True))
    if text and text.casefold() not in {"image", "file"}:
        return text
    if title:
        return title
    if "/wiki/" in href:
        from urllib.parse import unquote
        candidate = unquote(href.split("/wiki/", 1)[1]).replace("_", " ")
        if candidate.casefold().startswith(rejected_namespaces):
            return ""
        return candidate
    return ""


def _ingredient_groups(cell: Tag, resolver: SalvageResolver) -> tuple[list[list[Requirement]], list[str]]:
    """Parse salvage requirements, preserving `or`-separated alternatives.

    MediaWiki recipe cells typically contain quantity text followed by a linked
    salvage name. We scan DOM order, ignoring image links and incrementing the
    option group when a standalone `or` appears.
    """
    groups: list[list[Requirement]] = [[]]
    warnings: list[str] = []
    pending_qty: int | None = None

    for node in cell.descendants:
        if isinstance(node, NavigableString):
            # Text inside an anchor is handled when the anchor itself is visited.
            if isinstance(node.parent, Tag) and node.parent.name == "a":
                continue
            text = clean_text(str(node))
            if not text:
                continue
            if _OR.fullmatch(text) or text.lower() == "or":
                if groups[-1]:
                    groups.append([])
                pending_qty = None
                continue
            nums = _ints(text)
            if nums:
                pending_qty = nums[-1]
            # Some rendered templates place `or` with surrounding punctuation/text.
            if _OR.search(text) and text.lower() != "or":
                # Only split when `or` is semantically between already-complete groups.
                if groups[-1] and text.strip().lower().startswith("or"):
                    groups.append([])
                    pending_qty = nums[-1] if nums else None
            continue

        if not isinstance(node, Tag) or node.name != "a":
            continue
        raw_name = _anchor_candidate_name(node)
        if not raw_name:
            continue
        canonical = resolver.resolve(raw_name)
        if not canonical:
            warning = f"UNRESOLVED_SALVAGE: {raw_name}"
            if warning not in warnings:
                warnings.append(warning)
            continue
        qty = pending_qty or 1
        groups[-1].append(Requirement(canonical, qty, raw_name=raw_name))
        pending_qty = None

    groups = [g for g in groups if g]
    if not groups:
        # Fallback: parse visible text against known salvage names.
        text = clean_text(cell.get_text(" ", strip=True))
        chunks = [clean_text(x) for x in re.split(r"\s+or\s+", text, flags=re.I)]
        for chunk in chunks:
            found: list[tuple[int, Requirement]] = []
            for key, canonical in resolver.canonical_by_key.items():
                # canonical_key loses spaces, so use the human name for fallback matching.
                pos = chunk.lower().find(canonical.lower())
                if pos < 0:
                    continue
                before = chunk[:pos]
                nums = _ints(before)
                qty = nums[-1] if nums else 1
                found.append((pos, Requirement(canonical, qty, raw_name=canonical)))
            if found:
                found.sort(key=lambda x: x[0])
                groups.append([req for _, req in found])
    if not groups:
        warnings.append(f"No salvage requirements parsed from cell: {clean_text(cell.get_text(' ', strip=True))[:200]}")
    return groups, warnings


def _cost_options(cell: Tag) -> list[int | None]:
    text = clean_text(cell.get_text(" ", strip=True))
    parts = [clean_text(x) for x in re.split(r"\s+or\s+", text, flags=re.I)]
    values: list[int | None] = []
    for part in parts:
        nums = _ints(part)
        if nums:
            values.append(nums[0])
    if not values:
        nums = _ints(text)
        return [nums[0] if nums else None]
    return values


def _pair_options(costs: list[int | None], groups: list[list[Requirement]]) -> list[CraftOption]:
    if not groups:
        return [CraftOption(costs[0] if costs else None, [])]
    if len(costs) == len(groups):
        return [CraftOption(costs[i], groups[i]) for i in range(len(groups))]
    if len(costs) == 1:
        return [CraftOption(costs[0], g) for g in groups]
    if len(groups) == 1:
        return [CraftOption(c, list(groups[0])) for c in costs]
    # Preserve all data rather than guess; pair what we can and let validation flag it.
    n = max(len(costs), len(groups))
    return [
        CraftOption(costs[min(i, len(costs) - 1)] if costs else None,
                    list(groups[min(i, len(groups) - 1)]))
        for i in range(n)
    ]


def _copy_requirements(options: list[CraftOption], costs: list[int | None]) -> list[CraftOption]:
    if not options:
        return []
    req_groups = [list(o.requirements) for o in options]
    if len(costs) == len(req_groups):
        return [CraftOption(costs[i], req_groups[i]) for i in range(len(req_groups))]
    if len(costs) == 1:
        return [CraftOption(costs[0], req) for req in req_groups]
    return [CraftOption(costs[min(i, len(costs)-1)], req_groups[min(i, len(req_groups)-1)]) for i in range(max(len(costs), len(req_groups)))]


def parse_salvage_tiers_page(html: str, source_url: str = "") -> list[Salvage]:
    """Parse the compact Invention Salvage Tiers page.

    The wiki table is organized as Common/Uncommon/Rare columns, with 12 rows
    per level tier. Per the page's legend/layout, the first six rows in each tier
    are Technological and the next six are Arcane.
    """
    soup = BeautifulSoup(html, "html.parser")
    result: list[Salvage] = []
    rarity_by_col = ["common", "uncommon", "rare"]
    tier_map = {
        "10-25": "low",
        "30-40": "mid",
        "45-50": "high",
    }

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        current_tier: str | None = None
        data_index = 0
        for row in rows:
            cells = row.find_all(["th", "td"], recursive=False)
            if not cells:
                continue
            row_text = clean_text(row.get_text(" ", strip=True))
            m = re.search(r"Level\s*(10-25|30-40|45-50)", row_text, re.I)
            if m:
                current_tier = tier_map[m.group(1)]
                data_index = 0
                continue
            if not current_tier:
                continue
            if len(cells) < 3:
                continue
            # Header row can recur before each tier.
            if "common" in row_text.lower() and "uncommon" in row_text.lower() and "rare" in row_text.lower():
                continue
            anchors = []
            valid = True
            for cell in cells[:3]:
                a = next((x for x in cell.find_all("a") if _anchor_candidate_name(x)), None)
                if not a:
                    valid = False
                    break
                anchors.append(a)
            if not valid:
                continue
            origin = "technological" if data_index < 6 else "arcane"
            for col, a in enumerate(anchors):
                name = _anchor_candidate_name(a)
                if not name:
                    continue
                title = clean_text(a.get("title", "")) or name
                href = clean_text(a.get("href", ""))
                item_url = urljoin(source_url, href) if source_url and href else source_url
                result.append(Salvage(name=name, rarity=rarity_by_col[col], level_tier=current_tier,
                                      origin=origin, wiki_title=title, wiki_url=item_url))
            data_index += 1

    # Dedupe preserving first appearance.
    seen: set[str] = set()
    deduped: list[Salvage] = []
    for item in result:
        key = canonical_key(item.name)
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped



def classify_salvage_categories(categories: Iterable[str]) -> tuple[str | None, str | None, str | None]:
    """Return (rarity, level_tier, origin) from an individual salvage page's categories."""
    cats = {clean_text(c).casefold().replace("_", " ") for c in categories}
    rarity = next((value for label, value in (
        ("common invention salvage", "common"),
        ("uncommon invention salvage", "uncommon"),
        ("rare invention salvage", "rare"),
    ) if label in cats), None)
    tier = next((value for label, value in (
        ("invention salvage low-level", "low"),
        ("invention salvage mid-level", "mid"),
        ("invention salvage high-level", "high"),
    ) if label in cats), None)
    origin = next((value for label, value in (
        ("invention arcane salvage", "arcane"),
        ("invention technological salvage", "technological"),
    ) if label in cats), None)
    return rarity, tier, origin


def extract_recipe_titles_from_salvage_page(html: str) -> list[str]:
    """Extract pages listed in a salvage article's ``Recipes Used In`` section.

    Every invention-salvage article exposes a reverse index of things that use
    that salvage.  It is an excellent independent discovery path: if a new
    salvage-consuming invention recipe is added but an older recipe index or
    category is not updated, the reverse index can still surface it.  This
    function intentionally returns some non-recipe candidates (for example
    Empowerment buffs); the generic recipe parser is the final authority and
    will reject pages without a recognized Recipe crafting table.
    """
    soup = BeautifulSoup(html, "html.parser")
    heading: Tag | None = None
    for h in soup.find_all(["h2", "h3"]):
        if clean_text(h.get_text(" ", strip=True)).casefold() == "recipes used in":
            heading = h
            break
    if heading is None:
        return []

    level = int(heading.name[1])
    found: dict[str, str] = {}
    for el in heading.find_all_next():
        if not isinstance(el, Tag):
            continue
        if el.name and re.fullmatch(r"h[1-6]", el.name) and int(el.name[1]) <= level:
            break
        if el.name != "a":
            continue
        title = clean_text(el.get("title", "")) or _anchor_candidate_name(el)
        if not title:
            continue
        lower = title.casefold()
        if lower.startswith(("file:", "image:", "category:", "help:", "special:", "template:", "talk:")):
            continue
        href = clean_text(el.get("href", ""))
        if href and "/wiki/" not in href:
            continue
        found.setdefault(canonical_key(title), title)
    return sorted(found.values(), key=str.casefold)


def extract_recipe_drop_pool_titles(html: str) -> list[str]:
    """Extract set-enhancement recipe page titles from Recipe Drop Pools.

    Recipe entries on that index are list items and use the conventional
    ``Set Name: Piece Name`` page title.  Namespace/media links and article
    prose are deliberately excluded.  The individual page parser remains the
    final authority: a discovered title is retained only if it has a recognized
    Recipe table that consumes invention salvage.
    """
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, str] = {}
    blocked_prefixes = (
        "category:", "file:", "image:", "help:", "special:", "template:",
        "talk:", "temporary power:", "costume piece:",
    )
    for li in soup.find_all("li"):
        for a in li.find_all("a"):
            title = clean_text(a.get("title", "")) or _anchor_candidate_name(a)
            if not title or ":" not in title:
                continue
            lower = title.casefold()
            if lower.startswith(blocked_prefixes):
                continue
            href = clean_text(a.get("href", ""))
            if href and "/wiki/" not in href:
                continue
            found.setdefault(canonical_key(title), title)
    return sorted(found.values(), key=str.casefold)


def extract_temp_power_titles(html: str) -> list[str]:
    """Extract the individual recipe-bearing Temporary Power pages from its index."""
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, str] = {}
    for a in soup.find_all("a"):
        title = clean_text(a.get("title", "")) or _anchor_candidate_name(a)
        # MediaWiki redlinks render titles like
        # ``Temporary Power: Foo (page does not exist)``.  The suffix is UI
        # metadata, not part of the canonical page title.
        title = re.sub(r"\s*\(page does not exist\)\s*$", "", title, flags=re.I)
        if not title.casefold().startswith("temporary power:"):
            continue
        found.setdefault(canonical_key(title), title)
    return sorted(found.values(), key=str.casefold)


def _section_until_next_heading(heading: Tag, max_level: int | None = None) -> list[Tag]:
    """Return sibling-ish tags after a heading until the next peer/higher heading."""
    level = max_level or (int(heading.name[1]) if heading.name and re.fullmatch(r"h[1-6]", heading.name) else 3)
    out: list[Tag] = []
    for el in heading.find_all_next():
        if not isinstance(el, Tag) or el is heading:
            continue
        if el.name and re.fullmatch(r"h[1-6]", el.name) and int(el.name[1]) <= level:
            break
        out.append(el)
    return out


def parse_costume_index_page(
    *, html: str, source_title: str, source_url: str, revision_id: int | None,
    timestamp: str | None, resolver: SalvageResolver,
) -> list[Recipe]:
    """Parse all salvage-crafted costume recipes from Invention Made Costumes.

    The wiki keeps these recipes together on one canonical article rather than
    consistently exposing one recipe table per individual costume article.
    Each h3 recipe section contains a cost and a ``Salvage Required`` table.
    """
    soup = BeautifulSoup(html, "html.parser")
    recipes: list[Recipe] = []
    ignored = {"overview", "see also", "back details", "lower body pieces"}

    for heading in soup.find_all(["h3", "h4"]):
        name = clean_text(heading.get_text(" ", strip=True))
        if not name or name.casefold() in ignored:
            continue
        section = _section_until_next_heading(heading)
        table = next((x for x in section if x.name == "table" and "salvage required" in clean_text(x.get_text(" ", strip=True)).casefold()), None)
        if table is None:
            continue

        cost: int | None = None
        reqs: list[Requirement] = []
        notes: list[str] = []
        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"], recursive=False)
            if len(cells) < 2:
                continue
            label = clean_text(cells[0].get_text(" ", strip=True)).casefold()
            if "cost" in label:
                vals = _ints(cells[1].get_text(" ", strip=True))
                cost = vals[0] if vals else None
            if "salvage required" in label:
                groups, warns = _ingredient_groups(cells[-1], resolver)
                notes.extend(warns)
                if groups:
                    reqs = groups[0]

        if not reqs:
            # Some old wiki markup may put Cost and Salvage Required in a single
            # row/table cell.  Fall back to the whole table rather than guessing.
            groups, warns = _ingredient_groups(table, resolver)
            notes.extend(warns)
            if groups:
                reqs = groups[0]
        if cost is None:
            # Use text nearest the section, bounded at the next peer heading.
            section_text = " ".join(clean_text(x.get_text(" ", strip=True)) for x in section if x.name in {"p", "table"})
            m = re.search(r"\bCost\b[^0-9]*(\d[\d,]*)", section_text, re.I)
            if m:
                cost = int(m.group(1).replace(",", ""))

        if not reqs:
            continue
        recipes.append(Recipe(
            name=f"Costume Piece: {name}",
            recipe_type="costume",
            source_title=source_title,
            source_url=source_url,
            source_revision=revision_id,
            source_timestamp=timestamp,
            recipe_rarity="costume",
            levels=[RecipeLevel(level=1, options=[CraftOption(cost, reqs)])],
            categories=[],
            notes=notes,
        ))
    return recipes


def parse_vanity_pet_page(
    *, html: str, source_title: str, source_url: str, revision_id: int | None,
    timestamp: str | None, resolver: SalvageResolver,
) -> list[Recipe]:
    """Parse inline vanity-pet recipes that consume standard invention salvage.

    Homecoming's newer vanity-pet recipes are not part of the older Recipe Drop
    Pools article.  Most use Prismatic Aether (outside this database's 108-item
    invention-salvage scope), but the Hamidon Bud recipe is documented inline
    as consuming Hamidon Goo.  This parser is intentionally data-driven: any
    heading whose local section explicitly says it "requires ... inf to craft"
    and contains one or more known invention-salvage names is retained.

    These newer recipes are level-independent on the source article, represented
    internally as level 0 rather than inventing a character/recipe level.
    """
    soup = BeautifulSoup(html, "html.parser")
    recipes: list[Recipe] = []
    seen: set[str] = set()
    for heading in soup.find_all(["h3", "h4", "h5", "h6"]):
        display = clean_text(heading.get_text(" ", strip=True))
        if not display:
            continue
        # Only prose before the *next heading of any depth* belongs directly
        # to this heading.  This prevents a parent section such as "Rare Recipe
        # Drop from Hamidon Buds" from borrowing the requirement text of its
        # nested "Hamidon Bud" item.
        section: list[Tag] = []
        for el in heading.find_all_next():
            if not isinstance(el, Tag) or el is heading:
                continue
            if el.name and re.fullmatch(r"h[1-6]", el.name):
                break
            section.append(el)
        text = clean_text(" ".join(
            x.get_text(" ", strip=True) for x in section
            if x.name in {"p", "div", "li"}
        ))
        if "requires" not in text.casefold() or "to craft" not in text.casefold():
            continue
        cost_match = re.search(r"([0-9][0-9,]*)\s*inf(?:luence)?\s+to\s+craft", text, re.I)
        if not cost_match:
            continue
        cost = int(cost_match.group(1).replace(",", ""))

        reqs: list[Requirement] = []
        # Match against the finite canonical salvage vocabulary and take the
        # closest number directly preceding each matched salvage name.
        lower_text = text.casefold()
        for canonical in sorted(set(resolver.canonical_by_key.values()), key=len, reverse=True):
            pos = lower_text.find(canonical.casefold())
            if pos < 0:
                continue
            prefix = text[:pos]
            nums = _ints(prefix)
            qty = nums[-1] if nums else 1
            reqs.append(Requirement(canonical, qty, raw_name=canonical))
        if not reqs:
            continue

        # The current in-game naming convention for these recipe-produced vanity
        # pets is "Pet: <name>".  Preserve the source heading in notes for audit.
        name = display if display.casefold().startswith("pet:") else f"Pet: {display}"
        key = canonical_key(name)
        if key in seen:
            continue
        seen.add(key)
        recipes.append(Recipe(
            name=name,
            recipe_type="vanity_pet",
            source_title=source_title,
            source_url=source_url,
            source_revision=revision_id,
            source_timestamp=timestamp,
            recipe_rarity=None,
            levels=[RecipeLevel(level=0, options=[CraftOption(cost, reqs)])],
            categories=[],
            notes=[f"Inline vanity-pet recipe parsed from source heading: {display}"],
        ))
    return recipes

def infer_recipe_rarity(categories: Iterable[str], body_text: str) -> str | None:
    cats = {clean_text(c).lower().replace("_", " ") for c in categories}
    mapping = [
        ("pvp recipe drops", "pvp"),
        ("very rare recipe drops", "very_rare"),
        ("common recipe drops", "common"),
        ("uncommon recipe drops", "uncommon"),
        ("rare recipe drops", "rare"),
        ("mission recipe drops", "mission"),
        ("random rare recipe roll", "random_rare"),
        ("special recipe drops", "special"),
    ]
    for cat, value in mapping:
        if cat in cats:
            return value
    lower = body_text.lower()
    if "it is an uncommon drop" in lower:
        return "uncommon"
    if "it is a rare drop" in lower:
        return "rare"
    if "pvp drop" in lower:
        return "pvp"
    if "very rare" in lower and "recipe" in lower:
        return "very_rare"
    return None




def _apply_documented_range_override(
    levels_by_number: dict[int, RecipeLevel], body_text: str, notes: list[str]
) -> None:
    """Apply a wiki-authored Recipe Note that corrects displayed salvage bands.

    At least one Homecoming Wiki set (Exploit Weakness) documents that the
    rendered crafting tables visually group level 40 with the middle band even
    though the actual salvage boundary is 40-50.  We parse that explicit note
    rather than hard-code the set name.
    """
    compact = clean_text(body_text)
    m = re.search(
        r"middle level range.*?actually\s+(\d{1,2})\s*[--]\s*(\d{1,2}).*?"
        r"upper level range.*?actually\s+(\d{1,2})\s*[--]\s*(\d{1,2})",
        compact,
        flags=re.I,
    )
    if not m:
        return
    mid_start, mid_end, upper_start, upper_end = map(int, m.groups())
    if upper_start not in levels_by_number:
        notes.append(
            f"Documented salvage range note found ({mid_start}-{mid_end}, {upper_start}-{upper_end}) "
            "but upper boundary level is absent; no override applied."
        )
        return

    # Find a displayed row already carrying the upper-band requirement set.
    source_level: int | None = None
    target = levels_by_number[upper_start]
    target_sig = [[(r.salvage_name, r.quantity) for r in o.requirements] for o in target.options]
    for level in sorted(k for k in levels_by_number if upper_start < k <= upper_end):
        candidate = levels_by_number[level]
        sig = [[(r.salvage_name, r.quantity) for r in o.requirements] for o in candidate.options]
        if sig and sig != target_sig:
            source_level = level
            break
    if source_level is None:
        # If the table already reflects the note, nothing needs to be changed.
        notes.append(
            f"Documented salvage ranges {mid_start}-{mid_end} and {upper_start}-{upper_end} "
            "found; table already appears consistent or no alternate requirement group was found."
        )
        return

    source_options = levels_by_number[source_level].options
    changed: list[int] = []
    for level in sorted(k for k in levels_by_number if upper_start <= k < source_level):
        current = levels_by_number[level]
        costs = [o.crafting_cost for o in current.options]
        current.options = _copy_requirements(source_options, costs)
        changed.append(level)
    notes.append(
        f"Applied wiki Recipe Note salvage-range override: middle {mid_start}-{mid_end}, "
        f"upper {upper_start}-{upper_end}; copied upper-band requirements from level {source_level} "
        f"to levels {changed}."
    )

def parse_generic_recipe_page(
    *, title: str, html: str, source_url: str, revision_id: int | None,
    timestamp: str | None, categories: list[str], recipe_type: str,
    resolver: SalvageResolver,
) -> Recipe | None:
    soup = BeautifulSoup(html, "html.parser")
    heading = _find_recipe_heading(soup)
    if not heading:
        return None
    tables = _tables_until_next_same_or_higher_heading(heading)
    if not tables:
        return None

    levels_by_number: dict[int, RecipeLevel] = {}
    notes: list[str] = []
    for table in tables:
        active_options: list[CraftOption] = []
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"], recursive=False)
            if len(cells) < 2:
                continue
            m = _LEVEL.match(clean_text(cells[0].get_text(" ", strip=True)))
            if not m:
                continue
            level = int(m.group(1))
            costs = _cost_options(cells[1])
            ingredient_cell = cells[2] if len(cells) >= 3 else None
            if ingredient_cell is not None:
                groups, warns = _ingredient_groups(ingredient_cell, resolver)
                notes.extend(warns)
                if groups:
                    active_options = _pair_options(costs, groups)
                elif active_options:
                    active_options = _copy_requirements(active_options, costs)
                else:
                    active_options = [CraftOption(costs[0] if costs else None, [])]
            elif active_options:
                active_options = _copy_requirements(active_options, costs)
            else:
                # Some malformed/edge tables may omit salvage entirely. Preserve row.
                active_options = [CraftOption(costs[0] if costs else None, [])]
            levels_by_number[level] = RecipeLevel(level=level, options=[
                CraftOption(o.crafting_cost, list(o.requirements)) for o in active_options
            ])

    if not levels_by_number:
        return None
    # This database is intentionally scoped to recipes that consume at least
    # one of the 108 standard invention salvage items.  Enhancement-only pages
    # (ATO/Winter/Event pieces), obsolete power pages, and costume entries that
    # explicitly say "No salvage required" must not become empty recipe rows.
    if not any(
        option.requirements
        for level in levels_by_number.values()
        for option in level.options
    ):
        return None
    text = clean_text(soup.get_text(" ", strip=True))
    _apply_documented_range_override(levels_by_number, text, notes)
    set_name = title.split(":", 1)[0].strip() if recipe_type == "set_enhancement" and ":" in title else None
    return Recipe(
        name=title,
        recipe_type=recipe_type,
        source_title=title,
        source_url=source_url,
        source_revision=revision_id,
        source_timestamp=timestamp,
        recipe_rarity=infer_recipe_rarity(categories, text),
        set_name=set_name,
        levels=[levels_by_number[k] for k in sorted(levels_by_number)],
        categories=categories,
        notes=notes,
    )


def _find_salvage_cell(cells: list[Tag], resolver: SalvageResolver) -> Tag | None:
    """Find the ingredient cell without assuming a fixed table column.

    The Homecoming wiki's common-recipe templates have changed presentation
    details over time.  Crafting cost remains numeric, but the salvage cell can
    shift when headers/rowspans are rendered differently.  Prefer the cell that
    actually contains one or more links resolving to the finite 108-item
    invention-salvage vocabulary.
    """
    for cell in reversed(cells[1:]):
        for a in cell.find_all("a"):
            raw_name = _anchor_candidate_name(a)
            if raw_name and resolver.resolve(raw_name):
                return cell
    return None


def parse_common_io_page(
    *, html: str, source_title: str, source_url: str, revision_id: int | None,
    timestamp: str | None, resolver: SalvageResolver,
) -> list[Recipe]:
    soup = BeautifulSoup(html, "html.parser")
    recipes: list[Recipe] = []
    for heading in soup.find_all("h2"):
        name = clean_text(heading.get_text(" ", strip=True))
        if not name or name.lower() in {"navigation menu", "see also"}:
            continue
        table: Tag | None = None
        for el in heading.find_all_next():
            if isinstance(el, Tag) and el.name == "h2" and el is not heading:
                break
            if isinstance(el, Tag) and el.name == "table":
                txt = clean_text(el.get_text(" ", strip=True)).lower()
                if "level" in txt and "invention salvage" in txt:
                    table = el
                    break
        if table is None:
            continue

        levels: list[RecipeLevel] = []
        active_reqs: list[Requirement] = []
        notes: list[str] = []
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"], recursive=False)
            if len(cells) < 4:
                continue
            m = _LEVEL.match(clean_text(cells[0].get_text(" ", strip=True)))
            if not m:
                continue
            level = int(m.group(1))
            # Common IO tables normally render as level, purchase, resale,
            # crafting, memorized, salvage.  Do not hard-code the salvage
            # column: find it from known salvage links so template/layout
            # variations cannot silently erase an entire level band.
            crafting_costs = _cost_options(cells[3])
            salvage_cell = _find_salvage_cell(cells, resolver)
            if salvage_cell is not None:
                groups, warns = _ingredient_groups(salvage_cell, resolver)
                notes.extend(warns)
                if groups:
                    # Common recipes do not have alternate salvage choices.
                    active_reqs = groups[0]
            if not active_reqs:
                notes.append(f"No active salvage requirements at level {level}")
            levels.append(RecipeLevel(level=level, options=[CraftOption(
                crafting_costs[0] if crafting_costs else None,
                list(active_reqs),
            )]))

        if levels:
            canonical_common_name = "Healing/Absorb" if name.casefold() == "healing" else name
            recipes.append(Recipe(
                name=f"Invention: {canonical_common_name}",
                recipe_type="common_io",
                source_title=source_title,
                source_url=source_url,
                source_revision=revision_id,
                source_timestamp=timestamp,
                recipe_rarity="common",
                levels=levels,
                categories=[],
                notes=notes,
            ))
    return recipes
