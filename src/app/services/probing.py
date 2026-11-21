"""Validate an offline follow-up proposal and commit a single decision."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from typing import Callable
from unicodedata import normalize
from uuid import uuid4

from app.db.business import SessionError, check_business_schema, find_operation, find_session
from app.db.sqlite import canonical_json, connect
from app.schemas.probing import ProbeProposal
from app.services.answers import ExchangeRecord, read_exchanges
from app.services.scoring import locate_evidence

STEP = "decide_follow_up"
MAX_PROBES = 2


@dataclass(frozen=True)
class ProbeContext:
    turn_id: str
    objective: str = field(repr=False)
    exchanges: tuple[ExchangeRecord, ...]
    asked_questions: tuple[str, ...] = field(repr=False)
    reference_points: tuple[str, ...] = field(repr=False)


DemoProposer = Callable[[ProbeContext], object]


def _compare_text(value: str) -> str:
    return "".join(char for char in normalize("NFKC", value).casefold() if char.isalnum())


def _validate_proposal(value: object, context: ProbeContext) -> ProbeProposal:
    proposal = ProbeProposal.model_validate(value)
    answers = {exchange.id: exchange.answer for exchange in context.exchanges if exchange.answer is not None}
    evidence = [locate_evidence(ref, answers) for ref in proposal.evidence_refs]
    if proposal.should_probe:
        if not any(ref.exchange_id == context.exchanges[-1].id for ref in evidence):
            raise ValueError("追问必须引用当前轮的已提交答案")
        question = proposal.probe_question
        key = _compare_text(question)
        if not key or not question.isprintable() or any(key == _compare_text(old) for old in context.asked_questions):
            raise ValueError("追问题面不可用或重复")
        if any(normalized and normalized in key for normalized in map(_compare_text, context.reference_points)):
            raise ValueError("追问题面直接复述了参考答案")
    return proposal.model_copy(update={"evidence_refs": evidence})


def _load_work(db: sqlite3.Connection, session_id: str, operation_id: str, epoch: int) -> tuple[ProbeContext | None, dict | None]:
    check_business_schema(db)
    session = find_session(db, session_id)
    operation = find_operation(db, session_id, operation_id, "answer")
    saved = db.execute("SELECT result_json FROM operation_step WHERE operation_id=? AND step_key=?",
                       (operation_id, STEP)).fetchone()
    if saved is not None:
        return None, json.loads(saved["result_json"])["refs"]
    if (session["phase"] != "ACTIVE" or operation["status"] != "queued"
            or session["control_epoch"] != epoch or operation["expected_epoch"] != epoch):
        raise SessionError("ILLEGAL_ACTION", "回答处理操作已失效，请读取当前进度")
    request = json.loads(operation["safe_request_json"])
    turn = db.execute("SELECT * FROM turn WHERE id=? AND session_id=? AND state='processing'",
                      (request["turn_id"], session_id)).fetchone()
    if turn is None:
        raise SessionError("ILLEGAL_ACTION", "没有与该操作匹配的待处理题目")
    exchanges = read_exchanges(db, turn["id"])
    if (not exchanges or exchanges[-1].id != request["exchange_id"] or any(item.answer is None for item in exchanges)
            or [item.round_no for item in exchanges] != list(range(turn["probe_count"] + 1))):
        raise SessionError("ILLEGAL_ACTION", "问答轮次与已保存操作不一致")
    asked = tuple(row[0] for row in db.execute("""SELECT e.question_text FROM exchange e JOIN turn t ON t.id=e.turn_id
                                                 WHERE t.session_id=? ORDER BY t.id,e.round_no""", (session_id,)))
    question = json.loads(turn["question_snapshot_json"])
    rubric = json.loads(turn["rubric_snapshot_json"])
    reference = tuple(rubric.get("answer_points", ())) + tuple(rubric.get("acceptable_alternatives", ()))
    return ProbeContext(turn["id"], question["objective"], exchanges, asked, reference), None


def _decide(context: ProbeContext, proposer: DemoProposer | None) -> tuple[ProbeProposal | None, str, str | None]:
    if context.exchanges[-1].round_no >= MAX_PROBES:
        return None, "round_limit", None
    if proposer is None:
        raise SessionError("CONFIGURATION_ERROR", "本批需显式提供本地追问样例才能处理未达上限的轮次")
    try:
        proposal = _validate_proposal(proposer(context), context)
    except SessionError:
        raise
    except OSError:
        return None, "provider_unavailable", "MODEL_UNAVAILABLE"
    except ValueError:
        return None, "invalid_proposal", "OUTPUT_INVALID"
    return proposal, "probe" if proposal.should_probe else "no_probe", None


def process_answer(path: Path, session_id: str, operation_id: str, epoch: int, proposer: DemoProposer | None) -> dict:
    with closing(connect(path, readonly=True)) as db, db:
        db.execute("BEGIN")
        context, saved = _load_work(db, session_id, operation_id, epoch)
    if saved is not None:
        return {key: saved[key] for key in ("turn_id", "exchange_id")}
    proposal, reason_code, error_code = _decide(context, proposer)

    with closing(connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        current, saved = _load_work(db, session_id, operation_id, epoch)
        if saved is not None:
            return {key: saved[key] for key in ("turn_id", "exchange_id")}
        if current != context:
            raise SessionError("ILLEGAL_ACTION", "问答已更新，请读取当前进度")
        decision = "score_pending"
        exchange_id = context.exchanges[-1].id
        result = {}
        if proposal is not None:
            detail = {"reason": proposal.reason, "evidence_refs": [ref.model_dump() for ref in proposal.evidence_refs]}
            if proposal.should_probe:
                exchange_id = uuid4().hex
                round_no = context.exchanges[-1].round_no + 1
                db.execute("INSERT INTO exchange(id,turn_id,round_no,question_text,purpose,reason_json) VALUES (?,?,?,?,?,?)",
                           (exchange_id, context.turn_id, round_no, proposal.probe_question, proposal.purpose, canonical_json(detail)))
                db.execute("UPDATE turn SET state='awaiting_answer',probe_count=? WHERE id=?", (round_no, context.turn_id))
                decision = "awaiting_answer"
            else:
                result["detail"] = detail
        refs = {"turn_id": context.turn_id, "exchange_id": exchange_id,
                "decision": decision, "decision_reason": reason_code}
        result["refs"] = refs
        now = datetime.now(UTC).isoformat()
        version = {"proposer": None if reason_code == "round_limit" else "local-demo-v1"}
        db.execute("INSERT INTO operation_step VALUES (?,?,'succeeded',?,1,?)",
                   (operation_id, STEP, canonical_json(result), canonical_json(version)))
        db.execute("UPDATE operation SET status='succeeded',result_refs_json=?,error_code=?,updated_at=? WHERE id=?",
                   (canonical_json(refs), error_code, now, operation_id))
        db.execute("UPDATE interview_session SET revision=revision+1 WHERE id=?", (session_id,))
        return {"turn_id": context.turn_id, "exchange_id": exchange_id}
