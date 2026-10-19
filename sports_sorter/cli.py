from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

from .core import (LABELS, Database, classify, copy_images, duplicates, export_json,
                   scan, set_reviews, summary, verify)
from .engines import load_engine


def main(argv=None):
    parser = argparse.ArgumentParser(description="Copy-only local sports image sorter")
    parser.add_argument("--db", default=".sports-sorter/state.sqlite", help="Private SQLite database, outside source")
    commands = parser.add_subparsers(dest="action", required=True)
    scanning = commands.add_parser("scan")
    scanning.add_argument("source", type=Path)
    classifying = commands.add_parser("classify")
    classifying.add_argument("--engine", default="vision")
    classifying.add_argument("--revision", type=int, default=2)
    classifying.add_argument("--retry", action="store_true")
    classifying.add_argument("--command", help="Explicitly trusted command adapter; may have external side effects")
    classifying.add_argument("--model-id")
    for action in ("copy", "verify"):
        command = commands.add_parser(action)
        command.add_argument("output", type=Path)
        command.add_argument("--engine-key")
        if action == "copy":
            command.add_argument("--dry-run", action="store_true")
    commands.add_parser("summary").add_argument("--engine-key")
    commands.add_parser("duplicates")
    commands.add_parser("export").add_argument("path", type=Path)
    reviewing = commands.add_parser("review")
    reviewing.add_argument("image_ids", type=int, nargs="+")
    reviewing.add_argument("--label", choices=LABELS, required=True)
    reviewing.add_argument("--evidence", default="")
    reviewing.add_argument("--human-verified", action="store_true", help="Only set after independent human visual confirmation")
    serving = commands.add_parser("serve")
    serving.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        with Database(args.db) as db:
            if args.action == "scan":
                result = scan(args.source, db)
            elif args.action == "classify":
                result = classify(db, load_engine(args.engine, args.revision, args.command, args.model_id), args.retry)
            elif args.action == "copy":
                result = copy_images(db, args.output, args.engine_key, args.dry_run)
            elif args.action == "verify":
                result = verify(db, args.output, args.engine_key)
            elif args.action == "summary":
                result = summary(db, args.engine_key)
            elif args.action == "duplicates":
                result = duplicates(db)
            elif args.action == "export":
                result = export_json(db, args.path)
            elif args.action == "review":
                result = {"updated": set_reviews(db, args.image_ids, args.label, args.evidence, args.human_verified)}
            else:
                from .server import serve
                serve(db.path, port=args.port)
                return 0
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if args.action == "verify" and not result["ok"] else 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Error ({type(error).__name__}): {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
