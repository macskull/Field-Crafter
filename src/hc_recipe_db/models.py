from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(slots=True)
class Salvage:
    name: str
    rarity: str
    level_tier: str
    origin: str
    wiki_title: str | None = None
    wiki_url: str | None = None


@dataclass(slots=True)
class Requirement:
    salvage_name: str
    quantity: int
    raw_name: str | None = None


@dataclass(slots=True)
class CraftOption:
    crafting_cost: int | None
    requirements: list[Requirement] = field(default_factory=list)


@dataclass(slots=True)
class RecipeLevel:
    level: int
    options: list[CraftOption] = field(default_factory=list)


@dataclass(slots=True)
class Recipe:
    name: str
    recipe_type: str
    source_title: str
    source_url: str
    source_revision: int | None
    source_timestamp: str | None
    recipe_rarity: str | None = None
    set_name: str | None = None
    levels: list[RecipeLevel] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def min_level(self) -> Optional[int]:
        return min((x.level for x in self.levels), default=None)

    @property
    def max_level(self) -> Optional[int]:
        return max((x.level for x in self.levels), default=None)
