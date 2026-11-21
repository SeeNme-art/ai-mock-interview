import copy
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from app.kb.seed import SeedBank, read_seed
from app.kb.store import MIGRATION, import_seed, list_questions

SEED = ROOT / "data" / "seeds" / "knowledge_questions.json"


class QuestionBankTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "questions.db"
        self.bank = read_seed(SEED)

    def test_seed_import_can_repeat_and_defaults_to_verified_only(self):
        size = len(self.bank.questions)
        self.assertEqual(import_seed(self.path, self.bank), {"inserted": size, "updated": 0, "unchanged": 0})
        first = list_questions(self.path, verified_only=False)
        self.assertEqual(len(first), size)
        self.assertEqual(list_questions(self.path), [row for row in first if row["status"] == "verified"])
        self.assertEqual(import_seed(self.path, self.bank), {"inserted": 0, "updated": 0, "unchanged": size})
        self.assertEqual(list_questions(self.path, verified_only=False), first)
        self.assertEqual({(item["dimension"], item["difficulty"]) for item in first},
                         {(dimension, difficulty) for dimension in ("agent", "rag", "backend")
                          for difficulty in ("basic", "applied")})
        for question, row in zip(sorted(self.bank.questions, key=lambda item: item.stable_faiss_id), first, strict=True):
            self.assertEqual([source["id"] for source in row["sources"]], question.source_refs)
            self.assertEqual(row["answer_points"], question.answer_points)

    def test_candidates_filter_status_dimension_difficulty_and_seen_ids(self):
        import_seed(self.path, self.bank)
        reviewed = self.bank.model_dump(mode="json")
        for item in reviewed["questions"]:
            item["revision"] += 1
            item["status"] = {"rag-failure-attribution": "verified", "rag-chunk-boundary": "verified",
                              "rag-cosine-faiss": "retired", "agent-interrupt-replay": "verified"}.get(item["id"], "draft")
        self.assertEqual(import_seed(self.path, SeedBank.model_validate(reviewed))["updated"], len(self.bank.questions))
        candidates = list_questions(self.path, dimension="rag", difficulty="applied", exclude_ids=["rag-chunk-boundary"])
        self.assertEqual([(item["id"], item["stable_faiss_id"]) for item in candidates], [("rag-failure-attribution", 203)])
        self.assertEqual({item["id"] for item in list_questions(self.path)},
                         {"rag-failure-attribution", "rag-chunk-boundary", "agent-interrupt-replay"})
        with self.assertRaisesRegex(ValueError, "版本过旧"):
            import_seed(self.path, self.bank)

    def test_partial_updates_roll_back_on_conflicting_revision(self):
        import_seed(self.path, self.bank)
        before = list_questions(self.path, verified_only=False)
        bad = self.bank.model_dump(mode="json")
        bad["questions"][0]["revision"] += 1
        bad["questions"][0]["content"] += "请举一个例子。"
        bad["questions"][1]["objective"] = "这是一项未提升版本的变更"
        with self.assertRaisesRegex(ValueError, "提高 revision"):
            import_seed(self.path, SeedBank.model_validate(bad))
        self.assertEqual(list_questions(self.path, verified_only=False), before)

    def test_stable_vector_id_cannot_change_on_a_new_revision(self):
        import_seed(self.path, self.bank)
        bad = self.bank.model_dump(mode="json")
        bad["questions"][0]["revision"] += 1
        bad["questions"][0]["stable_faiss_id"] = 2**63 - 1
        with self.assertRaisesRegex(ValueError, "向量 ID 必须保持不变"):
            import_seed(self.path, SeedBank.model_validate(bad))
        self.assertEqual(list_questions(self.path, verified_only=False)[0]["stable_faiss_id"], 101)

    def test_loader_rejects_missing_or_duplicate_source_and_normalized_duplicate_question(self):
        data = self.bank.model_dump(mode="json")
        missing = copy.deepcopy(data)
        missing["questions"][0]["source_refs"] = ["unknown-source"]
        duplicate_source = copy.deepcopy(data)
        duplicate_source["questions"][0]["source_refs"] *= 2
        duplicate_question = copy.deepcopy(data)
        original = data["questions"][0]["content"]
        duplicate_question["questions"][1]["content"] = "　" + original.replace("Agent", "ＡＧＥＮＴ")
        wrong_revision = copy.deepcopy(data)
        wrong_revision["questions"][0]["revision"] = "1"
        for name, invalid in {"missing": missing, "source_twice": duplicate_source,
                              "same_question": duplicate_question, "string_revision": wrong_revision}.items():
            with self.subTest(name=name), self.assertRaises(ValidationError):
                SeedBank.model_validate_json(json.dumps(invalid, ensure_ascii=False))

    def test_refuse_to_initialize_an_existing_unregistered_db(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE other (message TEXT)")
            db.execute("INSERT INTO other VALUES ('preserve')")
        with self.assertRaisesRegex(ValueError, "未登记数据库"):
            import_seed(self.path, self.bank)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT message FROM other").fetchone()[0], "preserve")
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)

    def test_failed_initial_migration_rolls_back_schema_and_version(self):
        bad_migration = self.path.with_suffix(".sql")
        sql = MIGRATION.read_text(encoding="utf-8")
        bad_migration.write_text(sql + "\nCREATE TABLE question(id INTEGER);\n", encoding="utf-8")
        with patch("app.kb.store.MIGRATION", bad_migration), self.assertRaises(sqlite3.OperationalError):
            import_seed(self.path, self.bank)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master").fetchall(), [])
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
        self.assertEqual(import_seed(self.path, self.bank)["inserted"], len(self.bank.questions))

    def test_refuse_modified_migration_history(self):
        import_seed(self.path, self.bank)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE schema_migration SET checksum='different'")
        with self.assertRaisesRegex(ValueError, "迁移校验值不匹配"):
            import_seed(self.path, self.bank)
        with self.assertRaisesRegex(ValueError, "迁移校验值不匹配"):
            list_questions(self.path, verified_only=False)
