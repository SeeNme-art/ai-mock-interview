from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from app.db.business import MIGRATIONS, SessionError
from app.db.sqlite import connect
from app.kb.seed import read_seed
from app.kb.store import import_seed
from app.schemas.materials import Confirmation, MaterialInput
from app.schemas.planning import PlanChanges
from app.schemas.scoring import DIMENSIONS
from app.services.advancement import accept_advance
from app.services.answers import accept_answer
from app.services.evaluation import accept_evaluation
from app.services.finalization import accept_abort
from app.services.first_question import accept_start, read_state
from app.services.materials import confirm_material, start_draft
from app.services.planning import rebuild_plan
from app.services.reports import generate_report, read_report

try:
    from app.workflow.interview import resume_interview, run_advance, run_answer, run_evaluation, run_finalize, run_first_question
except ModuleNotFoundError as exc:
    if exc.name not in {"langgraph", "langsmith"}:
        raise
    HAS_GRAPH = False
else:
    HAS_GRAPH = True

ANSWER = "工作流按固定步骤执行，Agent 可以在边界内选择下一步。"
PROBE = "你会如何验证 Agent 的权限约束？"


@unittest.skipUnless(HAS_GRAPH, "install the locked LangGraph dependencies to test finalization")
class FinalizationTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name) / "interviews.db"
        self.bank = Path(folder.name) / "bank.db"
        import_seed(self.bank, read_seed(ROOT / "data/seeds/knowledge_questions.json"))

    def plan(self, *, version=5, with_plan=True):
        material = MaterialInput()
        draft = start_draft(material, "form")
        with patch("app.db.business.MIGRATIONS", MIGRATIONS[:version]):
            self.session = confirm_material(self.db, draft.id, draft.id,
                                            Confirmation(material=material, mode="knowledge_only"), draft=draft)
        if with_plan:
            rebuild_plan(self.db, self.bank, self.session, 1, "plan", PlanChanges())

    def state(self):
        return read_state(self.db, self.session)

    def start(self, *, run=True):
        operation, _ = accept_start(self.db, self.session, self.state()["revision"], "start")
        if run:
            run_first_question(self.db, self.bank, self.session, operation)
        return operation

    def respond(self):
        current = self.state()
        turn = current["current"]
        return accept_answer(self.db, self.session, turn["turn_id"], turn["exchange_id"],
                             current["revision"], "answer", ANSWER)["operation_id"]

    def abort(self, *, reason="user_cancel", key="end", revision=None):
        return accept_abort(self.db, self.session, self.state()["revision"] if revision is None else revision, key, reason)

    @staticmethod
    def probe(context):
        return {"should_probe": True, "purpose": "deepen", "reason": "合成追问",
                "probe_question": PROBE, "evidence_refs": [{"exchange_id": context.exchanges[-1].id, "quote": ANSWER}]}

    @staticmethod
    def assessment(context, value=3):
        rubric = json.loads(context.rubric_snapshot)
        active = set(rubric["applicable_dimensions"])
        evidence = [{"exchange_id": context.exchanges[0].id, "quote": ANSWER}]
        return {"task_type": context.task_type, "rubric_version": rubric["rubric_version"], "dimensions": {
            name: {"applicable": name in active, "value": value if name in active else None,
                   "reason": "合成评分", "evidence": evidence if name in active else []}
            for name in DIMENSIONS[context.task_type]
        }}

    def test_zero_answers_finishes_without_scoring_and_repeated_abort_reuses_original_end(self):
        self.plan()
        self.start()
        revision = self.state()["revision"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            requests = [pool.submit(self.abort, reason="no_time", revision=revision) for _ in range(2)]
            receipts = [request.result() for request in requests]
        self.assertEqual(receipts[0][0], receipts[1][0])
        self.assertEqual(sorted(accepted for _, accepted in receipts), [False, True])
        operation = receipts[0][0]
        before = self.state()
        self.assertEqual((before["phase"], before["end_reason"], before["operation"]["status"]),
                         ("FINISHED", "no_time", "queued"))
        with self.assertRaises(SessionError) as busy:
            generate_report(self.db, self.session)
        self.assertEqual(busy.exception.code, "OPERATION_IN_PROGRESS")
        evaluator = Mock(side_effect=AssertionError("zero answers must not be evaluated"))
        run_finalize(self.db, self.session, operation, evaluator)
        evaluator.assert_not_called()
        summary = read_report(self.db, self.session)
        self.assertEqual((summary["kind"], summary["is_partial"], summary["counts"]["answered"]), ("summary", True, 0))
        self.assertEqual(summary["counts"]["displayed"], 1)
        self.assertEqual((self.abort(key="new-key", reason="too_hard"), self.state()["ended_at"]),
                         ((operation, False), before["ended_at"]))
        with self.assertRaises(SessionError) as conflict:
            self.abort(reason="too_hard", revision=revision)
        self.assertEqual(conflict.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_late_probe_cannot_publish_after_abort_and_first_answer_is_scored(self):
        self.plan()
        self.start()
        answer = self.respond()
        accepted = []

        def late_probe(context):
            accepted.extend(self.abort())
            return self.probe(context)

        with self.assertRaises(SessionError) as late:
            run_answer(self.db, self.session, answer, late_probe)
        self.assertEqual(late.exception.code, "ILLEGAL_ACTION")
        operation, created = accepted
        self.assertTrue(created)
        run_finalize(self.db, self.session, operation, self.assessment)
        report = read_report(self.db, self.session)
        item = report["items"][0]
        self.assertEqual((report["knowledge_score"], report["counts"]["scored"], item["interrupted"]), (75, 1, True))
        self.assertEqual([exchange["answer"] for exchange in item["exchanges"]], [ANSWER])
        self.assertEqual((item["probe_count"], report["items"][1]["outcome"]), (0, "unanswered"))
        with closing(connect(self.db, readonly=True)) as db:
            row = db.execute("SELECT status,error_code FROM operation WHERE id=?", (answer,)).fetchone()
            self.assertEqual(tuple(row), ("cancelled", "CANCELLED"))

    def test_open_followup_does_not_count_as_a_missing_answer_for_scoring(self):
        self.plan()
        self.start()
        run_answer(self.db, self.session, self.respond(), self.probe)
        operation, _ = self.abort()

        def only_submitted(context):
            self.assertEqual([row.answer for row in context.exchanges], [ANSWER])
            return self.assessment(context, value=4)

        run_finalize(self.db, self.session, operation, only_submitted)
        first = read_report(self.db, self.session)["items"][0]
        self.assertEqual((first["evaluation"]["item_score"], first["probe_count"]), (100, 1))
        self.assertEqual([row["answer"] for row in first["exchanges"]], [ANSWER, None])
        self.assertEqual(first["exchanges"][1]["question"], PROBE)

    def test_late_normal_score_cannot_overwrite_finalize_and_pending_evaluation_is_reused(self):
        self.plan()
        self.start()
        answer = self.respond()
        run_answer(self.db, self.session, answer, lambda _: {"should_probe": False, "reason": "收尾"})
        scoring, _ = accept_evaluation(self.db, self.session, answer, self.state()["revision"], "score")
        with closing(connect(self.db, readonly=True)) as db:
            evaluation_id, = db.execute("SELECT id FROM evaluation").fetchone()

        def late_score(context):
            operation, _ = self.abort()
            run_finalize(self.db, self.session, operation, lambda item: self.assessment(item, value=0))
            return self.assessment(context, value=4)

        with self.assertRaises(SessionError) as late:
            run_evaluation(self.db, self.session, scoring, late_score)
        self.assertEqual(late.exception.code, "ILLEGAL_ACTION")
        report = read_report(self.db, self.session)
        self.assertEqual((report["items"][0]["evaluation"]["id"], report["knowledge_score"]), (evaluation_id, 0))
        self.assertEqual(self.state()["phase"], "FINISHED")

    def test_abort_cancels_queued_advance_and_reuses_prior_score(self):
        self.plan()
        self.start()
        answer = self.respond()
        run_answer(self.db, self.session, answer, lambda _: {"should_probe": False, "reason": "收尾"})
        scoring, _ = accept_evaluation(self.db, self.session, answer, self.state()["revision"], "score")
        run_evaluation(self.db, self.session, scoring, self.assessment)
        advance, _ = accept_advance(self.db, self.session, self.state()["revision"], "next")
        operation, _ = self.abort()
        with self.assertRaises(SessionError):
            run_advance(self.db, self.bank, self.session, advance)
        evaluator = Mock(side_effect=AssertionError("must reuse already closed evaluation"))
        run_finalize(self.db, self.session, operation, evaluator)
        evaluator.assert_not_called()
        report = read_report(self.db, self.session)
        self.assertEqual((report["knowledge_score"], report["counts"]["displayed"], report["counts"]["scored"]), (75, 1, 1))

    def test_queued_start_cannot_publish_after_abort(self):
        self.plan()
        invalid = self.state()["revision"]
        start = self.start(run=False)
        with self.assertRaises(SessionError) as stale:
            self.abort(revision=invalid)
        self.assertEqual(stale.exception.code, "STALE_REVISION")
        operation, _ = self.abort()
        with self.assertRaises(SessionError):
            run_first_question(self.db, self.bank, self.session, start)
        run_finalize(self.db, self.session, operation)
        self.assertEqual(read_report(self.db, self.session)["counts"]["displayed"], 0)

    def test_cli_abort_preserves_saved_answer_when_scoring_is_unavailable(self):
        self.plan()
        self.start()
        self.respond()
        revision = self.state()["revision"]
        prefix = [sys.executable, "-X", "utf8", str(ROOT / "scripts/interview.py"), "--db", str(self.db)]

        def invoke(*arguments):
            process = subprocess.run([*prefix, *arguments], encoding="utf-8", capture_output=True, text=True, check=True)
            return json.loads(process.stdout)

        status = invoke("abort", self.session, "--expected-revision", str(revision), "--key", "cli-abort", "--reason", "too_hard")
        self.assertEqual((status["phase"], status["end_reason"], status["operation"]["evaluation_status"]),
                         ("FINISHED", "too_hard", "failed"))
        report = invoke("report", self.session, "--revision", "1")
        self.assertEqual((report["kind"], report["counts"]["answered"], report["counts"]["scored"]), ("summary", 1, 0))
        item = report["items"][0]
        self.assertEqual((item["evaluation"]["error_code"], item["evaluation"]["item_score"]), ("MODEL_UNAVAILABLE", None))
        self.assertEqual(item["exchanges"][0]["answer"], ANSWER)

    def test_report_failure_keeps_finished_session_and_resume_reuses_scoring(self):
        self.plan()
        self.start()
        self.respond()
        operation, _ = self.abort()
        with patch("app.workflow.interview.generate_report", side_effect=OSError("injected report error")):
            with self.assertRaises(OSError):
                run_finalize(self.db, self.session, operation, self.assessment)
        self.assertEqual((self.state()["phase"], self.state()["operation"]["status"]), ("FINISHED", "succeeded"))
        self.assertEqual(self.state()["resume"], {"kind": "finalize", "operation_id": operation, "reason": "report_pending"})
        self.assertEqual(read_report(self.db, self.session)["status"], "pending")
        evaluator = Mock(side_effect=AssertionError("resume must not re-score"))
        with patch("app.workflow.interview._run_interview", side_effect=AssertionError("only the report remains")):
            restored = resume_interview(self.db, self.bank, self.session, operation_id=operation, evaluator=evaluator)
        evaluator.assert_not_called()
        self.assertIsNone(restored["resume"])
        self.assertEqual(read_report(self.db, self.session)["knowledge_score"], 75)

    def test_unified_resume_finalizes_a_queued_end_and_reuses_its_report(self):
        self.plan()
        self.start()
        self.respond()
        operation, _ = self.abort()
        self.assertEqual(self.state()["resume"], {"kind": "finalize", "operation_id": operation, "reason": "queued"})
        saved = resume_interview(self.db, self.bank, self.session, operation_id=operation, evaluator=self.assessment)
        self.assertEqual((saved["phase"], saved["progress"]["scored"], saved["resume"]), ("FINISHED", 1, None))
        self.assertEqual(resume_interview(self.db, self.bank, self.session), saved)
        self.assertEqual(read_report(self.db, self.session)["revision_no"], 1)

    def test_material_only_v1_session_can_abort_and_finalize_after_upgrade(self):
        self.plan(version=1, with_plan=False)
        operation, accepted = self.abort(reason="no_time")
        self.assertTrue(accepted)
        self.assertEqual(self.state()["operation"]["operation_id"], operation)
        process = subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "scripts/interview.py"), "--db", str(self.db),
                                  "resume-finalize", self.session, operation],
                                 encoding="utf-8", text=True, capture_output=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        report = read_report(self.db, self.session)
        self.assertEqual((report["kind"], report["counts"]["planned"], report["end_reason"]), ("summary", 0, "no_time"))
        with closing(connect(self.db, readonly=True)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertEqual(db.execute("SELECT count(*) FROM operation_step WHERE operation_id=?", (operation,)).fetchone()[0], 1)
