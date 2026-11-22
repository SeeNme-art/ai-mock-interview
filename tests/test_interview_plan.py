from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from app.db.business import MIGRATIONS
from app.db.sqlite import connect, initialize_or_check
from app.kb.seed import SeedBank, read_seed
from app.kb.store import import_seed, list_questions
from app.schemas.materials import Confirmation, MaterialInput, ProjectInput
from app.schemas.planning import PlanChanges
from app.services.materials import confirm_material, load_material, start_draft
from app.services.planning import PlanningError, rebuild_plan, show_plan

SEED = ROOT / "data/seeds/knowledge_questions.json"


class InterviewPlanTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name) / "interviews.db"
        self.bank = Path(folder.name) / "bank.db"
        self.seed = read_seed(SEED)
        import_seed(self.bank, self.seed)

    def session(self, *projects, mode="mixed"):
        material = MaterialInput(jd_text="仅在会话内保存的合成 JD", skills=["仅限材料的技能"], projects=list(projects))
        draft = start_draft(material, "form")
        return confirm_material(self.db, draft.id, draft.id, Confirmation(material=material, mode=mode), draft=draft)

    def build(self, session, key="plan-one", revision=1, **changes):
        return rebuild_plan(self.db, self.bank, session, revision, key, PlanChanges(**changes))

    def rows(self, session):
        with closing(connect(self.db, readonly=True)) as db:
            return [dict(row) for row in db.execute("SELECT * FROM plan_item WHERE session_id=? ORDER BY seq", (session,))]

    def test_default_mixed_plan_interleaves_and_keeps_actual_topic_references(self):
        session = self.session(ProjectInput(action="实现检索分块", result="用固定查询对比分块大小"))
        result = self.build(session)
        self.assertEqual(result["counts"], {"knowledge": 3, "project": 2})
        self.assertEqual([row["task_type"] for row in result["items"]],
                         ["knowledge", "project", "knowledge", "project", "knowledge"])
        self.assertEqual(sorted(sum(group.values()) for group in result["coverage"].values()), [1, 2, 2])
        self.assertEqual((result["phase"], result["revision"], result["plan_version"]), ("PLANNING", 2, 1))
        rows = self.rows(session)
        projects = [row for row in rows if row["task_type"] == "project"]
        self.assertEqual([(row["project_ref"], row["project_field"]) for row in projects],
                         [("p_1", "action"), ("p_1", "result")])
        knowledge = [row for row in rows if row["task_type"] == "knowledge"]
        bank = {row.id: row for row in self.seed.questions if row.status == "verified"}
        self.assertEqual(len({row["question_id"] for row in knowledge}), 3)
        for row in knowledge:
            question = bank[row["question_id"]]
            self.assertEqual((row["dimension"], row["difficulty"], row["question_revision"]),
                             (question.dimension, question.difficulty, question.revision))
            self.assertEqual(json.loads(row["scoring_targets_json"]), question.applicable_dimensions)

    def test_vector_ranks_only_current_reviewed_rows_and_replays_without_model(self):
        from app.services.first_question import accept_start, prepare_first_question, start_epoch

        session = self.session(mode="knowledge_only")
        vector = (self.db.parent / "model", self.db.parent / "index")

        def reverse_order(bank, _, embedder, *, dimension, difficulty, jd, skills, **params):
            self.assertEqual((jd, skills), ("仅在会话内保存的合成 JD", ["仅限材料的技能"]))
            rows = list_questions(bank, dimension=dimension, difficulty=difficulty)
            return [{"question": row, "index_version": "fixture", "query_version": "fixture-v1"} for row in reversed(rows)]

        with patch("app.kb.embedding.LocalChineseEmbedder"), patch("app.kb.retrieval.search_questions", side_effect=reverse_order):
            result = rebuild_plan(self.db, self.bank, session, 1, "rank", PlanChanges(), vector=vector)
        self.assertEqual(result["retrieval"]["method"], "faiss")
        first = self.rows(session)[0]
        latest = list_questions(self.bank, dimension=first["dimension"], difficulty=first["difficulty"])[-1]
        self.assertEqual((first["question_id"], first["form_reason"]), (latest["id"], "verified_bank_faiss"))
        self.assertNotIn("合成 JD", json.dumps(result, ensure_ascii=False))
        with patch("app.kb.embedding.LocalChineseEmbedder", side_effect=AssertionError("replay")):
            self.assertEqual(rebuild_plan(self.db, self.bank, session, 1, "rank", PlanChanges(), vector=vector), result)
        operation, _ = accept_start(self.db, session, result["revision"], "start")
        prepare_first_question(self.db, self.bank, session, operation, start_epoch(self.db, session, operation))
        with closing(connect(self.db, readonly=True)) as db:
            snapshot = json.loads(db.execute("SELECT question_snapshot_json FROM turn").fetchone()[0])
            self.assertEqual((snapshot["id"], snapshot["selection_method"]), (first["question_id"], "faiss"))

    def test_missing_local_embedding_falls_back_without_losing_five_question_plan(self):
        session = self.session(mode="knowledge_only")
        folder = self.db.parent / "missing"
        result = rebuild_plan(self.db, self.bank, session, 1, "rank", PlanChanges(), vector=(folder, folder))
        self.assertEqual(result["retrieval"], {"method": "sqlite", "reason": "VECTOR_UNAVAILABLE"})
        self.assertEqual(result["counts"], {"knowledge": 5, "project": 0})
        self.assertEqual({row["form_reason"] for row in self.rows(session)}, {"verified_bank"})

    def test_project_fallback_uses_reviewed_knowledge_and_explains_quota(self):
        session = self.session(ProjectInput(action="实现检索分块"))
        result = self.build(session)
        self.assertEqual(result["counts"], {"knowledge": 4, "project": 1})
        self.assertIn("审核知识题补足", result["hints"][0])
        unrelated = self.session(ProjectInput(action="维护学校社团活动"))
        result = self.build(unrelated, key="unmatched")
        self.assertEqual(result["counts"], {"knowledge": 5, "project": 0})
        self.assertIn("0 道项目题", result["hints"][0])

    def test_knowledge_only_and_eight_question_mix_follow_requested_counts(self):
        session = self.session(ProjectInput(action="实现 SQLite 数据库接口"), mode="knowledge_only")
        result = self.build(session)
        self.assertEqual(result["counts"], {"knowledge": 5, "project": 0})
        session = self.session(ProjectInput(action="实现检索分块", result="用固定查询比对召回"),
                               ProjectInput(action="用 FastAPI 实现返回接口"))
        result = self.build(session, key="eight-mix", question_count=8)
        self.assertEqual(result["counts"], {"knowledge": 5, "project": 3})

    def test_rebuild_swaps_plan_and_replays_original_receipt_without_the_bank(self):
        session = self.session(ProjectInput(action="实现检索分块", result="固定查询对比"))
        first = self.build(session)
        old_ids = {row["id"] for row in self.rows(session)}
        second = self.build(session, "plan-two", revision=2, difficulty="applied")
        self.assertEqual((second["revision"], second["plan_version"], second["difficulty"]), (3, 2, "applied"))
        self.assertTrue(old_ids.isdisjoint({row["id"] for row in self.rows(session)}))
        with patch("app.services.planning.list_questions", side_effect=AssertionError("replay must not read bank")):
            self.assertEqual(self.build(session), first)
            self.assertEqual(show_plan(self.db, session), second)
        with self.assertRaises(PlanningError) as error:
            self.build(session, difficulty="applied")
        self.assertEqual(error.exception.code, "IDEMPOTENCY_CONFLICT")
        with self.assertRaises(PlanningError) as error:
            self.build(session, "different-key", revision=2)
        self.assertEqual((error.exception.code, error.exception.current_revision), ("STALE_REVISION", 3))

    def test_limited_candidates_leave_existing_plan_intact(self):
        session = self.session(mode="knowledge_only")
        first = self.build(session)
        for changes in ({"difficulty": "applied"}, {"directions": ["rag"]}, {"question_count": 8}):
            with self.subTest(changes=changes), self.assertRaises(PlanningError) as error:
                self.build(session, "too-few", revision=2, **changes)
            self.assertEqual(error.exception.code, "QUESTION_UNAVAILABLE")
        self.assertEqual(show_plan(self.db, session), first)

    def test_retired_questions_and_duplicate_objectives_do_not_supply_capacity(self):
        session = self.session(mode="knowledge_only")
        payload = self.seed.model_dump(mode="json")
        rows = {question["id"]: question for question in payload["questions"]}
        victim = rows["agent-tool-executor"]
        victim.update(status="retired", revision=victim["revision"] + 1)
        duplicate = rows["agent-json-schema"]
        duplicate.update(objective=rows["agent-workflow-boundary"]["objective"], revision=duplicate["revision"] + 1)
        import_seed(self.bank, SeedBank.model_validate(payload))
        with self.assertRaisesRegex(PlanningError, "只有 4 道"):
            self.build(session)
        self.assertEqual(self.rows(session), [])
        self.assertEqual(load_material(self.db, session)["revision"], 1)

    def test_concurrent_same_key_and_stale_new_key(self):
        session = self.session(mode="knowledge_only")
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(self.build, session) for _ in range(2)]
            self.assertEqual(jobs[0].result(), jobs[1].result())
        self.assertEqual(len(self.rows(session)), 5)
        with closing(connect(self.db, readonly=True)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM operation").fetchone()[0], 1)
        with self.assertRaises(PlanningError) as error:
            self.build(session, "new-key")
        self.assertEqual(error.exception.code, "STALE_REVISION")

    def test_meeting_in_progress_freezes_plan_and_hides_deleting_session(self):
        session = self.session(mode="knowledge_only")
        first = self.build(session)
        with closing(connect(self.db)) as db, db:
            db.execute("UPDATE interview_session SET phase='ACTIVE' WHERE id=?", (session,))
        with self.assertRaises(PlanningError) as error:
            self.build(session, "active", revision=2)
        self.assertEqual(error.exception.code, "ILLEGAL_ACTION")
        self.assertEqual(self.build(session), first)
        with closing(connect(self.db)) as db, db:
            db.execute("UPDATE interview_session SET delete_state='deleting' WHERE id=?", (session,))
        with self.assertRaises(PlanningError) as error:
            self.build(session)
        self.assertEqual(error.exception.code, "RESOURCE_NOT_FOUND")

    def test_failed_write_preserves_the_previous_plan_revision_and_receipt(self):
        session = self.session(mode="knowledge_only")
        first = self.build(session)
        old_rows = self.rows(session)
        with closing(connect(self.db)) as db, db:
            db.execute("CREATE TRIGGER reject_plan BEFORE INSERT ON plan_item BEGIN SELECT RAISE(ABORT, 'test'); END;")
        with self.assertRaises(sqlite3.IntegrityError):
            self.build(session, "fail", revision=2)
        self.assertEqual(show_plan(self.db, session), first)
        self.assertEqual(self.rows(session), old_rows)
        with closing(connect(self.db, readonly=True)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM operation").fetchone()[0], 1)

    def test_v1_sessions_upgrade_with_a_consistent_backup_and_keep_material_confirmation(self):
        with closing(connect(self.db)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            initialize_or_check(db, MIGRATIONS[0], 1, "业务库")
        session = self.session(mode="knowledge_only")
        old = load_material(self.db, session)
        with self.assertRaisesRegex(PlanningError, "还没有计划"):
            show_plan(self.db, session)
        self.assertEqual(self.build(session)["revision"], 2)
        with closing(connect(self.db, readonly=True)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            entry = db.execute("SELECT * FROM migration_backup").fetchone()
        backup = self.db.with_name(entry["file_name"])
        self.assertEqual(load_material(backup, session), old)
        self.assertEqual(load_material(self.db, session)["resume_profile"], old["resume_profile"])
        with closing(connect(backup, readonly=True)) as db:
            self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(len(list(self.db.parent.glob("*.before-002-*.db"))), 1)

    def test_broken_upgrade_rolls_back_schema_and_preserves_the_v1_backup(self):
        with closing(connect(self.db)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            initialize_or_check(db, MIGRATIONS[0], 1, "业务库")
        session = self.session(mode="knowledge_only")
        old = load_material(self.db, session)
        broken = self.db.with_suffix(".sql")
        broken.write_text(MIGRATIONS[1].read_text(encoding="utf-8") + "\nCREATE TABLE interview_session (id TEXT);\n",
                          encoding="utf-8")
        with patch("app.db.business.MIGRATIONS", (MIGRATIONS[0], broken)), self.assertRaises(sqlite3.OperationalError):
            self.build(session)
        with closing(connect(self.db, readonly=True)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='plan_item'").fetchone())
        self.assertEqual(load_material(self.db, session), old)
        backups = list(self.db.parent.glob("*.before-002-*.db"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(load_material(backups[0], session), old)

    def test_pending_operation_blocks_new_plan(self):
        session = self.session(mode="knowledge_only")
        with closing(connect(self.db)) as db, db:
            db.execute("""INSERT INTO operation(id,session_id,owner_id,kind,idempotency_key,request_hash,
                          expected_epoch,status,safe_request_json,created_at,updated_at)
                          VALUES ('op1',?,'local','start','start-1','test',0,'queued','{}','2026-11-21','2026-11-21')""",
                       (session,))
        with self.assertRaises(PlanningError) as error:
            self.build(session)
        self.assertEqual(error.exception.code, "OPERATION_IN_PROGRESS")
        self.assertEqual(self.rows(session), [])

    def test_uncovered_directions_are_reported_even_when_other_directions_can_fill_plan(self):
        session = self.session(mode="knowledge_only")
        payload = self.seed.model_dump(mode="json")
        row = next(question for question in payload["questions"] if question["id"] == "rag-cosine-faiss")
        row.update(status="retired", revision=row["revision"] + 1)
        import_seed(self.bank, SeedBank.model_validate(payload))
        result = self.build(session)
        self.assertEqual(result["coverage"]["rag"], {"knowledge": 0, "project": 0})
        self.assertIn("未覆盖 rag", result["hints"][0])

    def test_cli_readback_uses_only_summary_fields(self):
        session = self.session(ProjectInput(action="实现检索分块", result="合成验证记录"))
        cli = [sys.executable, "-X", "utf8", str(ROOT / "scripts/interview_plan.py"), "--db", str(self.db), "--bank", str(self.bank)]
        created = subprocess.check_output([*cli, "build", session, "--expected-revision", "1", "--key", "cli"],
                                          encoding="utf-8", timeout=15)
        shown = subprocess.check_output([*cli, "show", session], encoding="utf-8", timeout=15)
        self.assertEqual(json.loads(created), json.loads(shown))
        self.assertEqual(set(json.loads(shown)["items"][0]), {"seq", "task_type", "dimension", "difficulty"})
        with closing(connect(self.db, readonly=True)) as db:
            receipt = tuple(db.execute("SELECT safe_request_json, result_refs_json FROM operation").fetchone())
        for marker in ("question_id", "仅在会话内保存的合成 JD", "实现检索分块", "合成验证记录"):
            self.assertNotIn(marker, shown + "".join(receipt))

    def test_changes_validate_direction_and_count(self):
        for changes in ({"directions": []}, {"directions": ["rag", "rag"]}, {"directions": ["vision"]},
                        {"question_count": 4}, {"question_count": 5.0}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                PlanChanges(**changes)
