from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "experiments"))
from model_preflight import CASES
from model_trial import send_once, start_run, summarize


def reply(case=0, *, prompt=20, completion=10, content=None):
    if content is None:
        content = json.dumps(CASES[case]["fixture"], ensure_ascii=False)
    return 200, json.dumps({
        "model": "deepseek-flash",
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                  "total_tokens": prompt + completion},
        "choices": [{"finish_reason": "stop", "message": {"content": content}}],
    }).encode()


class TrialTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name) / "one-shot"

    def run_with(self, sender, **kwargs):
        return start_run(self.folder, "synthetic-key", sender, **kwargs)

    def test_reservation_is_visible_before_each_request_and_success_is_settled(self):
        calls = 0

        def send(body, key):
            nonlocal calls
            with closing(sqlite3.connect(self.folder / "ledger.db")) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM attempts WHERE outcome='reserved'").fetchone()[0], 1)
            self.assertEqual(json.loads(body)["max_tokens"], 256)
            result = reply(calls)
            calls += 1
            return result

        report = self.run_with(send)
        self.assertEqual((calls, report["cases_passed"], report["stop_reason"]), (6, 6, "cases_finished"))
        self.assertEqual(report["peak_cost_from_known_usage_usd"], "0.000108")
        self.assertEqual(report["unknown_cost_reserved_usd"], "0")

    def test_budget_rejection_sends_nothing(self):
        send = Mock()
        report = self.run_with(send, budget=1)
        send.assert_not_called()
        self.assertEqual(report["stop_reason"], "budget_or_attempt_limit")

    def test_repair_cannot_exceed_twelve_total_attempts(self):
        responses = [response for i in range(6) for response in (reply(i, content="bad"), reply(i))]
        send = Mock(side_effect=responses)
        report = self.run_with(send)
        self.assertEqual((send.call_count, report["cases_passed"]), (12, 6))
        self.assertEqual([x["attempt"] for x in report["attempts"]], [1, 2] * 6)

    def test_multiple_failures_share_same_retry_slot(self):
        send = Mock(side_effect=[(503, b"ignored error body"), reply(content="broken")])
        with patch("model_trial.time.sleep") as sleep:
            report = self.run_with(send)
        sleep.assert_called_once_with(1)
        self.assertEqual((send.call_count, report["stop_reason"]), (2, "attempts_exhausted"))
        self.assertEqual(report["unknown_cost_reserved_usd"], report["attempts"][0]["reserved_usd"])

    def test_timeout_holds_reservation_and_stops(self):
        send = Mock(side_effect=TimeoutError("private text"))
        report = self.run_with(send)
        self.assertEqual((send.call_count, report["stop_reason"]), (1, "network_usage_unknown"))
        self.assertEqual(report["unknown_cost_reserved_usd"], report["attempts"][0]["reserved_usd"])
        self.assertNotIn("private text", json.dumps(report))

    def test_missing_usage_stops_without_retry(self):
        send = Mock(return_value=(200, b'{"model":"deepseek-flash","choices":[]}'))
        report = self.run_with(send)
        send.assert_called_once()
        self.assertEqual(report["stop_reason"], "usage_unknown")
        self.assertIsNone(report["attempts"][0]["peak_cost_from_usage_usd"])

    def test_usage_over_plan_is_recorded_and_stops(self):
        send = Mock(return_value=reply(prompt=2000))
        report = self.run_with(send)
        send.assert_called_once()
        self.assertEqual(report["stop_reason"], "usage_over_plan")
        self.assertEqual(report["attempts"][0]["prompt_tokens"], 2000)

    def test_auth_failure_stops_and_excludes_error_body(self):
        send = Mock(return_value=(401, b"private error"))
        report = self.run_with(send)
        send.assert_called_once()
        self.assertEqual(report["stop_reason"], "http_stop")
        self.assertEqual(report["attempts"][0]["http_status"], 401)
        self.assertNotIn("private error", json.dumps(report))

    def test_interrupted_run_remains_inspectable_and_cannot_send_again(self):
        first = Mock(side_effect=KeyboardInterrupt)
        with self.assertRaises(KeyboardInterrupt):
            self.run_with(first)
        with closing(sqlite3.connect(self.folder / "ledger.db")) as db:
            report = summarize(db)
        self.assertEqual(report["stop_reason"], "incomplete")
        self.assertEqual(report["attempts"][0]["outcome"], "reserved")
        self.assertNotEqual(report["unknown_cost_reserved_usd"], "0")
        second = Mock()
        with self.assertRaises(FileExistsError):
            self.run_with(second)
        second.assert_not_called()

    def test_transport_issues_one_post_and_does_not_follow_redirect(self):
        with patch("model_trial.http.client.HTTPSConnection") as connect:
            conn = connect.return_value
            response = conn.getresponse.return_value
            response.status = 307
            self.assertEqual(send_once(b"{}", "synthetic-key"), (307, b""))
        connect.assert_called_once_with("api.deepseek.com", timeout=30)
        conn.request.assert_called_once_with("POST", "/chat/completions", body=b"{}", headers={
            "Authorization": "Bearer synthetic-key", "Content-Type": "application/json",
        })
        response.read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
