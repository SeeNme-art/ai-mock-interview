"""Publish reviewed questions and derive progress from the frozen plan."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from app.db.sqlite import canonical_json
from app.kb.seed import prompt_key
from app.kb.store import list_questions
from app.schemas.scoring import CONTENT_DIMENSIONS

RUBRIC_VERSION = "knowledge-v1"


def _choose_question(db: sqlite3.Connection, bank: Path, slot: sqlite3.Row) -> tuple[dict | None, str | None]:
    later = db.execute("""SELECT question_id,topic FROM plan_item
                          WHERE session_id=? AND plan_version=? AND seq>? AND status='pending' AND task_type='knowledge'""",
                       (slot["session_id"], slot["plan_version"], slot["seq"])).fetchall()
    ids = {row["question_id"] for row in later}
    topics = {prompt_key(row["topic"]) for row in later}
    for row in db.execute("SELECT question_snapshot_json FROM turn WHERE session_id=?", (slot["session_id"],)):
        snapshot = json.loads(row["question_snapshot_json"])
        if snapshot.get("id"):
            ids.add(snapshot["id"])
        if snapshot.get("objective"):
            topics.add(prompt_key(snapshot["objective"]))
    asked = {prompt_key(row[0]) for row in db.execute(
        "SELECT e.question_text FROM exchange e JOIN turn t ON t.id=e.turn_id WHERE t.session_id=?", (slot["session_id"],),
    )}
    candidates = list_questions(bank, dimension=slot["dimension"], difficulty=slot["difficulty"], exclude_ids=tuple(sorted(ids)))
    eligible = [row for row in candidates if row["content"].strip() and row["sources"]
                and CONTENT_DIMENSIONS["knowledge"].intersection(row["applicable_dimensions"])
                and prompt_key(row["objective"]) not in topics and prompt_key(row["content"]) not in asked]
    for row in eligible:
        if row["id"] == slot["question_id"]:
            if row["revision"] == slot["question_revision"]:
                return row, None
            if prompt_key(row["objective"]) == prompt_key(slot["topic"]):
                return row, "reviewed_revision_updated"
    fallback = next((row for row in eligible if row["id"] != slot["question_id"]), None)
    return fallback, "planned_question_unavailable"


def _store_question(db: sqlite3.Connection, slot: sqlite3.Row, question: dict, reason: str | None, now: str) -> dict:
    turn_id, exchange_id = uuid4().hex, uuid4().hex
    snapshot = {"id": question["id"], "revision": question["revision"], "content_hash": question["content_hash"],
                "objective": question["objective"], "plan_version": slot["plan_version"]}
    if reason is None and slot["form_reason"] == "verified_bank_faiss":
        snapshot["selection_method"] = "faiss"
    rubric_keys = ("applicable_dimensions", "answer_points", "acceptable_alternatives", "common_errors")
    rubric = {"task_type": "knowledge", "rubric_version": RUBRIC_VERSION,
              **{key: question[key] for key in rubric_keys}}
    db.execute("""INSERT INTO turn (id,session_id,plan_item_id,task_type,dimension,difficulty,generation_method,
                  fallback_reason,state,question_snapshot_json,source_snapshot_json,rubric_snapshot_json,created_at)
                  VALUES (?,?,?,'knowledge',?,?,'original',?,'awaiting_answer',?,?,?,?)""",
               (turn_id, slot["session_id"], slot["id"], question["dimension"], question["difficulty"], reason,
                canonical_json(snapshot), canonical_json(question["sources"]), canonical_json(rubric), now))
    db.execute("INSERT INTO exchange (id,turn_id,round_no,question_text) VALUES (?,?,0,?)",
               (exchange_id, turn_id, question["content"]))
    db.execute("UPDATE plan_item SET status='active' WHERE id=?", (slot["id"],))
    return {"turn_id": turn_id, "exchange_id": exchange_id}


def publish_plan_item(db: sqlite3.Connection, bank: Path, slot: sqlite3.Row, now: str) -> tuple[dict, str | None]:
    if slot["task_type"] == "project":
        error_code = "MODEL_UNAVAILABLE"
    else:
        question, reason = _choose_question(db, bank, slot)
        if question is not None:
            return _store_question(db, slot, question, reason, now), None
        error_code = "QUESTION_UNAVAILABLE"
    db.execute("UPDATE plan_item SET status='closed',outcome='generation_failed' WHERE id=?", (slot["id"],))
    return {"turn_id": None, "exchange_id": None}, error_code


def finish_if_all_closed(db: sqlite3.Connection, session_id: str, version: int, now: str) -> bool:
    updated = db.execute("""UPDATE interview_session SET phase='FINISHED',end_reason='completed',ended_at=?
                    WHERE id=? AND phase='ACTIVE' AND plan_version=? AND question_count=(
                        SELECT count(*) FROM plan_item WHERE session_id=? AND plan_version=?
                        HAVING count(*)=sum(status='closed')
                    )""", (now, session_id, version, session_id, version))
    return updated.rowcount == 1


def session_progress(db: sqlite3.Connection, session: sqlite3.Row, schema_version: int) -> dict[str, int]:
    counts = dict.fromkeys(("planned", "closed", "answered", "scored", "skipped", "failed"), 0)
    if schema_version < 2 or not session["plan_version"]:
        return counts
    scope = (session["id"], session["plan_version"])
    items = db.execute("SELECT status,outcome FROM plan_item WHERE session_id=? AND plan_version=?", scope).fetchall()
    counts["planned"] = len(items)
    counts["closed"] = sum(row["status"] == "closed" for row in items)
    counts["skipped"] = sum(row["outcome"] == "skipped" for row in items)
    counts["failed"] = sum(row["outcome"] == "generation_failed" for row in items)
    if schema_version >= 3:
        counts["answered"] = db.execute("""SELECT count(*) FROM turn t JOIN plan_item p ON p.id=t.plan_item_id
                            WHERE t.session_id=? AND p.plan_version=? AND EXISTS (
                                SELECT 1 FROM exchange e WHERE e.turn_id=t.id AND e.answer_text IS NOT NULL
                            )""", scope).fetchone()[0]
    if schema_version >= 4:
        evaluation = db.execute("""SELECT e.status,count(*) FROM evaluation e JOIN turn t ON t.id=e.turn_id
                                    JOIN plan_item p ON p.id=t.plan_item_id WHERE t.session_id=? AND p.plan_version=?
                                    GROUP BY e.status""", scope)
        by_status = dict(evaluation)
        counts["scored"] = by_status.get("ready", 0)
        counts["failed"] += by_status.get("failed", 0)
    return counts
