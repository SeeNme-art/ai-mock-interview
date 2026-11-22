"""The business schema, migrations, and local session lookups."""
from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from .sqlite import apply_migration, check_migration, connect, initialize_or_check, sha256

OWNER_ID = "local"
MIGRATIONS = tuple(Path(__file__).with_name("migrations") / name for name in (
    "001_init.sql", "002_interview_plan.sql", "003_first_question.sql", "004_evaluation.sql",
    "005_report_revision.sql", "006_session_budget.sql", "007_deletion_job.sql", "008_cloud_budget.sql",
))


class SessionError(ValueError):
    def __init__(self, code: str, message: str, *, current_revision: int | None = None):
        super().__init__(message)
        self.code = code
        self.current_revision = current_revision


def validate_request_key(key: str) -> None:
    if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key) is None:
        raise ValueError("请求标识格式无效")


def check_business_schema(db: sqlite3.Connection) -> int:
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if not 1 <= version <= len(MIGRATIONS):
        raise ValueError("业务库结构版本不受支持")
    for number, migration in enumerate(MIGRATIONS[:version], start=1):
        check_migration(db, number, sha256(migration.read_text(encoding="utf-8")), "业务库")
    return version


def initialize_business(db: sqlite3.Connection) -> None:
    if db.execute("PRAGMA user_version").fetchone()[0] == 0:
        initialize_or_check(db, MIGRATIONS[0], 1, "业务库")
        for number, migration in enumerate(MIGRATIONS[1:], start=2):
            apply_migration(db, migration.read_text(encoding="utf-8"), number)
    else:
        check_business_schema(db)


def upgrade_business(db: sqlite3.Connection, path: Path, version: int) -> None:
    """Back up the last committed version and apply the next migration."""
    previous = check_business_schema(db)
    if version != previous + 1 or version > len(MIGRATIONS):
        raise ValueError("业务库迁移须按版本顺序执行")
    backup = path.with_name(f"{path.stem}.before-{version:03d}-{uuid4().hex}.db")
    with backup.open("xb"):
        pass
    try:
        # The backup source must be a separate read connection, before any DDL.
        with closing(connect(path, readonly=True)) as source, closing(connect(backup)) as target:
            source.backup(target)
            check_business_schema(target)
    except (OSError, sqlite3.Error, ValueError):
        backup.unlink(missing_ok=True)
        raise
    apply_migration(db, MIGRATIONS[version - 1].read_text(encoding="utf-8"), version)
    db.execute("INSERT INTO migration_backup VALUES (?,?,?)", (backup.name, previous, datetime.now(UTC).isoformat()))


def find_session(db: sqlite3.Connection, session_id: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM interview_session WHERE id=? AND owner_id=? AND delete_state='none'",
                     (session_id, OWNER_ID)).fetchone()
    if row is None:
        raise SessionError("RESOURCE_NOT_FOUND", "找不到可访问的会话")
    return row


def find_replay(db: sqlite3.Connection, kind: str, key: str, request_hash: str) -> sqlite3.Row | None:
    row = db.execute("""SELECT id, request_hash, result_refs_json FROM operation
                        WHERE owner_id=? AND kind=? AND idempotency_key=?""", (OWNER_ID, kind, key)).fetchone()
    if row is not None and row["request_hash"] != request_hash:
        raise SessionError("IDEMPOTENCY_CONFLICT", "该请求标识已用于另一请求")
    return row


def find_operation(db: sqlite3.Connection, session_id: str, operation_id: str, kind: str) -> sqlite3.Row:
    operation = db.execute("SELECT * FROM operation WHERE id=? AND session_id=? AND owner_id=? AND kind=?",
                           (operation_id, session_id, OWNER_ID, kind)).fetchone()
    if operation is None:
        raise SessionError("RESOURCE_NOT_FOUND", "找不到该会话的操作")
    return operation


def operation_epoch(path: Path, session_id: str, operation_id: str, kind: str) -> int:
    with closing(connect(path, readonly=True)) as db, db:
        db.execute("BEGIN")
        version = check_business_schema(db)
        find_session(db, session_id)
        if version < 2:
            raise SessionError("RESOURCE_NOT_FOUND", "找不到该会话的操作")
        return find_operation(db, session_id, operation_id, kind)["expected_epoch"]


def ensure_session_idle(db: sqlite3.Connection, session: sqlite3.Row) -> None:
    if db.execute("""SELECT 1 FROM operation WHERE session_id=?
                     AND status IN ('queued','running','recovery_required')""", (session["id"],)).fetchone():
        raise SessionError("OPERATION_IN_PROGRESS", "该会话已有待处理操作", current_revision=session["revision"])


def cancel_active_operations(db: sqlite3.Connection, session_id: str, now: str) -> None:
    db.execute("""UPDATE operation SET status='cancelled',error_code='CANCELLED',updated_at=?
                  WHERE session_id=? AND status IN ('queued','running','recovery_required')""", (now, session_id))
