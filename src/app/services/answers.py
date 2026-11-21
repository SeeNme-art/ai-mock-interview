"""Accept one text answer for the currently displayed exchange."""
from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.db.business import (
    OWNER_ID, SessionError, check_business_schema, ensure_session_idle, find_replay,
    find_session, validate_request_key,
)
from app.db.sqlite import canonical_json, connect, sha256

MAX_ANSWER_CHARS = 8000


def _normalize_answer(answer: str) -> str:
    if not isinstance(answer, str):
        raise SessionError("INVALID_INPUT", "回答必须是文本")
    text = answer.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise SessionError("INVALID_INPUT", "回答不能为空")
    if len(text) > MAX_ANSWER_CHARS:
        raise SessionError("INVALID_INPUT", f"回答最多 {MAX_ANSWER_CHARS} 个字符")
    if any((ord(char) < 32 and char not in "\n\t") or 127 <= ord(char) <= 159 for char in text):
        raise SessionError("INVALID_INPUT", "回答含有不支持的控制字符，请清理后重试")
    try:
        text.encode("utf-8")
    except UnicodeError as exc:
        raise SessionError("INVALID_INPUT", "回答必须是有效的 Unicode 文本") from exc
    return text


def accept_answer(path: Path, session_id: str, turn_id: str, exchange_id: str,
                  expected_revision: int, key: str, answer: str) -> dict:
    validate_request_key(key)
    if type(expected_revision) is not int or expected_revision < 1:
        raise SessionError("INVALID_INPUT", "会话 revision 须为正整数")
    text = _normalize_answer(answer)
    if not path.is_file():
        raise SessionError("RESOURCE_NOT_FOUND", "找不到会话，请先准备正式题目")
    request = {"session_id": session_id, "turn_id": turn_id, "exchange_id": exchange_id,
               "expected_revision": expected_revision, "answer_mode": "text"}
    digest = sha256(canonical_json({**request, "answer": text}))
    receipt = {"session_id": session_id, "turn_id": turn_id, "exchange_id": exchange_id,
               "revision": expected_revision + 1, "accepted": True}
    with closing(connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        version = check_business_schema(db)
        session = find_session(db, session_id)
        revision = session["revision"]
        if version >= 3:
            replay = find_replay(db, "answer", key, digest)
            if replay is not None:
                return {**receipt, "operation_id": replay["id"]}
        if revision != expected_revision:
            raise SessionError("STALE_REVISION", "会话进度已更新，请刷新后再提交", current_revision=revision)
        if version < 3 or session["phase"] != "ACTIVE":
            raise SessionError("ILLEGAL_ACTION", "请在面试开始且正式题目展示后提交", current_revision=revision)
        exchange = db.execute("""SELECT t.state,e.round_no,e.answer_text IS NOT NULL AS answered,
               (SELECT max(round_no) FROM exchange WHERE turn_id=t.id) AS current_round
            FROM turn t JOIN exchange e ON e.turn_id=t.id
            WHERE t.session_id=? AND t.id=? AND e.id=?""", (session_id, turn_id, exchange_id)).fetchone()
        if exchange is None:
            raise SessionError("RESOURCE_NOT_FOUND", "找不到该会话的问答轮次")
        if exchange["answered"]:
            raise SessionError("ILLEGAL_ACTION", "这一轮已经提交过回答", current_revision=revision)
        if exchange["state"] != "awaiting_answer" or exchange["round_no"] != exchange["current_round"]:
            raise SessionError("ILLEGAL_ACTION", "这一轮当前不能提交回答", current_revision=revision)
        ensure_session_idle(db, session)

        now = datetime.now(UTC).isoformat()
        updated = db.execute("""UPDATE exchange SET answer_text=?,answer_mode='text',answered_at=?
                                WHERE id=? AND turn_id=? AND answer_text IS NULL""", (text, now, exchange_id, turn_id))
        if updated.rowcount != 1:
            raise SessionError("ILLEGAL_ACTION", "这一轮已经提交过回答", current_revision=revision)
        db.execute("UPDATE turn SET state='processing' WHERE id=? AND session_id=?", (turn_id, session_id))
        epoch = session["control_epoch"] + 1
        db.execute("UPDATE interview_session SET revision=revision+1,control_epoch=? WHERE id=?", (epoch, session_id))
        operation_id = uuid4().hex
        db.execute("""INSERT INTO operation (id,session_id,owner_id,kind,idempotency_key,request_hash,
                      expected_epoch,status,safe_request_json,created_at,updated_at)
                      VALUES (?,?,?,'answer',?,?,?,'queued',?,?,?)""",
                   (operation_id, session_id, OWNER_ID, key, digest, epoch, canonical_json(request), now, now))
        return {**receipt, "operation_id": operation_id}
