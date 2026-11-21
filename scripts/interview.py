"""Start a planned local interview, submit a text answer, or read saved progress."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from app.db.business import SessionError
from app.services.answers import MAX_ANSWER_CHARS, accept_answer
from app.services.first_question import accept_start, read_state


def read_answer(path: Path | None) -> str:
    limit = MAX_ANSWER_CHARS * 4 + 3
    if path is None:
        raw = sys.stdin.buffer.read(limit + 1)
    else:
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise SessionError("INVALID_INPUT", f"回答最多 {MAX_ANSWER_CHARS} 个字符")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeError as exc:
        raise SessionError("INVALID_INPUT", "请用 UTF-8 文本提交回答") from exc


def read_demo_proposal(path: Path) -> object:
    try:
        with path.open("rb") as stream:
            raw = stream.read(32769)
    except OSError as exc:
        raise SessionError("CONFIGURATION_ERROR", "无法读取本地追问样例") from exc
    if len(raw) > 32768:
        raise ValueError("本地追问样例超过长度限制")
    return json.loads(raw.decode("utf-8-sig"))


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
    answer = actions.add_parser("answer", help="read UTF-8 text from stdin or --answer-file and save it")
    answer.add_argument("session_id")
    answer.add_argument("turn_id")
    answer.add_argument("exchange_id")
    answer.add_argument("--expected-revision", required=True, type=int)
    answer.add_argument("--key", required=True)
    answer.add_argument("--answer-file", type=Path)
    process = actions.add_parser("process-answer", help="process a saved answer using an optional local demo proposal")
    process.add_argument("session_id")
    process.add_argument("operation_id")
    process.add_argument("--demo-proposal-file", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "answer":
            result = accept_answer(args.db, args.session_id, args.turn_id, args.exchange_id,
                                   args.expected_revision, args.key, read_answer(args.answer_file))
        elif args.action == "state":
            result = read_state(args.db, args.session_id)
        else:
            from app.workflow.interview import run_answer, run_first_question

            if args.action == "start":
                operation_id, accepted = accept_start(args.db, args.session_id, args.expected_revision, args.key)
                if accepted:
                    run_first_question(args.db, args.bank, args.session_id, operation_id)
            elif args.action == "resume-start":
                run_first_question(args.db, args.bank, args.session_id, args.operation_id)
            else:
                proposer = (lambda _: read_demo_proposal(args.demo_proposal_file)) if args.demo_proposal_file is not None else None
                run_answer(args.db, args.session_id, args.operation_id, proposer)
            result = read_state(args.db, args.session_id)
    except ModuleNotFoundError as exc:
        if exc.name not in {"langgraph", "langsmith"}:
            raise
        print("面试流程需要 LangGraph，请使用已安装项目 LangGraph 依赖的 Python 环境。", file=sys.stderr)
        return 1
    except SessionError as exc:
        print(json.dumps({"code": exc.code, "message": str(exc), "current_revision": exc.current_revision},
                         ensure_ascii=False), file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error):
        print("暂时无法读取输入或访问数据库。请用 state 核对已保存进度后再重试。", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
