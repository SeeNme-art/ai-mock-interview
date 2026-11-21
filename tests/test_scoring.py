from pathlib import Path
import sys
import unittest

from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from app.schemas.scoring import DIMENSIONS, Assessment
from app.services.scoring import ReportItem, aggregate_scores, score_assessment

ANSWERS = {"x_1": "先🙂用固定查询，再看召回。固定查询。", "x_2": "从测试日志核实结果。"}


def assessment_data(task_type, values):
    return {
        "task_type": task_type,
        "rubric_version": f"{task_type}-v1",
        "dimensions": {key: {
            "applicable": key in values,
            "value": values.get(key),
            "reason": "对应已提交内容" if key in values else "该维度未考察",
            "evidence": [{"exchange_id": "x_1", "quote": "固定查询", "start": 90, "end": 99}] if key in values else [],
        } for key in DIMENSIONS[task_type]},
    }


def scoring(payload, active, **overrides):
    context = dict(task_type=payload["task_type"], rubric_version=payload["rubric_version"],
                   applicable_dimensions=active, answers=ANSWERS, source_refs=("reviewed-source",))
    return score_assessment(Assessment.model_validate(payload), **(context | overrides))


def rated_item(item_id, task_type, **values):
    score = scoring(assessment_data(task_type, values), values.keys())
    return ReportItem(item_id, task_type, displayed=True, has_answer=True, outcome="answered",
                      evaluation_status="ready", scored=score)


class ScoringTests(unittest.TestCase):
    def test_only_applicable_dimensions_count_and_offsets_are_recomputed(self):
        values = {"correctness": 4, "depth": 3, "structure": 1}
        payload = assessment_data("knowledge", values)
        payload["dimensions"]["correctness"]["source_refs"] = ["reviewed-source"]
        raw = Assessment.model_validate(payload)
        result = score_assessment(raw, task_type="knowledge", rubric_version="knowledge-v1",
                                  applicable_dimensions=values, answers=ANSWERS, source_refs=("reviewed-source",))
        self.assertEqual(result.item_score, 66.7)
        raw_dimension = raw.dimensions["correctness"]
        ref = result.assessment.dimensions["correctness"].evidence[0]
        self.assertEqual((ref.start, ref.end), (3, 7))
        self.assertEqual(ANSWERS[ref.exchange_id][ref.start:ref.end], "固定查询")
        self.assertEqual(raw_dimension.evidence[0].start, 90)
        raw_dimension.source_refs.clear()
        self.assertEqual(result.assessment.dimensions["correctness"].source_refs, ["reviewed-source"])

    def test_score_types_out_of_range_and_model_total_are_rejected(self):
        for bad in (True, "3", 3.0, -1, 5):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                Assessment.model_validate(assessment_data("knowledge", {"correctness": bad}))
        payload = assessment_data("knowledge", {"correctness": 3})
        with self.assertRaises(ValidationError):
            Assessment.model_validate(payload | {"item_score": 100})

    def test_dimensions_and_version_must_match_frozen_scoring_targets(self):
        values = {"technical_reasoning": 3}
        payload = assessment_data("project", values)
        with self.assertRaisesRegex(ValueError, "版本"):
            scoring(payload, values, rubric_version="project-v2")
        with self.assertRaisesRegex(ValueError, "全部维度"):
            scoring(payload | {"dimensions": {}}, values)
        with self.assertRaisesRegex(ValueError, "缺少合法的内容维度"):
            scoring(payload, ("communication",))
        with self.assertRaisesRegex(ValueError, "适用性"):
            scoring(payload, ("technical_reasoning", "validation_evidence"))
        payload["dimensions"]["communication"]["value"] = 0
        with self.assertRaisesRegex(ValueError, "未考察"):
            scoring(payload, values)
        payload = assessment_data("project", {"technical_reasoning": None})
        with self.assertRaisesRegex(ValueError, "须给出分值"):
            scoring(payload, values)

    def test_rejects_fabricated_quote_wrong_exchange_and_unknown_source(self):
        for evidence in (
            {"exchange_id": "x_1", "quote": "我测出了 90%"},
            {"exchange_id": "x_2", "quote": "固定查询"},
            {"exchange_id": "hidden-question", "quote": "固定查询"},
            {"exchange_id": "x_1", "quote": " "},
        ):
            with self.subTest(evidence=evidence), self.assertRaisesRegex(ValueError, "已提交答案"):
                payload = assessment_data("knowledge", {"correctness": 2})
                payload["dimensions"]["correctness"]["evidence"] = [evidence]
                scoring(payload, ("correctness",))
        payload = assessment_data("knowledge", {"correctness": 2})
        payload["dimensions"]["correctness"]["source_refs"] = ["unknown-source"]
        with self.assertRaisesRegex(ValueError, "本题快照"):
            scoring(payload, ("correctness",))

    def test_empty_evidence_requires_reason_and_at_least_one_submitted_answer(self):
        payload = assessment_data("knowledge", {"correctness": 0})
        payload["dimensions"]["correctness"]["evidence"] = []
        with self.assertRaisesRegex(ValueError, "缺证说明"):
            scoring(payload, ("correctness",), answers={"x_1": "不知道"})
        payload["dimensions"]["correctness"]["evidence_absence_reason"] = "用户明确表示不知道"
        self.assertEqual(scoring(payload, ("correctness",), answers={"x_1": "不知道"}).item_score, 0)
        with self.assertRaisesRegex(ValueError, "至少一条已提交答案"):
            scoring(payload, ("correctness",), answers={"x_1": " "})

    def test_report_separates_scores_counts_and_actual_dimension_coverage(self):
        items = [
            rated_item("p1", "knowledge", correctness=4),
            rated_item("p2", "knowledge", correctness=0, depth=1),
            rated_item("p3", "project", technical_reasoning=3, validation_evidence=1),
            ReportItem("p4", "project", True, True, "skipped"),
            ReportItem("p5", "project", True, True, "answered", "failed"),
            ReportItem("p6", "knowledge", outcome="generation_failed"),
            ReportItem("p7", "knowledge", displayed=True, outcome="unanswered"),
            ReportItem("p8", "knowledge", True, True, "answered", "pending"),
        ]
        report = aggregate_scores(items, ended_early=False)
        self.assertEqual((report["knowledge_score"], report["project_score"]), (56.2, 50.0))
        self.assertEqual(report["category_counts"], {"knowledge": 2, "project": 1})
        self.assertEqual(report["counts"], {"planned": 8, "displayed": 7, "answered": 6, "scored": 3,
                                           "skipped": 1, "system_failed": 2})
        self.assertEqual(report["dimension_summary"]["correctness"], {"mean": 2.0, "count": 2})
        self.assertEqual(report["dimension_summary"]["depth"], {"mean": 1.0, "count": 1})
        self.assertEqual(report["dimension_summary"]["communication"], {"mean": None, "count": 0})
        self.assertTrue(report["is_partial"])
        self.assertNotIn("overall", report)

    def test_zero_and_missing_scores_are_distinct_and_early_end_is_partial(self):
        zero = rated_item("p1", "knowledge", correctness=0)
        report = aggregate_scores([zero], ended_early=False)
        self.assertEqual(report["knowledge_score"], 0)
        self.assertIsNone(report["project_score"])
        self.assertFalse(report["is_partial"])
        self.assertTrue(aggregate_scores([zero], ended_early=True)["is_partial"])
        missing = aggregate_scores([ReportItem("p1", "knowledge")], ended_early=True)
        self.assertIsNone(missing["knowledge_score"])
        self.assertEqual(missing["counts"]["scored"], 0)
        self.assertTrue(missing["is_partial"])

    def test_duplicate_and_inconsistent_report_rows_cannot_enter_denominator(self):
        rated = rated_item("p1", "knowledge", correctness=4)
        with self.assertRaisesRegex(ValueError, "重复"):
            aggregate_scores([rated, rated], ended_early=False)
        with self.assertRaisesRegex(ValueError, "评分状态"):
            ReportItem("p1", "knowledge", True, True, "skipped", "ready", rated.scored)
        with self.assertRaisesRegex(ValueError, "只有评分成功"):
            ReportItem("p1", "knowledge", True, True, "answered", "ready")
        with self.assertRaisesRegex(ValueError, "实际评分题型"):
            ReportItem("p1", "project", True, True, "answered", "ready", rated.scored)
