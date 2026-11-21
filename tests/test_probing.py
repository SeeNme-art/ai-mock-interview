from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from app.db.business import SessionError
from app.db.sqlite import connect
from app.kb.seed import read_seed
from app.kb.store import import_seed
from app.schemas.materials import Confirmation, MaterialInput
from app.schemas.planning import PlanChanges
from app.services.answers import accept_answer
from app.services.first_question import accept_start, prepare_first_question, read_state, start_epoch
from app.services.materials import confirm_material, start_draft
from app.services.planning import rebuild_plan

try:
    from app.workflow.interview import run_answer
except ModuleNotFoundError as exc:
    if exc.name not in {"langgraph", "langsmith"}:
        raise
    HAS_GRAPH = False
else:
    HAS_GRAPH = True

ANSWER = "我会用固定流程，但需要动态选工具。"
FIRST = "你会怎么限制模型能选择的工具？"
SECOND = "如果它提出未授权的工具，你打算怎么处理？"
INTERNAL_REASON = "合成内部原因，仅供业务读取"


@unittest.skipUnless(HAS_GRAPH, "install the locked LangGraph dependencies to exercise answer processing")
class ProbingTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name) / "interviews.db"
        self.bank = Path(folder.name) / "questions.db"
        import_seed(self.bank, read_seed(ROOT / "data/seeds/knowledge_questions.json"))
        draft = start_draft(MaterialInput(), "form")
        self.session = confirm_material(self.db, draft.id, draft.id,
                                       Confirmation(material=MaterialInput(), mode="knowledge_only"), draft=draft)
        rebuild_plan(self.db, self.bank, self.session, 1, "plan", PlanChanges())
        operation, _ = accept_start(self.db, self.session, 2, "start")
        prepare_first_question(self.db, self.bank, self.session, operation, start_epoch(self.db, self.session, operation))
        self.original = self.state()["current"]
        self.operation = self.submit(ANSWER, "answer-main")

    def state(self):
        return read_state(self.db, self.session)

    def submit(self, text, key):
        state = self.state()
        current = state["current"]
        result = accept_answer(self.db, self.session, current["turn_id"], current["exchange_id"],
                               state["revision"], key, text)
        return result["operation_id"]

    def process(self, proposer=None, *, operation=None):
        return run_answer(self.db, self.session, operation or self.operation, proposer)

    def exchanges(self):
        with closing(connect(self.db, readonly=True)) as db:
            return [dict(row) for row in db.execute("SELECT * FROM exchange ORDER BY round_no")]

    @staticmethod
    def propose(context, question=FIRST):
        exchange = context.exchanges[-1]
        return {"should_probe": True, "purpose": "deepen", "reason": INTERNAL_REASON, "probe_question": question,
                "evidence_refs": [{"exchange_id": exchange.id, "quote": exchange.answer, "start": 99, "end": 120}]}

    def test_grounded_follow_up_is_saved_and_public_view_hides_decision_detail(self):
        def proposer(context):
            self.assertEqual(context.exchanges[0].answer, ANSWER)
            self.assertEqual(context.exchanges[0].question, self.original["question"])
            self.assertTrue(context.reference_points)
            return self.propose(context)

        with patch("socket.socket.connect", side_effect=AssertionError("offline")):
            graph = self.process(proposer)
        state = self.state()
        self.assertEqual(set(graph), {"session_id", "operation_id", "control_epoch", "turn_id", "exchange_id"})
        self.assertEqual((state["current"]["round_no"], state["current"]["question"]), (1, FIRST))
        self.assertNotIn("answer", state["current"])
        self.assertEqual(state["operation"]["decision"], "awaiting_answer")
        rows = self.exchanges()
        self.assertEqual(rows[0]["answer_text"], ANSWER)
        self.assertEqual(rows[1]["purpose"], "deepen")
        evidence = json.loads(rows[1]["reason_json"])["evidence_refs"][0]
        self.assertEqual((evidence["start"], evidence["end"]), (0, len(ANSWER)))
        for marker in (INTERNAL_REASON, ANSWER):
            self.assertNotIn(marker, json.dumps(state, ensure_ascii=False) + json.dumps(graph, ensure_ascii=False))

    def test_no_probe_and_invalid_output_have_different_results(self):
        self.process(lambda _: {"should_probe": False, "reason": INTERNAL_REASON})
        state = self.state()
        self.assertEqual((state["operation"]["decision"], state["operation"]["decision_reason"]), ("score_pending", "no_probe"))
        self.assertIsNone(state["operation"]["error_code"])
        self.assertEqual(state["current"]["answer"], ANSWER)
        with closing(connect(self.db, readonly=True)) as db:
            result = json.loads(db.execute("SELECT result_json FROM operation_step WHERE operation_id=?",
                                           (self.operation,)).fetchone()[0])
            self.assertEqual(result["detail"]["reason"], INTERNAL_REASON)
        self.assertNotIn(INTERNAL_REASON, json.dumps(state, ensure_ascii=False))

    def assert_rejected_proposal(self, proposer):
        self.process(proposer)
        state = self.state()
        self.assertEqual(state["operation"]["error_code"], "OUTPUT_INVALID")
        self.assertEqual((state["operation"]["decision"], state["operation"]["decision_reason"]),
                         ("score_pending", "invalid_proposal"))
        self.assertEqual(state["current"]["answer"], ANSWER)
        self.assertEqual(len(self.exchanges()), 1)

    def test_malformed_proposal_is_distinct_from_no_probe(self):
        self.assert_rejected_proposal(lambda _: {"should_probe": "true", "purpose": "unapproved", "reason": INTERNAL_REASON})

    def test_fabricated_or_cross_scope_evidence_is_rejected(self):
        def fabricated(context):
            proposal = self.propose(context)
            proposal["evidence_refs"] = [{"exchange_id": "wrong-exchange", "quote": ANSWER}]
            return proposal

        self.assert_rejected_proposal(fabricated)

    def test_duplicate_after_normalizing_case_whitespace_and_punctuation_is_rejected(self):
        self.assert_rejected_proposal(lambda ctx: self.propose(ctx, ctx.exchanges[0].question.replace("？", "  ?")))

    def test_direct_repetition_of_frozen_answer_point_is_rejected(self):
        self.assert_rejected_proposal(lambda ctx: self.propose(ctx, "参考答案是" + ctx.reference_points[0] + "，请继续解释。"))

    def test_provider_failure_records_system_reason_and_keeps_answer(self):
        proposer = Mock(side_effect=TimeoutError("synthetic failure"))
        self.process(proposer)
        state = self.state()
        self.assertEqual((state["operation"]["decision_reason"], state["operation"]["error_code"]),
                         ("provider_unavailable", "MODEL_UNAVAILABLE"))
        self.assertEqual(state["current"]["answer"], ANSWER)
        self.assertEqual(len(self.exchanges()), 1)

    def test_two_formal_follow_ups_stop_before_invoking_the_proposer(self):
        self.process(self.propose)
        after_first = self.submit("先登记经过管理员批准的工具。", "answer-first")

        def second(context):
            self.assertEqual([row.round_no for row in context.exchanges], [0, 1])
            self.assertEqual(context.exchanges[1].question, FIRST)
            return self.propose(context, SECOND)

        self.process(second, operation=after_first)
        at_limit = self.submit("拒绝执行并记录原因。", "answer-second")
        forbidden = Mock(side_effect=AssertionError("must not call at round limit"))
        self.process(forbidden, operation=at_limit)
        forbidden.assert_not_called()
        self.assertEqual([row["round_no"] for row in self.exchanges()], [0, 1, 2])
        summary = self.state()["operation"]
        self.assertEqual((summary["decision"], summary["decision_reason"]), ("score_pending", "round_limit"))
        with closing(connect(self.db, readonly=True)) as db:
            row = db.execute("SELECT version_context_json FROM operation_step WHERE operation_id=?", (at_limit,)).fetchone()
            self.assertEqual(json.loads(row[0]), {"proposer": None})

    def test_published_follow_up_replays_without_reloading_the_proposal(self):
        result = self.process(self.propose)
        forbidden = Mock(side_effect=AssertionError("already processed"))
        self.assertEqual(self.process(forbidden), result)
        forbidden.assert_not_called()
        self.assertEqual(len(self.exchanges()), 2)

    def test_concurrent_processing_publishes_only_one_follow_up(self):
        ready = Barrier(2, timeout=10)

        def synchronized(context):
            ready.wait()
            return self.propose(context)

        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(self.process, synchronized) for _ in range(2)]
            self.assertEqual(jobs[0].result(), jobs[1].result())
        self.assertEqual(len(self.exchanges()), 2)

    def test_injected_write_failure_rolls_back_question_and_supports_explicit_retry(self):
        with closing(connect(self.db)) as db, db:
            db.execute("""CREATE TRIGGER reject_probe_step BEFORE INSERT ON operation_step
                          WHEN NEW.step_key='decide_follow_up' BEGIN SELECT RAISE(ABORT,'test'); END;""")
        previous = self.state()
        with self.assertRaises(sqlite3.IntegrityError):
            self.process(self.propose)
        self.assertEqual(self.state(), previous)
        self.assertEqual(len(self.exchanges()), 1)
        with closing(connect(self.db)) as db, db:
            db.execute("DROP TRIGGER reject_probe_step")
        self.process(self.propose)
        self.assertEqual(len(self.exchanges()), 2)

    def test_phase_and_epoch_rechecked_after_proposer_returns(self):
        def end_during_proposal(context):
            with closing(connect(self.db)) as db, db:
                db.execute("UPDATE interview_session SET phase='FINISHED',control_epoch=control_epoch+1 WHERE id=?",
                           (self.session,))
            return self.propose(context)

        with self.assertRaisesRegex(SessionError, "操作已失效"):
            self.process(end_during_proposal)
        self.assertEqual(len(self.exchanges()), 1)
        self.assertEqual(self.state()["phase"], "FINISHED")

    def test_cli_requires_explicit_demo_below_cap_and_can_resume_in_a_fresh_process(self):
        cli = [sys.executable, "-X", "utf8", str(ROOT / "scripts/interview.py"), "--db", str(self.db)]
        command = [*cli, "process-answer", self.session, self.operation]
        missing = subprocess.run(command, capture_output=True, timeout=20)
        self.assertEqual(json.loads(missing.stderr)["code"], "CONFIGURATION_ERROR")
        self.assertEqual(self.state()["operation"]["status"], "queued")
        proposal = self.db.with_name("demo-proposal.json")
        proposal.write_text(json.dumps({"should_probe": True, "purpose": "clarify", "reason": INTERNAL_REASON,
                                       "probe_question": FIRST, "evidence_refs": [
                                           {"exchange_id": self.original["exchange_id"], "quote": "动态选工具"}]}), encoding="utf-8")
        result = json.loads(subprocess.check_output([*command, "--demo-proposal-file", str(proposal)], timeout=20))
        self.assertEqual(result["current"]["question"], FIRST)
        self.assertEqual(json.loads(subprocess.check_output(command, timeout=20)), result)
