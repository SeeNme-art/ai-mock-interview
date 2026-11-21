"""The business database schema and the first upgrade of persisted sessions."""
from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from .sqlite import apply_migration, check_migration, connect, initialize_or_check, sha256

OWNER_ID = "local"
MIGRATIONS = tuple(Path(__file__).with_name("migrations") / name
                   for name in ("001_init.sql", "002_interview_plan.sql"))


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
        apply_migration(db, MIGRATIONS[1].read_text(encoding="utf-8"), 2)
    else:
        check_business_schema(db)


def upgrade_business_v1(db: sqlite3.Connection, path: Path) -> None:
    """Back up the committed v1 database while its writer lock is held."""
    backup = path.with_name(f"{path.stem}.before-002-{uuid4().hex}.db")
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
    apply_migration(db, MIGRATIONS[1].read_text(encoding="utf-8"), 2)
    db.execute("INSERT INTO migration_backup VALUES (?,?,?)", (backup.name, 1, datetime.now(UTC).isoformat()))
