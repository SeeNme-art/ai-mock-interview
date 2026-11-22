from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
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
from app.services.advancement import SKIP, accept_advance, accept_skip
from app.services.answers import accept_answer
from app.services.evaluation import accept_evaluation
from app.services.first_question import accept_start, prepare_first_question, read_state, start_epoch
from app.services.materials import confirm_material, start_draft
from app.services.planning import rebuild_plan
from app.services.reports import generate_report

try:
    from app.workflow.interview import resume_interview, run_advance, run_answer, run_evaluation
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

    def respond(self):
        state = self.state()
        current = state["current"]
        receipt = accept_answer(self.db, self.session, current["turn_id"], current["exchange_id"],
                                state["revision"], f"answer-{current['seq']}", ANSWER)
        return receipt["operation_id"]

    def queue_score(self):
        current = self.state()["current"]
        answer = self.respond()
        run_answer(self.db, self.session, answer, lambda _: {"should_probe": False, "reason": "合成收尾"})
        score, _ = accept_evaluation(self.db, self.session, answer, self.state()["revision"],
                                     f"score-{current['seq']}")
        return score

    def close_current(self, *, valid=True):
        score = self.queue_score()
        run_evaluation(self.db, self.session, score, self.assessment if valid else lambda _: {})

    def queue(self, key="advance-1", revision=None):
        return accept_advance(self.db, self.session, self.state()["revision"] if revision is None else revision, key)

    def advance(self, key):
        operation, accepted = self.queue(key)
        self.assertTrue(accepted)
        return run_advance(self.db, self.bank, self.session, operation)

    def skip_request(self, key):
        state = self.state()
        return self.db, self.session, state["current"]["turn_id"], state["revision"], key

    def skip(self, key):
        operation, accepted = accept_skip(*self.skip_request(key))
        self.assertTrue(accepted)
        return run_advance(self.db, self.bank, self.session, operation, kind=SKIP)

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
        with patch("app.services.question_delivery._project_question", return_value=None):
            self.advance("mixed")
        state = self.state()
        self.assertEqual(state["current"]["seq"], 3)
        self.assertEqual(state["operation"]["unavailable"], [{"seq": 2, "reason_code": "QUESTION_UNAVAILABLE"}])
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
        with patch("app.services.question_delivery._project_question", return_value=None):
            with self.assertRaises(sqlite3.IntegrityError):
                run_advance(self.db, self.bank, self.session, op)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.slots()[1]["status"], "pending")
        with closing(connect(self.db)) as db, db:
            db.execute("DROP TRIGGER reject_next")
        with patch("app.services.question_delivery._project_question", return_value=None):
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

    def test_skip_moves_to_next_frozen_item_and_replay_cannot_skip_it_again(self):
        self.plan()
        with self.assertRaises(SessionError) as missing:
            accept_skip(self.db, self.session, "no-turn", self.state()["revision"], "not-started")
        self.assertEqual(missing.exception.code, "ILLEGAL_ACTION")
        plan_ids = [slot["id"] for slot in self.slots()]
        self.start()
        request = self.skip_request("skip-first")
        turn_id = request[2]
        with self.assertRaises(SessionError) as stale:
            accept_skip(self.db, self.session, turn_id, request[3] - 1, "stale")
        self.assertEqual(stale.exception.code, "STALE_REVISION")
        operation, _ = accept_skip(*request)
        self.assertIsNone(self.state()["current"])
        self.assertEqual(self.state()["operation"]["status"], "queued")
        result = run_advance(self.db, self.bank, self.session, operation, kind=SKIP)
        self.assertEqual(accept_skip(*request), (operation, False))
        with patch("app.services.question_delivery.list_questions", side_effect=AssertionError("already published")):
            self.assertEqual(run_advance(self.db, self.bank, self.session, operation, kind=SKIP), result)
        state = self.state()
        self.assertEqual((state["current"]["seq"], state["operation"]["skipped_seq"]), (2, 1))
        self.assertEqual(state["progress"], {"planned": 5, "closed": 1, "answered": 0, "scored": 0, "skipped": 1, "failed": 0})
        self.assertEqual([slot["id"] for slot in self.slots()], plan_ids)
        with self.assertRaises(SessionError) as conflict:
            accept_skip(*self.skip_request("skip-first"))
        self.assertEqual(conflict.exception.code, "IDEMPOTENCY_CONFLICT")
        with self.assertRaises(SessionError) as closed:
            accept_skip(self.db, self.session, turn_id, state["revision"], "already-closed")
        self.assertEqual(closed.exception.code, "ILLEGAL_ACTION")

    def test_skip_rejects_late_probe_and_retains_submitted_answer(self):
        self.plan()
        self.start()
        answer_id = self.respond()
        probe = "你会怎样验证这组约束？"

        def late_probe(context):
            self.skip("skip-during-probe")
            return {"should_probe": True, "purpose": "deepen", "reason": "合成追问", "probe_question": probe,
                    "evidence_refs": [{"exchange_id": context.exchanges[0].id, "quote": ANSWER}]}

        with self.assertRaises(SessionError) as late:
            run_answer(self.db, self.session, answer_id, late_probe)
        self.assertEqual(late.exception.code, "ILLEGAL_ACTION")
        self.assertEqual(self.state()["current"]["seq"], 2)
        self.assertEqual(self.state()["progress"]["answered"], 1)
        with closing(connect(self.db, readonly=True)) as db:
            self.assertEqual(tuple(db.execute("SELECT status,error_code FROM operation WHERE id=?", (answer_id,)).fetchone()),
                             ("cancelled", "CANCELLED"))
            self.assertEqual(db.execute("SELECT count(*) FROM exchange WHERE question_text=?", (probe,)).fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM exchange WHERE answer_text=?", (ANSWER,)).fetchone()[0], 1)

    def test_late_score_cannot_change_skip_and_report_uses_only_remaining_valid_scores(self):
        self.plan()
        self.start()
        score = self.queue_score()

        def late_assessment(context):
            self.skip("skip-during-score")
            return self.assessment(context)

        with self.assertRaises(SessionError) as late:
            run_evaluation(self.db, self.session, score, late_assessment)
        self.assertEqual(late.exception.code, "ILLEGAL_ACTION")
        with closing(connect(self.db, readonly=True)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM evaluation").fetchone()[0], 0)
        for seq in range(2, 6):
            self.assertEqual(self.state()["current"]["seq"], seq)
            self.close_current()
            if seq < 5:
                self.advance(f"after-{seq}")
        report = generate_report(self.db, self.session)
        self.assertEqual((report["end_reason"], report["knowledge_score"], report["is_partial"]), ("completed", 75, True))
        self.assertEqual((report["counts"]["answered"], report["counts"]["scored"], report["counts"]["skipped"]), (5, 4, 1))
        skipped = report["items"][0]
        self.assertEqual((skipped["outcome"], skipped["evaluation"], skipped["exchanges"][0]["answer"]),
                         ("skipped", None, ANSWER))

    def test_skip_last_slot_finishes_and_reports_previous_score(self):
        self.plan()
        self.start()
        for seq in range(1, 5):
            self.close_current()
            self.advance(f"completed-{seq}")
        self.skip("skip-last")
        last = self.state()
        self.assertEqual((last["phase"], last["end_reason"], last["current"]), ("FINISHED", "completed", None))
        self.assertEqual((last["progress"]["skipped"], last["progress"]["scored"]), (1, 4))
        self.assertEqual((last["operation"]["outcome"], last["operation"]["skipped_seq"]), ("finished", 5))
        self.assertEqual(generate_report(self.db, self.session)["knowledge_score"], 75)

    def test_skip_handles_unavailable_following_slots_and_report_preserves_the_reasons(self):
        self.plan(mixed=True)
        self.start()
        with patch("app.services.question_delivery._project_question", return_value=None):
            self.skip("skip-before-project")
            self.assertEqual(self.state()["current"]["seq"], 3)
            self.assertEqual(self.state()["operation"]["unavailable"], [{"seq": 2, "reason_code": "QUESTION_UNAVAILABLE"}])
            self.skip("skip-second-knowledge")
        self.skip("skip-final-knowledge")
        report = generate_report(self.db, self.session)
        self.assertEqual((report["kind"], report["counts"]["skipped"], report["counts"]["system_failed"]), ("summary", 3, 2))
        self.assertEqual([item["generation_error_code"] for item in report["items"] if item["outcome"] == "generation_failed"],
                         ["QUESTION_UNAVAILABLE", "QUESTION_UNAVAILABLE"])

    def test_mixed_project_questions_use_confirmed_fields_and_separate_scoring(self):
        self.plan(mixed=True)
        self.start()
        for seq in range(1, 6):
            state = self.state()
            self.assertEqual(state["current"]["seq"], seq)
            self.close_current()
            if seq < 5:
                self.advance(f"after-mixed-{seq}")
        report = generate_report(self.db, self.session)
        self.assertEqual(report["category_counts"], {"knowledge": 3, "project": 2})
        self.assertEqual((report["knowledge_score"], report["project_score"]), (75, 75))
        facts = [item["question_snapshot"] for item in report["items"] if item["task_type"] == "project"]
        self.assertEqual([(item["project_ref"], item["project_field"], item["project_quote"]) for item in facts],
                         [("p_1", "action", "实现检索分块"), ("p_1", "result", "以固定查询对比结果")])

    def test_failed_skip_acceptance_rolls_back_and_preparation_failure_can_resume_in_a_new_process(self):
        self.plan()
        self.start()
        score = self.queue_score()
        state = self.state()
        request = self.skip_request("rollback")
        with closing(connect(self.db)) as db, db:
            db.execute("""CREATE TRIGGER reject_skip BEFORE INSERT ON operation WHEN NEW.kind='skip'
                          BEGIN SELECT RAISE(ABORT,'test'); END;""")
        with self.assertRaises(sqlite3.IntegrityError):
            accept_skip(*request)
        self.assertEqual(self.state(), state)
        with closing(connect(self.db)) as db, db:
            self.assertEqual(db.execute("SELECT status FROM operation WHERE id=?", (score,)).fetchone()[0], "queued")
            self.assertEqual(db.execute("SELECT status FROM evaluation").fetchone()[0], "pending")
            db.execute("DROP TRIGGER reject_skip")
        operation, accepted = accept_skip(*request)
        self.assertTrue(accepted)
        with patch("app.services.advancement.publish_plan_item", side_effect=sqlite3.OperationalError("test")):
            with self.assertRaises(sqlite3.OperationalError):
                run_advance(self.db, self.bank, self.session, operation, kind=SKIP)
        self.assertEqual((self.slots()[0]["outcome"], self.state()["operation"]["status"]), ("skipped", "queued"))
        cli = [sys.executable, "-X", "utf8", str(ROOT / "scripts/interview.py"), "--db", str(self.db), "--bank", str(self.bank)]
        resumed = json.loads(subprocess.check_output([*cli, "resume-skip", self.session, operation], timeout=20))
        self.assertEqual((resumed["current"]["seq"], resumed["progress"]["skipped"]), (2, 1))

    def test_concurrent_clis_skip_only_one_question(self):
        self.plan()
        self.start()
        _, session, turn, revision, key = self.skip_request("cli-skip")
        cli = [sys.executable, "-X", "utf8", str(ROOT / "scripts/interview.py"), "--db", str(self.db), "--bank", str(self.bank),
               "skip", session, turn, "--expected-revision", str(revision), "--key", key]
        with ThreadPoolExecutor(max_workers=2) as pool:
            requests = [pool.submit(subprocess.check_output, cli, timeout=20) for _ in range(2)]
            operations = [json.loads(request.result())["operation"]["operation_id"] for request in requests]
        self.assertEqual(operations[0], operations[1])
        self.assertEqual((self.state()["current"]["seq"], self.state()["progress"]["skipped"]), (2, 1))
        with closing(connect(self.db, readonly=True)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM operation WHERE kind='skip'").fetchone()[0], 1)

    def test_cold_cli_resume_routes_each_saved_operation_to_its_existing_step(self):
        self.plan()
        cli = [sys.executable, "-X", "utf8", str(ROOT / "scripts/interview.py"), "--db", str(self.db), "--bank", str(self.bank)]

        def call(*args):
            return json.loads(subprocess.check_output([*cli, *args], timeout=20))

        def assert_target(kind, operation_id):
            state = call("state", self.session)
            self.assertEqual(state["resume"], {"kind": kind, "operation_id": operation_id, "reason": "queued"})
            return state

        start, _ = accept_start(self.db, self.session, self.state()["revision"], "resume-start")
        assert_target("start", start)
        first = call("resume", self.session, "--operation-id", start)
        self.assertEqual(first["current"]["seq"], 1)
        self.assertEqual(call("resume", self.session), first)

        answer = self.respond()
        queued = assert_target("answer", answer)
        self.assertEqual(queued["current"]["answer"], ANSWER)
        failure = subprocess.run([*cli, "resume", self.session], text=True, encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(failure.returncode, 1)
        self.assertEqual(json.loads(failure.stderr)["code"], "CONFIGURATION_ERROR")
        self.assertEqual(self.state(), queued)

        proposal = self.db.with_name("proposal.json")
        proposal.write_text(json.dumps({"should_probe": False, "reason": "合成样例"}), encoding="utf-8")
        processed = call("resume", self.session, "--demo-proposal-file", str(proposal))
        self.assertEqual(processed["operation"]["decision"], "score_pending")
        self.assertEqual(call("resume", self.session), processed)

        score, _ = accept_evaluation(self.db, self.session, answer, processed["revision"], "resume-score")
        assert_target("evaluate_answer", score)
        with closing(connect(self.db, readonly=True)) as db:
            rubric, task = db.execute("SELECT rubric_snapshot_json,task_type FROM turn").fetchone()
        context = SimpleNamespace(rubric_snapshot=rubric, task_type=task,
                                  exchanges=[SimpleNamespace(id=first["current"]["exchange_id"])])
        assessment = self.db.with_name("assessment.json")
        assessment.write_text(json.dumps(self.assessment(context)), encoding="utf-8")
        scored = call("resume", self.session, "--demo-assessment-file", str(assessment))
        self.assertEqual((scored["progress"]["answered"], scored["progress"]["scored"], scored["resume"]), (1, 1, None))
        self.assertEqual(call("resume", self.session, "--operation-id", score), scored)

        advance, _ = self.queue("resume-advance")
        assert_target("advance", advance)
        self.assertEqual(call("resume", self.session)["current"]["seq"], 2)
        skip, _ = accept_skip(*self.skip_request("resume-skip"))
        assert_target("skip", skip)
        after = call("resume", self.session)
        self.assertEqual((after["current"]["seq"], after["progress"]["skipped"], after["progress"]["scored"]), (3, 1, 1))
        with closing(connect(self.db, readonly=True)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM evaluation").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM exchange WHERE answer_text=?", (ANSWER,)).fetchone()[0], 1)

    def test_resume_refuses_stale_revision_other_operation_and_old_control_epoch(self):
        self.plan()
        original = self.state()
        self.assertEqual(resume_interview(self.db, self.bank, self.session), original)
        start, _ = accept_start(self.db, self.session, original["revision"], "resume-start")
        for options, code in (({"expected_revision": original["revision"]}, "STALE_REVISION"),
                              ({"operation_id": "another-operation"}, "ILLEGAL_ACTION")):
            with self.assertRaises(SessionError) as refused:
                resume_interview(self.db, self.bank, self.session, **options)
            self.assertEqual(refused.exception.code, code)
        with self.assertRaises(SessionError) as inaccessible:
            resume_interview(self.db, self.bank, "another-session", operation_id=start)
        self.assertEqual(inaccessible.exception.code, "RESOURCE_NOT_FOUND")
        with closing(connect(self.db)) as db, db:
            db.execute("UPDATE interview_session SET control_epoch=control_epoch+1 WHERE id=?", (self.session,))
        state = resume_interview(self.db, self.bank, self.session, operation_id=start)
        self.assertIsNone(state["resume"])
        self.assertIsNone(state["current"])
        self.assertEqual(state["operation"]["status"], "queued")

    def test_concurrent_resumes_publish_one_question_and_create_no_extra_operation(self):
        self.plan()
        start, _ = accept_start(self.db, self.session, self.state()["revision"], "resume-start")
        with ThreadPoolExecutor(max_workers=2) as pool:
            work = [pool.submit(resume_interview, self.db, self.bank, self.session, operation_id=start) for _ in range(2)]
            views = [job.result() for job in work]
        self.assertEqual(views[0], views[1])
        self.assertEqual((views[0]["current"]["seq"], views[0]["resume"]), (1, None))
        with closing(connect(self.db, readonly=True)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM turn").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM operation WHERE kind='start'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM operation_step WHERE operation_id=?", (start,)).fetchone()[0], 1)

    def test_resume_after_normal_end_only_reads_saved_state(self):
        self.plan()
        self.start()
        self.close_current()
        self.retire([q.id for q in self.seed.questions])
        self.advance("all-missing")
        state = self.state()
        self.assertEqual((state["phase"], state["resume"]), ("FINISHED", None))
        self.assertEqual(resume_interview(self.db, self.bank, self.session), state)
        with closing(connect(self.db, readonly=True)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM report_revision").fetchone()[0], 0)
