from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from app.db.business import SessionError
from app.db.sqlite import connect
from app.kb.seed import read_seed
from app.kb.store import import_seed
from app.schemas.materials import Confirmation, MaterialInput
from app.schemas.planning import PlanChanges
from app.services.answers import MAX_ANSWER_CHARS, accept_answer
from app.services.first_question import accept_start, prepare_first_question, read_state, start_epoch
from app.services.materials import confirm_material, start_draft
from app.services.planning import rebuild_plan

BODY = "合成答案正文标记\n  预先确定路径，Agent 可在权限内选择下一步。"


class AnswerTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name) / "interviews.db"
        self.bank = Path(folder.name) / "questions.db"
        import_seed(self.bank, read_seed(ROOT / "data/seeds/knowledge_questions.json"))
        self.session = self.ready_session()
        ready = read_state(self.db, self.session)
        self.revision = ready["revision"]
        self.turn = ready["current"]["turn_id"]
        self.exchange = ready["current"]["exchange_id"]

    def ready_session(self):
        material = MaterialInput(skills=["Python"])
        draft = start_draft(material, "form")
        session = confirm_material(self.db, draft.id, draft.id, Confirmation(material=material, mode="knowledge_only"), draft=draft)
        rebuild_plan(self.db, self.bank, session, 1, "plan-" + session, PlanChanges())
        op, _ = accept_start(self.db, session, 2, "start-" + session)
        prepare_first_question(self.db, self.bank, session, op, start_epoch(self.db, session, op))
        return session

    def send(self, body=BODY, *, key="answer-1", revision=None, session=None, turn=None, exchange=None):
        return accept_answer(self.db, session or self.session, turn or self.turn, exchange or self.exchange,
                             self.revision if revision is None else revision, key, body)

    def answer_operations(self):
        with closing(connect(self.db, readonly=True)) as db:
            return [dict(row) for row in db.execute("SELECT * FROM operation WHERE kind='answer' AND session_id=?", (self.session,))]

    def test_saved_answer_status_epoch_and_body_free_receipt(self):
        receipt = self.send()
        state = read_state(self.db, self.session)
        self.assertEqual(receipt["revision"], self.revision + 1)
        self.assertTrue(receipt["accepted"])
        self.assertEqual(state["current"]["answer"], BODY)
        self.assertTrue(state["current"]["answered_at"].endswith("+00:00"))
        self.assertEqual(state["current"]["state"], "processing")
        self.assertEqual(state["operation"], {"operation_id": receipt["operation_id"], "kind": "answer",
                                             "status": "queued", "error_code": None})
        operation, = self.answer_operations()
        self.assertEqual(operation["expected_epoch"], 2)
        self.assertNotIn("合成答案正文标记", json.dumps(receipt, ensure_ascii=False))
        self.assertNotIn("合成答案正文标记", json.dumps(operation, ensure_ascii=False))
        self.assertNotIn("answer", json.loads(operation["safe_request_json"]))
        with closing(connect(self.db, readonly=True)) as db:
            revision, epoch = db.execute("SELECT revision,control_epoch FROM interview_session WHERE id=?", (self.session,)).fetchone()
            self.assertEqual((revision, epoch), (receipt["revision"], operation["expected_epoch"]))
            self.assertEqual(db.execute("SELECT answer_mode FROM exchange WHERE id=?", (self.exchange,)).fetchone()[0], "text")

    def test_same_key_replays_receipt_but_changed_answer_conflicts(self):
        first = self.send(BODY.replace("\n", "\r\n"))
        self.assertEqual(self.send(), first)
        with self.assertRaises(SessionError) as error:
            self.send("改写后的回答")
        self.assertEqual(error.exception.code, "IDEMPOTENCY_CONFLICT")
        self.assertEqual(len(self.answer_operations()), 1)
        self.assertEqual(read_state(self.db, self.session)["current"]["answer"], BODY)

    def test_new_key_with_old_or_current_revision_cannot_overwrite_the_answer(self):
        receipt = self.send()
        with self.assertRaises(SessionError) as error:
            self.send("旧窗口改写", key="new-key")
        self.assertEqual((error.exception.code, error.exception.current_revision), ("STALE_REVISION", receipt["revision"]))
        with self.assertRaisesRegex(SessionError, "这一轮已经提交过回答") as error:
            self.send("新窗口改写", key="new-key", revision=receipt["revision"])
        self.assertEqual(error.exception.code, "ILLEGAL_ACTION")
        self.assertEqual(len(self.answer_operations()), 1)
        self.assertEqual(read_state(self.db, self.session)["current"]["answer"], BODY)

    def test_concurrent_same_key_returns_one_receipt(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(self.send) for _ in range(2)]
            self.assertEqual(jobs[0].result(), jobs[1].result())
        self.assertEqual(len(self.answer_operations()), 1)

    def test_concurrent_different_keys_cannot_save_two_answers(self):
        def submit(body, key):
            try:
                return self.send(body, key=key)
            except SessionError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = [job.result() for job in (pool.submit(submit, "答案甲", "key-a"), pool.submit(submit, "答案乙", "key-b"))]
        self.assertIn("STALE_REVISION", outcomes)
        self.assertEqual(sum(isinstance(result, dict) for result in outcomes), 1)
        self.assertEqual(len(self.answer_operations()), 1)
        self.assertIn(read_state(self.db, self.session)["current"]["answer"], ("答案甲", "答案乙"))

    def test_empty_oversized_and_non_text_inputs_are_rejected_and_i_dont_know_is_valid(self):
        for body in ("", "  \r\n\t", "长" * (MAX_ANSWER_CHARS + 1), "a\x00b", "x\ud800", None):
            with self.subTest(body_length=len(body) if body is not None else None), self.assertRaises(SessionError) as error:
                self.send(body)
            self.assertEqual(error.exception.code, "INVALID_INPUT")
        self.assertEqual(self.answer_operations(), [])
        self.send("不知道")
        self.assertEqual(read_state(self.db, self.session)["current"]["answer"], "不知道")

    def test_exact_length_and_unicode_spacing_are_kept(self):
        text = "🙂\t" + "字" * (MAX_ANSWER_CHARS - 4) + " \n"
        self.send(text)
        self.assertEqual(len(text), MAX_ANSWER_CHARS)
        self.assertEqual(read_state(self.db, self.session)["current"]["answer"], text)

    def test_wrong_session_wrong_exchange_and_old_round_are_rejected(self):
        other = self.ready_session()
        another = read_state(self.db, other)["current"]
        for ids in ({"session": other}, {"exchange": another["exchange_id"]}, {"turn": another["turn_id"]}):
            with self.subTest(ids=ids), self.assertRaises(SessionError) as error:
                self.send(**ids)
            self.assertEqual(error.exception.code, "RESOURCE_NOT_FOUND")
        with closing(connect(self.db)) as db, db:
            db.execute("INSERT INTO exchange(id,turn_id,round_no,question_text) VALUES ('follow-up',?,1,'合成追问')", (self.turn,))
        with self.assertRaisesRegex(SessionError, "当前不能提交回答"):
            self.send()
        self.assertEqual(self.answer_operations(), [])
        self.assertEqual(read_state(self.db, self.session)["current"]["exchange_id"], "follow-up")

    def test_inactive_and_deleting_sessions_reject_new_or_replayed_submission(self):
        with closing(connect(self.db)) as db, db:
            db.execute("UPDATE interview_session SET phase='FINISHED' WHERE id=?", (self.session,))
        with self.assertRaisesRegex(SessionError, "正式题目展示后"):
            self.send()
        with closing(connect(self.db)) as db, db:
            db.execute("UPDATE interview_session SET phase='ACTIVE' WHERE id=?", (self.session,))
        self.send()
        with closing(connect(self.db)) as db, db:
            db.execute("UPDATE interview_session SET delete_state='deleting' WHERE id=?", (self.session,))
        for read in (self.send, lambda: read_state(self.db, self.session)):
            with self.assertRaises(SessionError) as error:
                read()
            self.assertEqual(error.exception.code, "RESOURCE_NOT_FOUND")

    def test_queued_operation_prevents_another_operation_before_editing_the_exchange(self):
        with closing(connect(self.db)) as db, db:
            db.execute("UPDATE operation SET status='queued' WHERE session_id=? AND kind='start'", (self.session,))
        with self.assertRaises(SessionError) as error:
            self.send()
        self.assertEqual(error.exception.code, "OPERATION_IN_PROGRESS")
        self.assertNotIn("answer", read_state(self.db, self.session)["current"])
        self.assertEqual(self.answer_operations(), [])

    def test_failure_registering_operation_rolls_back_answer_and_progress(self):
        with closing(connect(self.db)) as db, db:
            db.execute("""CREATE TRIGGER reject_answer_operation BEFORE INSERT ON operation WHEN NEW.kind='answer'
                          BEGIN SELECT RAISE(ABORT,'test rollback'); END;""")
        initial = read_state(self.db, self.session)
        with self.assertRaises(sqlite3.IntegrityError):
            self.send()
        self.assertEqual(read_state(self.db, self.session), initial)
        self.assertEqual(self.answer_operations(), [])
        with closing(connect(self.db)) as db, db:
            self.assertEqual(db.execute("SELECT control_epoch FROM interview_session WHERE id=?", (self.session,)).fetchone()[0], 1)
            db.execute("DROP TRIGGER reject_answer_operation")
        self.send()
        self.assertEqual(read_state(self.db, self.session)["current"]["answer"], BODY)

    def test_cli_reads_utf8_file_ready_state_in_a_new_process_and_replays_from_stdin(self):
        cli = [sys.executable, "-X", "utf8", str(ROOT / "scripts/interview.py"), "--db", str(self.db)]
        args = ["answer", self.session, self.turn, self.exchange, "--expected-revision", str(self.revision), "--key", "cli-answer"]
        answer_file = self.db.with_suffix(".txt")
        answer_file.write_bytes(b"\xff")
        invalid = subprocess.run([*cli, *args, "--answer-file", str(answer_file)], capture_output=True, timeout=15)
        self.assertEqual(invalid.returncode, 1)
        self.assertEqual(json.loads(invalid.stderr)["code"], "INVALID_INPUT")
        answer_file.write_text(BODY, encoding="utf-8-sig")
        out = subprocess.check_output([*cli, *args, "--answer-file", str(answer_file)], encoding="utf-8", timeout=15)
        self.assertNotIn("合成答案正文标记", out)
        receipt = json.loads(out)
        retry = subprocess.run([*cli, *args], input=BODY.encode(), capture_output=True, timeout=15, check=True)
        self.assertEqual(json.loads(retry.stdout), receipt)
        state = json.loads(subprocess.check_output([*cli, "state", self.session], encoding="utf-8", timeout=15))
        self.assertEqual(state["current"]["answer"], BODY)
        self.assertEqual(state["operation"]["operation_id"], receipt["operation_id"])
