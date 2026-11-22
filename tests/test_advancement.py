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
from app.db.business import SessionError
from app.db.sqlite import connect
from app.kb.seed import SeedBank, read_seed
from app.kb.store import import_seed
from app.schemas.materials import Confirmation, MaterialInput, ProjectInput
from app.schemas.planning import PlanChanges
from app.schemas.scoring import DIMENSIONS
from app.services.advancement import accept_advance
from app.services.answers import accept_answer
from app.services.evaluation import accept_evaluation
from app.services.first_question import accept_start, prepare_first_question, read_state, start_epoch
from app.services.materials import confirm_material, start_draft
from app.services.planning import rebuild_plan

try:
    from app.workflow.interview import run_advance, run_answer, run_evaluation
except ModuleNotFoundError as exc:
    if exc.name not in {"langgraph", "langsmith"}:
        raise
    HAS_GRAPH = False
else:
    HAS_GRAPH = True

ANSWER = "这是测试用的回答，记录了预先定义的约束。"


@unittest.skipUnless(HAS_GRAPH, "install the locked LangGraph dependencies to exercise advancement")
class AdvancementTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name) / "interviews.db"
        self.bank = Path(folder.name) / "questions.db"
        self.seed = read_seed(ROOT / "data/seeds/knowledge_questions.json")
        import_seed(self.bank, self.seed)

    def plan(self, *, mixed=False):
        projects = [ProjectInput(action="实现检索分块", result="以固定查询对比结果")] if mixed else []
        profile = MaterialInput(projects=projects)
        draft = start_draft(profile, "form")
        self.session = confirm_material(self.db, draft.id, draft.id,
                                        Confirmation(material=profile, mode="mixed" if mixed else "knowledge_only"), draft=draft)
        rebuild_plan(self.db, self.bank, self.session, 1, "plan-" + self.session, PlanChanges())

    def start(self):
        op, _ = accept_start(self.db, self.session, self.state()["revision"], "start-" + self.session)
        prepare_first_question(self.db, self.bank, self.session, op, start_epoch(self.db, self.session, op))

    def state(self):
        return read_state(self.db, self.session)

    def slots(self):
        with closing(connect(self.db, readonly=True)) as db:
            return [dict(row) for row in db.execute("SELECT * FROM plan_item WHERE session_id=? ORDER BY seq", (self.session,))]

    def retire(self, ids):
        payload = self.seed.model_dump(mode="json")
        for item in payload["questions"]:
            if item["id"] in ids:
                item.update(status="retired", revision=item["revision"] + 1)
        self.seed = SeedBank.model_validate(payload)
        import_seed(self.bank, self.seed)

    @staticmethod
    def assessment(context):
        rubric = json.loads(context.rubric_snapshot)
        active = set(rubric["applicable_dimensions"])
        evidence = [{"exchange_id": context.exchanges[0].id, "quote": ANSWER}]
        return {"task_type": context.task_type, "rubric_version": rubric["rubric_version"], "dimensions": {
            key: {"applicable": key in active, "value": 3 if key in active else None,
                  "reason": "合成已答检查", "evidence": evidence if key in active else []}
            for key in DIMENSIONS[context.task_type]
        }}

    def close_current(self, *, valid=True):
        state = self.state()
        current = state["current"]
        receipt = accept_answer(self.db, self.session, current["turn_id"], current["exchange_id"],
                                state["revision"], f"answer-{current['seq']}", ANSWER)
        run_answer(self.db, self.session, receipt["operation_id"], lambda _: {"should_probe": False, "reason": "合成收尾"})
        score, _ = accept_evaluation(self.db, self.session, receipt["operation_id"], self.state()["revision"],
                                     f"score-{current['seq']}")
        run_evaluation(self.db, self.session, score, self.assessment if valid else lambda _: {})

    def queue(self, key="advance-1", revision=None):
        return accept_advance(self.db, self.session, self.state()["revision"] if revision is None else revision, key)

    def advance(self, key):
        operation, accepted = self.queue(key)
        self.assertTrue(accepted)
        return run_advance(self.db, self.bank, self.session, operation)

    def test_five_question_knowledge_session_finishes_on_last_score(self):
        self.plan()
        self.start()
        questions = set()
        for seq in range(1, 6):
            state = self.state()
            self.assertEqual((state["phase"], state["current"]["seq"], state["progress"]["closed"]), ("ACTIVE", seq, seq - 1))
            self.assertNotIn(state["current"]["question"], questions)
            questions.add(state["current"]["question"])
            self.close_current()
            if seq < 5:
                self.advance(f"advance-{seq}")
        final = self.state()
        self.assertEqual((final["phase"], final["end_reason"], final["plan_version"]), ("FINISHED", "completed", 1))
        self.assertTrue(final["ended_at"].endswith("+00:00"))
        self.assertIsNone(final["current"])
        self.assertEqual(final["progress"], {"planned": 5, "closed": 5, "answered": 5, "scored": 5, "skipped": 0, "failed": 0})
        self.assertNotIn("item_score", json.dumps(final))
        with self.assertRaises(SessionError) as error:
            self.queue("beyond-plan")
        self.assertEqual(error.exception.code, "ILLEGAL_ACTION")

    def test_mixed_plan_records_unavailable_project_and_publishes_next_reviewed_question(self):
        self.plan(mixed=True)
        planned = self.slots()
        self.assertEqual([row["task_type"] for row in planned], ["knowledge", "project", "knowledge", "project", "knowledge"])
        self.start()
        self.close_current()
        self.advance("mixed")
        state = self.state()
        self.assertEqual(state["current"]["seq"], 3)
        self.assertEqual(state["operation"]["unavailable"], [{"seq": 2, "reason_code": "MODEL_UNAVAILABLE"}])
        self.assertEqual(state["progress"]["failed"], 1)
        self.assertEqual(self.slots()[1]["outcome"], "generation_failed")
        self.assertEqual([row["id"] for row in self.slots()], [row["id"] for row in planned])

    def test_failed_score_does_not_block_progress(self):
        self.plan()
        self.start()
        self.close_current(valid=False)
        self.assertEqual((self.state()["progress"]["answered"], self.state()["progress"]["scored"]), (1, 0))
        self.advance("after-failed-score")
        self.assertEqual(self.state()["current"]["seq"], 2)
        self.assertEqual(self.state()["progress"]["failed"], 1)

    def test_missing_first_question_allows_advance_without_a_turn(self):
        self.plan()
        first = self.slots()[0]
        self.retire([q.id for q in self.seed.questions if q.dimension == first["dimension"] and q.difficulty == first["difficulty"]])
        self.start()
        self.assertIsNone(self.state()["current"])
        self.assertEqual(self.state()["operation"]["error_code"], "QUESTION_UNAVAILABLE")
        self.advance("after-first-unavailable")
        self.assertEqual(self.state()["current"]["seq"], 2)

    def test_all_remaining_slots_unavailable_closes_the_session_without_scores_for_them(self):
        self.plan()
        self.start()
        self.close_current()
        self.retire([q.id for q in self.seed.questions])
        self.advance("all-missing")
        state = self.state()
        self.assertEqual((state["phase"], state["end_reason"], state["operation"]["outcome"]), ("FINISHED", "completed", "finished"))
        self.assertEqual([item["seq"] for item in state["operation"]["unavailable"]], [2, 3, 4, 5])
        self.assertEqual(state["progress"], {"planned": 5, "closed": 5, "answered": 1, "scored": 1, "skipped": 0, "failed": 4})

    def test_actual_fallback_is_not_reused_when_a_later_planned_question_is_retired(self):
        self.plan()
        slots = self.slots()
        self.retire([slots[0]["question_id"]])
        self.start()
        with closing(connect(self.db, readonly=True)) as db:
            actual = json.loads(db.execute("SELECT question_snapshot_json FROM turn").fetchone()[0])["id"]
        self.assertNotEqual(actual, slots[0]["question_id"])
        for seq in range(1, 4):
            self.close_current()
            if seq < 3:
                self.advance(f"active-{seq}")
        self.retire([slots[3]["question_id"]])
        self.advance("no-reuse")
        self.assertEqual(self.state()["current"]["seq"], 5)
        self.assertEqual(self.state()["operation"]["unavailable"], [{"seq": 4, "reason_code": "QUESTION_UNAVAILABLE"}])
        with closing(connect(self.db, readonly=True)) as db:
            seen = [json.loads(row[0])["id"] for row in db.execute("SELECT question_snapshot_json FROM turn")]
        self.assertEqual(seen.count(actual), 1)

    def test_before_start_or_with_active_question_or_pending_score_cannot_advance(self):
        self.plan()
        with self.assertRaises(SessionError):
            self.queue()
        self.start()
        with self.assertRaises(SessionError):
            self.queue()
        current = self.state()
        accept_answer(self.db, self.session, current["current"]["turn_id"], current["current"]["exchange_id"],
                      current["revision"], "still-processing", ANSWER)
        with self.assertRaises(SessionError) as error:
            self.queue()
        self.assertEqual(error.exception.code, "OPERATION_IN_PROGRESS")

    def test_concurrent_registration_and_replay_publish_only_one_next_question(self):
        self.plan()
        self.start()
        self.close_current()
        revision = self.state()["revision"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(self.queue, revision=revision) for _ in range(2)]
            receipts = [job.result() for job in jobs]
            op = receipts[0][0]
            self.assertEqual(receipts[1][0], op)
            self.assertEqual(sum(accepted for _, accepted in receipts), 1)
            work = [pool.submit(run_advance, self.db, self.bank, self.session, op) for _ in range(2)]
            self.assertEqual(work[0].result(), work[1].result())
        with patch("app.services.question_delivery.list_questions", side_effect=AssertionError("already published")):
            self.assertEqual(run_advance(self.db, self.bank, self.session, op)["exchange_id"], self.state()["current"]["exchange_id"])
        with self.assertRaises(SessionError) as stale:
            self.queue("new-key", revision)
        self.assertEqual(stale.exception.code, "STALE_REVISION")
        with closing(connect(self.db, readonly=True)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM turn").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM operation_step WHERE step_key='advance_plan'").fetchone()[0], 1)

    def test_unavailable_slots_and_new_question_roll_back_together_on_write_failure(self):
        self.plan(mixed=True)
        self.start()
        self.close_current()
        op, _ = self.queue()
        before = self.state()
        with closing(connect(self.db)) as db, db:
            db.execute("CREATE TRIGGER reject_next BEFORE INSERT ON exchange BEGIN SELECT RAISE(ABORT,'test'); END;")
        with self.assertRaises(sqlite3.IntegrityError):
            run_advance(self.db, self.bank, self.session, op)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.slots()[1]["status"], "pending")
        with closing(connect(self.db)) as db, db:
            db.execute("DROP TRIGGER reject_next")
        run_advance(self.db, self.bank, self.session, op)
        self.assertEqual(self.state()["current"]["seq"], 3)

    def test_late_or_cross_session_advance_cannot_publish_a_turn(self):
        self.plan()
        self.start()
        self.close_current()
        op, _ = self.queue()
        with self.assertRaisesRegex(SessionError, "找不到可访问的会话"):
            run_advance(self.db, self.bank, "other", op)
        with closing(connect(self.db)) as db, db:
            db.execute("UPDATE interview_session SET phase='FINISHED',control_epoch=control_epoch+1 WHERE id=?", (self.session,))
        with self.assertRaisesRegex(SessionError, "推进操作已失效"):
            run_advance(self.db, self.bank, self.session, op)
        self.assertEqual(self.slots()[1]["status"], "pending")

    def test_cli_can_resume_pending_advancement_in_a_new_process(self):
        self.plan()
        self.start()
        self.close_current()
        revision = self.state()["revision"]
        op, _ = self.queue(revision=revision)
        cli = [sys.executable, "-X", "utf8", str(ROOT / "scripts/interview.py"), "--db", str(self.db), "--bank", str(self.bank)]

        def call(*args):
            return json.loads(subprocess.check_output([*cli, *args], timeout=20))

        duplicate = ["advance", self.session, "--expected-revision", str(revision), "--key", "advance-1"]
        self.assertEqual(call(*duplicate)["operation"]["status"], "queued")
        prepared = call("resume-advance", self.session, op)
        self.assertEqual(prepared["current"]["seq"], 2)
        self.assertEqual(call(*duplicate), prepared)
        self.assertEqual(call("state", self.session), prepared)
