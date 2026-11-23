"""End an interview immediately and finalize any submitted answer separately."""
from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from app.db.business import (
    OWNER_ID, SessionError, cancel_active_operations, check_business_schema, find_operation, find_replay, find_session,
    upgrade_business, validate_request_key,
)
from app.db.sqlite import canonical_json, connect, sha256
from app.services.answers import read_exchanges
from app.services.evaluation import DemoEvaluator, EvaluationContext, evaluation_context, score_context, store_evaluation

FINALIZE = "finalize"
ABORT_REASONS = ("user_cancel", "too_hard", "no_time")


def _close_plan(db: sqlite3.Connection, session: sqlite3.Row, version: int, now: str) -> str | None:
    answered_turn = None
    if version >= 3:
        active = db.execute("SELECT id,plan_item_id FROM turn WHERE session_id=? AND state<>'closed'",
                            (session["id"],)).fetchone()
        if active is not None:
            if any(item.answer is not None for item in read_exchanges(db, active["id"])):
                answered_turn = active["id"]
                db.execute("UPDATE plan_item SET status='closed',outcome='answered' WHERE id=?", (active["plan_item_id"],))
            db.execute("UPDATE turn SET state='closed',interrupted=1,closed_at=? WHERE id=?", (now, active["id"]))
    db.execute("""UPDATE plan_item SET status='closed',outcome='unanswered'
                  WHERE session_id=? AND plan_version=? AND status<>'closed'""",
               (session["id"], session["plan_version"]))
    return answered_turn


def accept_abort(path: Path, session_id: str, expected_revision: int, key: str, reason: str) -> tuple[str | None, bool]:
    validate_request_key(key)
    if type(expected_revision) is not int or expected_revision < 1 or reason not in ABORT_REASONS:
        raise SessionError("INVALID_INPUT", "中止需提供正整数 revision 和有效的用户结束原因")
    if not path.is_file():
        raise SessionError("RESOURCE_NOT_FOUND", "找不到可访问的会话")
    request = {"session_id": session_id, "expected_revision": expected_revision, "reason": reason}
    digest = sha256(canonical_json(request))
    with closing(connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        version = check_business_schema(db)
        session = find_session(db, session_id)
        replay = find_replay(db, FINALIZE, key, digest) if version >= 2 else None
        if replay is not None:
            return replay["id"], False
        if session["phase"] == "FINISHED":
            existing = db.execute("SELECT id FROM operation WHERE session_id=? AND kind=? AND owner_id=?",
                                  (session_id, FINALIZE, OWNER_ID)).fetchone() if version >= 2 else None
            return existing["id"] if existing is not None else None, False
        if session["revision"] != expected_revision:
            raise SessionError("STALE_REVISION", "会话进度已更新，请刷新后再结束", current_revision=session["revision"])
        if version == 1:
            upgrade_business(db, path, 2)
            session = find_session(db, session_id)
            version = 2
        now, epoch = datetime.now(UTC).isoformat(), session["control_epoch"] + 1
        turn_id = _close_plan(db, session, version, now)
        cancel_active_operations(db, session_id, now)
        db.execute("""UPDATE interview_session SET phase='FINISHED',end_reason=?,ended_at=?,
                      revision=revision+1,control_epoch=? WHERE id=?""", (reason, now, epoch, session_id))
        operation_id = uuid4().hex
        db.execute("""INSERT INTO operation(id,session_id,owner_id,kind,idempotency_key,request_hash,expected_epoch,
                      status,safe_request_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,'queued',?,?,?)""",
                   (operation_id, session_id, OWNER_ID, FINALIZE, key, digest, epoch,
                    canonical_json({**request, "turn_id": turn_id}), now, now))
        return operation_id, True


def _load_work(db: sqlite3.Connection, session_id: str, operation_id: str, epoch: int,
               version: int) -> tuple[EvaluationContext | None, dict, bool]:
    session = find_session(db, session_id)
    operation = find_operation(db, session_id, operation_id, FINALIZE)
    if operation["status"] == "succeeded":
        return None, json.loads(operation["result_refs_json"]), True
    if (session["phase"] != "FINISHED" or session["end_reason"] not in ABORT_REASONS
            or operation["status"] != "queued" or epoch != operation["expected_epoch"] or epoch != session["control_epoch"]):
        raise SessionError("ILLEGAL_ACTION", "结束收尾操作已失效，请读取当前进度")
    turn_id = json.loads(operation["safe_request_json"])["turn_id"]
    refs = {"turn_id": turn_id, "evaluation_id": None}
    if turn_id is None:
        return None, refs, False
    turn = db.execute("""SELECT t.* FROM turn t JOIN plan_item p ON p.id=t.plan_item_id
                         WHERE t.id=? AND t.session_id=? AND t.state='closed' AND t.interrupted=1 AND p.outcome='answered'""",
                      (turn_id, session_id)).fetchone()
    if turn is None:
        raise SessionError("ILLEGAL_ACTION", "待收尾题目与结束记录不一致")
    exchanges = tuple(item for item in read_exchanges(db, turn_id) if item.answer is not None)
    if not exchanges:
        raise SessionError("ILLEGAL_ACTION", "待收尾题目没有已提交答案")
    if version >= 4:
        evaluation = db.execute("SELECT id,status FROM evaluation WHERE turn_id=?", (turn_id,)).fetchone()
        if evaluation is not None:
            refs["evaluation_id"] = evaluation["id"]
            if evaluation["status"] != "pending":
                return None, {**refs, "evaluation_status": evaluation["status"]}, False
    return evaluation_context(turn, exchanges), refs, False


def finalize_interview(path: Path, session_id: str, operation_id: str, epoch: int,
                       evaluator: DemoEvaluator | None, *, model_version: str = "local-demo-v1") -> dict:
    with closing(connect(path, readonly=True)) as db, db:
        db.execute("BEGIN")
        context, refs, done = _load_work(db, session_id, operation_id, epoch, check_business_schema(db))
    if done:
        return refs
    result, error_code = None, "MODEL_UNAVAILABLE"
    diagnostic: dict[str, str] = {}
    if context is not None and evaluator is not None:
        result, error_code = score_context(context, evaluator, diagnostic=diagnostic)
    with closing(connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        version = check_business_schema(db)
        current, current_refs, done = _load_work(db, session_id, operation_id, epoch, version)
        if done:
            return current_refs
        if current != context or current_refs != refs:
            raise SessionError("ILLEGAL_ACTION", "结束收尾依据已更新，请读取当前进度")
        now, version_context = datetime.now(UTC).isoformat(), "{}"
        if version == 2:
            upgrade_business(db, path, 3)
        if context is not None:
            if version == 3:
                upgrade_business(db, path, 4)
            evaluation_id = refs["evaluation_id"]
            if evaluation_id is None:
                evaluation_id = uuid4().hex
                db.execute("""INSERT INTO evaluation(id,turn_id,status,model_context_json,created_at,updated_at)
                              VALUES (?,?,'pending','{}',?,?)""", (evaluation_id, context.turn_id, now, now))
            status, version_context = store_evaluation(db, context, evaluation_id, result, error_code, now,
                                                       evaluator_version=model_version if evaluator else None,
                                                       diagnostic=diagnostic)
            refs = {**refs, "evaluation_id": evaluation_id, "evaluation_status": status}
        saved = canonical_json(refs)
        db.execute("INSERT INTO operation_step VALUES (?,?,'succeeded',?,1,?)",
                   (operation_id, FINALIZE, saved, version_context))
        db.execute("UPDATE operation SET status='succeeded',result_refs_json=?,updated_at=? WHERE id=?",
                   (saved, now, operation_id))
        db.execute("UPDATE interview_session SET revision=revision+1 WHERE id=?", (session_id,))
        return refs
