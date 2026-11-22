"""Close skipped questions and move an active interview through its frozen plan."""
from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from app.db.business import (
    OWNER_ID, SessionError, cancel_active_operations, check_business_schema, ensure_session_idle, find_operation, find_replay,
    find_session, validate_request_key,
)
from app.db.sqlite import canonical_json, connect, sha256
from app.services.question_delivery import finish_if_all_closed, publish_plan_item

ADVANCE = "advance"
SKIP = "skip"
STEP = "advance_plan"


def _ready_plan(db: sqlite3.Connection, session: sqlite3.Row) -> list[sqlite3.Row]:
    slots = db.execute("SELECT * FROM plan_item WHERE session_id=? AND plan_version=? ORDER BY seq",
                       (session["id"], session["plan_version"])).fetchall()
    if len(slots) != session["question_count"] or not any(row["status"] == "closed" for row in slots):
        raise SessionError("ILLEGAL_ACTION", "需要先完成首个计划项，再推进面试")
    if (any(row["status"] == "active" for row in slots)
            or db.execute("SELECT 1 FROM turn WHERE session_id=? AND state<>'closed'", (session["id"],)).fetchone()):
        raise SessionError("ILLEGAL_ACTION", "当前题目尚未收尾，请先完成当前题", current_revision=session["revision"])
    return slots


def _queue_transition(db: sqlite3.Connection, session: sqlite3.Row, kind: str, key: str,
                      digest: str, request: str, now: str) -> str:
    epoch = session["control_epoch"] + 1
    operation_id = uuid4().hex
    db.execute("""INSERT INTO operation(id,session_id,owner_id,kind,idempotency_key,request_hash,expected_epoch,
                  status,safe_request_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,'queued',?,?,?)""",
               (operation_id, session["id"], OWNER_ID, kind, key, digest, epoch, request, now, now))
    db.execute("UPDATE interview_session SET revision=revision+1,control_epoch=? WHERE id=?", (epoch, session["id"]))
    return operation_id


def accept_advance(path: Path, session_id: str, expected_revision: int, key: str) -> tuple[str, bool]:
    validate_request_key(key)
    if type(expected_revision) is not int or expected_revision < 1:
        raise SessionError("INVALID_INPUT", "会话 revision 须为正整数")
    if not path.is_file():
        raise SessionError("RESOURCE_NOT_FOUND", "找不到待推进的会话")
    request = canonical_json({"session_id": session_id, "expected_revision": expected_revision})
    digest = sha256(request)
    with closing(connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        version = check_business_schema(db)
        session = find_session(db, session_id)
        replay = find_replay(db, ADVANCE, key, digest) if version >= 2 else None
        if replay is not None:
            return replay["id"], False
        if version < 3 or session["phase"] != "ACTIVE":
            raise SessionError("ILLEGAL_ACTION", "当前会话不能推进到下一题", current_revision=session["revision"])
        if session["revision"] != expected_revision:
            raise SessionError("STALE_REVISION", "会话进度已更新，请刷新后再推进", current_revision=session["revision"])
        ensure_session_idle(db, session)
        _ready_plan(db, session)
        return _queue_transition(db, session, ADVANCE, key, digest, request, datetime.now(UTC).isoformat()), True


def accept_skip(path: Path, session_id: str, turn_id: str, expected_revision: int, key: str) -> tuple[str, bool]:
    validate_request_key(key)
    if type(expected_revision) is not int or expected_revision < 1:
        raise SessionError("INVALID_INPUT", "会话 revision 须为正整数")
    if not path.is_file():
        raise SessionError("RESOURCE_NOT_FOUND", "找不到可访问的会话")
    request = {"session_id": session_id, "turn_id": turn_id, "expected_revision": expected_revision}
    digest = sha256(canonical_json(request))
    with closing(connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        version = check_business_schema(db)
        session = find_session(db, session_id)
        replay = find_replay(db, SKIP, key, digest) if version >= 2 else None
        if replay is not None:
            return replay["id"], False
        revision = session["revision"]
        if version < 3 or session["phase"] != "ACTIVE":
            raise SessionError("ILLEGAL_ACTION", "当前会话不能跳题", current_revision=revision)
        if revision != expected_revision:
            raise SessionError("STALE_REVISION", "会话进度已更新，请刷新后再跳过", current_revision=revision)
        turn = db.execute("""SELECT p.id AS plan_item_id,p.seq FROM turn t
                             JOIN plan_item p ON p.session_id=t.session_id AND p.id=t.plan_item_id
                             WHERE t.id=? AND t.session_id=? AND t.state<>'closed'
                               AND p.status='active' AND p.plan_version=?""",
                          (turn_id, session_id, session["plan_version"])).fetchone()
        if turn is None:
            raise SessionError("ILLEGAL_ACTION", "只能跳过当前仍在进行的题目", current_revision=revision)
        if version >= 4:
            evaluation = db.execute("SELECT status FROM evaluation WHERE turn_id=?", (turn_id,)).fetchone()
            if evaluation is not None:
                if evaluation["status"] != "pending":
                    raise SessionError("ILLEGAL_ACTION", "评分已收尾，不能再跳过该题", current_revision=revision)
                # This placeholder has no score or evidence. Answers remain in exchange.
                db.execute("DELETE FROM evaluation WHERE turn_id=? AND status='pending'", (turn_id,))
        now = datetime.now(UTC).isoformat()
        cancel_active_operations(db, session_id, now)
        db.execute("UPDATE turn SET state='closed',closed_at=? WHERE id=?", (now, turn_id))
        db.execute("UPDATE plan_item SET status='closed',outcome='skipped' WHERE id=?", (turn["plan_item_id"],))
        receipt = canonical_json({**request, "skipped_seq": turn["seq"]})
        return _queue_transition(db, session, SKIP, key, digest, receipt, now), True


def advance_plan(path: Path, bank: Path, session_id: str, operation_id: str, epoch: int, *, kind: str = ADVANCE) -> dict:
    with closing(connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        check_business_schema(db)
        session = find_session(db, session_id)
        operation = find_operation(db, session_id, operation_id, kind)
        saved = db.execute("SELECT result_json FROM operation_step WHERE operation_id=? AND step_key=?",
                           (operation_id, STEP)).fetchone()
        if saved is not None:
            refs = json.loads(saved["result_json"])
            return {"turn_id": refs["turn_id"], "exchange_id": refs["exchange_id"]}
        if (session["phase"] != "ACTIVE" or operation["status"] != "queued"
                or session["control_epoch"] != epoch or operation["expected_epoch"] != epoch):
            raise SessionError("ILLEGAL_ACTION", "推进操作已失效，请读取当前进度")
        pending = [slot for slot in _ready_plan(db, session) if slot["status"] == "pending"]
        unavailable = []
        refs = {"turn_id": None, "exchange_id": None}
        now = datetime.now(UTC).isoformat()
        outcome = "finished"
        for slot in pending:
            refs, error_code = publish_plan_item(db, bank, slot, now)
            if error_code is None:
                outcome = "question_ready"
                break
            unavailable.append({"seq": slot["seq"], "reason_code": error_code})
        if outcome == "finished" and not finish_if_all_closed(db, session_id, session["plan_version"], now):
            raise SessionError("ILLEGAL_ACTION", "还有未收尾的题目，不能结束面试")

        details = {**refs, "outcome": outcome, "unavailable": unavailable}
        if kind == SKIP:
            details["skipped_seq"] = json.loads(operation["safe_request_json"])["skipped_seq"]
        result = canonical_json(details)
        db.execute("INSERT INTO operation_step VALUES (?,?,'succeeded',?,1,?)",
                   (operation_id, STEP, result, canonical_json({"plan_version": session["plan_version"]})))
        db.execute("UPDATE operation SET status='succeeded',result_refs_json=?,updated_at=? WHERE id=?",
                   (result, now, operation_id))
        db.execute("UPDATE interview_session SET revision=revision+1 WHERE id=?", (session_id,))
        return refs
