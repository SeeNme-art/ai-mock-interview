"""Accept a start command and publish its first reviewed question once."""
from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from app.db.business import (
    OWNER_ID, SessionError, check_business_schema, ensure_session_idle, find_operation, find_replay,
    find_session, operation_epoch, upgrade_business, validate_request_key,
)
from app.db.sqlite import canonical_json, connect, sha256
from app.services.question_delivery import publish_plan_item, session_progress

STEP = "prepare_first_question"


def accept_start(path: Path, session_id: str, expected_revision: int, key: str) -> tuple[str, bool]:
    validate_request_key(key)
    if type(expected_revision) is not int or expected_revision < 1:
        raise ValueError("会话 revision 须为正整数")
    if not path.is_file():
        raise SessionError("RESOURCE_NOT_FOUND", "业务库尚未创建，请先确认材料")
    request = canonical_json({"session_id": session_id, "expected_revision": expected_revision})
    with closing(connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        version = check_business_schema(db)
        session = find_session(db, session_id)
        if version >= 2:
            replay = find_replay(db, "start", key, sha256(request))
            if replay:
                return replay["id"], False
        revision = session["revision"]
        if session["phase"] != "PLANNING":
            raise SessionError("ILLEGAL_ACTION", "会话已开始或结束，请读取当前进度", current_revision=revision)
        if revision != expected_revision:
            raise SessionError("STALE_REVISION", "会话进度已更新，请刷新后再开始", current_revision=revision)
        if version < 2 or not session["plan_version"]:
            raise SessionError("ILLEGAL_ACTION", "请先生成面试计划", current_revision=revision)
        ensure_session_idle(db, session)
        slot = _first_slot(db, session)
        if slot["status"] != "pending":
            raise SessionError("ILLEGAL_ACTION", "首个计划项已经处理", current_revision=revision)
        if version == 2:
            upgrade_business(db, path, 3)
        epoch = session["control_epoch"] + 1
        now = datetime.now(UTC).isoformat()
        operation_id = uuid4().hex
        db.execute("UPDATE interview_session SET phase='ACTIVE', revision=revision+1, control_epoch=? WHERE id=?",
                   (epoch, session_id))
        db.execute("""INSERT INTO operation (id, session_id, owner_id, kind, idempotency_key, request_hash,
                      expected_epoch, status, safe_request_json, created_at, updated_at)
                      VALUES (?,?,?,'start',?,?,?,'queued',?,?,?)""",
                   (operation_id, session_id, OWNER_ID, key, sha256(request), epoch, request, now, now))
        return operation_id, True


def _first_slot(db: sqlite3.Connection, session: sqlite3.Row) -> sqlite3.Row:
    slot = db.execute("SELECT * FROM plan_item WHERE session_id=? AND plan_version=? AND seq=1",
                      (session["id"], session["plan_version"])).fetchone()
    if slot is None or slot["task_type"] != "knowledge":
        raise SessionError("ILLEGAL_ACTION", "当前计划缺少可处理的知识首题，请检查计划")
    return slot


def start_epoch(path: Path, session_id: str, operation_id: str) -> int:
    return operation_epoch(path, session_id, operation_id, "start")


def prepare_first_question(path: Path, bank: Path, session_id: str, operation_id: str, epoch: int) -> dict:
    """Publish the original question and its readiness receipt in one transaction."""
    with closing(connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        check_business_schema(db)
        session = find_session(db, session_id)
        operation = find_operation(db, session_id, operation_id, "start")
        result = db.execute("SELECT result_json FROM operation_step WHERE operation_id=? AND step_key=?",
                            (operation_id, STEP)).fetchone()
        if result:
            return json.loads(result["result_json"])
        if (operation["status"] != "queued" or session["phase"] != "ACTIVE"
                or epoch != operation["expected_epoch"] or epoch != session["control_epoch"]):
            raise SessionError("ILLEGAL_ACTION", "开始操作已失效，请读取当前进度")
        slot = _first_slot(db, session)
        if slot["status"] != "pending":
            raise SessionError("ILLEGAL_ACTION", "首个计划项已经处理")
        now = datetime.now(UTC).isoformat()
        refs, error_code = publish_plan_item(db, bank, slot, now)
        status = "failed" if error_code else "succeeded"
        refs_json = canonical_json(refs)
        db.execute("INSERT INTO operation_step VALUES (?,?,?,?,1,?)",
                   (operation_id, STEP, status, refs_json, canonical_json({"plan_version": slot["plan_version"]})))
        db.execute("UPDATE operation SET status=?, result_refs_json=?, error_code=?, updated_at=? WHERE id=?",
                   (status, refs_json, error_code, now, operation_id))
        db.execute("UPDATE interview_session SET revision=revision+1 WHERE id=?", (session_id,))
        return refs


def read_state(path: Path, session_id: str) -> dict:
    with closing(connect(path, readonly=True)) as db, db:
        db.execute("BEGIN")
        version = check_business_schema(db)
        session = find_session(db, session_id)
        operation = current = None
        if version >= 2:
            operation = db.execute("""SELECT id AS operation_id,kind,status,error_code,result_refs_json FROM operation
                         WHERE session_id=? AND owner_id=? AND kind IN ('start','answer','evaluate_answer','advance','finalize')
                         ORDER BY expected_epoch DESC LIMIT 1""", (session_id, OWNER_ID)).fetchone()
        if version >= 3:
            current = db.execute("""SELECT t.id AS turn_id,e.id AS exchange_id,p.seq,e.round_no,t.state,e.question_text AS question,
                                  t.task_type,t.dimension,t.difficulty,t.fallback_reason,e.answer_text AS answer,e.answered_at
                                  FROM turn t JOIN plan_item p ON p.id=t.plan_item_id
                                  JOIN exchange e ON e.turn_id=t.id AND e.round_no=(
                                      SELECT max(round_no) FROM exchange WHERE turn_id=t.id
                                  ) WHERE t.session_id=? AND t.state IN ('awaiting_answer','processing') AND ?='ACTIVE'""",
                                 (session_id, session["phase"])).fetchone()
        visible = dict(current) if current is not None else None
        if visible is not None and visible["answer"] is None:
            del visible["answer"], visible["answered_at"]
        operation_view = dict(operation) if operation is not None else None
        if operation_view is not None:
            refs = json.loads(operation_view.pop("result_refs_json") or "{}")
            if operation_view["kind"] == "answer" and "decision" in refs:
                operation_view["decision"] = refs["decision"]
                operation_view["decision_reason"] = refs["decision_reason"]
            if operation_view["kind"] == "evaluate_answer":
                operation_view["evaluation_status"] = refs.get("evaluation_status", "pending")
            if operation_view["kind"] == "finalize" and "evaluation_status" in refs:
                operation_view["evaluation_status"] = refs["evaluation_status"]
            if operation_view["kind"] == "advance" and "outcome" in refs:
                operation_view["outcome"] = refs["outcome"]
                operation_view["unavailable"] = refs["unavailable"]
        return {
            "session_id": session_id, "phase": session["phase"], "revision": session["revision"],
            "plan_version": session["plan_version"] if version >= 2 else 0,
            "end_reason": session["end_reason"], "ended_at": session["ended_at"],
            "progress": session_progress(db, session, version), "current": visible, "operation": operation_view,
        }
