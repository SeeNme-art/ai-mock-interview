from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

try:
    from m2_demo import run_case
except ModuleNotFoundError as exc:
    if exc.name not in {"langgraph", "langsmith"}:
        raise
    run_case = None


@unittest.skipIf(run_case is None, "install the locked LangGraph dependencies to exercise the CLI demo")
class M2DemoTests(unittest.TestCase):
    def demo(self, case):
        with TemporaryDirectory() as folder, patch("socket.socket.connect", side_effect=AssertionError("offline demo")):
            return run_case(case, Path(folder))

    def test_mixed_and_knowledge_finish_with_expected_coverage_and_two_probe_cap(self):
        for scenario, expected in (("knowledge", {"knowledge": 5, "project": 0}),
                                   ("mixed", {"knowledge": 3, "project": 2})):
            with self.subTest(scenario=scenario):
                result = self.demo(scenario)
                self.assertEqual((result["end_reason"], result["counts"]["scored"]), ("completed", 5))
                self.assertEqual(result["category_counts"], expected)
                self.assertEqual(result["probe_counts"], [2, 0, 0, 0, 0])
                self.assertEqual(result["reserved_nanos"], 0)
                self.assertEqual(result["attempts"], 11)

    def test_budget_stop_saves_answer_without_making_a_call_or_fabricating_a_score(self):
        result = self.demo("budget")
        self.assertEqual((result["end_reason"], result["report_kind"]), ("budget_exhausted", "summary"))
        self.assertEqual((result["counts"]["answered"], result["counts"]["scored"], result["attempts"]), (1, 0, 0))

    def test_provider_failure_uses_exactly_two_attempts_per_step_and_retains_unknown_usage(self):
        result = self.demo("provider-failure")
        self.assertEqual((result["end_reason"], result["report_kind"], result["counts"]["answered"]), ("completed", "summary", 5))
        self.assertEqual((result["counts"]["system_failed"], result["attempts"]), (5, 20))
        self.assertGreater(result["reserved_nanos"], 0)
        self.assertIsNone(result["knowledge_score"])
