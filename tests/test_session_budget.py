from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event
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
from app.services.answers import accept_answer
from app.services.budget import PriceSnapshot, Usage, configure_budget, invoke_budgeted, read_budget, settle_call
from app.services.finalization import accept_abort
from app.services.first_question import accept_start, prepare_first_question, read_state, start_epoch
from app.services.materials import confirm_material, start_draft
from app.services.planning import rebuild_plan

PRICE = PriceSnapshot("synthetic-model", "fixture-v1", 10, 20, 2)
SAMPLE_USAGE = Usage(60, 20, 30)
BODY = "仅供测试的回答正文，不能进入预算账本"


class SessionBudgetTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name) / "interviews.db"
        self.bank = Path(folder.name) / "bank.db"
        import_seed(self.bank, read_seed(ROOT / "data/seeds/knowledge_questions.json"))
        self.session, self.operation = self.ready_answer()

    def ready_answer(self, *, version=6, db=None):
        path = self.db if db is None else db
        material = MaterialInput()
        draft = start_draft(material, "form")
        with patch("app.db.business.MIGRATIONS", MIGRATIONS[:version]):
            session = confirm_material(path, draft.id, draft.id,
                                       Confirmation(material=material, mode="knowledge_only"), draft=draft)
        rebuild_plan(path, self.bank, session, 1, "plan-" + session, PlanChanges())
        operation, _ = accept_start(path, session, 2, "start-" + session)
        prepare_first_question(path, self.bank, session, operation, start_epoch(path, session, operation))
        state = read_state(path, session)
        current = state["current"]
        receipt = accept_answer(path, session, current["turn_id"], current["exchange_id"],
                                state["revision"], "answer-" + session, BODY)
        return session, receipt["operation_id"]

    def configure(self, limit=10_000, reserve=0):
        return configure_budget(self.db, self.session, hard_limit_nanos=limit, closing_reserve_nanos=reserve)

    def invoke(self, send, *, step="decide_follow_up", role="probe", number=1, operation=None):
        return invoke_budgeted(self.db, self.session, operation or self.operation, step, role=role, attempt_no=number,
                               price=PRICE, estimated_input_tokens=100, max_output_tokens=50, send_once=send)

    def ledger(self):
        return read_budget(self.db, self.session)

    def test_missing_budget_and_different_configuration_do_not_send_or_reset_the_account(self):
        callback = Mock()
        with self.assertRaises(SessionError) as missing:
            self.invoke(callback)
        self.assertEqual(missing.exception.code, "BUDGET_NOT_CONFIGURED")
        self.assertFalse(self.ledger()["configured"])
        callback.assert_not_called()
        original = self.configure()
        self.assertEqual(self.configure(), original)
        with self.assertRaises(SessionError) as change:
            self.configure(20_000)
        self.assertEqual(change.exception.code, "BUDGET_ALREADY_CONFIGURED")
        self.assertEqual(self.ledger(), original)

    def test_reservation_is_committed_before_sending_and_cached_usage_releases_unused_funds(self):
        self.configure()

        def send(output_limit):
            self.assertEqual(output_limit, 50)
            during = self.ledger()
            self.assertEqual((during["spent_nanos"], during["reserved_nanos"]), (0, 2000))
            self.assertEqual(during["attempts"][0]["status"], "reserved")
            return "合成模型正文不进账本", SAMPLE_USAGE

        result = self.invoke(send)
        self.assertEqual(result, "合成模型正文不进账本")
        ledger = self.ledger()
        attempt, = ledger["attempts"]
        self.assertEqual((ledger["spent_nanos"], ledger["reserved_nanos"], ledger["available_nanos"]), (760, 0, 9240))
        self.assertEqual((attempt["status"], attempt["actual_cached_input_tokens"], attempt["actual_nanos"]),
                         ("settled", 30, 760))
        self.assertEqual(attempt["pricing_snapshot"]["version"], "fixture-v1")
        serialized = json.dumps(ledger, ensure_ascii=False)
        self.assertNotIn(result, serialized)
        self.assertNotIn(BODY, serialized)

    def test_probe_cannot_consume_closing_reserve_but_evaluation_can(self):
        self.configure(limit=2400, reserve=1000)
        send = Mock(return_value=("fixture", SAMPLE_USAGE))
        with self.assertRaises(SessionError) as cap:
            self.invoke(send)
        self.assertEqual(cap.exception.code, "BUDGET_EXHAUSTED")
        send.assert_not_called()
        self.assertEqual(self.ledger()["attempts"], [])
        self.assertEqual(self.invoke(send, step="evaluate", role="evaluate"), "fixture")
        send.assert_called_once_with(50)
        self.assertEqual(self.ledger()["spent_nanos"], 760)

    def test_timeout_holds_funds_and_late_usage_is_reconciled_once_even_after_abort(self):
        self.configure(limit=4000)
        with self.assertRaises(SessionError) as timeout:
            self.invoke(Mock(side_effect=TimeoutError("合成供应商错误正文")))
        self.assertEqual(timeout.exception.code, "MODEL_UNAVAILABLE")
        self.assertNotIn("合成供应商错误正文", str(timeout.exception))
        held = self.ledger()
        attempt, = held["attempts"]
        self.assertEqual((held["spent_nanos"], held["reserved_nanos"], attempt["status"]), (0, 2000, "usage_unknown"))
        self.assertIsNone(attempt["actual_nanos"])
        settle_call(self.db, self.session, attempt["id"], None)
        self.assertEqual(self.ledger(), held)

        next_call = Mock(return_value=("fixture", Usage(0, 0)))
        with self.assertRaises(SessionError) as replay:
            self.invoke(next_call)
        self.assertEqual(replay.exception.code, "CALL_ALREADY_RECORDED")
        next_call.assert_not_called()
        self.invoke(next_call, number=2)
        with self.assertRaises(SessionError) as over_attempts:
            self.invoke(next_call, number=3)
        self.assertEqual(over_attempts.exception.code, "INVALID_INPUT")
        next_call.assert_called_once_with(50)
        self.assertEqual(self.ledger()["attempts"][-1]["actual_nanos"], 0)

        state = read_state(self.db, self.session)
        accept_abort(self.db, self.session, state["revision"], "stop", "no_time")
        with self.assertRaises(SessionError) as closed:
            self.invoke(next_call, step="obsolete")
        self.assertEqual(closed.exception.code, "ILLEGAL_ACTION")
        settle_call(self.db, self.session, attempt["id"], SAMPLE_USAGE)
        snapshot = self.ledger()
        settle_call(self.db, self.session, attempt["id"], SAMPLE_USAGE)
        self.assertEqual((snapshot["spent_nanos"], snapshot["reserved_nanos"]), (760, 0))
        self.assertEqual(self.ledger(), snapshot)
        with self.assertRaises(SessionError) as conflict:
            settle_call(self.db, self.session, attempt["id"], Usage(1, 1))
        self.assertEqual(conflict.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_simultaneous_calls_cannot_double_reserve_or_overdraw_the_same_account(self):
        self.configure(limit=3000)
        entered, release = Event(), Event()

        def wait_for_other(_):
            entered.set()
            self.assertTrue(release.wait(10))
            return "first", SAMPLE_USAGE

        blocked = Mock()
        with ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(self.invoke, wait_for_other)
            try:
                self.assertTrue(entered.wait(10))
                for kwargs, code in (({"step": "another_step"}, "BUDGET_EXHAUSTED"),
                                     ({}, "CALL_ALREADY_RECORDED"), ({"number": 2}, "ILLEGAL_ACTION")):
                    with self.assertRaises(SessionError) as rejected:
                        pool.submit(self.invoke, blocked, **kwargs).result(timeout=10)
                    self.assertEqual(rejected.exception.code, code)
                blocked.assert_not_called()
                self.assertEqual(self.ledger()["reserved_nanos"], 2000)
            finally:
                release.set()
            self.assertEqual(running.result(timeout=10), "first")
        self.assertEqual(len(self.ledger()["attempts"]), 1)

    def test_provider_overage_is_recorded_and_blocks_later_attempts(self):
        self.configure(limit=2400)
        send = Mock(return_value=("fixture", Usage(300, 20)))
        self.invoke(send)
        ledger = self.ledger()
        self.assertEqual((ledger["spent_nanos"], ledger["over_limit_nanos"], ledger["available_nanos"]), (3400, 1000, 0))
        with self.assertRaises(SessionError) as rejected:
            self.invoke(send, step="next")
        self.assertEqual(rejected.exception.code, "BUDGET_EXHAUSTED")
        send.assert_called_once()

    def test_interruption_and_sql_write_failure_leave_a_recoverable_ledger(self):
        self.configure()
        send = Mock(side_effect=KeyboardInterrupt)
        with closing(connect(self.db)) as db, db:
            db.execute("""CREATE TRIGGER block_budget BEFORE UPDATE ON budget_account
                          BEGIN SELECT RAISE(ABORT,'fixture'); END;""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.invoke(send)
        send.assert_not_called()
        self.assertEqual((self.ledger()["reserved_nanos"], self.ledger()["attempts"]), (0, []))
        with closing(connect(self.db)) as db, db:
            db.execute("DROP TRIGGER block_budget")
        with self.assertRaises(KeyboardInterrupt):
            self.invoke(send)
        attempt, = self.ledger()["attempts"]
        self.assertEqual((attempt["status"], self.ledger()["reserved_nanos"]), ("reserved", 2000))

        with closing(connect(self.db)) as db, db:
            db.execute("""CREATE TRIGGER block_budget BEFORE UPDATE ON budget_account
                          BEGIN SELECT RAISE(ABORT,'fixture'); END;""")
        with self.assertRaises(sqlite3.IntegrityError):
            settle_call(self.db, self.session, attempt["id"], SAMPLE_USAGE)
        self.assertEqual(self.ledger()["attempts"], [attempt])
        with closing(connect(self.db)) as db, db:
            db.execute("DROP TRIGGER block_budget")
        settle_call(self.db, self.session, attempt["id"], SAMPLE_USAGE)
        self.assertEqual(self.ledger()["spent_nanos"], 760)

    def test_budget_and_attempts_are_isolated_by_session(self):
        self.configure()
        other, other_operation = self.ready_answer()
        configure_budget(self.db, other, hard_limit_nanos=1000)
        send = Mock(return_value=("fixture", SAMPLE_USAGE))
        with self.assertRaises(SessionError) as wrong:
            self.invoke(send, operation=other_operation)
        self.assertEqual(wrong.exception.code, "ILLEGAL_ACTION")
        send.assert_not_called()
        self.invoke(send)
        call_id = self.ledger()["attempts"][0]["id"]
        with self.assertRaises(SessionError) as wrong_call:
            settle_call(self.db, other, call_id, SAMPLE_USAGE)
        self.assertEqual(wrong_call.exception.code, "RESOURCE_NOT_FOUND")
        self.assertEqual(read_budget(self.db, other)["attempts"], [])

    def test_v5_budget_configuration_through_cli_backs_up_and_preserves_progress(self):
        prior = self.db.with_name("legacy.db")
        session, _ = self.ready_answer(version=5, db=prior)
        expected = read_state(prior, session)
        cli = [sys.executable, "-X", "utf8", str(ROOT / "scripts/interview.py"), "--db", str(prior)]

        def run(*args):
            return json.loads(subprocess.check_output([*cli, *args], timeout=20))

        self.assertFalse(run("budget-status", session)["configured"])
        with closing(connect(prior, readonly=True)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 5)
        initialized = run("configure-budget", session, "--hard-limit-nanos", "10000", "--closing-reserve-nanos", "1000")
        self.assertEqual(initialized, run("budget-status", session))
        self.assertEqual(read_state(prior, session), expected)
        with closing(connect(prior, readonly=True)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 6)
            backup = db.execute("SELECT file_name FROM migration_backup WHERE schema_version=5").fetchone()[0]
        self.assertEqual(read_state(prior.with_name(backup), session), expected)

    def test_budget_gate_preserves_saved_answer_when_injected_into_the_probing_node(self):
        try:
            from app.workflow.interview import run_answer
        except ModuleNotFoundError as exc:
            if exc.name in {"langgraph", "langsmith"}:
                self.skipTest("install the locked LangGraph dependencies to exercise the probing node")
            raise
        self.configure(limit=1000)
        before = read_state(self.db, self.session)
        transport = Mock()
        run_answer(self.db, self.session, self.operation, lambda _: self.invoke(transport))
        transport.assert_not_called()
        after = read_state(self.db, self.session)
        self.assertEqual(after["operation"]["decision_reason"], "probe_budget_exhausted")
        self.assertEqual((before["current"]["answer"], after["current"]["answer"]), (BODY, BODY))
