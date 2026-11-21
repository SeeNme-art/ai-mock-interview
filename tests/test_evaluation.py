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
from app.db.business import MIGRATIONS, SessionError
from app.db.sqlite import connect
from app.kb.seed import SeedBank, read_seed
from app.kb.store import import_seed
from app.schemas.materials import Confirmation, MaterialInput
from app.schemas.planning import PlanChanges
from app.schemas.scoring import DIMENSIONS
from app.services.answers import accept_answer
from app.services.evaluation import EVALUATE, accept_evaluation
from app.services.first_question import accept_start, prepare_first_question, read_state, start_epoch
from app.services.materials import confirm_material, start_draft
from app.services.planning import rebuild_plan

try:
    from app.workflow.interview import run_answer, run_evaluation
except ModuleNotFoundError as exc:
    if exc.name not in {"langgraph", "langsmith"}:
        raise
    HAS_GRAPH = False
else:
    HAS_GRAPH = True

ANSWER = "工作流按预先规定的步骤执行，Agent 在权限内决定下一步。"
FOLLOWUP_ANSWER = "我会限制可用工具，拒绝调用未授权的接口。"
PRIVATE_REASON = "合成评分理由，仅供业务记录"


@unittest.skipUnless(HAS_GRAPH, "install the locked LangGraph dependencies to exercise evaluation")
class EvaluationTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name) / "interviews.db"
        self.bank = Path(folder.name) / "questions.db"
        self.seed = read_seed(ROOT / "data/seeds/knowledge_questions.json")
        import_seed(self.bank, self.seed)
        self.session = self.ready_session()
        self.answer_op = self.submit(ANSWER, "answer")

    def ready_session(self, *, version=4):
        with patch("app.db.business.MIGRATIONS", MIGRATIONS[:version]):
            draft = start_draft(MaterialInput(), "form")
            session = confirm_material(self.db, draft.id, draft.id, Confirmation(material=MaterialInput(), mode="knowledge_only"),
                                       draft=draft)
        rebuild_plan(self.db, self.bank, session, 1, "plan-" + session, PlanChanges())
        op, _ = accept_start(self.db, session, 2, "start-" + session)
        prepare_first_question(self.db, self.bank, session, op, start_epoch(self.db, session, op))
        return session

    def state(self):
        return read_state(self.db, self.session)

    def submit(self, answer, key):
        current = self.state()
        result = accept_answer(self.db, self.session, current["current"]["turn_id"], current["current"]["exchange_id"],
                               current["revision"], key, answer)
        return result["operation_id"]

    @staticmethod
    def probe(context):
        answer = context.exchanges[-1]
        return {"should_probe": True, "purpose": "deepen", "reason": "合成追问理由",
                "probe_question": "你会如何限制模型只使用经过批准的工具？",
                "evidence_refs": [{"exchange_id": answer.id, "quote": answer.answer}]}

    def stop_probing(self):
        run_answer(self.db, self.session, self.answer_op, lambda _: {"should_probe": False, "reason": "样例收尾"})

    def queue(self, *, key="grade", revision=None):
        current = self.state()["revision"] if revision is None else revision
        return accept_evaluation(self.db, self.session, self.answer_op, current, key)

    def evaluation(self):
        with closing(connect(self.db, readonly=True)) as db:
            row = db.execute("SELECT e.* FROM evaluation e JOIN turn t ON e.turn_id=t.id WHERE t.session_id=?",
                             (self.session,)).fetchone()
            return dict(row) if row else None

    @staticmethod
    def sample(context, values=(4, 3)):
        rubric = json.loads(context.rubric_snapshot)
        points = dict(zip(rubric["applicable_dimensions"], values))
        refs = [source["id"] for source in json.loads(context.source_snapshot)]
        result = {}
        for position, key in enumerate(DIMENSIONS[context.task_type]):
            exchange = context.exchanges[position % len(context.exchanges)]
            result[key] = {"applicable": key in points, "value": points.get(key), "reason": PRIVATE_REASON,
                           "evidence": [{"exchange_id": exchange.id, "quote": exchange.answer,
                                         "start": 500, "end": 600}] if key in points else [],
                           "source_refs": refs if key in points else []}
        return {"task_type": context.task_type, "rubric_version": rubric["rubric_version"], "dimensions": result}

    def test_scores_all_submitted_rounds_and_hides_the_score_until_report(self):
        run_answer(self.db, self.session, self.answer_op, self.probe)
        self.answer_op = self.submit(FOLLOWUP_ANSWER, "followup")
        self.stop_probing()
        operation, accepted = self.queue()
        self.assertTrue(accepted)
        self.assertEqual(self.state()["operation"]["evaluation_status"], "pending")

        def example(context):
            self.assertEqual([(row.round_no, row.answer) for row in context.exchanges], [(0, ANSWER), (1, FOLLOWUP_ANSWER)])
            return self.sample(context)

        with patch("socket.socket.connect", side_effect=AssertionError("offline")):
            graph = run_evaluation(self.db, self.session, operation, example)
        row = self.evaluation()
        self.assertEqual((row["status"], row["item_score"]), ("ready", 87.5))
        self.assertEqual(graph["evaluation_id"], row["id"])
        evidence = [item["evidence"][0] for item in json.loads(row["assessment_json"])["dimensions"].values() if item["applicable"]]
        self.assertEqual({ref["quote"] for ref in evidence}, {ANSWER, FOLLOWUP_ANSWER})
        self.assertTrue(all(ref["start"] == 0 and ref["end"] == len(ref["quote"]) for ref in evidence))
        self.assertEqual(set(graph), {"session_id", "operation_id", "control_epoch", "turn_id", "evaluation_id"})
        state = self.state()
        self.assertIsNone(state["current"])
        self.assertEqual(state["operation"]["evaluation_status"], "ready")
        visible = json.dumps(state, ensure_ascii=False) + json.dumps(graph, ensure_ascii=False)
        for marker in ("87.5", "item_score", "assessment_json", PRIVATE_REASON, ANSWER, FOLLOWUP_ANSWER):
            self.assertNotIn(marker, visible)
        with closing(connect(self.db, readonly=True)) as db:
            self.assertEqual(tuple(db.execute("SELECT status,outcome FROM plan_item WHERE seq=1").fetchone()), ("closed", "answered"))

    def test_refuses_before_probing_finishes_or_while_follow_up_is_open(self):
        with self.assertRaises(SessionError) as waiting:
            self.queue()
        self.assertEqual(waiting.exception.code, "OPERATION_IN_PROGRESS")
        run_answer(self.db, self.session, self.answer_op, self.probe)
        with self.assertRaisesRegex(SessionError, "尚未进入待评分"):
            self.queue()
        self.assertIsNone(self.evaluation())

    def test_frozen_rubric_and_source_are_used_after_bank_revision_changes(self):
        self.stop_probing()
        seed = self.seed.model_dump(mode="json")
        first = next(row for row in seed["questions"] if row["id"] == "agent-workflow-boundary")
        revision = first["revision"]
        points = first["answer_points"][:]
        first.update(revision=revision + 1, status="retired", answer_points=["后续修订的新审核要点"])
        import_seed(self.bank, SeedBank.model_validate(seed))
        op, _ = self.queue()

        def example(context):
            self.assertEqual(json.loads(context.question_snapshot)["revision"], revision)
            self.assertEqual(json.loads(context.rubric_snapshot)["answer_points"], points)
            self.assertEqual([row["id"] for row in json.loads(context.source_snapshot)], first["source_refs"])
            return self.sample(context)

        run_evaluation(self.db, self.session, op, example)
        self.assertEqual(json.loads(self.evaluation()["model_context_json"])["question_revision"], revision)

    def test_invalid_source_or_quote_saves_failed_without_a_fabricated_score(self):
        self.stop_probing()
        op, _ = self.queue()

        def invented(context):
            result = self.sample(context)
            result["dimensions"]["correctness"]["evidence"][0]["quote"] = "从未提交的答复"
            result["dimensions"]["depth"]["source_refs"] = ["不存在的来源"]
            return result

        run_evaluation(self.db, self.session, op, invented)
        row = self.evaluation()
        self.assertEqual((row["status"], row["item_score"], row["assessment_json"], row["error_code"]),
                         ("failed", None, None, "OUTPUT_INVALID"))
        self.assertEqual(self.state()["operation"]["evaluation_status"], "failed")
        with closing(connect(self.db, readonly=True)) as db:
            self.assertEqual(db.execute("SELECT answer_text FROM exchange").fetchone()[0], ANSWER)
            self.assertEqual(db.execute("SELECT outcome FROM plan_item WHERE seq=1").fetchone()[0], "answered")

    def test_real_zero_is_a_ready_result(self):
        self.stop_probing()
        op, _ = self.queue()
        run_evaluation(self.db, self.session, op, lambda ctx: self.sample(ctx, (0, 0)))
        self.assertEqual((self.evaluation()["status"], self.evaluation()["item_score"]), ("ready", 0.0))

    def test_synthetic_provider_timeout_is_failed_and_not_zero(self):
        self.stop_probing()
        op, _ = self.queue()
        run_evaluation(self.db, self.session, op, Mock(side_effect=TimeoutError("synthetic")))
        self.assertEqual((self.evaluation()["error_code"], self.evaluation()["item_score"]), ("MODEL_UNAVAILABLE", None))

    def test_same_key_and_post_commit_retry_reuse_the_evaluation(self):
        self.stop_probing()
        revision = self.state()["revision"]
        op, _ = self.queue(revision=revision)
        result = run_evaluation(self.db, self.session, op, self.sample)
        original = self.evaluation()
        forbidden = Mock(side_effect=AssertionError("published evaluations cannot change"))
        self.assertEqual(run_evaluation(self.db, self.session, op, forbidden), result)
        forbidden.assert_not_called()
        self.assertEqual(self.queue(revision=revision), (op, False))
        with self.assertRaises(SessionError) as replay:
            self.queue()
        self.assertEqual(replay.exception.code, "IDEMPOTENCY_CONFLICT")
        with self.assertRaises(SessionError):
            self.queue(key="overwrite")
        self.assertEqual(self.evaluation(), original)

    def test_concurrent_workers_commit_one_evaluation_and_one_step(self):
        self.stop_probing()
        revision = self.state()["revision"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = [pool.submit(self.queue, revision=revision) for _ in range(2)]
            replies = [a.result(), b.result()]
            self.assertEqual(replies[0][0], replies[1][0])
            self.assertEqual(sum(accepted for _, accepted in replies), 1)
            op = replies[0][0]
            gate = Barrier(2, timeout=10)

            def synchronized(ctx):
                gate.wait()
                return self.sample(ctx)

            jobs = [pool.submit(run_evaluation, self.db, self.session, op, synchronized) for _ in range(2)]
            self.assertEqual(jobs[0].result(), jobs[1].result())
        with closing(connect(self.db, readonly=True)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM evaluation").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM operation_step WHERE step_key=?", (EVALUATE,)).fetchone()[0], 1)

    def test_failed_write_rolls_back_score_plan_outcome_and_progress(self):
        self.stop_probing()
        op, _ = self.queue()
        initial = self.state()
        with closing(connect(self.db)) as db, db:
            db.execute("""CREATE TRIGGER reject_eval_step BEFORE INSERT ON operation_step WHEN NEW.step_key='evaluate_answer'
                          BEGIN SELECT RAISE(ABORT,'test'); END;""")
        with self.assertRaises(sqlite3.IntegrityError):
            run_evaluation(self.db, self.session, op, self.sample)
        self.assertEqual(self.state(), initial)
        self.assertEqual(self.evaluation()["status"], "pending")
        with closing(connect(self.db)) as db, db:
            self.assertIsNone(db.execute("SELECT outcome FROM plan_item WHERE seq=1").fetchone()[0])
            db.execute("DROP TRIGGER reject_eval_step")
        run_evaluation(self.db, self.session, op, self.sample)
        self.assertEqual(self.evaluation()["status"], "ready")

    def test_scope_and_control_epoch_reject_a_late_score(self):
        self.stop_probing()
        op, _ = self.queue()
        other = self.ready_session()
        with self.assertRaisesRegex(SessionError, "找不到该会话的操作"):
            run_evaluation(self.db, other, op, self.sample)

        def terminate(context):
            with closing(connect(self.db)) as db, db:
                db.execute("UPDATE interview_session SET phase='FINISHED',control_epoch=control_epoch+1 WHERE id=?", (self.session,))
            return self.sample(context)

        with self.assertRaisesRegex(SessionError, "评分操作已失效"):
            run_evaluation(self.db, self.session, op, terminate)
        self.assertEqual(self.evaluation()["status"], "pending")
        self.assertEqual(self.state()["phase"], "FINISHED")

    def test_upgrade_preserves_a_score_pending_v3_database_in_the_backup(self):
        self.db = self.db.with_name("legacy.db")
        self.session = self.ready_session(version=3)
        self.answer_op = self.submit(ANSWER, "legacy-answer")
        self.stop_probing()
        previous = self.state()
        op, _ = self.queue()
        with closing(connect(self.db, readonly=True)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
            recorded = db.execute("SELECT file_name FROM migration_backup WHERE schema_version=3").fetchone()[0]
        self.assertEqual(read_state(self.db.with_name(recorded), self.session), previous)
        run_evaluation(self.db, self.session, op, self.sample)
        self.assertEqual(self.evaluation()["status"], "ready")

    def test_cli_can_resume_pending_and_read_saved_evaluation_in_another_process(self):
        self.stop_probing()
        state = self.state()
        cli = [sys.executable, "-X", "utf8", str(ROOT / "scripts/interview.py"), "--db", str(self.db)]
        file = self.db.with_name("demo-evaluation.json")
        score = [*cli, "score-answer", self.session, self.answer_op, "--expected-revision", str(state["revision"]),
                 "--key", "cli-score", "--demo-assessment-file", str(file)]
        missing = subprocess.run(score, capture_output=True, timeout=20)
        self.assertEqual(json.loads(missing.stderr)["code"], "CONFIGURATION_ERROR")
        pending = self.state()["operation"]
        self.assertEqual((pending["status"], pending["evaluation_status"]), ("queued", "pending"))
        values = {"correctness": 4, "depth": 3}
        dims = {key: {"applicable": key in values, "value": values.get(key), "reason": PRIVATE_REASON,
                      "evidence": [{"exchange_id": state["current"]["exchange_id"], "quote": ANSWER}] if key in values else []}
                for key in DIMENSIONS["knowledge"]}
        file.write_text(json.dumps({"task_type": "knowledge", "rubric_version": "knowledge-v1", "dimensions": dims}), encoding="utf-8")
        ready = subprocess.check_output([*cli, "resume-score", self.session, pending["operation_id"],
                                         "--demo-assessment-file", str(file)], timeout=20)
        file.unlink()
        self.assertEqual(json.loads(ready), json.loads(subprocess.check_output(score, timeout=20)))
        self.assertEqual(self.evaluation()["item_score"], 87.5)
