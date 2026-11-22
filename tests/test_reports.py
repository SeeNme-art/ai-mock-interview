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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from app.db.business import MIGRATIONS, SessionError
from app.db.sqlite import canonical_json, connect
from app.kb.seed import read_seed
from app.kb.store import import_seed
from app.schemas.materials import Confirmation, MaterialInput, ProjectInput
from app.schemas.planning import PlanChanges
from app.schemas.scoring import DIMENSIONS
from app.services.materials import confirm_material, start_draft
from app.services.planning import rebuild_plan
from app.services.reports import generate_report, read_report

NOW = "2026-11-22T00:00:00+00:00"
ANSWER = "先记录约束，再用固定输入检验。"


class ReportTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "interviews.db"
        self.bank = Path(folder.name) / "questions.db"
        import_seed(self.bank, read_seed(ROOT / "data/seeds/knowledge_questions.json"))

    def plan(self, *, version=5, mixed=False):
        material = MaterialInput(projects=[ProjectInput(action="实现检索", result="比较结果")] if mixed else [])
        draft = start_draft(material, "form")
        with patch("app.db.business.MIGRATIONS", MIGRATIONS[:version]):
            session_id = confirm_material(self.path, draft.id, draft.id,
                                          Confirmation(material=material, mode="mixed" if mixed else "knowledge_only"),
                                          draft=draft)
        rebuild_plan(self.path, self.bank, session_id, 1, "plan-" + session_id, PlanChanges())
        return session_id

    def close_session(self, session_id, outcomes, *, end_reason="completed"):
        """Seed only the saved report inputs for cases unavailable in the current CLI."""
        with closing(connect(self.path)) as db, db:
            plan = db.execute("SELECT * FROM plan_item WHERE session_id=? ORDER BY seq", (session_id,)).fetchall()
            self.assertEqual(len(plan), len(outcomes))
            for slot, (outcome, score) in zip(plan, outcomes):
                db.execute("UPDATE plan_item SET status='closed',outcome=? WHERE id=?", (outcome, slot["id"]))
                if outcome != "generation_failed":
                    self.insert_turn(db, slot, score)
            db.execute("UPDATE interview_session SET phase='FINISHED',end_reason=?,ended_at=? WHERE id=?",
                       (end_reason, NOW, session_id))

    @staticmethod
    def insert_turn(db, slot, value):
        turn_id = "turn-" + slot["id"]
        exchange_id = "answer-" + slot["id"]
        task_type = slot["task_type"]
        dimensions = DIMENSIONS[task_type]
        rubric = {"rubric_version": "fixture-v1", "applicable_dimensions": [dimensions[0]]}
        question = {"id": "archived-question", "revision": 2}
        source = {"id": "fixture-source", "title": "合成审核出处", "locator": "fixture.md"}
        db.execute("""INSERT INTO turn(id,session_id,plan_item_id,task_type,dimension,difficulty,generation_method,
                                      state,question_snapshot_json,source_snapshot_json,rubric_snapshot_json,created_at)
                      VALUES (?,?,?,?,?,?,?,'closed',?,?,?,?)""",
                   (turn_id, slot["session_id"], slot["id"], task_type, slot["dimension"], slot["difficulty"],
                    slot["generation_method"], canonical_json(question), canonical_json([source]), canonical_json(rubric), NOW))
        db.execute("INSERT INTO exchange(id,turn_id,round_no,question_text,answer_text,answered_at) VALUES (?,?,0,?,?,?)",
                   (exchange_id, turn_id, "请解释验证方法。", ANSWER, NOW))
        if value is None:
            return
        if value == "failed":
            db.execute("""INSERT INTO evaluation(id,turn_id,status,error_code,model_context_json,created_at,updated_at)
                          VALUES (?,?,'failed','OUTPUT_INVALID','{}',?,?)""", ("eval-" + turn_id, turn_id, NOW, NOW))
            return
        assessment = {
            "task_type": task_type, "rubric_version": "fixture-v1",
            "improvements": ["未定位依据的旧建议"],
            "dimensions": {name: {
                "applicable": name == dimensions[0], "value": value if name == dimensions[0] else None,
                "reason": "合成观察", "evidence": [
                    {"exchange_id": exchange_id, "quote": ANSWER, "start": 0, "end": len(ANSWER)}
                ] if name == dimensions[0] else [],
                "source_refs": ["fixture-source"] if name == dimensions[0] else [],
            } for name in dimensions},
        }
        db.execute("""INSERT INTO evaluation(id,turn_id,status,assessment_json,item_score,model_context_json,created_at,updated_at)
                      VALUES (?,?,'ready',?,?,'{}',?,?)""",
                   ("eval-" + turn_id, turn_id, canonical_json(assessment), value * 25, NOW, NOW))

    def test_report_distinguishes_zero_missing_failed_and_skipped_with_an_answer(self):
        session = self.plan(mixed=True)
        self.close_session(session, [("answered", 0), ("answered", 3), ("answered", "failed"),
                                     ("generation_failed", None), ("skipped", None)])
        with patch("socket.socket.connect", side_effect=AssertionError("must stay offline")):
            report = generate_report(self.path, session)
        self.assertEqual((report["status"], report["kind"], report["is_partial"]), ("template_only", "report", True))
        self.assertEqual((report["knowledge_score"], report["project_score"]), (0.0, 75.0))
        self.assertEqual(report["category_counts"], {"knowledge": 1, "project": 1})
        self.assertEqual(report["counts"], {"planned": 5, "displayed": 4, "answered": 4, "scored": 2,
                                            "skipped": 1, "system_failed": 2})
        self.assertEqual(report["dimension_summary"]["correctness"], {"mean": 0.0, "count": 1})
        self.assertEqual(report["dimension_summary"]["depth"], {"mean": None, "count": 0})
        self.assertNotIn("overall", report)
        first, _, failed, missing, skipped = report["items"]
        self.assertEqual(failed["evaluation"]["error_code"], "OUTPUT_INVALID")
        self.assertIsNone(failed["evaluation"]["item_score"])
        self.assertEqual(missing["exchanges"], [])
        self.assertEqual(skipped["exchanges"][0]["answer"], ANSWER)
        suggestion, = report["improvements"]
        self.assertEqual(suggestion["seq"], 1)
        evidence = suggestion["evidence"]
        exchange = first["exchanges"][0]
        self.assertEqual(evidence["exchange_id"], exchange["id"])
        self.assertEqual(exchange["answer"][evidence["start"]:evidence["end"]], evidence["quote"])
        self.assertNotIn("未定位依据的旧建议", json.dumps(report, ensure_ascii=False))

    def test_complete_report_averages_valid_items_and_survives_live_row_changes(self):
        session = self.plan()
        self.close_session(session, [("answered", grade) for grade in (4, 3, 3, 2, 0)])
        report = generate_report(self.path, session)
        self.assertEqual((report["knowledge_score"], report["project_score"], report["is_partial"]), (60.0, None, False))
        self.assertEqual((report["counts"]["scored"], report["category_counts"]["project"]), (5, 0))
        self.assertEqual([item["seq"] for item in report["improvements"]], [5])
        with closing(connect(self.path)) as db, db:
            db.execute("UPDATE exchange SET answer_text='更新后的文字'")
            db.execute("UPDATE turn SET source_snapshot_json='[]'")
            db.execute("UPDATE interview_session SET ended_at='new timestamp' WHERE id=?", (session,))
        self.assertEqual(read_report(self.path, session, 1), report)
        self.assertEqual(generate_report(self.path, session), report)

    def test_unfinished_deleted_and_foreign_session_cannot_expose_reports(self):
        session = self.plan(version=4)
        for operation in (read_report, generate_report):
            with self.assertRaises(SessionError) as unfinished:
                operation(self.path, session)
            self.assertEqual(unfinished.exception.code, "ILLEGAL_ACTION")
        self.close_session(session, [("generation_failed", None)] * 5)
        self.assertEqual(read_report(self.path, session)["status"], "pending")
        with closing(connect(self.path)) as db, db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
            db.execute("UPDATE interview_session SET owner_id='foreign' WHERE id=?", (session,))
        for operation in (read_report, generate_report):
            with self.assertRaises(SessionError) as hidden:
                operation(self.path, session)
            self.assertEqual(hidden.exception.code, "RESOURCE_NOT_FOUND")
        with closing(connect(self.path)) as db, db:
            db.execute("UPDATE interview_session SET owner_id='local',delete_state='deleting' WHERE id=?", (session,))
        with self.assertRaises(SessionError) as hidden:
            generate_report(self.path, session)
        self.assertEqual(hidden.exception.code, "RESOURCE_NOT_FOUND")

    def test_old_unscored_session_generates_a_summary_with_sequential_backups(self):
        session = self.plan(version=3)
        self.close_session(session, [("generation_failed", None)] * 5, end_reason="system_failure")
        with closing(connect(self.path)) as db, db:
            db.execute("""INSERT INTO operation(id,session_id,owner_id,kind,idempotency_key,request_hash,expected_epoch,
                                                status,safe_request_json,error_code,created_at,updated_at)
                          VALUES ('start',?,'local','start','start','hash',1,'failed','{}','QUESTION_UNAVAILABLE',?,?)""",
                       (session, NOW, NOW))
        with self.assertRaises(SessionError) as missing:
            read_report(self.path, session, 1)
        self.assertEqual(missing.exception.code, "RESOURCE_NOT_FOUND")
        report = generate_report(self.path, session)
        self.assertEqual((report["kind"], report["knowledge_score"], report["project_score"]), ("summary", None, None))
        self.assertEqual((report["end_reason"], report["is_partial"], report["counts"]["system_failed"]),
                         ("system_failure", True, 5))
        self.assertEqual(report["items"][0]["generation_error_code"], "QUESTION_UNAVAILABLE")
        self.assertEqual(report["improvements"], [])
        with closing(connect(self.path, readonly=True)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 5)
            backups = db.execute("SELECT file_name,schema_version FROM migration_backup ORDER BY schema_version").fetchall()
        self.assertEqual([row["schema_version"] for row in backups], [3, 4])
        for name, version in backups:
            with closing(connect(self.path.with_name(name), readonly=True)) as backup:
                self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], version)

    def test_two_generators_create_one_revision_for_an_existing_v4_session(self):
        session = self.plan(version=4)
        self.close_session(session, [("answered", 3)] * 5)
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(generate_report, self.path, session) for _ in range(2)]
            reports = [job.result() for job in jobs]
        self.assertEqual(reports[0], reports[1])
        with closing(connect(self.path, readonly=True)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM report_revision").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM migration_backup WHERE schema_version=4").fetchone()[0], 1)

    def test_report_write_failure_preserves_finished_session_and_can_retry(self):
        session = self.plan()
        self.close_session(session, [("answered", 4)] * 5)
        with closing(connect(self.path)) as db, db:
            db.execute("""CREATE TRIGGER fail_report BEFORE INSERT ON report_revision
                          BEGIN SELECT RAISE(ABORT,'injected report write failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            generate_report(self.path, session)
        self.assertEqual(read_report(self.path, session)["status"], "pending")
        with closing(connect(self.path)) as db, db:
            self.assertEqual(db.execute("SELECT phase FROM interview_session").fetchone()[0], "FINISHED")
            db.execute("DROP TRIGGER fail_report")
        self.assertEqual(generate_report(self.path, session)["revision_no"], 1)

    def test_cli_reads_saved_revision_in_another_process(self):
        session = self.plan()
        self.close_session(session, [("answered", 4)] * 5, end_reason="no_time")
        prefix = [sys.executable, "-X", "utf8", str(ROOT / "scripts/interview.py"), "--db", str(self.path)]

        def invoke(*arguments):
            result = subprocess.run([*prefix, *arguments], text=True, encoding="utf-8",
                                    capture_output=True, check=True)
            return json.loads(result.stdout)

        self.assertEqual(invoke("report", session)["status"], "pending")
        saved = invoke("generate-report", session)
        self.assertEqual((saved["revision_no"], saved["knowledge_score"], saved["is_partial"]), (1, 100.0, True))
        self.assertEqual(invoke("report", session, "--revision", "1"), saved)
