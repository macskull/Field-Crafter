from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from typing import Any

from .calculator import CalculationError, format_text_result
from .game_memory import (
    GameInventoryReader, GameMemoryError, MemoryNameResolver,
    list_city_of_heroes_processes, review_from_memory_snapshot,
    refresh_memory_recipe_aliases,
)
from .recognition import calculate_review, load_review, scan_screenshots
from .updates import (
    UpdateCandidate, accept_update, build_update_candidate, database_info,
    format_update_diff, reject_update,
)


def _is_common_recipe_name(value: str) -> bool:
    return str(value).strip().startswith("Invention:")


_RARITY_ORDER = {"common": 0, "uncommon": 1, "rare": 2}


def _shopping_sort_key(row: dict[str, Any], mode: str):
    name = str(row.get("salvage") or "")
    qty = int(row.get("buy") or row.get("quantity") or row.get("surplus") or 0)
    rarity = str(row.get("rarity") or "").casefold()
    mode = str(mode or "Name").casefold()
    if mode == "quantity":
        return (-qty, name.casefold())
    if mode == "rarity":
        return (_RARITY_ORDER.get(rarity, 99), name.casefold())
    return (name.casefold(),)


def _auction_search_batches(rows: list[dict[str, Any]], sort_mode: str = "Name", max_chars: int = 128) -> list[str]:
    """Build AH comma-search strings that display in the requested order.

    Homecoming displays comma-separated search results in reverse submission order, so
    each size-limited batch is reversed only after the user-facing sort and batching.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    usable = [dict(row) for row in rows if int(row.get("buy") or 0) > 0 and str(row.get("salvage") or "").strip()]
    usable.sort(key=lambda row: _shopping_sort_key(row, sort_mode))
    batches: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for row in usable:
        name = str(row.get("salvage") or "").strip()
        if len(name) > max_chars:
            raise ValueError(f"Salvage name exceeds the {max_chars}-character Auction House search limit: {name}")
        added = len(name) + (1 if current else 0)
        if current and current_len + added > max_chars:
            batches.append(current)
            current = [name]
            current_len = len(name)
        else:
            current.append(name)
            current_len += added
    if current:
        batches.append(current)
    return [",".join(reversed(batch)) for batch in batches]


def _fmt_conf(value: Any) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return ""


def _review_summary(review: dict[str, Any]) -> dict[str, int]:
    recipes = review.get("recipes") or []
    inventory = review.get("inventory") or {}
    selected = [r for r in recipes if isinstance(r, dict) and r.get("selected", True)]
    return {
        "recipe_rows": len(recipes),
        "selected_rows": len(selected),
        "crafts": sum(int(r.get("quantity") or 0) for r in selected),
        "salvage_types": len(inventory),
        "salvage_total": sum(int(v or 0) for v in inventory.values()),
    }


def _aggregate_salvage_meta(review: dict[str, Any]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in ((review.get("recognition") or {}).get("salvage") or []):
        if isinstance(item, dict) and item.get("salvage"):
            grouped[str(item["salvage"])].append(item)
    out: dict[str, dict[str, Any]] = {}
    for name, items in grouped.items():
        name_confs = [float(i.get("confidence") or 0.0) for i in items]
        qty_confs = [float(i.get("quantity_confidence") or 0.0) for i in items if i.get("quantity_confidence") is not None]
        name_conf = min(name_confs) if name_confs else 0.0
        qty_conf = min(qty_confs) if qty_confs else None
        overall_conf = min(name_conf, qty_conf) if qty_conf is not None else name_conf
        out[name] = {
            "rarity": next((i.get("rarity") for i in items if i.get("rarity")), ""),
            "confidence": overall_conf,
            "name_confidence": name_conf,
            "quantity_confidence": qty_conf,
            "needs_review": any(bool(i.get("needs_review")) for i in items),
            "raw_text": " | ".join(str(i.get("raw_text") or "") for i in items if i.get("raw_text")),
            "items": items,
        }
    return out


def _parse_geometry_string(value: str) -> tuple[int, int, int, int] | None:
    m = re.match(r"^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$", str(value).strip())
    if not m:
        return None
    try:
        return tuple(int(x) for x in m.groups())  # type: ignore[return-value]
    except ValueError:
        return None


def _resource_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parents[2]


def _resource_path(*parts: str) -> Path:
    return _resource_root().joinpath(*parts)


class CraftingHelperGUI:
    def __init__(self, root, *, db_path: str | Path = "data/homecoming_recipes.sqlite"):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.db_path = Path(db_path)
        self.review: dict[str, Any] | None = None
        self.last_result: dict[str, Any] | None = None
        self.recipe_images: list[str] = []
        self.salvage_images: list[str] = []
        self.recipe_catalog: list[str] = []
        self.recipe_levels: dict[str, list[int]] = {}
        self.salvage_catalog: list[str] = []
        self.salvage_rarity: dict[str, str] = {}
        self.pending_update: UpdateCandidate | None = None
        self.game_processes = []
        self._game_process_by_label = {}
        self._dnd_available = False
        self._scan_progress_var = None
        self._scan_feedback_var = None
        self._app_state_dir = self._determine_app_state_dir()
        self._window_state_path = self._app_state_dir / "window_state.json"
        self._memory_alias_path = self._app_state_dir / "memory_recipe_aliases.json"
        self._result_sort_state: dict[tuple[int, str], bool] = {}
        self._auction_batches: list[str] = []
        self._update_cancel_event: threading.Event | None = None
        self._update_started_at: float | None = None

        self.root.title("Field Crafter 1.15")
        self.root.minsize(1040, 600)
        try:
            # The side-by-side Review & Edit layout no longer needs the old tall
            # working area. Start at the minimum supported height so the four tabs
            # feel compact while still leaving the user free to enlarge the window.
            self.root.geometry("1220x600")
        except Exception:
            pass
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._apply_window_icon()
        self.root.after(40, self._restore_window_geometry)

        self._load_catalogs()
        self._build_ui()
        self._update_input_lists()
        self._refresh_database_info()
        self._set_status("Read inventory from a running City of Heroes client, or use screenshots/OCR as a fallback.")
        self.root.after(150, self._refresh_game_processes)

    def _apply_window_icon(self) -> None:
        try:
            icon_ico = _resource_path("assets", "field_crafter.ico")
            icon_png = _resource_path("assets", "field_crafter_icon_transparent.png")
            # On Windows, prefer the native multi-resolution ICO. Calling iconphoto()
            # afterward can cause Tk/Windows to resample one large PNG for the taskbar,
            # which looks noticeably softer at 16/24/32 px.
            if os.name == "nt" and icon_ico.exists():
                try:
                    self.root.iconbitmap(default=str(icon_ico))
                    return
                except Exception:
                    pass
            if icon_png.exists():
                photo = self.tk.PhotoImage(file=str(icon_png))
                self._icon_photo = photo
                try:
                    self.root.iconphoto(True, photo)
                except Exception:
                    pass
        except Exception:
            pass

    def _connect_db(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            raise CalculationError(f"Recipe database does not exist: {self.db_path}")
        uri = f"file:{self.db_path.resolve().as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def _load_catalogs(self) -> None:
        with self._connect_db() as conn:
            rows = conn.execute("SELECT name FROM recipes ORDER BY name COLLATE NOCASE").fetchall()
            self.recipe_catalog = [str(r["name"]) for r in rows]
            level_rows = conn.execute(
                """
                SELECT r.name, rl.level
                FROM recipes r JOIN recipe_levels rl ON rl.recipe_id=r.id
                ORDER BY r.name COLLATE NOCASE, rl.level
                """
            ).fetchall()
            levels: dict[str, list[int]] = defaultdict(list)
            for row in level_rows:
                levels[str(row["name"])].append(int(row["level"]))
            self.recipe_levels = dict(levels)
            sal = conn.execute("SELECT name, rarity FROM salvage ORDER BY name COLLATE NOCASE").fetchall()
            self.salvage_catalog = [str(r["name"]) for r in sal]
            self.salvage_rarity = {str(r["name"]): str(r["rarity"]) for r in sal}

    def _build_ui(self) -> None:
        tk, ttk = self.tk, self.ttk
        outer = ttk.Frame(self.root, padding=8)
        outer.pack(fill="both", expand=True)

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)
        self.input_tab = ttk.Frame(self.notebook, padding=10)
        self.review_tab = ttk.Frame(self.notebook, padding=10)
        self.result_tab = ttk.Frame(self.notebook, padding=10)
        self.database_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.input_tab, text="1. Inventory Input")
        self.notebook.add(self.review_tab, text="2. Review & Edit")
        self.notebook.add(self.result_tab, text="3. Shopping List")
        self.notebook.add(self.database_tab, text="Database")

        self._build_input_tab()
        self._build_review_tab()
        self._build_result_tab()
        self._build_database_tab()

        status_frame = ttk.Frame(outer, padding=(0, 6, 0, 0))
        status_frame.pack(fill="x")
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(status_frame, textvariable=self.status_var, anchor="w").pack(side="left", fill="x", expand=True)
        # Progress is contextual in 1.15: keep the widget available, but do not
        # reserve footer space for it while the application is idle.
        self.progress = ttk.Progressbar(status_frame, mode="indeterminate", length=170)

    def _show_progress(self, *, determinate: bool = False, maximum: int = 100, value: int = 0) -> None:
        try:
            self.progress.stop()
        except Exception:
            pass
        mode = "determinate" if determinate else "indeterminate"
        self.progress.configure(mode=mode, maximum=max(1, int(maximum)), value=max(0, int(value)))
        if not self.progress.winfo_manager():
            self.progress.pack(side="right", padx=(8, 0))
        if not determinate:
            self.progress.start(10)

    def _update_progress(self, value: int, maximum: int | None = None) -> None:
        if maximum is not None:
            self.progress.configure(maximum=max(1, int(maximum)))
        self.progress.configure(value=max(0, int(value)))

    def _hide_progress(self) -> None:
        try:
            self.progress.stop()
        finally:
            if self.progress.winfo_manager():
                self.progress.pack_forget()

    def _build_input_tab(self) -> None:
        tk, ttk = self.tk, self.ttk
        tab = self.input_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)

        memory_frame = ttk.LabelFrame(tab, text="Read directly from the running game", padding=10)
        memory_frame.grid(row=0, column=0, sticky="ew")
        memory_frame.columnconfigure(1, weight=1)

        ttk.Label(memory_frame, text="Character:").grid(row=0, column=0, sticky="w")
        self.memory_process_var = tk.StringVar(value="")
        self.memory_process_combo = ttk.Combobox(
            memory_frame, textvariable=self.memory_process_var, state="readonly", width=55
        )
        self.memory_process_combo.grid(row=0, column=1, sticky="ew", padx=(8, 8))
        ttk.Button(memory_frame, text="Refresh", command=self._refresh_game_processes).grid(row=0, column=2, padx=(0, 8))
        self.memory_button = ttk.Button(memory_frame, text="Read inventory", command=self._start_memory_read)
        self.memory_button.grid(row=0, column=3, sticky="e")

        summary_row = ttk.Frame(memory_frame)
        summary_row.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(9, 0))
        self.memory_recipe_count_var = tk.StringVar(value="Recipes: Not read")
        self.memory_salvage_count_var = tk.StringVar(value="Salvage: Not read")
        self.input_db_status_var = tk.StringVar(value="Database: Loading...")
        ttk.Label(summary_row, textvariable=self.memory_recipe_count_var).pack(side="left")
        ttk.Label(summary_row, textvariable=self.memory_salvage_count_var).pack(side="left", padx=(24, 0))
        ttk.Label(summary_row, textvariable=self.input_db_status_var).pack(side="left", padx=(24, 0))

        self.memory_status_var = tk.StringVar(
            value="Choose a logged-in character. Field Crafter reads recipe and salvage inventory directly from cityofheroes.exe."
        )
        ttk.Label(memory_frame, textvariable=self.memory_status_var, wraplength=1050, justify="left").grid(
            row=2, column=0, columnspan=4, sticky="ew", pady=(7, 0)
        )

        utility_row = ttk.Frame(tab)
        utility_row.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self.ocr_toggle_button = ttk.Button(
            utility_row, text="Hide screenshot / OCR fallback", command=self._toggle_ocr_fallback
        )
        self.ocr_toggle_button.pack(side="left")
        ttk.Button(utility_row, text="Load saved review...", command=self._load_review_dialog).pack(side="left", padx=(8, 0))
        ttk.Label(
            utility_row, text="Use OCR only when direct game-memory reading is unavailable."
        ).pack(side="left", padx=(12, 0))

        fallback = ttk.LabelFrame(tab, text="Screenshots / OCR fallback", padding=8)
        self.ocr_fallback_frame = fallback
        fallback.grid(row=2, column=0, sticky="nsew", pady=(10, 0))
        fallback.columnconfigure(0, weight=1)
        fallback.rowconfigure(1, weight=1)

        self.input_help_var = tk.StringVar(
            value=(
                "Drop screenshots into either pane, paste directly from the clipboard after Print Screen, "
                "or use Add. OCR runs locally; nothing is uploaded."
            )
        )
        ttk.Label(fallback, textvariable=self.input_help_var, wraplength=1050).grid(
            row=0, column=0, sticky="ew", pady=(0, 8)
        )

        panes = ttk.Panedwindow(fallback, orient="horizontal")
        panes.grid(row=1, column=0, sticky="nsew")

        recipe_box = ttk.LabelFrame(panes, text="Recipe screenshots - drop files here", padding=8)
        salvage_box = ttk.LabelFrame(panes, text="Salvage screenshots - drop files here", padding=8)
        panes.add(recipe_box, weight=1)
        panes.add(salvage_box, weight=1)
        for box in (recipe_box, salvage_box):
            box.rowconfigure(0, weight=1)
            box.columnconfigure(0, weight=1)

        self.recipe_list = tk.Listbox(recipe_box, selectmode="extended", height=10)
        self.salvage_list = tk.Listbox(salvage_box, selectmode="extended", height=10)
        self.recipe_list.grid(row=0, column=0, columnspan=4, sticky="nsew")
        self.salvage_list.grid(row=0, column=0, columnspan=4, sticky="nsew")

        ttk.Button(recipe_box, text="Add...", command=self._add_recipe_images).grid(row=1, column=0, sticky="w", pady=(7, 0))
        ttk.Button(recipe_box, text="Paste clipboard", command=lambda: self._paste_clipboard("recipe")).grid(row=1, column=1, padx=5, pady=(7, 0))
        ttk.Button(recipe_box, text="Remove", command=lambda: self._remove_selected_images("recipe")).grid(row=1, column=2, pady=(7, 0))
        ttk.Button(recipe_box, text="Clear", command=lambda: self._clear_images("recipe")).grid(row=1, column=3, sticky="e", pady=(7, 0))

        ttk.Button(salvage_box, text="Add...", command=self._add_salvage_images).grid(row=1, column=0, sticky="w", pady=(7, 0))
        ttk.Button(salvage_box, text="Paste clipboard", command=lambda: self._paste_clipboard("salvage")).grid(row=1, column=1, padx=5, pady=(7, 0))
        ttk.Button(salvage_box, text="Remove", command=lambda: self._remove_selected_images("salvage")).grid(row=1, column=2, pady=(7, 0))
        ttk.Button(salvage_box, text="Clear", command=lambda: self._clear_images("salvage")).grid(row=1, column=3, sticky="e", pady=(7, 0))

        self.recipe_list.bind("<Control-v>", lambda e: (self._paste_clipboard("recipe"), "break")[1])
        self.salvage_list.bind("<Control-v>", lambda e: (self._paste_clipboard("salvage"), "break")[1])
        self._enable_drop_target(self.recipe_list, "recipe")
        self._enable_drop_target(self.salvage_list, "salvage")

        controls = ttk.Frame(fallback, padding=(0, 10, 0, 0))
        controls.grid(row=2, column=0, sticky="ew")
        ttk.Label(controls, text="OCR backend:").pack(side="left")
        self.backend_var = tk.StringVar(value="auto")
        ttk.Combobox(
            controls, textvariable=self.backend_var, values=("auto", "rapidocr", "tesseract"),
            width=12, state="readonly"
        ).pack(side="left", padx=(6, 16))
        self.scan_button = ttk.Button(controls, text="Scan screenshots", command=self._start_scan)
        self.scan_button.pack(side="left")

        self._scan_feedback_var = tk.StringVar(value="")
        ttk.Label(fallback, textvariable=self._scan_feedback_var, anchor="w").grid(row=3, column=0, sticky="ew", pady=(8, 0))

        # Keep the OCR fallback visible on first open so screenshot users can see
        # the complete input workflow immediately. The same button can collapse it
        # whenever the extra workspace is not needed.
        self._ocr_fallback_expanded = True

    def _toggle_ocr_fallback(self) -> None:
        self._ocr_fallback_expanded = not bool(getattr(self, "_ocr_fallback_expanded", False))
        if self._ocr_fallback_expanded:
            self.ocr_fallback_frame.grid()
            self.ocr_toggle_button.configure(text="Hide screenshot / OCR fallback")
        else:
            self.ocr_fallback_frame.grid_remove()
            self.ocr_toggle_button.configure(text="Show screenshot / OCR fallback")

    def _build_review_tab(self) -> None:
        tk, ttk = self.tk, self.ttk
        tab = self.review_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        review_header = ttk.Frame(tab)
        review_header.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        review_header.columnconfigure(0, weight=1)
        self.review_summary_var = tk.StringVar(value="No inventory loaded.")
        ttk.Label(review_header, textvariable=self.review_summary_var).grid(row=0, column=0, sticky="ew")
        self.review_notice_var = tk.StringVar(value="")
        warning_style = ttk.Style()
        warning_style.configure("ReviewWarning.TLabel", foreground="#b00020")
        self.review_notice_label = ttk.Label(
            review_header, textvariable=self.review_notice_var, style="ReviewWarning.TLabel"
        )

        paned = ttk.Panedwindow(tab, orient="horizontal")
        paned.grid(row=1, column=0, sticky="nsew")
        self.review_paned = paned
        self._review_sash_initialized = False
        paned.bind("<Map>", self._balance_review_panes_once, add="+")

        recipe_pane = ttk.Frame(paned)
        salvage_pane = ttk.Frame(paned)
        paned.add(recipe_pane, weight=11)
        paned.add(salvage_pane, weight=9)

        recipe_pane.columnconfigure(0, weight=1)
        recipe_pane.rowconfigure(0, weight=1)
        recipe_frame = ttk.LabelFrame(recipe_pane, text="Recipes", padding=5)
        recipe_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        recipe_frame.rowconfigure(0, weight=1)
        recipe_frame.columnconfigure(0, weight=1)
        self.recipe_tree = ttk.Treeview(
            recipe_frame,
            columns=("selected", "recipe", "qty", "level", "recipe_full"),
            displaycolumns=("selected", "recipe", "qty", "level"),
            show="headings", selectmode="browse", height=15,
        )
        # Source/OCR audit information lives in the selected-row details panel.
        # Rows requiring manual review are highlighted instead of consuming a Flag
        # column. A hidden full-name value lets the visible Recipe column use a
        # graceful ellipsis without losing the canonical recipe name.
        cols = {
            "selected": ("Craft?", 50, 44, "center", False),
            "recipe": ("Recipe", 390, 180, "w", True),
            "qty": ("Qty", 42, 38, "center", False),
            "level": ("Level", 50, 44, "center", False),
            "recipe_full": ("", 0, 0, "w", False),
        }
        for key, (label, width, minwidth, anchor, stretch) in cols.items():
            self.recipe_tree.heading(key, text=label)
            self.recipe_tree.column(
                key, width=width, minwidth=minwidth, anchor=anchor, stretch=stretch
            )
        self.recipe_tree.grid(row=0, column=0, sticky="nsew")
        rs = ttk.Scrollbar(recipe_frame, orient="vertical", command=self.recipe_tree.yview)
        rs.grid(row=0, column=1, sticky="ns")
        self.recipe_tree.configure(yscrollcommand=rs.set)
        self.recipe_tree.tag_configure("needs_review", foreground="#a40000", background="#ffe8e8")
        self.recipe_tree.bind("<Button-1>", self._recipe_tree_single_click)
        self.recipe_tree.bind("<Double-1>", self._edit_recipe_cell)
        self.recipe_tree.bind("<<TreeviewSelect>>", lambda e: self._show_recipe_details())
        self.recipe_tree.bind("<Configure>", self._schedule_recipe_display_refresh, add="+")
        self.recipe_tree.bind("<Motion>", self._recipe_tree_hover, add="+")
        self.recipe_tree.bind("<Leave>", lambda e: self._hide_recipe_tooltip(), add="+")
        self.recipe_tree.bind("<ButtonPress>", lambda e: self._hide_recipe_tooltip(), add="+")

        rbuttons = ttk.Frame(recipe_pane, padding=(0, 5, 4, 4))
        rbuttons.grid(row=1, column=0, sticky="ew")
        ttk.Button(rbuttons, text="Add recipe", command=self._add_recipe_row).pack(side="left")
        ttk.Button(rbuttons, text="Remove", command=self._remove_recipe_row).pack(side="left", padx=(5, 0))
        self.all_recipe_button_var = tk.StringVar(value="Select all")
        self.all_recipe_button = ttk.Button(
            rbuttons, textvariable=self.all_recipe_button_var, command=self._toggle_all_recipe_selection
        )
        self.all_recipe_button.pack(side="left", padx=(8, 0))
        self.common_recipe_button_var = tk.StringVar(value="Select commons")
        self.common_recipe_button = ttk.Button(
            rbuttons, textvariable=self.common_recipe_button_var, command=self._toggle_common_recipe_selection
        )
        self.common_recipe_button.pack(side="left", padx=(5, 0))

        # Give both side-by-side detail panels the same fixed working height. This
        # keeps the Recipes and Salvage table bottoms aligned while still allowing
        # long audit text to wrap within its own pane instead of overrunning it.
        detail_frame = ttk.LabelFrame(recipe_pane, text="Selected recipe details", padding=5, height=78)
        detail_frame.grid(row=2, column=0, sticky="ew", padx=(0, 4), pady=(0, 2))
        detail_frame.pack_propagate(False)
        self.recipe_detail_var = tk.StringVar(value="Select a recipe row to see details.")
        self.recipe_detail_label = ttk.Label(
            detail_frame, textvariable=self.recipe_detail_var, justify="left", anchor="nw"
        )
        self.recipe_detail_label.pack(fill="both", expand=True)
        detail_frame.bind(
            "<Configure>",
            lambda e: self.recipe_detail_label.configure(wraplength=max(120, e.width - 18)),
            add="+",
        )

        salvage_pane.columnconfigure(0, weight=1)
        salvage_pane.rowconfigure(0, weight=1)
        salvage_frame = ttk.LabelFrame(salvage_pane, text="Salvage inventory", padding=5)
        salvage_frame.grid(row=0, column=0, sticky="nsew", padx=(4, 0))
        salvage_frame.rowconfigure(0, weight=1)
        salvage_frame.columnconfigure(0, weight=1)
        self.salvage_tree = ttk.Treeview(
            salvage_frame,
            columns=("salvage", "qty", "rarity"),
            show="headings", selectmode="browse", height=15,
        )
        # OCR confidence and source audit information are shown only in the selected
        # salvage details panel. Rows needing manual review are highlighted in red.
        scols = {
            "salvage": ("Salvage", 230, 140, "w", True),
            "qty": ("Qty", 50, 42, "center", False),
            "rarity": ("Rarity", 100, 92, "center", False),
        }
        for key, (label, width, minwidth, anchor, stretch) in scols.items():
            self.salvage_tree.heading(key, text=label)
            self.salvage_tree.column(
                key, width=width, minwidth=minwidth, anchor=anchor, stretch=stretch
            )
        self.salvage_tree.grid(row=0, column=0, sticky="nsew")
        ss = ttk.Scrollbar(salvage_frame, orient="vertical", command=self.salvage_tree.yview)
        ss.grid(row=0, column=1, sticky="ns")
        self.salvage_tree.configure(yscrollcommand=ss.set)
        self.salvage_tree.tag_configure("needs_review", foreground="#a40000", background="#ffe8e8")
        self.salvage_tree.bind("<Double-1>", self._edit_salvage_cell)
        self.salvage_tree.bind("<<TreeviewSelect>>", lambda e: self._show_salvage_details())

        sb = ttk.Frame(salvage_pane, padding=(4, 5, 0, 4))
        sb.grid(row=1, column=0, sticky="ew")
        ttk.Button(sb, text="Add salvage", command=self._add_salvage_row).pack(side="left")
        ttk.Button(sb, text="Remove", command=self._remove_salvage_row).pack(side="left", padx=(5, 0))

        sdetail_frame = ttk.LabelFrame(salvage_pane, text="Selected salvage details", padding=5, height=78)
        sdetail_frame.grid(row=2, column=0, sticky="ew", padx=(4, 0), pady=(0, 2))
        sdetail_frame.pack_propagate(False)
        self.salvage_detail_var = tk.StringVar(value="Select a salvage row to see details.")
        self.salvage_detail_label = ttk.Label(
            sdetail_frame, textvariable=self.salvage_detail_var, justify="left", anchor="nw"
        )
        self.salvage_detail_label.pack(fill="both", expand=True)
        sdetail_frame.bind(
            "<Configure>",
            lambda e: self.salvage_detail_label.configure(wraplength=max(120, e.width - 18)),
            add="+",
        )

        options = ttk.LabelFrame(tab, text="Capacity, disposal policy, and confirmation", padding=8)
        options.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(options, text="Salvage used:").grid(row=0, column=0, sticky="w")
        self.used_var = tk.StringVar()
        self.capacity_var = tk.StringVar()
        self.used_entry = ttk.Entry(options, textvariable=self.used_var, width=8)
        self.used_entry.grid(row=0, column=1, sticky="w", padx=(5, 14))
        ttk.Label(options, text="Capacity:").grid(row=0, column=2, sticky="w")
        self.capacity_entry = ttk.Entry(options, textvariable=self.capacity_var, width=8)
        self.capacity_entry.grid(row=0, column=3, sticky="w", padx=(5, 18))
        self.capacity_source_var = tk.StringVar(value="")
        ttk.Label(options, textvariable=self.capacity_source_var).grid(row=0, column=4, sticky="w", padx=(0, 24))

        ttk.Label(options, text="Safe disposal:").grid(row=0, column=5, sticky="w")
        self.common_var = tk.BooleanVar(value=True)
        self.uncommon_var = tk.BooleanVar(value=False)
        self.rare_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(options, text="Common", variable=self.common_var).grid(row=0, column=6, sticky="w", padx=(6, 0))
        ttk.Checkbutton(options, text="Uncommon", variable=self.uncommon_var).grid(row=0, column=7, sticky="w", padx=(6, 0))
        ttk.Checkbutton(options, text="Rare", variable=self.rare_var).grid(row=0, column=8, sticky="w", padx=(6, 0))

        self.confirm_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            options,
            text="I have reviewed the detected recipes, quantities, levels, salvage inventory, and capacity.",
            variable=self.confirm_var,
        ).grid(row=1, column=0, columnspan=9, sticky="w", pady=(8, 0))

        action = ttk.Frame(tab, padding=(0, 8, 0, 0))
        action.grid(row=3, column=0, sticky="ew")
        ttk.Button(action, text="Save review...", command=self._save_review_dialog).pack(side="left")
        ttk.Button(action, text="Calculate shopping list", command=self._calculate_from_gui).pack(side="right")

    def _build_result_tab(self) -> None:
        tk, ttk = self.tk, self.ttk
        tab = self.result_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        self.result_summary_var = tk.StringVar(value="No calculation yet.")
        ttk.Label(tab, textvariable=self.result_summary_var, wraplength=1100, justify="left").grid(
            row=0, column=0, sticky="ew", pady=(0, 8)
        )

        vertical = ttk.Panedwindow(tab, orient="vertical")
        vertical.grid(row=1, column=0, sticky="nsew")
        compact_pane = ttk.Frame(vertical)
        text_pane = ttk.Frame(vertical)
        vertical.add(compact_pane, weight=2)
        vertical.add(text_pane, weight=1)
        compact_pane.columnconfigure(0, weight=1)
        compact_pane.rowconfigure(0, weight=1)

        compact = ttk.Panedwindow(compact_pane, orient="horizontal")
        compact.grid(row=0, column=0, sticky="nsew")
        buy_frame, self.buy_tree = self._make_result_tree(compact, "BUY")
        dispose_frame, self.dispose_tree = self._make_result_tree(compact, "SELL / DELETE TO MAKE ROOM")
        surplus_frame, self.surplus_tree = self._make_result_tree(compact, "OTHER SURPLUS (NOT REQUIRED)")
        compact.add(buy_frame, weight=1)
        compact.add(dispose_frame, weight=1)
        compact.add(surplus_frame, weight=1)

        # The Auction House comma-search helper is intentionally not exposed in
        # 1.14. Homecoming returns partial matches for each comma-separated term,
        # which makes the generated result list too noisy for the intended workflow.
        # The batching/sorting helper code remains available for future reuse.

        text_frame = ttk.LabelFrame(text_pane, text="Full result", padding=5)
        text_frame.pack(fill="both", expand=True)
        text_frame.rowconfigure(0, weight=1)
        text_frame.columnconfigure(0, weight=1)
        self.result_text = tk.Text(text_frame, wrap="none", height=14)
        self.result_text.grid(row=0, column=0, sticky="nsew")
        ys = ttk.Scrollbar(text_frame, orient="vertical", command=self.result_text.yview)
        ys.grid(row=0, column=1, sticky="ns")
        xs = ttk.Scrollbar(text_frame, orient="horizontal", command=self.result_text.xview)
        xs.grid(row=1, column=0, sticky="ew")
        self.result_text.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)

        buttons = ttk.Frame(tab, padding=(0, 8, 0, 0))
        buttons.grid(row=2, column=0, sticky="ew")
        ttk.Button(buttons, text="Save result text...", command=self._save_result_text).pack(side="left")
        ttk.Button(buttons, text="Save result JSON...", command=self._save_result_json).pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="Back to review", command=lambda: self.notebook.select(self.review_tab)).pack(side="right")

    def _balance_review_panes_once(self, _event=None) -> None:
        """Give Review & Edit enough width on both sides the first time it is shown.

        Ttk Panedwindow initially sizes panes partly from each child's requested
        width. The recipe table naturally requests more width, which can starve the
        salvage table at the application's minimum size. Start near 52/48 instead,
        then leave the sash entirely under user control.
        """
        if getattr(self, "_review_sash_initialized", False):
            return
        try:
            width = int(self.review_paned.winfo_width())
            if width < 300:
                self.root.after(25, self._balance_review_panes_once)
                return
            self.review_paned.sashpos(0, int(width * 0.52))
            self._review_sash_initialized = True
        except Exception:
            pass

    def _make_result_tree(self, parent, title: str):
        ttk = self.ttk
        frame = ttk.LabelFrame(parent, text=title, padding=5)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        tree = ttk.Treeview(frame, columns=("qty", "salvage", "rarity"), show="headings", height=12)
        tree.heading("qty", text="Qty", command=lambda t=tree: self._sort_result_tree(t, "qty"))
        tree.heading("salvage", text="Salvage", command=lambda t=tree: self._sort_result_tree(t, "salvage"))
        tree.heading("rarity", text="Rarity", command=lambda t=tree: self._sort_result_tree(t, "rarity"))
        # Keep all three columns visible even when the application is at its
        # minimum width. The name column stays bounded and Rarity receives spare
        # width, so longer rarity values are never clipped.
        tree.column("qty", width=40, minwidth=36, anchor="center", stretch=False)
        # Keep the salvage-name field compact instead of allowing it to absorb all
        # spare pane width. Rarity gets the flexible remainder so full values such
        # as "Uncommon" remain visible in BUY, SELL, and surplus at minimum width.
        tree.column("salvage", width=145, minwidth=100, anchor="w", stretch=False)
        tree.column("rarity", width=84, minwidth=80, anchor="center", stretch=True)
        tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        sb.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=sb.set)
        return frame, tree

    def _sort_result_tree(self, tree, column: str) -> None:
        state_key = (id(tree), column)
        ascending = not self._result_sort_state.get(state_key, False)
        self._result_sort_state[state_key] = ascending

        def value_key(iid):
            raw = tree.set(iid, column)
            if column == "qty":
                try:
                    return int(raw)
                except (TypeError, ValueError):
                    return 0
            if column == "rarity":
                return (_RARITY_ORDER.get(str(raw).casefold(), 99), str(raw).casefold())
            return str(raw).casefold()

        rows = list(tree.get_children(""))
        rows.sort(key=value_key, reverse=not ascending)
        for index, iid in enumerate(rows):
            tree.move(iid, "", index)

    def _refresh_auction_searches(self) -> None:
        if not hasattr(self, "ah_batches_frame"):
            return
        for child in self.ah_batches_frame.winfo_children():
            child.destroy()
        if not self.last_result:
            self._auction_batches = []
            self.ah_summary_var.set("Calculate a shopping list to generate Auction House search batches.")
            return
        rows = list(self.last_result.get("shopping_list") or [])
        try:
            batches = _auction_search_batches(rows, self.ah_sort_var.get(), 128)
        except ValueError as exc:
            self._auction_batches = []
            self.ah_summary_var.set(str(exc))
            return
        self._auction_batches = batches
        if not batches:
            self.ah_summary_var.set("Nothing needs to be purchased, so no Auction House search is needed.")
            return
        self.ah_summary_var.set(
            f"{len(batches)} search batch{'es' if len(batches) != 1 else ''}. "
            "Each string is at most 128 characters and is reversed so the Auction House displays the selected sort order top-to-bottom."
        )
        for idx, text in enumerate(batches):
            row = self.ttk.Frame(self.ah_batches_frame)
            row.grid(row=idx, column=0, columnspan=3, sticky="ew", pady=(0 if idx == 0 else 4, 0))
            row.columnconfigure(1, weight=1)
            self.ttk.Label(row, text=f"Batch {idx + 1} of {len(batches)} - {len(text)}/128:").grid(
                row=0, column=0, sticky="w", padx=(0, 7)
            )
            var = self.tk.StringVar(value=text)
            entry = self.ttk.Entry(row, textvariable=var, state="readonly")
            entry.grid(row=0, column=1, sticky="ew")
            self.ttk.Button(row, text="Copy", command=lambda i=idx: self._copy_auction_batch(i)).grid(
                row=0, column=2, sticky="e", padx=(7, 0)
            )

    def _copy_auction_batch(self, index: int) -> None:
        if index < 0 or index >= len(self._auction_batches):
            return
        text = self._auction_batches[index]
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update_idletasks()
        self._set_status(f"Copied Auction House batch {index + 1} of {len(self._auction_batches)} to the clipboard.")

    def _build_database_tab(self) -> None:
        tk, ttk = self.tk, self.ttk
        tab = self.database_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)

        info = ttk.LabelFrame(tab, text="Local database", padding=10)
        info.grid(row=0, column=0, sticky="ew")
        info.columnconfigure(1, weight=1)
        self.db_effective_var = tk.StringVar(value="")
        self.db_built_var = tk.StringVar(value="")
        self.db_revision_var = tk.StringVar(value="")
        self.db_counts_var = tk.StringVar(value="")
        self.db_release_var = tk.StringVar(value="")
        labels = [
            ("Effective date:", self.db_effective_var),
            ("Built/checked:", self.db_built_var),
            ("Newest included Wiki revision:", self.db_revision_var),
            ("Contents:", self.db_counts_var),
            ("Release data:", self.db_release_var),
        ]
        for row, (label, var) in enumerate(labels):
            ttk.Label(info, text=label).grid(row=row, column=0, sticky="nw", padx=(0, 12), pady=2)
            ttk.Label(info, textvariable=var).grid(row=row, column=1, sticky="nw", pady=2)

        controls = ttk.LabelFrame(tab, text="Database updates", padding=10)
        controls.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        controls.columnconfigure(0, weight=1)
        ttk.Label(
            controls,
            text=(
                "Scan Homecoming Wiki for a fresh database snapshot. The current database is left untouched while "
                "the candidate is built and validated. If crafting data changed, you can inspect differences and "
                "explicitly accept or reject the update."
            ),
            wraplength=1050, justify="left",
        ).grid(row=0, column=0, sticky="ew")
        button_row = ttk.Frame(controls)
        button_row.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self.update_button = ttk.Button(button_row, text="Scan for database changes", command=self._start_update_check)
        self.update_button.pack(side="left")
        self.cancel_update_button = ttk.Button(
            button_row, text="Cancel update", command=self._cancel_update_check, state="disabled"
        )
        self.cancel_update_button.pack(side="left", padx=(8, 0))
        self.memory_map_button = ttk.Button(
            button_row, text="Refresh game memory recipe map", command=self._start_memory_map_refresh
        )
        self.memory_map_button.pack(side="left", padx=(8, 0))
        ttk.Label(
            controls,
            text=(
                "The memory-recipe map translates the game's internal recipe IDs into Field Crafter names. "
                "Field Crafter also remembers unresolved mappings you correct manually in Review & Edit."
            ),
            wraplength=1050, justify="left",
        ).grid(row=2, column=0, sticky="ew", pady=(8, 0))

        log_frame = ttk.LabelFrame(tab, text="Last update scan", padding=5)
        self.update_log_frame = log_frame
        log_frame.grid(row=2, column=0, sticky="nsew", pady=(10, 0))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.update_text = tk.Text(log_frame, wrap="word", height=14)
        self.update_text.grid(row=0, column=0, sticky="nsew")
        update_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.update_text.yview)
        update_scroll.grid(row=0, column=1, sticky="ns")
        self.update_text.configure(yscrollcommand=update_scroll.set, state="disabled")
        # Do not devote most of the tab to an empty log. It opens automatically
        # whenever a database/map operation has something to report.
        log_frame.grid_remove()

    def _show_update_log(self) -> None:
        if hasattr(self, "update_log_frame") and not self.update_log_frame.winfo_manager():
            self.update_log_frame.grid()

    # ---------- database information / update flow ----------

    def _refresh_database_info(self) -> None:
        try:
            info = database_info(self.db_path)
        except Exception as exc:
            if hasattr(self, "db_effective_var"):
                self.db_effective_var.set("Unavailable")
                self.db_built_var.set(str(exc))
            return
        if hasattr(self, "db_effective_var"):
            self.db_effective_var.set(info.effective_date or "Unknown")
            self.db_built_var.set(info.built_at_utc or "Unknown")
            self.db_revision_var.set(info.latest_source_revision or "Unknown")
            self.db_counts_var.set(
                f"{info.recipe_count:,} recipes • {info.salvage_count:,} invention salvage • {info.source_count:,} source pages"
            )
            try:
                with self._connect_db() as conn:
                    meta = dict(conn.execute("SELECT key, value FROM metadata"))
                release_version = meta.get("field_crafter_release_version") or "unknown"
                validation = meta.get("field_crafter_release_validation") or "not stamped"
                self.db_release_var.set(f"Field Crafter {release_version} • {validation}")
            except Exception:
                self.db_release_var.set("Validation status unavailable")
        if hasattr(self, "input_db_status_var"):
            self.input_db_status_var.set(
                f"Database: {info.effective_date or 'Unknown'} • {info.recipe_count:,} recipes"
            )

    def _write_update_log(self, text: str) -> None:
        if not hasattr(self, "update_text"):
            return
        self._show_update_log()
        self.update_text.configure(state="normal")
        self.update_text.delete("1.0", "end")
        self.update_text.insert("1.0", text)
        self.update_text.see("end")
        self.update_text.configure(state="disabled")

    def _append_update_log(self, text: str) -> None:
        if not hasattr(self, "update_text"):
            return
        self._show_update_log()
        self.update_text.configure(state="normal")
        prior = self.update_text.get("1.0", "end-1c")
        if prior:
            self.update_text.insert("end", "\n" + text)
        else:
            self.update_text.insert("end", text)
        self.update_text.see("end")
        self.update_text.configure(state="disabled")

    def _start_memory_map_refresh(self) -> None:
        self.memory_map_button.configure(state="disabled")
        self._show_progress()
        self._set_status("Refreshing the game-memory recipe ID map from Homecoming Wiki...")
        self._write_update_log("Refreshing the game-memory recipe ID map from Homecoming Wiki...")

        def progress_cb(message: str) -> None:
            self.root.after(0, lambda m=message: self._set_status(m))
            self.root.after(0, lambda m=message: self._write_update_log(m))

        def worker():
            try:
                result = refresh_memory_recipe_aliases(
                    self.db_path, self._memory_alias_path, progress=progress_cb
                )
                self.root.after(0, lambda: self._memory_map_refresh_complete(result))
            except Exception as exc:
                # Transient Wiki failures are presented as a concise message. The full
                # Python traceback is intentionally not dumped into the user-facing UI.
                self.root.after(0, lambda d=str(exc): self._memory_map_refresh_failed(d))

        threading.Thread(target=worker, daemon=True).start()

    def _memory_map_refresh_complete(self, result) -> None:
        self._hide_progress()
        self.memory_map_button.configure(state="normal")
        text = (
            f"Game-memory recipe map refreshed.\n\n"
            f"Local database set coverage: {result.get('set_coverage', 'unknown')}\n"
            f"Sets represented by legacy Attrib index: {result.get('attrib_set_count', 0)} of {result.get('db_set_count', 0)}\n"
            f"Mapped internal set-piece IDs: {result.get('mapped_alias_count', 0)}\n"
            f"Supplemental aliases for DB sets missing from the index: {result.get('supplemental_alias_count', 0)}\n"
            f"Sets missing from the index: {', '.join(result.get('sets_missing_from_index', [])) or 'none'}\n"
            f"Set pages scraped: {', '.join(result.get('set_pages_scraped', [])) or 'none'}\n"
            f"Saved aliases before: {result.get('saved_aliases_before', 0)}\n"
            f"Saved aliases after: {result.get('saved_aliases_after', 0)}\n\n"
            f"{result.get('note', '')}"
        )
        self._write_update_log(text)
        self._set_status("Game-memory recipe ID map refreshed.")

    def _memory_map_refresh_failed(self, detail: str) -> None:
        self._hide_progress()
        self.memory_map_button.configure(state="normal")
        self._write_update_log("Game-memory recipe-map refresh failed.\n\n" + detail)
        self._set_status("Game-memory recipe-map refresh failed; existing aliases were left unchanged.")
        self._show_error(detail)

    def _start_update_check(self) -> None:
        if self.pending_update is not None:
            self._show_error("An update candidate is already awaiting review. Accept or reject it first.")
            return
        self._update_cancel_event = threading.Event()
        self._update_started_at = time.monotonic()
        self.update_button.configure(state="disabled")
        self.cancel_update_button.configure(state="normal")
        self._show_progress()
        self._write_update_log(
            "Building a candidate database from Homecoming Wiki.\n"
            "Unchanged recipe pages are reused from the persistent revision cache.\n"
            "Your current database will not be modified unless you explicitly accept the candidate."
        )
        self._set_status("Database update scan started.")

        def progress_cb(message: str) -> None:
            elapsed = int(time.monotonic() - (self._update_started_at or time.monotonic()))
            minutes, seconds = divmod(elapsed, 60)
            stamp = f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"
            text = f"[{stamp}] {message}"
            self.root.after(0, lambda m=message: self._set_status(f"Database update: {m}"))
            self.root.after(0, lambda t=text: self._append_update_log(t))

        def worker():
            try:
                event = self._update_cancel_event
                candidate = build_update_candidate(
                    self.db_path, progress=progress_cb,
                    cancel_check=(event.is_set if event is not None else None),
                )
                self.root.after(0, lambda: self._update_scan_complete(candidate))
            except Exception as exc:
                self.root.after(0, lambda d=str(exc): self._update_scan_failed(d))

        threading.Thread(target=worker, daemon=True).start()

    def _cancel_update_check(self) -> None:
        if self._update_cancel_event is None:
            return
        self._update_cancel_event.set()
        self.cancel_update_button.configure(state="disabled")
        self._set_status("Cancelling database update after the current request...")
        self._append_update_log("Cancellation requested. Waiting for the current bounded Wiki request to finish...")

    def _update_scan_complete(self, candidate: UpdateCandidate) -> None:
        self._hide_progress()
        self.update_button.configure(state="normal")
        self.cancel_update_button.configure(state="disabled")
        self._update_cancel_event = None
        self._update_started_at = None
        self.pending_update = candidate
        diff_text = format_update_diff(candidate.diff)
        build_val = candidate.build_result.get("validation") or {}
        candidate_info = candidate.diff.get("candidate") or {}
        log = (
            f"Candidate effective date: {candidate_info.get('effective_date') or 'Unknown'}\n"
            f"Validation: {build_val.get('pass', 0)} PASS, {build_val.get('warn', 0)} WARN, {build_val.get('error', 0)} ERROR\n\n"
            f"{diff_text}"
        )
        self._write_update_log(log)
        if not candidate.diff.get("changed"):
            reject_update(candidate)
            self.pending_update = None
            self._set_status("Database scan complete: no crafting-data changes found.")
            from tkinter import messagebox
            messagebox.showinfo(
                "Database Up to Date",
                "A fresh Homecoming Wiki snapshot was built and validated. No recipe or invention-salvage data changes were found, so the current database was left unchanged.",
                parent=self.root,
            )
            return
        self._set_status("Database changes found. Review and accept or reject the candidate update.")
        self._show_update_review(candidate)

    def _update_scan_failed(self, detail: str) -> None:
        self._hide_progress()
        self.update_button.configure(state="normal")
        self.cancel_update_button.configure(state="disabled")
        was_cancelled = "cancelled by user" in detail.casefold()
        self._update_cancel_event = None
        self._update_started_at = None
        if was_cancelled:
            self._append_update_log("Database update cancelled. The current database was not changed.")
            self._set_status("Database update cancelled; current database unchanged.")
            return
        self._append_update_log("Update scan failed. The current database was not changed.\n" + detail)
        self._set_status("Database update scan failed; current database unchanged.")
        self._show_error(detail)

    def _show_update_review(self, candidate: UpdateCandidate) -> None:
        tk, ttk = self.tk, self.ttk
        win = tk.Toplevel(self.root)
        win.title("Review Database Update")
        win.transient(self.root)
        win.minsize(760, 480)
        try:
            win.geometry("900x620")
        except Exception:
            pass
        win.columnconfigure(0, weight=1)
        win.rowconfigure(1, weight=1)

        old_info = candidate.diff.get("current") or {}
        new_info = candidate.diff.get("candidate") or {}
        ttk.Label(
            win,
            text=(
                f"Current effective date: {old_info.get('effective_date') or 'Unknown'}     "
                f"Candidate effective date: {new_info.get('effective_date') or 'Unknown'}"
            ),
        ).grid(row=0, column=0, sticky="ew", padx=10, pady=10)

        frame = ttk.Frame(win)
        frame.grid(row=1, column=0, sticky="nsew", padx=10)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        text = tk.Text(frame, wrap="word")
        text.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        sb.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=sb.set)
        text.insert("1.0", format_update_diff(candidate.diff, max_items=500))
        text.configure(state="disabled")

        buttons = ttk.Frame(win, padding=10)
        buttons.grid(row=2, column=0, sticky="ew")

        def reject():
            reject_update(candidate)
            self.pending_update = None
            win.destroy()
            self._set_status("Database update rejected; current database unchanged.")
            self._write_update_log(self.update_text.get("1.0", "end").strip() + "\n\nUPDATE REJECTED by user.")

        def accept():
            try:
                backup = accept_update(self.db_path, candidate)
            except Exception as exc:
                self._show_error(f"Could not install the database update: {exc}")
                return
            reject_update(candidate)
            self.pending_update = None
            win.destroy()
            self.recipe_catalog.clear()
            self.recipe_levels.clear()
            self.salvage_catalog.clear()
            self.salvage_rarity.clear()
            self._load_catalogs()
            self._refresh_database_info()
            self.confirm_var.set(False)
            if self.review is not None:
                self._populate_review_tables()
            self._write_update_log(self.update_text.get("1.0", "end").strip() + f"\n\nUPDATE ACCEPTED. Previous database backed up to:\n{backup}")
            self._set_status("Database update accepted and installed.")
            from tkinter import messagebox
            messagebox.showinfo(
                "Database Updated",
                f"The new database has been installed.\n\nThe previous database was backed up to:\n{backup}",
                parent=self.root,
            )

        ttk.Button(buttons, text="Reject update", command=reject).pack(side="left")
        ttk.Button(buttons, text="Accept update", command=accept).pack(side="right")
        win.protocol("WM_DELETE_WINDOW", reject)

    # ---------- live game-memory input ----------

    def _refresh_game_processes(self) -> None:
        if os.name != "nt":
            self.game_processes = []
            self._game_process_by_label = {}
            self.memory_process_combo.configure(values=())
            self.memory_process_var.set("")
            self.memory_status_var.set("Live game-memory reading is available only on Windows.")
            return
        prior_pid = None
        prior = self._game_process_by_label.get(self.memory_process_var.get())
        if prior is not None:
            prior_pid = prior.pid
        try:
            processes = list_city_of_heroes_processes()
        except GameMemoryError as exc:
            self.memory_status_var.set(str(exc))
            return
        self.game_processes = processes
        self._game_process_by_label = {proc.label: proc for proc in processes}
        labels = list(self._game_process_by_label)
        self.memory_process_combo.configure(values=labels)
        selected = ""
        if prior_pid is not None:
            selected = next((p.label for p in processes if p.pid == prior_pid), "")
        if not selected and labels:
            selected = labels[0]
        self.memory_process_var.set(selected)
        if not processes:
            self.memory_status_var.set("No running cityofheroes.exe clients found. Log a character in, refresh, or use screenshots/OCR below.")
        elif len(processes) == 1:
            self.memory_status_var.set(f"Ready to read {processes[0].label} directly from game memory.")
        else:
            self.memory_status_var.set(f"Found {len(processes)} City of Heroes clients. Choose the character/server above, then read inventory.")

    def _start_memory_read(self) -> None:
        label = self.memory_process_var.get().strip()
        process = self._game_process_by_label.get(label)
        if process is None:
            self._refresh_game_processes()
            label = self.memory_process_var.get().strip()
            process = self._game_process_by_label.get(label)
        if process is None:
            self._show_error("Choose a running City of Heroes character first. Use Refresh if the client was opened after Field Crafter.")
            return
        self.memory_button.configure(state="disabled")
        self._show_progress()
        self.memory_status_var.set(f"Reading {process.label} directly from game memory...")
        self._set_status(f"Reading recipe and salvage inventory from {process.label}.")

        def worker():
            try:
                reader = GameInventoryReader(self.db_path, alias_path=self._memory_alias_path)
                snapshot = reader.read(process)
                map_note = ""
                if snapshot.unresolved_recipe_count:
                    try:
                        self.root.after(0, lambda: self.memory_status_var.set(
                            "Unmapped recipe IDs found. Refreshing the game-memory recipe map from Homecoming Wiki..."
                        ))
                        def map_progress(message: str) -> None:
                            self.root.after(0, lambda m=message: self.memory_status_var.set(m))
                            self.root.after(0, lambda m=message: self._set_status(m))
                        refresh_result = refresh_memory_recipe_aliases(
                            self.db_path, self._memory_alias_path, progress=map_progress
                        )
                        reader = GameInventoryReader(self.db_path, alias_path=self._memory_alias_path)
                        snapshot = reader.read(process)
                        map_note = f" Automatic recipe-map refresh completed ({refresh_result.get('set_coverage', 'coverage unknown')})."
                    except Exception as map_exc:
                        map_note = f" Automatic recipe-map refresh could not complete: {map_exc}"
                review = review_from_memory_snapshot(snapshot)
                self.root.after(0, lambda: self._memory_read_complete(review, snapshot, map_note))
            except Exception as exc:
                detail = f"{exc}\n\n{traceback.format_exc()}"
                self.root.after(0, lambda: self._memory_read_failed(detail))

        threading.Thread(target=worker, daemon=True).start()

    def _memory_read_complete(self, review, snapshot, map_note="") -> None:
        self._hide_progress()
        self.memory_button.configure(state="normal")
        self.review = review
        self.confirm_var.set(False)
        self._populate_review_tables()
        unresolved = snapshot.unresolved_recipe_count + snapshot.unresolved_salvage_count
        self.memory_recipe_count_var.set(f"Recipes: {snapshot.recipe_total} / {snapshot.recipe_capacity}")
        self.memory_salvage_count_var.set(f"Salvage: {snapshot.salvage_total} / {snapshot.salvage_capacity}")
        source_label = self.memory_process_var.get().rsplit(" (PID ", 1)[0] or snapshot.window_title or "running game"
        status = (
            f"Inventory read complete from {source_label}: "
            f"{snapshot.recipe_total} recipes and {snapshot.salvage_total} invention salvage."
        )
        if unresolved:
            status += f" {unresolved} internal name mapping(s) remain unresolved; those recipe rows are unchecked."
        if map_note:
            status += map_note
        self.memory_status_var.set(status)
        self._set_status(status)
        self.notebook.select(self.review_tab)

    def _memory_read_failed(self, detail: str) -> None:
        self._hide_progress()
        self.memory_button.configure(state="normal")
        self.memory_status_var.set("Memory read failed. Screenshots/OCR remain available as a fallback.")
        self._set_status("Game-memory inventory read failed; no partial memory data was imported.")
        self._show_error(detail)

    # ---------- screenshot input ----------

    @staticmethod
    def _is_image_path(path: str | Path) -> bool:
        return Path(path).suffix.casefold() in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

    def _enable_drop_target(self, widget, kind: str) -> None:
        try:
            from tkinterdnd2 import DND_FILES
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", lambda event, k=kind: self._drop_images(event, k))
            self._dnd_available = True
        except Exception:
            # The GUI remains fully usable through Add/Paste if TkDnD is not installed.
            return

    def _drop_images(self, event, kind: str) -> str:
        try:
            paths = list(self.root.tk.splitlist(event.data))
        except Exception:
            paths = [str(getattr(event, "data", ""))]
        expanded: list[str] = []
        for raw in paths:
            p = Path(raw)
            if p.is_dir():
                expanded.extend(str(x) for x in sorted(p.iterdir()) if x.is_file() and self._is_image_path(x))
            elif p.is_file() and self._is_image_path(p):
                expanded.append(str(p))
        target = self.recipe_images if kind == "recipe" else self.salvage_images
        before = len(target)
        self._append_unique(target, expanded)
        self._update_input_lists()
        added = len(target) - before
        if added:
            self._set_status(f"Added {added} {kind} screenshot(s) by drag and drop.")
        elif paths:
            self._set_status("No supported image files were found in the drop.")
        return "break"

    def _clipboard_dir(self) -> Path:
        base = os.environ.get("LOCALAPPDATA")
        if base:
            out = Path(base) / "FieldCrafter" / "clipboard"
        else:
            out = Path.home() / ".field_crafter" / "clipboard"
        out.mkdir(parents=True, exist_ok=True)
        return out

    def _paste_clipboard(self, kind: str) -> None:
        try:
            from PIL import Image, ImageGrab
        except ImportError:
            self._show_error("Clipboard image support requires Pillow. Run the first-launch setup or setup_ocr.ps1.")
            return
        try:
            payload = ImageGrab.grabclipboard()
        except Exception as exc:
            self._show_error(f"Could not read an image from the clipboard: {exc}")
            return
        target = self.recipe_images if kind == "recipe" else self.salvage_images
        if isinstance(payload, Image.Image):
            stamp = f"{int(time.time() * 1000)}_{kind}.png"
            path = self._clipboard_dir() / stamp
            try:
                payload.convert("RGB").save(path, "PNG")
            except Exception as exc:
                self._show_error(f"Could not save the clipboard screenshot: {exc}")
                return
            self._append_unique(target, [str(path)])
            self._update_input_lists()
            self._set_status(f"Pasted clipboard image into {kind} screenshots.")
            return
        if isinstance(payload, list):
            images = [str(Path(x)) for x in payload if self._is_image_path(x) and Path(x).is_file()]
            self._append_unique(target, images)
            self._update_input_lists()
            if images:
                self._set_status(f"Added {len(images)} clipboard file(s) to {kind} screenshots.")
                return
        self._show_error("The clipboard does not currently contain a screenshot or supported image file.")

    def _add_recipe_images(self) -> None:
        from tkinter import filedialog
        paths = filedialog.askopenfilenames(title="Choose recipe screenshots", filetypes=self._image_filetypes())
        self._append_unique(self.recipe_images, paths)
        self._update_input_lists()

    def _add_salvage_images(self) -> None:
        from tkinter import filedialog
        paths = filedialog.askopenfilenames(title="Choose salvage screenshots", filetypes=self._image_filetypes())
        self._append_unique(self.salvage_images, paths)
        self._update_input_lists()

    @staticmethod
    def _image_filetypes():
        return [("Images", "*.png *.jpg *.jpeg *.webp *.bmp"), ("All files", "*.*")]

    @staticmethod
    def _append_unique(target: list[str], values) -> None:
        for value in values:
            text = str(value)
            if text and text not in target:
                target.append(text)

    def _remove_selected_images(self, kind: str) -> None:
        widget = self.recipe_list if kind == "recipe" else self.salvage_list
        target = self.recipe_images if kind == "recipe" else self.salvage_images
        for index in reversed(widget.curselection()):
            if 0 <= index < len(target):
                target.pop(index)
        self._update_input_lists()

    def _clear_images(self, kind: str) -> None:
        (self.recipe_images if kind == "recipe" else self.salvage_images).clear()
        self._update_input_lists()

    def _update_input_lists(self) -> None:
        for widget, values in ((self.recipe_list, self.recipe_images), (self.salvage_list, self.salvage_images)):
            widget.delete(0, "end")
            for path in values:
                widget.insert("end", path)

    def _start_scan(self) -> None:
        if not self.recipe_images and not self.salvage_images:
            self._show_error("Add at least one recipe or salvage screenshot first.")
            return
        self.scan_button.configure(state="disabled")
        recipe_images = list(self.recipe_images)
        salvage_images = list(self.salvage_images)
        backend = self.backend_var.get()
        total = len(recipe_images) + len(salvage_images)
        self._show_progress(determinate=True, maximum=total, value=0)
        self._set_scan_feedback(f"Scanning 0 of {total} screenshots...")
        self._set_status("OCR scan in progress. The Review & Edit tab will open automatically when the scan completes.")

        def progress_cb(index: int, total_count: int, kind: str, path: str) -> None:
            label = f"Scanning {index} of {total_count} screenshots... ({kind}: {Path(path).name})"
            self.root.after(0, lambda l=label: self._set_scan_feedback(l))
            self.root.after(0, lambda i=index, t=total_count: self._update_progress(i, t))

        def worker():
            try:
                review = scan_screenshots(
                    self.db_path,
                    recipe_images=recipe_images,
                    salvage_images=salvage_images,
                    backend=backend,
                    progress_callback=progress_cb,
                )
                self.root.after(0, lambda: self._scan_complete(review))
            except Exception as exc:  # show OCR/runtime failures without killing the GUI
                detail = f"{exc}\n\n{traceback.format_exc()}"
                self.root.after(0, lambda: self._scan_failed(detail))

        threading.Thread(target=worker, daemon=True).start()

    def _scan_complete(self, review: dict[str, Any]) -> None:
        self._hide_progress()
        self.scan_button.configure(state="normal")
        self._set_scan_feedback("Scan complete. Review the detected data below.")
        self.review = review
        self._apply_review_defaults()
        self.confirm_var.set(False)
        self._populate_review_tables()
        summary = _review_summary(review)
        self._set_status(
            f"Scan complete: {summary['recipe_rows']} recipe rows, {summary['salvage_types']} salvage types. Review before calculating."
        )
        self.notebook.select(self.review_tab)

    def _scan_failed(self, detail: str) -> None:
        self._hide_progress()
        self.scan_button.configure(state="normal")
        self._set_scan_feedback("")
        self._set_status("Scan failed.")
        self._show_error(detail)

    # ---------- review table population/editing ----------

    def _sort_review_inventory(self) -> None:
        if self.review is None:
            return
        recipes = list(self.review.get("recipes") or [])
        recognition = self.review.setdefault("recognition", {})
        rmeta = list(recognition.get("recipes") or [])
        paired = []
        for idx, item in enumerate(recipes):
            meta = rmeta[idx] if idx < len(rmeta) and isinstance(rmeta[idx], dict) else {}
            name = str(item.get("recipe", ""))
            is_common = name.startswith("Invention:")
            paired.append(((0 if is_common else 1, name.casefold()), item, meta))
        paired.sort(key=lambda row: row[0])
        self.review["recipes"] = [row[1] for row in paired]
        recognition["recipes"] = [row[2] for row in paired]

        inventory = self.review.get("inventory") or {}
        self.review["inventory"] = dict(sorted(inventory.items(), key=lambda kv: str(kv[0]).casefold()))

    def _populate_review_tables(self) -> None:
        if self.review is None:
            return
        self._sort_review_inventory()
        for tree in (self.recipe_tree, self.salvage_tree):
            tree.delete(*tree.get_children())

        rmeta = ((self.review.get("recognition") or {}).get("recipes") or [])
        for idx, item in enumerate(self.review.get("recipes") or []):
            meta = rmeta[idx] if idx < len(rmeta) and isinstance(rmeta[idx], dict) else {}
            selected = bool(item.get("selected", True))
            full_name = str(item.get("recipe", ""))
            self.recipe_tree.insert(
                "",
                "end",
                iid=f"r{idx}",
                values=(
                    "☑" if selected else "☐",
                    self._truncate_recipe_name(full_name),
                    item.get("quantity", 1),
                    "" if item.get("level") is None else item.get("level"),
                    full_name,
                ),
                tags=("needs_review",) if meta.get("needs_review") else (),
            )

        smeta = _aggregate_salvage_meta(self.review)
        for idx, (name, qty) in enumerate(sorted((self.review.get("inventory") or {}).items(), key=lambda kv: kv[0].lower())):
            meta = smeta.get(name, {})
            rarity = meta.get("rarity") or self.salvage_rarity.get(name, "")
            self.salvage_tree.insert(
                "",
                "end",
                iid=f"s{idx}",
                values=(name, qty, rarity),
                tags=("needs_review",) if meta.get("needs_review") else (),
            )

        cap = self.review.get("salvage_capacity") or {}
        self.used_var.set("" if cap.get("used") is None else str(cap.get("used")))
        self.capacity_var.set("" if cap.get("capacity") is None else str(cap.get("capacity")))
        source = cap.get("source") or "manual entry needed"
        self.capacity_source_var.set(f"Source: {source}")

        allowed = set(((self.review.get("disposal_policy") or {}).get("allowed_rarities") or ["common"]))
        self.common_var.set("common" in allowed)
        self.uncommon_var.set("uncommon" in allowed)
        self.rare_var.set("rare" in allowed)
        self._update_review_summary()
        self._update_recipe_selection_buttons()
        self._show_recipe_details()
        self._show_salvage_details()

    def _update_review_summary(self) -> None:
        if self.review is None:
            self.review_summary_var.set("No scan loaded.")
            self.review_notice_var.set("")
            self.review_notice_label.grid_remove()
            return
        self._sync_review_from_widgets(strict=False)
        summary = _review_summary(self.review)
        self.review_summary_var.set(
            f"{summary['recipe_rows']} recipes  •  {summary['crafts']} selected crafts  •  "
            f"{summary['salvage_types']} salvage types  •  {summary['salvage_total']} salvage"
        )
        recipe_review, salvage_review = self._manual_review_state()
        if recipe_review or salvage_review:
            self.review_notice_var.set(
                "Some recipes and/or salvage require manual review and are highlighted in red."
            )
            self.review_notice_label.grid(row=1, column=0, sticky="w", pady=(2, 0))
        else:
            self.review_notice_var.set("")
            self.review_notice_label.grid_remove()

    def _manual_review_state(self) -> tuple[bool, bool]:
        if self.review is None:
            return False, False
        recognition = self.review.get("recognition") or {}
        recipe_review = any(
            bool(item.get("needs_review"))
            for item in (recognition.get("recipes") or [])
            if isinstance(item, dict)
        )
        salvage_review = any(
            bool(item.get("needs_review"))
            for item in (recognition.get("salvage") or [])
            if isinstance(item, dict)
        )
        return recipe_review, salvage_review

    def _truncate_recipe_name(self, name: str) -> str:
        value = str(name or "")
        if not value or not hasattr(self, "recipe_tree"):
            return value
        try:
            from tkinter import font as tkfont
            font = tkfont.nametofont("TkDefaultFont")
            available = max(60, int(self.recipe_tree.column("recipe", "width")) - 14)
            if font.measure(value) <= available:
                return value
            suffix = "..."
            suffix_width = font.measure(suffix)
            lo, hi = 0, len(value)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if font.measure(value[:mid]) + suffix_width <= available:
                    lo = mid
                else:
                    hi = mid - 1
            return value[:lo].rstrip() + suffix
        except Exception:
            return value

    def _schedule_recipe_display_refresh(self, event=None) -> None:
        if getattr(self, "_recipe_display_refresh_pending", False):
            return
        self._recipe_display_refresh_pending = True

        def refresh() -> None:
            self._recipe_display_refresh_pending = False
            self._refresh_recipe_display_names()

        try:
            self.root.after_idle(refresh)
        except Exception:
            self._recipe_display_refresh_pending = False

    def _refresh_recipe_display_names(self) -> None:
        if not hasattr(self, "recipe_tree"):
            return
        for iid in self.recipe_tree.get_children():
            values = list(self.recipe_tree.item(iid, "values"))
            if len(values) < 5:
                continue
            full_name = str(values[4])
            display = self._truncate_recipe_name(full_name)
            if str(values[1]) != display:
                values[1] = display
                self.recipe_tree.item(iid, values=values)

    def _recipe_tree_hover(self, event) -> None:
        row = self.recipe_tree.identify_row(event.y)
        col = self.recipe_tree.identify_column(event.x)
        if not row or col != "#2":
            self._hide_recipe_tooltip()
            return
        values = list(self.recipe_tree.item(row, "values"))
        if len(values) < 5:
            self._hide_recipe_tooltip()
            return
        display, full_name = str(values[1]), str(values[4])
        if not full_name or display == full_name:
            self._hide_recipe_tooltip()
            return
        if getattr(self, "_recipe_tooltip_row", None) == row and getattr(self, "_recipe_tooltip_win", None):
            return
        self._hide_recipe_tooltip()
        self._recipe_tooltip_row = row

        def show() -> None:
            if getattr(self, "_recipe_tooltip_row", None) != row:
                return
            try:
                win = self.tk.Toplevel(self.root)
                win.wm_overrideredirect(True)
                win.attributes("-topmost", True)
                label = self.tk.Label(
                    win, text=full_name, justify="left", relief="solid", borderwidth=1,
                    padx=6, pady=4, background="#ffffe0", wraplength=560,
                )
                label.pack()
                x = self.root.winfo_pointerx() + 12
                y = self.root.winfo_pointery() + 18
                win.geometry(f"+{x}+{y}")
                self._recipe_tooltip_win = win
            except Exception:
                self._recipe_tooltip_win = None

        try:
            self._recipe_tooltip_after_id = self.root.after(450, show)
        except Exception:
            self._recipe_tooltip_after_id = None

    def _hide_recipe_tooltip(self) -> None:
        after_id = getattr(self, "_recipe_tooltip_after_id", None)
        if after_id:
            try:
                self.root.after_cancel(after_id)
            except Exception:
                pass
        self._recipe_tooltip_after_id = None
        win = getattr(self, "_recipe_tooltip_win", None)
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass
        self._recipe_tooltip_win = None
        self._recipe_tooltip_row = None

    def _recipe_full_name(self, iid: str) -> str:
        values = list(self.recipe_tree.item(iid, "values"))
        if len(values) >= 5 and str(values[4]).strip():
            return str(values[4]).strip()
        return str(values[1]).strip() if len(values) > 1 else ""

    def _recipe_item_index(self, iid: str) -> int | None:
        try:
            return int(iid[1:]) if iid.startswith("r") else None
        except ValueError:
            return None

    def _recipe_tree_single_click(self, event):
        row = self.recipe_tree.identify_row(event.y)
        col = self.recipe_tree.identify_column(event.x)
        if row and col == "#1":
            self.recipe_tree.selection_set(row)
            self.recipe_tree.focus(row)
            values = list(self.recipe_tree.item(row, "values"))
            values[0] = "☐" if values[0] == "☑" else "☑"
            if values[0] == "☑":
                self._clear_recipe_review_flag(row)
            self.recipe_tree.item(row, values=values)
            self.confirm_var.set(False)
            self._show_recipe_details()
            self._update_review_summary()
            self._update_recipe_selection_buttons()
            return "break"
        return None

    def _edit_recipe_cell(self, event) -> None:
        row = self.recipe_tree.identify_row(event.y)
        col = self.recipe_tree.identify_column(event.x)
        if not row or not col:
            return
        col_num = int(col[1:]) - 1
        if col_num == 0:
            return
        if col_num not in (1, 2, 3):
            return
        values = list(self.recipe_tree.item(row, "values"))
        if col_num == 1:
            self._popup_cell_editor(self.recipe_tree, row, col, self._recipe_full_name(row), self.recipe_catalog, self._recipe_cell_saved)
        else:
            self._popup_cell_editor(self.recipe_tree, row, col, values[col_num], None, self._recipe_cell_saved)

    def _recipe_cell_saved(self, tree, row: str, col: str, value: str) -> None:
        col_num = int(col[1:]) - 1
        values = list(tree.item(row, "values"))
        if col_num == 1 and value not in self.recipe_catalog:
            self._show_error("Choose a recipe name from the database list.")
            return
        if col_num == 2:
            try:
                if int(value) <= 0:
                    raise ValueError
                value = str(int(value))
            except ValueError:
                self._show_error("Recipe quantity must be a positive integer.")
                return
        if col_num == 3:
            try:
                value = "" if not value.strip() else str(int(value))
            except ValueError:
                self._show_error("Recipe level must be an integer or blank for a fixed-level recipe.")
                return
        if col_num == 1:
            values[4] = value
            values[1] = self._truncate_recipe_name(value)
        else:
            values[col_num] = value
        values[0] = "☑"
        if col_num == 1:
            levels = self.recipe_levels.get(value, [])
            if len(levels) == 1:
                values[3] = str(levels[0])
        tree.item(row, values=values)
        # If this was an unresolved game-memory recipe, remember the user's canonical
        # correction so the same internal ID resolves automatically next time.
        idx = self._recipe_item_index(row)
        meta = ((self.review or {}).get("recognition") or {}).get("recipes") or []
        if idx is not None and idx < len(meta) and isinstance(meta[idx], dict):
            meta[idx]["edited_manually"] = True
            meta[idx]["needs_review"] = False
            if col_num == 1:
                meta[idx]["recipe"] = value
        if idx is not None and idx < len(meta) and isinstance(meta[idx], dict) and meta[idx].get("source") == "game_memory":
            internal = str(meta[idx].get("internal_name") or "")
            if internal and value in self.recipe_catalog and col_num == 1:
                try:
                    resolver = MemoryNameResolver(self.db_path, alias_path=self._memory_alias_path)
                    resolver.remember_recipe_alias(internal, value)
                    meta[idx]["recipe"] = value
                    meta[idx]["mapping_source"] = "saved_alias"
                    meta[idx]["confidence"] = 1.0
                    meta[idx]["needs_review"] = False
                except Exception:
                    pass
        self._clear_recipe_review_flag(row)
        self.confirm_var.set(False)
        self._show_recipe_details()
        self._update_review_summary()
        self._update_recipe_selection_buttons()

    def _show_recipe_details(self) -> None:
        if self.review is None:
            self.recipe_detail_var.set("Select a recipe row to see details.")
            return
        sel = self.recipe_tree.selection()
        if not sel:
            self.recipe_detail_var.set("Select a recipe row to see details.")
            return
        idx = self._recipe_item_index(sel[0])
        meta = ((self.review.get("recognition") or {}).get("recipes") or [])
        if idx is None or idx >= len(meta) or not isinstance(meta[idx], dict) or not meta[idx]:
            self.recipe_detail_var.set("Source: Manual. Manually added recipe; no source audit details are available.")
            return
        item = meta[idx]
        raw = str(item.get("raw_text") or "")
        candidates = item.get("candidates") or []
        cand_text = "; ".join(f"{c.get('recipe')} ({float(c.get('score') or 0)*100:.1f}%)" for c in candidates[:3])
        if item.get("source") == "game_memory":
            canonical = self._recipe_full_name(sel[0])
            base, _level = MemoryNameResolver.base_recipe_internal_name(raw)
            piece_match = re.match(r"^(?P<set>.+)_(?P<piece>[A-Z])$", base)
            if base.startswith("Invention_"):
                mapping = "common Invention internal ID -> canonical common recipe"
            elif piece_match:
                set_label = piece_match.group("set").replace("_", " ")
                piece = piece_match.group("piece")
                ordinal = ord(piece) - ord("A") + 1
                suffix = "th"
                if ordinal % 10 == 1 and ordinal != 11:
                    suffix = "st"
                elif ordinal % 10 == 2 and ordinal != 12:
                    suffix = "nd"
                elif ordinal % 10 == 3 and ordinal != 13:
                    suffix = "rd"
                mapping = f"{set_label} + {piece} -> {ordinal}{suffix} set recipe"
            else:
                mapping = str(item.get("mapping_source") or "internal ID mapping")
            mapped = canonical if not canonical.startswith("[Unresolved]") else "Unmapped"
            edited = " (manually edited)" if item.get("edited_manually") else ""
            self.recipe_detail_var.set(
                f"Source: Game memory{edited}.    Internal ID: {raw}    Mapped to: {mapped}    Mapping: {mapping}"
            )
        elif item.get("source_image") or item.get("raw_text") or item.get("candidates"):
            confidence = _fmt_conf(item.get("confidence")) or "-"
            q_source = str(item.get("quantity_source") or "-").replace("_", " ")
            level_source = str(item.get("level_source") or "-").replace("_", " ")
            edited = " (manually edited)" if item.get("edited_manually") else ""
            self.recipe_detail_var.set(
                f"Source: OCR{edited}.    OCR confidence: {confidence}    OCR text: {raw}    Candidates: {cand_text}    "
                f"Quantity source: {q_source}    Level source: {level_source}"
            )
        else:
            self.recipe_detail_var.set("Source: Manual. No OCR or game-memory audit details are available.")

    def _show_salvage_details(self) -> None:
        if self.review is None:
            self.salvage_detail_var.set("Select a salvage row to see details.")
            return
        sel = self.salvage_tree.selection()
        if not sel:
            self.salvage_detail_var.set("Select a salvage row to see details.")
            return
        name = str(self.salvage_tree.item(sel[0], "values")[0])
        meta = _aggregate_salvage_meta(self.review).get(name) or {}
        items = meta.get("items") or []
        if not items:
            self.salvage_detail_var.set("Source: Manual. Manually added salvage; no source audit details are available.")
            return
        if any(i.get("source") == "game_memory" for i in items):
            raws = " | ".join(str(i.get("raw_text") or "") for i in items if i.get("raw_text"))
            qtys = ", ".join(str(i.get("quantity")) for i in items if i.get("quantity") is not None)
            edited = " (manually edited)" if any(i.get("edited_manually") for i in items) else ""
            self.salvage_detail_var.set(
                f"Source: Game memory{edited}.    Game memory ID: {raws}    Quantity: {qtys}    Exact memory read"
            )
            return
        name_conf = _fmt_conf(meta.get("name_confidence")) or "-"
        qty_conf = _fmt_conf(meta.get("quantity_confidence")) or "-"
        overall = _fmt_conf(meta.get("confidence")) or "-"
        raws = " | ".join(str(i.get("raw_text") or "") for i in items if i.get("raw_text"))
        qty_debug = []
        for item in items:
            for cand in item.get("quantity_retry_candidates") or []:
                raw = str(cand.get("raw_text") or "")
                q = cand.get("quantity")
                conf = _fmt_conf(cand.get("confidence")) or ""
                qty_debug.append(f"{cand.get('variant')}: {raw} -> {q} ({conf})")
        extra = f"    Qty OCR variants: {'; '.join(qty_debug[:6])}" if qty_debug else ""
        edited = " (manually edited)" if any(i.get("edited_manually") for i in items) else ""
        self.salvage_detail_var.set(
            f"Source: OCR{edited}.    Overall: {overall}    Name: {name_conf}    Qty: {qty_conf}    OCR: {raws}{extra}"
        )

    def _clear_recipe_review_flag(self, row: str) -> None:
        if self.review is None:
            return
        idx = self._recipe_item_index(row)
        meta = ((self.review.get("recognition") or {}).get("recipes") or [])
        if idx is not None and idx < len(meta) and isinstance(meta[idx], dict):
            meta[idx]["needs_review"] = False
        if hasattr(self, "recipe_tree") and row in self.recipe_tree.get_children():
            self.recipe_tree.item(row, tags=())

    def _clear_salvage_review_flag(self, name: str) -> None:
        if self.review is None:
            return
        for item in ((self.review.get("recognition") or {}).get("salvage") or []):
            if isinstance(item, dict) and str(item.get("salvage") or "") == name:
                item["needs_review"] = False
        if hasattr(self, "salvage_tree"):
            for iid in self.salvage_tree.get_children():
                values = self.salvage_tree.item(iid, "values")
                if values and str(values[0]) == name:
                    self.salvage_tree.item(iid, tags=())

    def _add_recipe_row(self) -> None:
        if self.review is None:
            self.review = self._blank_review()
        choice = self._simple_choice_dialog("Add recipe", "Recipe", self.recipe_catalog)
        if not choice:
            return
        levels = self.recipe_levels.get(choice, [])
        level = levels[0] if len(levels) == 1 else (levels[-1] if levels else None)
        self.review.setdefault("recipes", []).append({
            "recipe": choice,
            "quantity": 1,
            "level": level,
            "option_index": None,
            "selected": True,
        })
        self.confirm_var.set(False)
        self._populate_review_tables()

    def _remove_recipe_row(self) -> None:
        if self.review is None:
            return
        sel = self.recipe_tree.selection()
        if not sel:
            return
        idx = self._recipe_item_index(sel[0])
        self._sync_review_from_widgets(strict=False)
        if idx is not None and idx < len(self.review.get("recipes") or []):
            self.review["recipes"].pop(idx)
            rec = (self.review.get("recognition") or {}).get("recipes")
            if isinstance(rec, list) and idx < len(rec):
                rec.pop(idx)
            self.confirm_var.set(False)
            self._populate_review_tables()

    def _recipe_selection_state(self) -> tuple[list[str], list[str]]:
        all_rows = list(self.recipe_tree.get_children())
        common_rows = [
            iid for iid in all_rows
            if _is_common_recipe_name(self._recipe_full_name(iid))
        ]
        return all_rows, common_rows

    def _update_recipe_selection_buttons(self) -> None:
        if not hasattr(self, "all_recipe_button_var"):
            return
        all_rows, common_rows = self._recipe_selection_state()
        all_selected = bool(all_rows) and all(
            self.recipe_tree.item(iid, "values")[0] == "☑" for iid in all_rows
        )
        commons_selected = bool(common_rows) and all(
            self.recipe_tree.item(iid, "values")[0] == "☑" for iid in common_rows
        )
        self.all_recipe_button_var.set("Select none" if all_selected else "Select all")
        self.common_recipe_button_var.set("Deselect commons" if commons_selected else "Select commons")
        if all_rows:
            self.all_recipe_button.state(["!disabled"])
        else:
            self.all_recipe_button.state(["disabled"])
        if common_rows:
            self.common_recipe_button.state(["!disabled"])
        else:
            self.common_recipe_button.state(["disabled"])

    def _toggle_all_recipe_selection(self) -> None:
        all_rows, _common_rows = self._recipe_selection_state()
        select = not (bool(all_rows) and all(
            self.recipe_tree.item(iid, "values")[0] == "☑" for iid in all_rows
        ))
        self._set_all_recipe_selection(select)

    def _toggle_common_recipe_selection(self) -> None:
        _all_rows, common_rows = self._recipe_selection_state()
        select = not (bool(common_rows) and all(
            self.recipe_tree.item(iid, "values")[0] == "☑" for iid in common_rows
        ))
        self._set_common_recipe_selection(select)

    def _set_all_recipe_selection(self, selected: bool) -> None:
        for iid in self.recipe_tree.get_children():
            vals = list(self.recipe_tree.item(iid, "values"))
            vals[0] = "☑" if selected else "☐"
            self.recipe_tree.item(iid, values=vals)
            if selected:
                self._clear_recipe_review_flag(iid)
        self.confirm_var.set(False)
        self._update_review_summary()
        self._update_recipe_selection_buttons()

    def _set_common_recipe_selection(self, selected: bool) -> None:
        """Toggle only common IO recipes; set-recipe selections are left unchanged."""
        for iid in self.recipe_tree.get_children():
            vals = list(self.recipe_tree.item(iid, "values"))
            recipe_name = self._recipe_full_name(iid)
            if not _is_common_recipe_name(recipe_name):
                continue
            vals[0] = "☑" if selected else "☐"
            if selected:
                self._clear_recipe_review_flag(iid)
            self.recipe_tree.item(iid, values=vals)
        self.confirm_var.set(False)
        self._update_review_summary()
        self._update_recipe_selection_buttons()

    def _edit_salvage_cell(self, event) -> None:
        row = self.salvage_tree.identify_row(event.y)
        col = self.salvage_tree.identify_column(event.x)
        if not row or not col:
            return
        col_num = int(col[1:]) - 1
        if col_num == 0:
            value = self.salvage_tree.item(row, "values")[0]
            self._popup_cell_editor(self.salvage_tree, row, col, value, self.salvage_catalog, self._salvage_cell_saved)
        elif col_num == 1:
            value = self.salvage_tree.item(row, "values")[1]
            self._popup_cell_editor(self.salvage_tree, row, col, value, None, self._salvage_cell_saved)

    def _salvage_cell_saved(self, tree, row: str, col: str, value: str) -> None:
        col_num = int(col[1:]) - 1
        values = list(tree.item(row, "values"))
        old_name = str(values[0])
        if col_num == 0:
            if value not in self.salvage_catalog:
                self._show_error("Choose a salvage name from the database list.")
                return
            # Prevent accidental duplicate aggregate rows.
            for iid in tree.get_children():
                if iid != row and tree.item(iid, "values")[0] == value:
                    self._show_error("That salvage already exists in the inventory table. Edit its quantity instead.")
                    return
            values[0] = value
            values[2] = self.salvage_rarity.get(value, "")
        else:
            try:
                qty = int(value)
                if qty < 0:
                    raise ValueError
                values[1] = str(qty)
            except ValueError:
                self._show_error("Salvage quantity must be a non-negative integer.")
                return
        tree.item(row, values=values)
        recognition = ((self.review or {}).get("recognition") or {}).get("salvage") or []
        for item in recognition:
            if not isinstance(item, dict) or str(item.get("salvage") or "") != old_name:
                continue
            item["edited_manually"] = True
            item["needs_review"] = False
            if col_num == 0:
                item["salvage"] = str(values[0])
        self._clear_salvage_review_flag(str(values[0]))
        self.confirm_var.set(False)
        self._show_salvage_details()
        self._update_review_summary()

    def _add_salvage_row(self) -> None:
        if self.review is None:
            self.review = self._blank_review()
        existing = set((self.review.get("inventory") or {}).keys())
        choices = [x for x in self.salvage_catalog if x not in existing]
        choice = self._simple_choice_dialog("Add salvage", "Salvage", choices)
        if not choice:
            return
        self._sync_review_from_widgets(strict=False)
        self.review.setdefault("inventory", {})[choice] = 0
        self.confirm_var.set(False)
        self._populate_review_tables()

    def _remove_salvage_row(self) -> None:
        if self.review is None:
            return
        sel = self.salvage_tree.selection()
        if not sel:
            return
        name = self.salvage_tree.item(sel[0], "values")[0]
        self._sync_review_from_widgets(strict=False)
        self.review.setdefault("inventory", {}).pop(name, None)
        self.confirm_var.set(False)
        self._populate_review_tables()

    def _popup_cell_editor(self, tree, row: str, col: str, current: str, values: list[str] | None, callback) -> None:
        ttk = self.ttk
        bbox = tree.bbox(row, col)
        if not bbox:
            return
        x, y, w, h = bbox
        if values is not None:
            # Recipe/salvage names must be selected from the canonical database.
            # A readonly combobox also prevents an unresolved memory ID from being
            # accidentally committed as free-form text.
            editor = ttk.Combobox(tree, values=values, state="readonly")
            editor.set(str(current))
        else:
            editor = ttk.Entry(tree)
            editor.insert(0, str(current))
        editor.place(x=x, y=y, width=max(w, 80), height=h)
        editor.focus_set()
        try:
            editor.selection_range(0, "end")
        except Exception:
            pass

        done = {"value": False}

        def save(event=None):
            if done["value"]:
                return
            done["value"] = True
            val = editor.get()
            editor.destroy()
            callback(tree, row, col, val)

        def cancel(event=None):
            if done["value"]:
                return
            done["value"] = True
            editor.destroy()

        editor.bind("<Return>", save)
        editor.bind("<Tab>", save)
        editor.bind("<Escape>", cancel)
        if values is not None:
            # Opening a ttk.Combobox dropdown can generate FocusOut before the user
            # has picked an item. The previous FocusOut behavior saved at that moment
            # unresolved text. Commit only after an actual dropdown selection (or
            # explicit Return/Tab) so manual memory-recipe mapping works reliably.
            editor.bind("<<ComboboxSelected>>", save)
        else:
            editor.bind("<FocusOut>", save)

    def _simple_choice_dialog(self, title: str, label: str, choices: list[str]) -> str | None:
        tk, ttk = self.tk, self.ttk
        if not choices:
            self._show_error(f"No {label.lower()} choices are available.")
            return None
        win = tk.Toplevel(self.root)
        win.title(title)
        win.transient(self.root)
        win.grab_set()
        win.resizable(True, False)
        ttk.Label(win, text=f"{label}:").grid(row=0, column=0, padx=10, pady=10, sticky="w")
        var = tk.StringVar(value=choices[0])
        combo = ttk.Combobox(win, textvariable=var, values=choices, width=75)
        combo.grid(row=0, column=1, padx=(0, 10), pady=10, sticky="ew")
        combo.focus_set()
        result = {"value": None}

        def ok():
            if var.get() in choices:
                result["value"] = var.get()
                win.destroy()
            else:
                self._show_error(f"Choose a valid {label.lower()} from the database list.")

        ttk.Button(win, text="Add", command=ok).grid(row=1, column=1, padx=10, pady=(0, 10), sticky="e")
        win.bind("<Return>", lambda e: ok())
        self.root.wait_window(win)
        return result["value"]

    # ---------- synchronization / save / load ----------

    def _blank_review(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "confirmed": False,
            "needs_review": True,
            "ocr_backend": "manual",
            "recipes": [],
            "inventory": {},
            "salvage_capacity": None,
            "disposal_policy": {"allowed_rarities": ["common"]},
            "recognition": {"recipes": [], "salvage": [], "capacity_candidates": [], "images": []},
        }

    def _sync_review_from_widgets(self, *, strict: bool = True) -> None:
        if self.review is None:
            return
        recipes = []
        for iid in self.recipe_tree.get_children():
            vals = self.recipe_tree.item(iid, "values")
            recipe = self._recipe_full_name(iid)
            try:
                qty = int(vals[2])
                if qty <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                if strict:
                    raise CalculationError(f"Invalid recipe quantity for {recipe!r}: {vals[2]!r}")
                qty = 1
            level_text = str(vals[3]).strip()
            try:
                level = int(level_text) if level_text else None
            except ValueError:
                if strict:
                    raise CalculationError(f"Invalid recipe level for {recipe!r}: {level_text!r}")
                level = None
            recipes.append({
                "recipe": recipe,
                "quantity": qty,
                "level": level,
                "option_index": None,
                "selected": str(vals[0]) == "☑",
            })
        self.review["recipes"] = recipes

        inventory: dict[str, int] = {}
        for iid in self.salvage_tree.get_children():
            vals = self.salvage_tree.item(iid, "values")
            name = str(vals[0]).strip()
            try:
                qty = int(vals[1])
                if qty < 0:
                    raise ValueError
            except (TypeError, ValueError):
                if strict:
                    raise CalculationError(f"Invalid salvage quantity for {name!r}: {vals[1]!r}")
                qty = 0
            if qty > 0:
                inventory[name] = qty
        self.review["inventory"] = inventory

        used_text = self.used_var.get().strip()
        cap_text = self.capacity_var.get().strip()
        if used_text or cap_text:
            if not used_text or not cap_text:
                if strict:
                    raise CalculationError("Enter both salvage used and salvage capacity, or leave both blank.")
                self.review["salvage_capacity"] = None
            else:
                try:
                    used, cap = int(used_text), int(cap_text)
                    if used < 0 or cap <= 0 or used > cap:
                        raise ValueError
                except ValueError:
                    if strict:
                        raise CalculationError("Salvage capacity must be valid integers with 0 <= used <= capacity.")
                    self.review["salvage_capacity"] = None
                else:
                    prior = self.review.get("salvage_capacity") or {}
                    source = prior.get("source") or "manual"
                    if str(prior.get("used")) != str(used) or str(prior.get("capacity")) != str(cap):
                        source = "manual"
                    self.review["salvage_capacity"] = {"used": used, "capacity": cap, "source": source}
        else:
            self.review["salvage_capacity"] = None

        allowed = []
        if self.common_var.get():
            allowed.append("common")
        if self.uncommon_var.get():
            allowed.append("uncommon")
        if self.rare_var.get():
            allowed.append("rare")
        self.review["disposal_policy"] = {"allowed_rarities": allowed}
        self.review["confirmed"] = bool(self.confirm_var.get())

    def _save_review_dialog(self) -> None:
        from tkinter import filedialog
        if self.review is None:
            self._show_error("There is no review to save yet.")
            return
        try:
            self._sync_review_from_widgets(strict=True)
        except CalculationError as exc:
            self._show_error(str(exc))
            return
        path = filedialog.asksaveasfilename(
            title="Save recognition review",
            defaultextension=".json",
            initialfile="recognition_review.json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        Path(path).write_text(json.dumps(self.review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self._set_status(f"Saved review to {path}")

    def _load_review_dialog(self) -> None:
        from tkinter import filedialog
        path = filedialog.askopenfilename(title="Load recognition review", filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            self.review = load_review(path)
        except CalculationError as exc:
            self._show_error(str(exc))
            return
        self.confirm_var.set(bool(self.review.get("confirmed")))
        self._populate_review_tables()
        self._set_status(f"Loaded review from {path}")
        self.notebook.select(self.review_tab)

    # ---------- calculation / results ----------

    def _calculate_from_gui(self) -> None:
        if self.review is None:
            self._show_error("Scan screenshots or load a saved review first.")
            return
        try:
            self._sync_review_from_widgets(strict=True)
            if not self.confirm_var.get():
                raise CalculationError("Check the confirmation box after reviewing the detected data before calculating.")
            self.review["confirmed"] = True
            result = calculate_review(self.db_path, self.review, require_confirmed=True)
        except CalculationError as exc:
            self._show_error(str(exc))
            return
        self.last_result = result
        self._populate_results(result)
        self.notebook.select(self.result_tab)
        self._set_status("Shopping list calculated.")

    def _populate_results(self, result: dict[str, Any]) -> None:
        for tree in (self.buy_tree, self.dispose_tree, self.surplus_tree):
            tree.delete(*tree.get_children())
        for row in result.get("shopping_list") or []:
            self.buy_tree.insert("", "end", values=(row.get("buy", 0), row.get("salvage", ""), row.get("rarity", "")))
        space = result.get("space_plan") or {}
        recommended = space.get("recommended_disposals") or []
        disposed_names: dict[str, int] = defaultdict(int)
        for row in recommended:
            qty = int(row.get("quantity") or 0)
            disposed_names[str(row.get("salvage"))] += qty
            self.dispose_tree.insert("", "end", values=(qty, row.get("salvage", ""), row.get("rarity", "")))
        for row in result.get("safe_surplus") or []:
            remaining = int(row.get("surplus") or 0) - disposed_names.get(str(row.get("salvage")), 0)
            if remaining > 0:
                self.surplus_tree.insert("", "end", values=(remaining, row.get("salvage", ""), row.get("rarity", "")))

        s = result["summary"]
        parts = [
            f"{s['craft_count']} crafts",
            f"buy {s['shopping_total']} salvage across {s['shopping_types']} types",
        ]
        if s.get("crafting_cost_total") is not None:
            parts.append(f"crafting cost {int(s['crafting_cost_total']):,} influence")
        if space:
            if space.get("remaining_room_shortfall"):
                parts.append(f"still short {space['remaining_room_shortfall']} inventory spaces")
            else:
                parts.append("enough room after recommended disposals")
        self.result_summary_var.set(" • ".join(parts))

        text = format_text_result(result)
        self.result_text.delete("1.0", "end")
        self.result_text.insert("1.0", text)
        self._refresh_auction_searches()

    def _save_result_text(self) -> None:
        from tkinter import filedialog
        if self.last_result is None:
            self._show_error("There is no calculated result to save.")
            return
        path = filedialog.asksaveasfilename(
            title="Save shopping list",
            defaultextension=".txt",
            initialfile="shopping_list.txt",
            filetypes=[("Text", "*.txt"), ("All files", "*.*")],
        )
        if path:
            Path(path).write_text(format_text_result(self.last_result), encoding="utf-8")
            self._set_status(f"Saved result to {path}")

    def _save_result_json(self) -> None:
        from tkinter import filedialog
        if self.last_result is None:
            self._show_error("There is no calculated result to save.")
            return
        path = filedialog.asksaveasfilename(
            title="Save shopping list JSON",
            defaultextension=".json",
            initialfile="shopping_list.json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if path:
            Path(path).write_text(json.dumps(self.last_result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            self._set_status(f"Saved result to {path}")

    # ---------- small UI helpers ----------

    def _determine_app_state_dir(self) -> Path:
        base = os.environ.get("LOCALAPPDATA")
        path = Path(base) / "FieldCrafter" if base else Path.home() / ".field_crafter"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _set_scan_feedback(self, text: str) -> None:
        if self._scan_feedback_var is not None:
            self._scan_feedback_var.set(text)

    def _apply_review_defaults(self) -> None:
        if self.review is None or self.review.get("confirmed"):
            return
        rmeta = ((self.review.get("recognition") or {}).get("recipes") or [])
        for idx, item in enumerate(self.review.get("recipes") or []):
            meta = rmeta[idx] if idx < len(rmeta) and isinstance(rmeta[idx], dict) else {}
            if meta.get("needs_review"):
                item["selected"] = False

    def _restore_window_geometry(self) -> None:
        try:
            if not self._window_state_path.exists():
                return
            data = json.loads(self._window_state_path.read_text(encoding="utf-8"))
            geometry = _parse_geometry_string(data.get("geometry"))
            if not geometry:
                return
            w, h, x, y = geometry
            self.root.update_idletasks()
            vx = int(getattr(self.root, "winfo_vrootx")())
            vy = int(getattr(self.root, "winfo_vrooty")())
            vw = int(getattr(self.root, "winfo_vrootwidth")()) or int(self.root.winfo_screenwidth())
            vh = int(getattr(self.root, "winfo_vrootheight")()) or int(self.root.winfo_screenheight())
            # Migrate the exact factory-default heights from 1.14 and the first
            # 1.15 candidate to the final compact 1.15 default. Other saved sizes
            # are treated as deliberate user choices and are preserved.
            if w == 1220 and h in (820, 700):
                h = 600
            w = max(1040, min(w, vw))
            h = max(600, min(h, vh))
            visible_margin = 120
            if x + visible_margin < vx or y + visible_margin < vy or x > vx + vw - visible_margin or y > vy + vh - visible_margin:
                x = vx + max(0, (vw - w) // 2)
                y = vy + max(0, (vh - h) // 2)
            else:
                x = min(max(x, vx), max(vx, vx + vw - w))
                y = min(max(y, vy), max(vy, vy + vh - h))
            self.root.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            return

    def _save_window_geometry(self) -> None:
        try:
            self.root.update_idletasks()
            payload = {"geometry": self.root.geometry(), "saved_at": int(time.time())}
            self._window_state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _on_close(self) -> None:
        self._save_window_geometry()
        try:
            self.root.destroy()
        except Exception:
            pass

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)
        try:
            self.root.update_idletasks()
        except Exception:
            pass

    def _show_error(self, text: str) -> None:
        from tkinter import messagebox
        messagebox.showerror("Field Crafter", text, parent=self.root)


def launch_gui(*, db_path: str | Path = "data/homecoming_recipes.sqlite") -> None:
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("FieldCrafter.App.1.15")
        except Exception:
            pass
    try:
        import tkinter as tk
    except ImportError as exc:
        raise CalculationError(
            "Tkinter is not available in this Python installation. On Windows, install/use the standard python.org build with Tcl/Tk support."
        ) from exc
    try:
        from tkinterdnd2 import TkinterDnD
        root = TkinterDnD.Tk()
    except Exception:
        # Add/Paste still work if native drag/drop is unavailable.
        root = tk.Tk()
    CraftingHelperGUI(root, db_path=db_path)
    root.mainloop()
