"""Generate or read a confirmed session's local interview plan."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from app.schemas.planning import DIRECTIONS, PlanChanges
from app.services.planning import PlanningError, rebuild_plan, show_plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "interviews.db")
    parser.add_argument("--bank", type=Path, default=ROOT / "data" / "question_bank.db")
    actions = parser.add_subparsers(dest="action", required=True)
    build = actions.add_parser("build", help="generate or replace the unstarted plan")
    build.add_argument("session_id")
    build.add_argument("--expected-revision", required=True, type=int)
    build.add_argument("--key", required=True, help="use the same key when retrying the same request")
    build.add_argument("--direction", action="append", choices=DIRECTIONS)
    build.add_argument("--difficulty", choices=("basic", "applied"))
    build.add_argument("--count", type=int, choices=(5, 8))
    build.add_argument("--vector", action="store_true", help="rank reviewed questions with the existing local FAISS index")
    build.add_argument("--model-dir", type=Path, default=ROOT / "data" / "index" / "bge-small-zh-v1.5")
    build.add_argument("--index-dir", type=Path, default=ROOT / "data" / "index" / "questions")
    actions.add_parser("show", help="read the saved plan summary").add_argument("session_id")
    args = parser.parse_args()
    try:
        if args.action == "show":
            result = show_plan(args.db, args.session_id)
        else:
            changes = PlanChanges(directions=args.direction, difficulty=args.difficulty, question_count=args.count)
            vector = (args.model_dir, args.index_dir) if args.vector else None
            result = rebuild_plan(args.db, args.bank, args.session_id, args.expected_revision, args.key, changes, vector=vector)
    except PlanningError as exc:
        print(json.dumps({"code": exc.code, "message": str(exc), "current_revision": exc.current_revision},
                         ensure_ascii=False), file=sys.stderr)
        return 1
    except ValidationError as exc:
        print(f"规划配置无效，字段 {exc.errors(include_input=False)[0]['loc'][0]} 请检查取值或重复项。", file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error):
        print("规划数据库暂不可用，请检查路径与库状态。", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
