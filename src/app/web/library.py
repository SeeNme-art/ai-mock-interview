"""Read history, clone confirmed setup, export reports and remove local session data."""
from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from app.db.business import OWNER_ID, SessionError, cancel_active_operations, check_business_schema, find_session, upgrade_business
from app.db.sqlite import connect, sha256
from app.services.question_delivery import session_progress
from app.services.reports import read_report

ORDER = "(CASE WHEN phase='ACTIVE' THEN 0 ELSE 1 END), created_at DESC, id DESC"


def history(path: Path, limit: int, cursor: str | None, phase: str | None) -> dict:
    if not path.is_file():
        return {"sessions": [], "next_cursor": None, "deletions": []}
    with closing(connect(path, readonly=True)) as db, db:
        db.execute("BEGIN")
        version = check_business_schema(db)
        scope = (OWNER_ID, phase, phase)
        selection = """SELECT * FROM interview_session WHERE owner_id=? AND delete_state='none'
                       AND (? IS NULL OR phase=?)"""
        if cursor:
            anchor = db.execute(selection + " AND id=?", (*scope, cursor)).fetchone()
            if anchor is None:
                raise SessionError("INVALID_INPUT", "历史记录已变化，请从首页重新加载")
            active = 0 if anchor["phase"] == "ACTIVE" else 1
            selection += """ AND ((CASE WHEN phase='ACTIVE' THEN 0 ELSE 1 END)>? OR
                              ((CASE WHEN phase='ACTIVE' THEN 0 ELSE 1 END)=? AND
                               (created_at<? OR (created_at=? AND id<?))))"""
            scope += (active, active, anchor["created_at"], anchor["created_at"], anchor["id"])
        rows = db.execute(selection + f" ORDER BY {ORDER} LIMIT ?", (*scope, limit + 1)).fetchall()
        sessions = []
        for row in rows[:limit]:
            report = db.execute("""SELECT knowledge_score,project_score FROM report_revision WHERE session_id=?
                                   ORDER BY revision_no DESC LIMIT 1""", (row["id"],)).fetchone() if version >= 5 else None
            progress = session_progress(db, row, version)
            sessions.append({**{key: row[key] for key in ("id", "phase", "revision", "created_at", "mode", "question_count")},
                             "progress": progress, "scores": dict(report) if report else None})
        deletions = [dict(row) for row in db.execute("""SELECT id,status,error_code FROM deletion_job
                                                        WHERE status<>'completed' ORDER BY created_at""")] if version >= 7 else []
    return {"sessions": sessions, "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None, "deletions": deletions}


def clone(path: Path, session_id: str, key: str) -> str:
    if not path.is_file():
        raise SessionError("RESOURCE_NOT_FOUND", "找不到可访问的会话")
    with closing(connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        version = check_business_schema(db)
        source = find_session(db, session_id)
        if source["phase"] != "FINISHED":
            raise SessionError("ILLEGAL_ACTION", "请在本场结束后再练一次")
        receipt_key = "clone-" + sha256(key)
        previous = db.execute("SELECT id,confirmation_hash FROM interview_session WHERE owner_id=? AND confirmation_key=?",
                              (OWNER_ID, receipt_key)).fetchone()
        if previous:
            if previous["confirmation_hash"] != sha256(session_id):
                raise SessionError("IDEMPOTENCY_CONFLICT", "请求标识已用于其他会话")
            return previous["id"]
        fresh = uuid4().hex
        fields = ["owner_id", "profile_id", "level", "mode", "question_count", "selected_dimensions_json",
                  "input_mode", "jd_text", "resume_profile_json", "policy_snapshot_json"]
        if version >= 2:
            fields.append("difficulty")
        # Column names are fixed here, never taken from a request.
        db.execute(f"""INSERT INTO interview_session(id,draft_id,confirmation_key,confirmation_hash,created_at,{','.join(fields)})
                       VALUES (?,?,?,?,?,{','.join('?' for _ in fields)})""",
                   (fresh, uuid4().hex, receipt_key, sha256(session_id), datetime.now(UTC).isoformat(), *(source[field] for field in fields)))
        return fresh


def _markdown_text(text: str) -> str:
    return re.sub(r"([\\`*_{\[\]()<>#!|~])", r"\\\1", text)


def export_report(path: Path, report_id: str) -> str:
    if not path.is_file():
        raise SessionError("RESOURCE_NOT_FOUND", "找不到可导出的报告")
    with closing(connect(path, readonly=True)) as db, db:
        db.execute("BEGIN")
        version = check_business_schema(db)
        row = db.execute("""SELECT r.session_id,r.revision_no,s.policy_snapshot_json FROM report_revision r
                            JOIN interview_session s ON s.id=r.session_id
                            WHERE r.id=? AND s.owner_id=? AND s.phase='FINISHED' AND s.delete_state='none'""",
                         (report_id, OWNER_ID)).fetchone() if version >= 5 else None
    if row is None:
        raise SessionError("RESOURCE_NOT_FOUND", "找不到可导出的报告")
    report = read_report(path, row["session_id"], row["revision_no"])
    lines = ["# 模拟面试复盘", "", f"已答 {report['counts']['answered']} 题，有效评分 {report['counts']['scored']} 题。",
             f"跳过 {report['counts']['skipped']} 题，系统失败 {report['counts']['system_failed']} 题。", ""]
    if json.loads(row["policy_snapshot_json"]).get("web_source") == "example":
        lines.extend(["此报告包含示例固定评分。", ""])
    for label, score in (("知识题", report["knowledge_score"]), ("项目题", report["project_score"])):
        value = "暂无有效评分" if score is None else f"{score:.1f}"
        lines.append(f"- {label}　{value}")
    for item in report["items"]:
        label = "知识题" if item["task_type"] == "knowledge" else "项目题"
        lines.extend(["", f"## 第 {item['seq']} 题 · {label}", ""])
        for exchange in item["exchanges"]:
            lines.extend([_markdown_text(exchange["question"]), "", _markdown_text(exchange["answer"] or "未提交回答"), ""])
        for dimension, assessment in ((item["evaluation"] or {}).get("assessment") or {}).get("dimensions", {}).items():
            if assessment["applicable"]:
                lines.append(f"- {dimension}　{assessment['value']}/4　{_markdown_text(assessment['reason'])}")
                lines.extend("  - 原文依据　" + _markdown_text(evidence["quote"]) for evidence in assessment["evidence"])
        lines.extend("- 审核出处　" + _markdown_text(source.get("title") or source["id"]) for source in item["sources"])
        for suggestion in report["improvements"]:
            if suggestion["seq"] == item["seq"]:
                lines.append("- 练习　" + _markdown_text(suggestion["action"]))
                lines.append("  - 自查　" + _markdown_text(suggestion["self_check"]))
    return "\n".join(lines) + "\n"


def request_deletion(path: Path, session_id: str, expected_revision: int | None) -> str | None:
    if not path.is_file():
        return None
    with closing(connect(path)) as db:
        while True:
            with db:
                db.execute("BEGIN IMMEDIATE")
                version = check_business_schema(db)
                if version >= 7:
                    existing = db.execute("SELECT id FROM deletion_job WHERE target_session_id=?", (session_id,)).fetchone()
                    if existing:
                        return existing["id"]
                session = db.execute("SELECT * FROM interview_session WHERE id=? AND owner_id=?", (session_id, OWNER_ID)).fetchone()
                if session is None:
                    return None
                if session["phase"] != "FINISHED":
                    raise SessionError("ILLEGAL_ACTION", "请先结束会话再删除")
                if session["revision"] != expected_revision:
                    raise SessionError("STALE_REVISION", "会话已变化，请刷新后再删除", current_revision=session["revision"])
                if version < 7:
                    upgrade_business(db, path, version + 1)
                    continue
                job, now = uuid4().hex, datetime.now(UTC).isoformat()
                cancel_active_operations(db, session_id, now)
                db.execute("""UPDATE interview_session SET delete_state='deleting',control_epoch=control_epoch+1,revision=revision+1
                              WHERE id=?""", (session_id,))
                db.execute("INSERT INTO deletion_job VALUES (?,?,'queued',NULL,?,?)", (job, session_id, now, now))
                return job


def deletion_status(path: Path, job: str) -> dict:
    if path.is_file():
        with closing(connect(path, readonly=True)) as db, db:
            db.execute("BEGIN")
            version = check_business_schema(db)
            row = db.execute("SELECT id,status,error_code FROM deletion_job WHERE id=?",
                             (job,)).fetchone() if version >= 7 else None
            if row is not None:
                return dict(row)
    raise SessionError("RESOURCE_NOT_FOUND", "找不到删除任务")


def _purge_backups(db: sqlite3.Connection, path: Path, session_id: str) -> None:
    owned_name = re.compile(re.escape(path.stem) + r"\.before-\d{3}-[0-9a-f]{32}\.db")
    files = {item.name for item in path.parent.iterdir() if owned_name.fullmatch(item.name)}
    records = {row["file_name"] for row in db.execute("SELECT file_name FROM migration_backup")}
    if any(not owned_name.fullmatch(name) for name in records):
        raise ValueError("invalid migration backup record")
    for name in sorted(files | records):
        backup = path.parent / name
        if backup.is_file() and not backup.is_symlink():
            try:
                with closing(connect(backup, readonly=True)) as snapshot:
                    check_business_schema(snapshot)
                    affected = snapshot.execute("SELECT 1 FROM interview_session WHERE id=?", (session_id,)).fetchone()
                if affected is None:
                    continue
            except (sqlite3.Error, ValueError):
                # An unreadable application backup cannot rule out the target's presence.
                pass
        backup.unlink(missing_ok=True)
        db.execute("DELETE FROM migration_backup WHERE file_name=?", (name,))


def _purge_observation(path: Path, session_id: str) -> None:
    if not path.is_file():
        return
    with closing(connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trace'").fetchone():
            db.execute("DELETE FROM span WHERE trace_id IN (SELECT id FROM trace WHERE session_id=?)", (session_id,))
            db.execute("DELETE FROM trace WHERE session_id=?", (session_id,))


def run_deletion(path: Path, observe: Path, job: str) -> None:
    with closing(connect(path)) as db:
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                target = db.execute("SELECT * FROM deletion_job WHERE id=?", (job,)).fetchone()
                if target is None or target["status"] == "completed":
                    return
                session_id = target["target_session_id"]
                if not db.execute("SELECT 1 FROM interview_session WHERE id=? AND delete_state='deleting'", (session_id,)).fetchone():
                    raise ValueError("session not marked for deletion")
                _purge_backups(db, path, session_id)
                _purge_observation(observe, session_id)
                for statement in (
                    "DELETE FROM report_revision WHERE session_id=?",
                    "DELETE FROM evaluation WHERE turn_id IN (SELECT id FROM turn WHERE session_id=?)",
                    "DELETE FROM exchange WHERE turn_id IN (SELECT id FROM turn WHERE session_id=?)",
                    "DELETE FROM turn WHERE session_id=?",
                    "DELETE FROM plan_item WHERE session_id=?",
                    "DELETE FROM call_attempt WHERE session_id=?",
                    "DELETE FROM operation_step WHERE operation_id IN (SELECT id FROM operation WHERE session_id=?)",
                    "DELETE FROM operation WHERE session_id=?",
                    "DELETE FROM budget_account WHERE session_id=?",
                    "DELETE FROM interview_session WHERE id=?",
                ):
                    db.execute(statement, (session_id,))
                db.execute("UPDATE deletion_job SET status='completed',error_code=NULL,updated_at=? WHERE id=?",
                           (datetime.now(UTC).isoformat(), job))
        except (OSError, sqlite3.Error, ValueError):
            with db:
                db.execute("""UPDATE deletion_job SET status='failed',error_code='DELETE_FAILED',updated_at=?
                              WHERE id=? AND status<>'completed'""", (datetime.now(UTC).isoformat(), job))
