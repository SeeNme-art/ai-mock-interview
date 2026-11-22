from pathlib import Path
import json
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from app.config import Settings
from app.db.business import SessionError
from app.kb.seed import read_seed
from app.kb.store import import_seed
from app.schemas.materials import Confirmation, MaterialInput
from app.schemas.planning import PlanChanges
from app.services.answers import accept_answer
from app.services.budget import PriceSnapshot, configure_budget, read_budget
from app.services.first_question import accept_start, prepare_first_question, read_state, start_epoch
from app.services.materials import confirm_material, start_draft
from app.services.model_gateway import MAX_RESPONSE_BYTES, DeepSeekGateway
from app.services.planning import rebuild_plan

MODEL = "deepseek-flash"
PRICE = PriceSnapshot(MODEL, "synthetic-price-v1", 10, 20, 2)
ANSWER = "我用固定查询比较了两组结果。"
USAGE = {"prompt_tokens": 60, "completion_tokens": 20, "total_tokens": 80,
         "prompt_cache_hit_tokens": 30, "prompt_cache_miss_tokens": 30}


def reply(*, content='{"ok":true}', finish="stop", usage=USAGE, model=MODEL):
    return json.dumps({"model": model, "choices": [
        {"finish_reason": finish, "message": {"role": "assistant", "content": content}},
    ], "usage": usage}).encode("utf-8")


class ModelGatewayTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name) / "interviews.db"
        bank = Path(folder.name) / "bank.db"
        import_seed(bank, read_seed(ROOT / "data/seeds/knowledge_questions.json"))
        material = MaterialInput()
        draft = start_draft(material, "form")
        self.session = confirm_material(self.db, draft.id, draft.id,
                                        Confirmation(material=material, mode="knowledge_only"), draft=draft)
        rebuild_plan(self.db, bank, self.session, 1, "plan", PlanChanges())
        start, _ = accept_start(self.db, self.session, 2, "start")
        prepare_first_question(self.db, bank, self.session, start, start_epoch(self.db, self.session, start))
        state = read_state(self.db, self.session)
        turn = state["current"]
        self.operation = accept_answer(self.db, self.session, turn["turn_id"], turn["exchange_id"], state["revision"],
                                       "answer", ANSWER)["operation_id"]
        self.settings = Settings(_env_file=None, deepseek_api_key="synthetic-key", llm_model_main=MODEL,
                                 llm_timeout_s=7, observe_store_io=False)

    def configure(self, limit=100_000):
        configure_budget(self.db, self.session, hard_limit_nanos=limit)

    def call(self, transport, *, step="decide_follow_up", number=1, output_limit=32):
        gateway = DeepSeekGateway(self.db, self.settings, PRICE, transport=transport)
        return self.invoke(gateway, step=step, number=number, output_limit=output_limit)

    def invoke(self, gateway, *, step="decide_follow_up", number=1, output_limit=32):
        return gateway.invoke_json(self.session, self.operation, step, role="probe", attempt_no=number,
                                   system_prompt="检查已提交的回答。", user_prompt=ANSWER, max_output_tokens=output_limit)

    def ledger(self):
        return read_budget(self.db, self.session)

    def test_send_uses_the_reserved_limit_and_known_usage_includes_cache(self):
        self.configure()

        def send(body, key, timeout):
            payload = json.loads(body)
            self.assertEqual((key, timeout), ("synthetic-key", 7))
            self.assertEqual(payload["model"], MODEL)
            self.assertEqual(payload["max_tokens"], 32)
            self.assertEqual(payload["thinking"], {"type": "disabled"})
            self.assertEqual(payload["response_format"], {"type": "json_object"})
            self.assertIs(payload["stream"], False)
            self.assertEqual(payload["messages"][1], {"role": "user", "content": ANSWER})
            self.assertIn("JSON", payload["messages"][0]["content"])
            ledger = self.ledger()
            attempt, = ledger["attempts"]
            self.assertEqual(attempt["max_output_tokens"], payload["max_tokens"])
            self.assertEqual(ledger["reserved_nanos"], attempt["estimated_input_tokens"] * 10 + 32 * 20)
            self.assertEqual(attempt["status"], "reserved")
            return 200, reply()

        self.assertEqual(self.call(send), {"ok": True})
        ledger = self.ledger()
        attempt, = ledger["attempts"]
        self.assertEqual((ledger["spent_nanos"], ledger["reserved_nanos"]), (760, 0))
        self.assertEqual((attempt["actual_input_tokens"], attempt["actual_output_tokens"],
                          attempt["actual_cached_input_tokens"]), (60, 20, 30))
        for private in (ANSWER, "synthetic-key", '"ok"'):
            self.assertNotIn(private, json.dumps(ledger, ensure_ascii=False))

    def test_unconfigured_or_exhausted_budget_never_calls_the_provider(self):
        transport = Mock()
        for code in ("BUDGET_NOT_CONFIGURED", "BUDGET_EXHAUSTED"):
            with self.subTest(code=code):
                with self.assertRaises(SessionError) as failure:
                    self.call(transport)
                self.assertEqual(failure.exception.code, code)
                if code == "BUDGET_NOT_CONFIGURED":
                    self.configure(1)
        transport.assert_not_called()
        self.assertEqual(self.ledger()["attempts"], [])
        self.assertEqual(read_state(self.db, self.session)["current"]["answer"], ANSWER)

    def test_bad_or_truncated_model_json_does_not_erase_known_usage(self):
        self.configure()
        for number, (content, finish) in enumerate((
            ('{"ok":1,"ok":2}', "stop"), ('{"cost":NaN}', "stop"), ('[]', "stop"),
            ('{"ok":true}', "length"), ("   ", "stop"),
        )):
            with self.subTest(content=content, finish=finish):
                with self.assertRaises(SessionError) as failure:
                    self.call(Mock(return_value=(200, reply(content=content, finish=finish))), step=f"invalid-{number}")
                self.assertEqual(failure.exception.code, "OUTPUT_INVALID")
        self.assertEqual((self.ledger()["spent_nanos"], self.ledger()["reserved_nanos"]), (5 * 760, 0))

    def test_missing_or_malformed_usage_keeps_the_reservation(self):
        self.configure()
        for number, usage in enumerate((None, {**USAGE, "total_tokens": 79},
                                        {**USAGE, "prompt_cache_hit_tokens": 61},
                                        {**USAGE, "prompt_tokens_details": {"cached_tokens": 20}},
                                        {**USAGE, "completion_tokens": True})):
            self.assertEqual(self.call(Mock(return_value=(200, reply(usage=usage))), step=f"usage-{number}"), {"ok": True})
        ledger = self.ledger()
        self.assertEqual({item["status"] for item in ledger["attempts"]}, {"usage_unknown"})
        self.assertEqual(ledger["spent_nanos"], 0)
        self.assertEqual(ledger["reserved_nanos"], sum(row["reserved_nanos"] for row in ledger["attempts"]))

    def test_missing_cache_information_uses_uncached_rate(self):
        self.configure()
        usage = {"prompt_tokens": 60, "completion_tokens": 20, "total_tokens": 80}
        self.call(Mock(return_value=(200, reply(usage=usage))))
        self.assertEqual(self.ledger()["spent_nanos"], 1000)

    def test_oversized_envelope_and_an_unexpected_model_hold_funds(self):
        self.configure()
        for number, body in enumerate((b"a" * (MAX_RESPONSE_BYTES + 1), reply(model="different-model"))):
            with self.assertRaises(SessionError) as failure:
                self.call(Mock(return_value=(200, body)), step=f"bad-response-{number}")
            self.assertEqual(failure.exception.code, "OUTPUT_INVALID")
        self.assertEqual({item["status"] for item in self.ledger()["attempts"]}, {"usage_unknown"})

    def test_explicit_output_usage_over_the_requested_limit_is_charged_and_rejected(self):
        self.configure()
        with self.assertRaises(SessionError) as failure:
            self.call(Mock(return_value=(200, reply())), output_limit=19)
        self.assertEqual(failure.exception.code, "OUTPUT_INVALID")
        self.assertEqual(self.ledger()["spent_nanos"], 760)

    def test_http_errors_are_classified_without_echoing_provider_body_or_retrying(self):
        self.configure()
        for status, code in ((401, "CONFIGURATION_ERROR"), (422, "CONFIGURATION_ERROR"),
                             (429, "MODEL_UNAVAILABLE"), (503, "MODEL_UNAVAILABLE"),
                             (302, "MODEL_REQUEST_REJECTED")):
            transport = Mock(return_value=(status, b"synthetic upstream error body"))
            with self.subTest(status=status):
                with self.assertRaises(SessionError) as failure:
                    self.call(transport, step=f"http-{status}")
                self.assertEqual(failure.exception.code, code)
                self.assertNotIn("synthetic upstream error body", str(failure.exception))
                self.assertEqual(transport.call_count, 1)
        self.assertEqual({row["status"] for row in self.ledger()["attempts"]}, {"usage_unknown"})

    def test_timeout_is_recorded_and_same_attempt_cannot_send_again(self):
        self.configure()
        transport = Mock(side_effect=TimeoutError("synthetic transport details"))
        with self.assertRaises(SessionError) as failure:
            self.call(transport)
        self.assertEqual(failure.exception.code, "MODEL_UNAVAILABLE")
        self.assertNotIn("synthetic transport details", str(failure.exception))
        with self.assertRaises(SessionError) as replay:
            self.call(transport)
        self.assertEqual(replay.exception.code, "CALL_ALREADY_RECORDED")
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(self.ledger()["attempts"][0]["status"], "usage_unknown")

    def test_local_configuration_and_token_limits_fail_before_reservation(self):
        self.configure()
        transport = Mock()
        for setting in ({"llm_timeout_s": 0}, {"deepseek_api_key": ""}, {"llm_model_main": "other"}):
            with self.assertRaises(SessionError) as failure:
                DeepSeekGateway(self.db, self.settings.model_copy(update=setting), PRICE, transport=transport)
            self.assertEqual(failure.exception.code, "CONFIGURATION_ERROR")
        for limit in (False, 0, 393_217):
            with self.assertRaises(SessionError) as failure:
                self.call(transport, output_limit=limit)
            self.assertEqual(failure.exception.code, "INVALID_INPUT")
        transport.assert_not_called()
        self.assertEqual(self.ledger()["attempts"], [])

    def test_default_transport_posts_once_and_closes_the_http_resources(self):
        self.configure()
        connection = Mock()
        response = connection.getresponse.return_value
        response.status = 200
        response.read.return_value = reply()
        with patch("app.services.model_gateway.http.client.HTTPSConnection", return_value=connection) as factory:
            self.assertEqual(self.invoke(DeepSeekGateway(self.db, self.settings, PRICE)), {"ok": True})
        factory.assert_called_once_with("api.deepseek.com", timeout=7)
        self.assertEqual(connection.request.call_count, 1)
        request = connection.request.call_args
        self.assertEqual(request.args, ("POST", "/chat/completions"))
        self.assertEqual(request.kwargs["headers"]["Authorization"], "Bearer synthetic-key")
        response.read.assert_called_once_with(MAX_RESPONSE_BYTES + 1)
        response.close.assert_called_once_with()
        connection.close.assert_called_once_with()

    def test_injected_model_result_can_be_consumed_by_the_existing_answer_node(self):
        try:
            from app.workflow.interview import run_answer
        except ModuleNotFoundError as exc:
            if exc.name in {"langgraph", "langsmith"}:
                self.skipTest("install the locked LangGraph dependencies to exercise the answer node")
            raise
        self.configure()
        content = json.dumps({"should_probe": False, "reason": "合成推进检查"}, ensure_ascii=False)
        send = Mock(return_value=(200, reply(content=content)))
        run_answer(self.db, self.session, self.operation, lambda _: self.call(send))
        state = read_state(self.db, self.session)
        self.assertEqual((state["operation"]["decision"], state["current"]["answer"]), ("score_pending", ANSWER))
        self.assertEqual(self.ledger()["spent_nanos"], 760)
        self.assertEqual(send.call_count, 1)
