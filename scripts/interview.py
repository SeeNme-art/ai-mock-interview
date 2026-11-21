"""Start a planned local interview or read its saved first question."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from app.db.business import SessionError
from app.services.first_question import accept_start, read_state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "interviews.db")
    parser.add_argument("--bank", type=Path, default=ROOT / "data" / "question_bank.db")
    actions = parser.add_subparsers(dest="action", required=True)
    start = actions.add_parser("start", help="freeze the plan and prepare the first question")
    start.add_argument("session_id")
    start.add_argument("--expected-revision", required=True, type=int)
    start.add_argument("--key", required=True)
    actions.add_parser("state", help="read already saved progress").add_argument("session_id")
    resume = actions.add_parser("resume-start", help="explicitly continue a saved start command")
    resume.add_argument("session_id")
    resume.add_argument("operation_id")
    args = parser.parse_args()
    try:
        if args.action != "state":
            from app.workflow.interview import run_first_question

            if args.action == "start":
                operation_id, accepted = accept_start(args.db, args.session_id, args.expected_revision, args.key)
                if accepted:
                    run_first_question(args.db, args.bank, args.session_id, operation_id)
            else:
                run_first_question(args.db, args.bank, args.session_id, args.operation_id)
        result = read_state(args.db, args.session_id)
    except ModuleNotFoundError as exc:
        if exc.name not in {"langgraph", "langsmith"}:
            raise
        print("首题流程需要 LangGraph，请使用已安装项目 LangGraph 依赖的 Python 环境。", file=sys.stderr)
        return 1
    except SessionError as exc:
        print(json.dumps({"code": exc.code, "message": str(exc), "current_revision": exc.current_revision},
                         ensure_ascii=False), file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error):
        print("暂时无法读写业务库或题库。可用 state 检查已保存进度，再继续已登记的开始操作。", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
