from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from evaluate_offline import DATASET, SEED, load_samples, measure_materials, read_seed, summarize_rankings, validate_labels


class OfflineEvaluationTests(unittest.TestCase):
    def test_corpus_counts_splits_and_references(self):
        samples, versions = load_samples(DATASET)
        bank = {question.id: question for question in read_seed(SEED).questions if question.status == "verified"}
        validate_labels(samples, bank)
        for suite, expected in {"core": (10, 10), "retrieval": (15, 15), "materials": (5, 5)}.items():
            self.assertEqual(versions[suite]["version"], "m4-eval-v1")
            self.assertEqual(tuple(sum(case["split"] == split for case in samples[suite])
                                   for split in ("debug", "holdout")), expected)

    def test_retrieval_denominators_and_truncation(self):
        rows = [
            {"relevant_ids": ["a", "b"], "ranked": ["c", "b", "d", "a"]},
            {"relevant_ids": ["z"], "ranked": ["a"]},
            {"relevant_ids": [], "ranked": []},
            {"relevant_ids": [], "ranked": ["a"]},
        ]
        self.assertEqual(summarize_rankings(rows, "ranked"),
                         {"positives": 2, "hit_at_3": 0.5, "recall_at_3": 0.25, "mrr_at_3": 0.25,
                          "no_candidate_samples": 2, "no_candidate_correct": 1})

    def test_debug_materials_run_production_parsers(self):
        samples, _ = load_samples(DATASET)
        results = measure_materials([case for case in samples["materials"] if case["split"] == "debug"])
        self.assertEqual((results["passing"], results["samples"]), (5, 5))
        self.assertTrue(any(row["format"] == "pdf" and row["columns"] == 2 for row in results["cases"]))
