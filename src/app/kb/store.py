"""SQLite persistence and candidate filtering for the public question bank."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Sequence

from .seed import Difficulty, Dimension, Question, SeedBank, prompt_key

SCHEMA_VERSION = 1
MIGRATION = Path(__file__).with_name("migrations") / "001_question_bank.sql"
JSON_FIELDS = ("answer_points", "acceptable_alternatives", "common_errors", "applicable_dimensions", "tags")


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


def check_schema(db: sqlite3.Connection, checksum: str) -> None:
    if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        raise ValueError("题库结构版本不受支持")
    applied = db.execute("SELECT checksum FROM schema_migration WHERE version=?", (SCHEMA_VERSION,)).fetchone()
    if not applied or applied["checksum"] != checksum:
        raise ValueError("题库迁移校验值不匹配")


def initialize_or_check(db: sqlite3.Connection) -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    checksum = sha256(sql)
    if db.execute("PRAGMA user_version").fetchone()[0] != 0:
        check_schema(db, checksum)
        return
    existing = db.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1").fetchone()
    if existing:
        raise ValueError("拒绝用题库迁移初始化已有数据的未登记数据库")
    # executescript would commit the open transaction. Execute numbered SQL
    # statements individually so schema and seed writes roll back together.
    statement = ""
    for line in sql.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ""
    if statement.strip():
        raise ValueError("迁移 SQL 存在不完整语句")
    db.execute("INSERT INTO schema_migration(version, checksum) VALUES (?,?)", (SCHEMA_VERSION, checksum))
    db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def save_question(db: sqlite3.Connection, question: Question) -> str:
    record = question.model_dump(mode="json")
    digest = sha256(canonical_json(record))
    previous = db.execute("SELECT revision, stable_faiss_id, content_hash FROM question WHERE id=?",
                          (question.id,)).fetchone()
    if previous:
        if previous["stable_faiss_id"] != question.stable_faiss_id:
            raise ValueError(f"{question.id} 的向量 ID 必须保持不变")
        if question.revision < previous["revision"]:
            raise ValueError(f"{question.id} 的导入版本过旧")
        if question.revision == previous["revision"]:
            if previous["content_hash"] != digest:
                raise ValueError(f"{question.id} 已修改，请提高 revision 后导入")
            return "unchanged"

    source_refs = record.pop("source_refs")
    for key in JSON_FIELDS:
        record[key + "_json"] = canonical_json(record.pop(key))
    record.update(content_hash=digest, prompt_key=prompt_key(question.content))
    # Column names come exclusively from the declared Question fields.
    columns = ",".join(record)
    values = ",".join(f":{key}" for key in record)
    updates = ",".join(f"{key}=excluded.{key}" for key in record if key not in {"id", "stable_faiss_id"})
    db.execute(f"INSERT INTO question({columns}) VALUES ({values}) ON CONFLICT(id) DO UPDATE SET {updates}", record)
    db.execute("DELETE FROM question_source WHERE question_id=?", (question.id,))
    db.executemany("INSERT INTO question_source(question_id,source_id,position) VALUES (?,?,?)",
                   [(question.id, source_id, position) for position, source_id in enumerate(source_refs)])
    return "updated" if previous else "inserted"


def import_seed(path: Path, bank: SeedBank) -> dict[str, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    with closing(connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        initialize_or_check(db)
        for source in bank.sources:
            data = source.model_dump(mode="json")
            old = db.execute("SELECT * FROM knowledge_source WHERE id=?", (source.id,)).fetchone()
            if old is not None:
                if dict(old) != data:
                    raise ValueError(f"来源 {source.id} 已有记录，更新内容请分配新 ID")
                continue
            db.execute("""INSERT INTO knowledge_source VALUES
                        (:id,:title,:locator,:section,:source_type,:source_version,:checked_at)""", data)
        for question in bank.questions:
            counts[save_question(db, question)] += 1
    return counts


def list_questions(path: Path, *, dimension: Dimension | None = None, difficulty: Difficulty | None = None,
                   exclude_ids: Sequence[str] = (), verified_only: bool = True) -> list[dict]:
    where = ["status='verified'"] if verified_only else []
    args = []
    if dimension is not None:
        where.append("dimension=?")
        args.append(dimension)
    if difficulty is not None:
        where.append("difficulty=?")
        args.append(difficulty)
    if exclude_ids:
        where.append("id NOT IN (" + ",".join("?" for _ in exclude_ids) + ")")
        args.extend(exclude_ids)
    filters = " WHERE " + " AND ".join(where) if where else ""
    with closing(connect(path, readonly=True)) as db, db:
        db.execute("BEGIN")
        check_schema(db, sha256(MIGRATION.read_text(encoding="utf-8")))
        questions = []
        for row in db.execute("SELECT * FROM question" + filters + " ORDER BY stable_faiss_id", args):
            result = dict(row)
            result.pop("prompt_key")
            for key in JSON_FIELDS:
                result[key] = json.loads(result.pop(key + "_json"))
            sources = db.execute("""SELECT s.* FROM question_source r JOIN knowledge_source s ON s.id=r.source_id
                                  WHERE r.question_id=? ORDER BY r.position""", (result["id"],))
            result["sources"] = [dict(source) for source in sources]
            questions.append(result)
        return questions
