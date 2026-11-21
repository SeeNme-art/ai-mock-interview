"""Shared SQLite connection and numbered initial migration helpers."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    else:
        db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def check_schema(db: sqlite3.Connection, version: int, checksum: str, label: str) -> None:
    if db.execute("PRAGMA user_version").fetchone()[0] != version:
        raise ValueError(f"{label}结构版本不受支持")
    check_migration(db, version, checksum, label)


def check_migration(db: sqlite3.Connection, version: int, checksum: str, label: str) -> None:
    applied = db.execute("SELECT checksum FROM schema_migration WHERE version=?", (version,)).fetchone()
    if not applied or applied["checksum"] != checksum:
        raise ValueError(f"{label}迁移校验值不匹配")


def initialize_or_check(db: sqlite3.Connection, migration: Path, version: int, label: str) -> None:
    sql = migration.read_text(encoding="utf-8")
    checksum = sha256(sql)
    if db.execute("PRAGMA user_version").fetchone()[0] != 0:
        check_schema(db, version, checksum, label)
        return
    existing = db.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1").fetchone()
    if existing:
        raise ValueError(f"拒绝用{label}迁移初始化已有数据的未登记数据库")
    apply_migration(db, sql, version)


def apply_migration(db: sqlite3.Connection, sql: str, version: int) -> None:
    # executescript would commit the caller's open transaction.
    statement = ""
    for line in sql.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ""
    if statement.strip():
        raise ValueError("迁移 SQL 存在不完整语句")
    db.execute("INSERT INTO schema_migration(version, checksum) VALUES (?,?)", (version, sha256(sql)))
    db.execute(f"PRAGMA user_version={version}")
