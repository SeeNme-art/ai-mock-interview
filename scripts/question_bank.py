"""Import the local knowledge question seed and inspect eligible candidates."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from app.kb.seed import read_seed
from app.kb.store import import_seed, list_questions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "question_bank.db")
    actions = parser.add_subparsers(dest="action", required=True)
    load = actions.add_parser("import")
    load.add_argument("--seed", type=Path, default=ROOT / "data" / "seeds" / "knowledge_questions.json")
    listing = actions.add_parser("list")
    listing.add_argument("--all-statuses", action="store_true", help="include draft and retired questions for inspection")
    listing.add_argument("--dimension", choices=("agent", "rag", "backend"))
    listing.add_argument("--difficulty", choices=("basic", "applied"))
    listing.add_argument("--exclude", action="append", default=[], metavar="QUESTION_ID")
    args = parser.parse_args()
    try:
        if args.action == "import":
            result = import_seed(args.db, read_seed(args.seed))
        else:
            result = list_questions(args.db, dimension=args.dimension, difficulty=args.difficulty,
                                    exclude_ids=args.exclude, verified_only=not args.all_statuses)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"题库操作失败：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
