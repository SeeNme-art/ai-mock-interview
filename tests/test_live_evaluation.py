from pathlib import Path
import json
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from evaluate_live import run
from app.config import Settings


class LiveEvaluationTests(unittest.TestCase):
    def test_both_splits_cannot_exceed_the_approved_plan_or_replay(self):
        calls = []

        def transport(body, _key, _timeout):
            request = json.loads(body)
            calls.append(request["max_tokens"])
            result = {"should_probe": False, "reason": "合成测试"}
            data = {"model": request["model"], "usage": {"prompt_tokens": 200, "completion_tokens": 15, "total_tokens": 215},
                    "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(result)}}]}
            return 200, json.dumps(data).encode()

        with TemporaryDirectory() as temporary:
            directory = Path(temporary) / "trial"
            settings = Settings(_env_file=None, deepseek_api_key="fixture-key")
            for split in ("debug", "holdout"):
                result = run(split, directory, settings, transport=transport)
                self.assertIsNone(result["stop"])
                self.assertEqual(len(result["cases"]), 10)
            with self.assertRaises(FileExistsError):
                run("holdout", directory, settings, transport=transport)
            self.assertEqual(calls, [1200] * 39)
            self.assertEqual(len(result["attempts"]), 39)
            self.assertLessEqual(max(row["estimated_input_tokens"] for row in result["attempts"]), 4000)
