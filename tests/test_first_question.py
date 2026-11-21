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
from app.db.sqlite import connect
from app.kb.seed import SeedBank, read_seed
from app.kb.store import import_seed
from app.schemas.materials import Confirmation, MaterialInput
from app.schemas.planning import PlanChanges
from app.services.first_question import accept_start, prepare_first_question, read_state
from app.services.materials import confirm_material, load_material, start_draft
from app.services.planning import rebuild_plan, show_plan

try:
    from app.workflow.interview import run_first_question
except ModuleNotFoundError as exc:
    if exc.name not in {"langgraph", "langsmith"}:
        raise
    HAS_GRAPH = False
else:
    HAS_GRAPH = True


@unittest.skipUnless(HAS_GRAPH, "install the locked LangGraph dependencies to exercise the first-question workflow")
class FirstQuestionTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name) / "interviews.db"
        self.bank = Path(folder.name) / "questions.db"
        self.seed = read_seed(ROOT / "data/seeds/knowledge_questions.json")
        import_seed(self.bank, self.seed)

    def session(self, *, plan=True):
        material = MaterialInput(jd_text="合成材料正文标记", skills=["Python"])
        draft = start_draft(material, "form")
        session_id = confirm_material(self.db, draft.id, draft.id, Confirmation(material=material, mode="knowledge_only"),
                                      draft=draft)
        if plan:
            rebuild_plan(self.db, self.bank, session_id, 1, "plan-" + session_id, PlanChanges())
        return session_id

    def start(self, session):
        operation, accepted = accept_start(self.db, session, 2, "start-" + session)
        self.assertTrue(accepted)
        return operation

    def run_graph(self, session, operation):
        return run_first_question(self.db, self.bank, session, operation)

    def first_slot(self, session):
        with closing(connect(self.db, readonly=True)) as db:
            return dict(db.execute("SELECT * FROM plan_item WHERE session_id=? AND seq=1", (session,)).fetchone())

    def snapshot(self, session):
        with closing(connect(self.db, readonly=True)) as db:
            row = db.execute("SELECT * FROM turn WHERE session_id=?", (session,)).fetchone()
            return tuple(json.loads(row[key]) for key in ("question_snapshot_json", "source_snapshot_json", "rubric_snapshot_json"))

    def counts(self):
        with closing(connect(self.db, readonly=True)) as db:
            return tuple(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                         for table in ("turn", "exchange", "operation_step"))

    def update_bank(self, ids, **fields):
        payload = self.seed.model_dump(mode="json")
        for question in payload["questions"]:
            if question["id"] in ids:
                question["revision"] += 1
                question.update(fields)
        self.seed = SeedBank.model_validate(payload)
        import_seed(self.bank, self.seed)

    def test_start_freezes_plan_and_graph_publishes_only_the_formal_question(self):
        session = self.session()
        slot = self.first_slot(session)
        operation = self.start(session)
        pending = read_state(self.db, session)
        self.assertEqual((pending["phase"], pending["revision"], pending["plan_version"]), ("ACTIVE", 3, 1))
        self.assertEqual(pending["operation"]["status"], "queued")
        self.assertIsNone(pending["current"])
        with self.assertRaisesRegex(SessionError, "计划已冻结"):
            rebuild_plan(self.db, self.bank, session, 3, "late-plan", PlanChanges())
        with patch("socket.socket.connect", side_effect=AssertionError("network is forbidden in the test")):
            graph = self.run_graph(session, operation)
        self.assertEqual(set(graph), {"session_id", "operation_id", "control_epoch", "turn_id", "exchange_id"})
        ready = read_state(self.db, session)
        self.assertEqual((ready["revision"], ready["operation"]["status"]), (4, "succeeded"))
        self.assertEqual((ready["current"]["round_no"], ready["current"]["state"]), (0, "awaiting_answer"))
        self.assertEqual(ready["current"]["exchange_id"], graph["exchange_id"])
        question, sources, rubric = self.snapshot(session)
        self.assertEqual((question["id"], question["revision"]), (slot["question_id"], slot["question_revision"]))
        self.assertTrue(sources)
        self.assertEqual(rubric["rubric_version"], "knowledge-v1")
        self.assertEqual(rubric["applicable_dimensions"], json.loads(slot["scoring_targets_json"]))
        self.assertEqual(self.first_slot(session)["status"], "active")
        for marker in ("answer_points", "合成材料正文标记", "source_snapshot", rubric["answer_points"][0]):
            self.assertNotIn(marker, json.dumps(ready, ensure_ascii=False) + json.dumps(graph, ensure_ascii=False))

    def test_repeat_start_and_graph_replay_reuse_the_same_saved_turn(self):
        session = self.session()
        operation = self.start(session)
        key = "start-" + session
        self.assertEqual(accept_start(self.db, session, 2, key), (operation, False))
        first = self.run_graph(session, operation)
        with patch("app.services.first_question.list_questions", side_effect=AssertionError("no new selection")):
            self.assertEqual(self.run_graph(session, operation), first)
            self.assertEqual(accept_start(self.db, session, 2, key), (operation, False))
        with self.assertRaises(SessionError) as error:
            accept_start(self.db, session, 4, key)
        self.assertEqual(error.exception.code, "IDEMPOTENCY_CONFLICT")
        with self.assertRaises(SessionError) as error:
            accept_start(self.db, session, 4, "second-key")
        self.assertEqual(error.exception.code, "ILLEGAL_ACTION")
        self.assertEqual(self.counts(), (1, 1, 1))
        self.assertEqual(read_state(self.db, session)["revision"], 4)

    def test_saved_question_and_rubric_survive_later_bank_changes(self):
        session = self.session()
        operation = self.start(session)
        self.run_graph(session, operation)
        before = self.snapshot(session)
        state = read_state(self.db, session)
        self.update_bank([before[0]["id"]], status="retired", answer_points=["更新后的评分要点"])
        self.assertEqual(self.snapshot(session), before)
        self.assertEqual(read_state(self.db, session), state)

    def test_missing_plan_old_revision_and_wrong_session_do_not_start(self):
        session = self.session(plan=False)
        with self.assertRaisesRegex(SessionError, "先生成面试计划"):
            accept_start(self.db, session, 1, "early")
        rebuild_plan(self.db, self.bank, session, 1, "plan-ready", PlanChanges())
        with self.assertRaises(SessionError) as error:
            accept_start(self.db, session, 1, "stale")
        self.assertEqual(error.exception.code, "STALE_REVISION")
        with self.assertRaises(SessionError) as error:
            accept_start(self.db, "other", 2, "other")
        self.assertEqual(error.exception.code, "RESOURCE_NOT_FOUND")
        self.assertIsNone(read_state(self.db, session)["operation"])

    def test_retired_planned_question_uses_verified_fallback_without_reserved_questions(self):
        session = self.session()
        slot = self.first_slot(session)
        with closing(connect(self.db, readonly=True)) as db:
            later = {row[0] for row in db.execute("SELECT question_id FROM plan_item WHERE session_id=? AND seq>1", (session,))}
        operation = self.start(session)
        self.update_bank([slot["question_id"]], status="retired")
        self.run_graph(session, operation)
        question, _, _ = self.snapshot(session)
        self.assertNotIn(question["id"], later | {slot["question_id"]})
        current = read_state(self.db, session)["current"]
        self.assertEqual((current["dimension"], current["difficulty"]), (slot["dimension"], slot["difficulty"]))
        self.assertEqual(current["fallback_reason"], "planned_question_unavailable")
        self.assertEqual(self.first_slot(session)["question_id"], slot["question_id"])

    def test_new_reviewed_revision_can_preserve_the_plan_objective(self):
        session = self.session()
        slot = self.first_slot(session)
        self.update_bank([slot["question_id"]], answer_points=["新版审核后的合成要点"])
        operation = self.start(session)
        self.run_graph(session, operation)
        question, _, rubric = self.snapshot(session)
        self.assertEqual(question["revision"], slot["question_revision"] + 1)
        self.assertEqual(rubric["answer_points"], ["新版审核后的合成要点"])
        self.assertEqual(read_state(self.db, session)["current"]["fallback_reason"], "reviewed_revision_updated")

    def test_no_verified_fallback_records_failure_without_a_question(self):
        session = self.session()
        slot = self.first_slot(session)
        ids = [row.id for row in self.seed.questions
               if row.dimension == slot["dimension"] and row.difficulty == slot["difficulty"]]
        self.update_bank(ids, status="retired")
        operation = self.start(session)
        self.run_graph(session, operation)
        state = read_state(self.db, session)
        self.assertIsNone(state["current"])
        self.assertEqual(state["operation"]["error_code"], "QUESTION_UNAVAILABLE")
        self.assertEqual(self.first_slot(session)["outcome"], "generation_failed")
        self.assertEqual(self.counts(), (0, 0, 1))

    def test_failure_between_turn_and_exchange_rolls_back_then_replays(self):
        session = self.session()
        operation = self.start(session)
        with closing(connect(self.db)) as db, db:
            db.execute("CREATE TRIGGER reject_exchange BEFORE INSERT ON exchange BEGIN SELECT RAISE(ABORT, 'test'); END;")
        with self.assertRaises(sqlite3.IntegrityError):
            self.run_graph(session, operation)
        self.assertEqual(self.counts(), (0, 0, 0))
        self.assertEqual(read_state(self.db, session)["operation"]["status"], "queued")
        with closing(connect(self.db)) as db, db:
            db.execute("DROP TRIGGER reject_exchange")
        self.run_graph(session, operation)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_result_lost_after_commit_does_not_duplicate_side_effects(self):
        session = self.session()
        operation = self.start(session)

        def commit_and_lose_output(*args):
            prepare_first_question(*args)
            raise RuntimeError("simulated interruption after commit")

        with patch("app.workflow.interview.prepare_first_question", side_effect=commit_and_lose_output):
            with self.assertRaisesRegex(RuntimeError, "after commit"):
                self.run_graph(session, operation)
        saved = read_state(self.db, session)
        replayed = self.run_graph(session, operation)
        self.assertEqual(replayed["exchange_id"], saved["current"]["exchange_id"])
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_concurrent_start_and_execution_create_only_one_turn(self):
        session = self.session()
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(accept_start, self.db, session, 2, "same") for _ in range(2)]
            replies = [job.result() for job in jobs]
            self.assertEqual(replies[0][0], replies[1][0])
            self.assertEqual(sum(accepted for _, accepted in replies), 1)
            runs = [pool.submit(self.run_graph, session, replies[0][0]) for _ in range(2)]
            self.assertEqual(runs[0].result(), runs[1].result())
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_epoch_change_and_wrong_operation_scope_block_late_publication(self):
        session = self.session()
        operation = self.start(session)
        other = self.session()
        with self.assertRaisesRegex(SessionError, "找不到该会话"):
            self.run_graph(other, operation)
        with closing(connect(self.db)) as db, db:
            db.execute("UPDATE interview_session SET control_epoch=control_epoch+1 WHERE id=?", (session,))
        with self.assertRaisesRegex(SessionError, "已失效"):
            self.run_graph(session, operation)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_v2_plan_upgrades_with_a_backup_before_start(self):
        with patch("app.db.business.MIGRATIONS", MIGRATIONS[:2]):
            session = self.session()
        original_plan = show_plan(self.db, session)
        original_material = load_material(self.db, session)
        operation = self.start(session)
        self.run_graph(session, operation)
        with closing(connect(self.db, readonly=True)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)
            entry = db.execute("SELECT file_name FROM migration_backup WHERE schema_version=2").fetchone()
        backup = self.db.with_name(entry[0])
        self.assertEqual(show_plan(backup, session), original_plan)
        self.assertEqual(load_material(backup, session), original_material)
        self.assertEqual(read_state(backup, session)["phase"], "PLANNING")

    def test_cli_recovers_pending_operation_in_a_separate_process(self):
        session = self.session()
        operation = self.start(session)
        cli = [sys.executable, "-X", "utf8", str(ROOT / "scripts/interview.py"), "--db", str(self.db), "--bank", str(self.bank)]

        def call(*args):
            return json.loads(subprocess.check_output([*cli, *args], encoding="utf-8", timeout=15))

        pending = call("state", session)
        self.assertIsNone(pending["current"])
        duplicate = call("start", session, "--expected-revision", "2", "--key", "start-" + session)
        self.assertEqual(duplicate, pending)
        self.assertEqual(call("state", session), pending)
        ready = call("resume-start", session, operation)
        self.assertEqual(call("state", session), ready)
        self.assertEqual(call("start", session, "--expected-revision", "2", "--key", "start-" + session), ready)
        self.assertEqual(self.counts(), (1, 1, 1))
