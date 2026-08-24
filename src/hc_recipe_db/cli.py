from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .calculator import CalculationError, calculate_payload, format_text_result, load_payload, parse_capacity_text
from .validation import write_validation_report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Homecoming recipe database, screenshot recognizer, and salvage shopping-list calculator"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="Scrape/update the Homecoming Wiki and build SQLite + exports")
    b.add_argument("--db", default="data/homecoming_recipes.sqlite")
    b.add_argument("--cache", default="cache")
    b.add_argument("--exports", default="exports")
    b.add_argument("--report", default="data/validation_report.txt")
    b.add_argument("--report-json", default="data/validation_report.json")
    b.add_argument("--refresh", action="store_true", help="Ignore cached API responses")
    b.add_argument("--delay", type=float, default=0.25, help="Minimum delay between wiki requests in seconds")
    b.add_argument("--quiet", action="store_true", help="Suppress progress messages (JSON result still prints)")

    v = sub.add_parser("validate", help="Validate an existing SQLite database")
    v.add_argument("--db", default="data/homecoming_recipes.sqlite")
    v.add_argument("--report", default="data/validation_report.txt")
    v.add_argument("--report-json", default="data/validation_report.json")

    c = sub.add_parser("calculate", help="Generate a shopping list and safe salvage disposal/space plan")
    c.add_argument("input", help="JSON file containing selected recipes, inventory, and optional capacity")
    c.add_argument("--db", default="data/homecoming_recipes.sqlite")
    c.add_argument("--format", choices=("text", "json"), default="text")
    c.add_argument("--output", help="Write result to a file instead of stdout")


    s = sub.add_parser("scan", help="OCR recipe/salvage screenshots into an editable review JSON")
    s.add_argument("--recipe", action="append", default=[], help="Recipe screenshot path (repeat for multiple screenshots)")
    s.add_argument("--salvage", action="append", default=[], help="Salvage screenshot path (repeat for multiple screenshots)")
    s.add_argument("--db", default="data/homecoming_recipes.sqlite")
    s.add_argument("--backend", choices=("auto", "rapidocr", "tesseract"), default="auto")
    s.add_argument("--scale", type=float, help="Override adaptive screenshot upscale factor")
    s.add_argument("--output", default="recognition_review.json")

    cr = sub.add_parser("calculate-review", help="Calculate from an edited/confirmed screenshot recognition review")
    cr.add_argument("input", help="Review JSON produced by the scan command")
    cr.add_argument("--db", default="data/homecoming_recipes.sqlite")
    cr.add_argument("--format", choices=("text", "json"), default="text")
    cr.add_argument("--output")
    cr.add_argument("--allow-unconfirmed", action="store_true", help="Developer/testing override; normal use should confirm the review first")

    m = sub.add_parser("memory-read", help="Read recipes and invention salvage directly from a running City of Heroes client")
    m.add_argument("--pid", type=int, help="cityofheroes.exe PID; omit to list/auto-select when exactly one client is running")
    m.add_argument("--db", default="data/homecoming_recipes.sqlite")
    m.add_argument("--aliases", help="Optional JSON file for remembered internal recipe-name mappings")
    m.add_argument("--output", help="Write review JSON to this file instead of stdout")

    g = sub.add_parser("gui", help="Launch the desktop inventory review and shopping-list interface")
    g.add_argument("--db", default="data/homecoming_recipes.sqlite")

    p = sub.add_parser("parse-capacity", help="Test used/capacity extraction from OCR text")
    p.add_argument("text", help='OCR text containing a fraction such as "169 / 172"')

    args = parser.parse_args()
    try:
        if args.command == "build":
            # Import the scraper only when the build command is actually used.
            # Normal calculator/capacity commands are intentionally stdlib-only at runtime
            # and should not require BeautifulSoup/requests to be installed.
            from .builder import build_database

            progress = None if args.quiet else (lambda msg: print(msg, file=sys.stderr, flush=True))
            result = build_database(
                db_path=args.db,
                cache_dir=args.cache,
                export_dir=args.exports,
                report_path=args.report,
                report_json_path=args.report_json,
                refresh=args.refresh,
                delay_seconds=args.delay,
                progress=progress,
            )
            print(json.dumps(result, indent=2))
        elif args.command == "validate":
            checks = write_validation_report(args.db, args.report, args.report_json)
            for check in checks:
                print(f"{check.severity:<5} {check.name}: {check.detail}")
        elif args.command == "scan":
            if not args.recipe and not args.salvage:
                raise CalculationError("scan requires at least one --recipe or --salvage screenshot")
            from .ocr import OCRError
            from .recognition import scan_screenshots
            try:
                review = scan_screenshots(
                    args.db, recipe_images=args.recipe, salvage_images=args.salvage,
                    backend=args.backend, scale=args.scale,
                )
            except OCRError as exc:
                raise CalculationError(str(exc)) from exc
            Path(args.output).write_text(json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"Wrote {args.output}")
            print(f"Recognized {len(review['recipes'])} recipe rows and {len(review['inventory'])} salvage types.")
            if review.get("needs_review"):
                print("Review required: inspect recognition confidence/quantities/capacity before calculation.")
            else:
                print("Recognition is high-confidence, but confirmation is still required before calculation.")
        elif args.command == "calculate-review":
            from .recognition import calculate_review, load_review
            review = load_review(args.input)
            result = calculate_review(args.db, review, require_confirmed=not args.allow_unconfirmed)
            rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n" if args.format == "json" else format_text_result(result)
            if args.output:
                Path(args.output).write_text(rendered, encoding="utf-8")
                print(f"Wrote {args.output}", file=sys.stderr)
            else:
                print(rendered, end="")
        elif args.command == "memory-read":
            from .game_memory import GameInventoryReader, GameMemoryError, list_city_of_heroes_processes, review_from_memory_snapshot
            processes = list_city_of_heroes_processes()
            if args.pid is None:
                if not processes:
                    raise CalculationError("No running cityofheroes.exe clients were found")
                if len(processes) != 1:
                    print("Multiple City of Heroes clients are running; rerun with --pid:", file=sys.stderr)
                    for proc in processes:
                        print(f"  {proc.label}", file=sys.stderr)
                    raise SystemExit(2)
                process = processes[0]
            else:
                process = next((x for x in processes if x.pid == args.pid), args.pid)
            try:
                snap = GameInventoryReader(args.db, alias_path=args.aliases).read(process)
            except GameMemoryError as exc:
                raise CalculationError(str(exc)) from exc
            review = review_from_memory_snapshot(snap)
            rendered = json.dumps(review, ensure_ascii=False, indent=2) + "\n"
            if args.output:
                Path(args.output).write_text(rendered, encoding="utf-8")
                print(f"Wrote {args.output}")
            else:
                print(rendered, end="")
        elif args.command == "gui":
            from .gui import launch_gui
            launch_gui(db_path=args.db)
        elif args.command == "parse-capacity":
            observation = parse_capacity_text(args.text)
            print(json.dumps({
                "used": observation.used,
                "capacity": observation.capacity,
                "source": observation.source,
                "raw_text": observation.raw_text,
            }, indent=2))
        else:
            payload = load_payload(args.input)
            result = calculate_payload(args.db, payload)
            rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n" if args.format == "json" else format_text_result(result)
            if args.output:
                Path(args.output).write_text(rendered, encoding="utf-8")
                print(f"Wrote {args.output}", file=sys.stderr)
            else:
                print(rendered, end="")
    except CalculationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
