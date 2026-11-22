"""Create and read immutable local reports from finished interview facts."""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from app.db.business import (
    OWNER_ID, SessionError, check_business_schema, ensure_session_idle, find_session, upgrade_business,
)
from app.db.sqlite import canonical_json, connect
from app.schemas.scoring import Assessment
from app.services.answers import read_exchanges
from app.services.scoring import ReportItem, aggregate_scores, score_assessment

TEMPLATE_VERSION = "local-report-v1"
# One concrete action and self-check for each dimension the rubric can assess.
PRACTICE = {
    "correctness": ("用自己的话写出这个考点的定义和一个反例。", "对照本题审核来源，核对定义与反例的边界。"),
    "depth": ("补充一个技术选型的取舍及其适用条件。", "用一个条件变化的场景检验结论是否仍成立。"),
    "structure": ("把答案重排成结论、依据和例子。", "只读每部分的第一句，检查论证是否连贯。"),
    "relevance": ("对照题干划出每个要求，并为每项补一句直接回答。", "逐项检查是否能在新答案中找到对应句子。"),
    "technical_reasoning": ("补充一项技术选择的备选方案和取舍。", "说明一个会让你换方案的条件。"),
    "implementation_ownership": ("指出一个自己交付的模块和具体改动。", "用可展示的接口、测试或提交记录核对你的职责。"),
    "validation_evidence": ("补充一次实际检查的输入、指标和结果。", "检查别人是否能用同样的输入复现测量。"),
    "communication": ("用问题、行动和结果重新讲述这段经历。", "请听者复述你的行动和结果，检查表述是否清楚。"),
}


def _finished_session(db: sqlite3.Connection, session_id: str) -> sqlite3.Row:
    session = find_session(db, session_id)
    if session["phase"] != "FINISHED":
        raise SessionError("ILLEGAL_ACTION", "面试结束后才能查看或生成整场报告", current_revision=session["revision"])
    return session


def _saved_revision(db: sqlite3.Connection, session_id: str, revision: int | None = None) -> sqlite3.Row | None:
    if revision is None:
        return db.execute("""SELECT * FROM report_revision WHERE session_id=?
                             ORDER BY revision_no DESC LIMIT 1""", (session_id,)).fetchone()
    return db.execute("SELECT * FROM report_revision WHERE session_id=? AND revision_no=?",
                      (session_id, revision)).fetchone()


def _view(row: sqlite3.Row) -> dict:
    counts = json.loads(row["counts_json"])
    return {
        "id": row["id"], "session_id": row["session_id"], "revision_no": row["revision_no"],
        "status": row["status"], "kind": "report" if counts["scored"] else "summary",
        "is_partial": bool(row["is_partial"]), "counts": counts,
        "knowledge_score": row["knowledge_score"], "project_score": row["project_score"],
        "dimension_summary": json.loads(row["dimension_summary_json"]),
        "improvements": json.loads(row["improvements_json"]),
        **json.loads(row["evaluation_snapshot_json"]),
        "model_context": json.loads(row["model_context_json"]), "created_at": row["created_at"],
    }


def read_report(path: Path, session_id: str, revision: int | None = None) -> dict:
    if revision is not None and (type(revision) is not int or revision < 1):
        raise SessionError("INVALID_INPUT", "报告修订号须为正整数")
    if not path.is_file():
        raise SessionError("RESOURCE_NOT_FOUND", "找不到可访问的会话")
    with closing(connect(path, readonly=True)) as db, db:
        db.execute("BEGIN")
        version = check_business_schema(db)
        session = _finished_session(db, session_id)
        row = _saved_revision(db, session_id, revision) if version >= 5 else None
        if row is not None:
            return _view(row)
        if revision is not None:
            raise SessionError("RESOURCE_NOT_FOUND", "该会话没有此报告修订")
        return {"session_id": session_id, "status": "pending", "revision_no": None,
                "end_reason": session["end_reason"], "ended_at": session["ended_at"]}


def _generation_errors(db: sqlite3.Connection, session_id: str) -> dict[int, str]:
    errors = {}
    for row in db.execute("""SELECT kind,error_code,result_refs_json FROM operation
                             WHERE session_id=? AND owner_id=? AND kind IN ('start','advance','skip')
                               AND status IN ('succeeded','failed')""", (session_id, OWNER_ID)):
        if row["kind"] == "start":
            if row["error_code"] is not None:
                errors[1] = row["error_code"]
        else:
            errors.update({entry["seq"]: entry["reason_code"]
                           for entry in json.loads(row["result_refs_json"] or "{}").get("unavailable", [])})
    return errors


def _report_items(db: sqlite3.Connection, session: sqlite3.Row) -> tuple[list[ReportItem], list[dict]]:
    rows = db.execute("""SELECT p.id AS plan_item_id,p.seq,p.outcome,p.topic,p.task_type AS planned_type,
                                t.id AS turn_id,t.task_type,t.probe_count,t.interrupted,t.fallback_reason,
                                t.question_snapshot_json,t.source_snapshot_json,t.rubric_snapshot_json,
                                e.id AS evaluation_id,e.status AS evaluation_status,e.assessment_json,
                                e.item_score,e.error_code,e.model_context_json
                         FROM plan_item p
                         LEFT JOIN turn t ON t.session_id=p.session_id AND t.plan_item_id=p.id
                         LEFT JOIN evaluation e ON e.turn_id=t.id
                         WHERE p.session_id=? AND p.plan_version=? ORDER BY p.seq""",
                      (session["id"], session["plan_version"])).fetchall()
    generation_errors = _generation_errors(db, session["id"])
    facts, items = [], []
    for row in rows:
        exchanges = read_exchanges(db, row["turn_id"]) if row["turn_id"] is not None else ()
        answers = {entry.id: entry.answer for entry in exchanges if entry.answer is not None}
        sources = json.loads(row["source_snapshot_json"] or "[]")
        rubric = json.loads(row["rubric_snapshot_json"] or "{}")
        task_type = row["task_type"] or row["planned_type"]
        scored = None
        if row["evaluation_status"] == "ready":
            scored = score_assessment(Assessment.model_validate_json(row["assessment_json"]),
                                      task_type=task_type, rubric_version=rubric["rubric_version"],
                                      applicable_dimensions=rubric["applicable_dimensions"],
                                      answers=answers, source_refs={source["id"] for source in sources})
            if scored.item_score != row["item_score"]:
                raise SessionError("DATA_INCONSISTENT", "已保存的单题分与冻结评分依据不一致")
        facts.append(ReportItem(row["plan_item_id"], task_type, bool(exchanges), bool(answers),
                                row["outcome"], row["evaluation_status"], scored))
        evaluation = None
        if row["evaluation_id"] is not None:
            evaluation = {
                "id": row["evaluation_id"], "status": row["evaluation_status"], "item_score": row["item_score"],
                "error_code": row["error_code"], "model_context": json.loads(row["model_context_json"]),
                "assessment": scored.assessment.model_dump(mode="json", exclude={"improvements"}) if scored else None,
            }
        items.append({
            "plan_item_id": row["plan_item_id"], "seq": row["seq"], "task_type": task_type,
            "planned_type": row["planned_type"], "topic": row["topic"], "outcome": row["outcome"],
            "generation_error_code": generation_errors.get(row["seq"]) if row["outcome"] == "generation_failed" else None,
            "turn_id": row["turn_id"], "probe_count": row["probe_count"] or 0, "interrupted": bool(row["interrupted"]),
            "fallback_reason": row["fallback_reason"], "question_snapshot": json.loads(row["question_snapshot_json"] or "null"),
            "sources": sources, "exchanges": [asdict(entry) for entry in exchanges], "evaluation": evaluation,
        })
    return facts, items


def _improvements(items: list[dict]) -> list[dict]:
    candidates = []
    for item in items:
        evaluation = item["evaluation"]
        assessment = evaluation["assessment"] if evaluation is not None else None
        if assessment is None:
            continue
        for name, dimension in assessment["dimensions"].items():
            if dimension["value"] is not None and dimension["value"] < 3 and dimension["evidence"]:
                evidence = dimension["evidence"][0]
                action, self_check = PRACTICE[name]
                candidates.append({
                    "seq": item["seq"], "turn_id": item["turn_id"], "dimension": name,
                    "value": dimension["value"], "evidence": evidence, "action": action, "self_check": self_check,
                })
    candidates.sort(key=lambda entry: (entry["value"], entry["seq"], entry["dimension"]))
    by_dimension = {}
    for candidate in candidates:
        by_dimension.setdefault(candidate["dimension"], candidate)
    return list(by_dimension.values())[:3]


def generate_report(path: Path, session_id: str) -> dict:
    if not path.is_file():
        raise SessionError("RESOURCE_NOT_FOUND", "找不到可访问的会话")
    with closing(connect(path)) as db:
        while True:
            with db:
                db.execute("BEGIN IMMEDIATE")
                version = check_business_schema(db)
                session = _finished_session(db, session_id)
                if version >= 5:
                    saved = _saved_revision(db, session_id)
                    if saved is not None:
                        return _view(saved)
                ensure_session_idle(db, session)
                if version < 4:
                    # Commit each legacy step so the next migration backs up its real predecessor.
                    upgrade_business(db, path, version + 1)
                    continue
                facts, items = _report_items(db, session)
                summary = aggregate_scores(facts, ended_early=session["end_reason"] != "completed")
                snapshot = {
                    "end_reason": session["end_reason"], "ended_at": session["ended_at"],
                    "mode": session["mode"], "plan_version": session["plan_version"],
                    "category_counts": summary["category_counts"], "items": items,
                }
                if version == 4:
                    upgrade_business(db, path, 5)
                report_id = uuid4().hex
                db.execute("""INSERT INTO report_revision(
                                  id,session_id,revision_no,status,is_partial,counts_json,knowledge_score,project_score,
                                  dimension_summary_json,evaluation_snapshot_json,improvements_json,model_context_json,created_at
                              ) VALUES (?, ?, 1,'template_only', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                           (report_id, session_id, summary["is_partial"], canonical_json(summary["counts"]),
                            summary["knowledge_score"], summary["project_score"], canonical_json(summary["dimension_summary"]),
                            canonical_json(snapshot), canonical_json(_improvements(items)),
                            canonical_json({"template_version": TEMPLATE_VERSION}), datetime.now(UTC).isoformat()))
                return _view(db.execute("SELECT * FROM report_revision WHERE id=?", (report_id,)).fetchone())
