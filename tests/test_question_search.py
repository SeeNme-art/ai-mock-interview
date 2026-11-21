import importlib.util
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from app.kb.seed import SeedBank, read_seed
from app.kb.store import import_seed, list_questions

HAS_FAISS = importlib.util.find_spec("faiss") is not None
if HAS_FAISS:
    from app.kb.retrieval import (IndexUnavailable, document_text, query_text,
                                 rebuild_index, search_questions)


class FixedVectors:
    model_id = "fixture-1"
    dimension = 2

    def __init__(self, vectors):
        self.vectors = vectors

    def embed(self, texts):
        return (self.vectors[text] for text in texts)


@unittest.skipUnless(HAS_FAISS, "install requirements-embed.txt to exercise native FAISS")
class QuestionSearchTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name) / "questions.db"
        self.index = Path(folder.name) / "index"
        seed = read_seed(ROOT / "data" / "seeds" / "knowledge_questions.json")
        sample_ids = {"agent-workflow-boundary", "agent-json-schema", "agent-tool-executor",
                      "agent-interrupt-replay", "rag-cosine-faiss", "backend-sqlite-context"}
        self.bank = seed.model_copy(update={"questions": [
            question.model_copy(update={"status": "verified"})
            for question in seed.questions if question.id in sample_ids
        ]})
        import_seed(self.db, self.bank)
        self.objective = "限制模型可以执行的操作"
        self.jd = "实现有权限控制的 Agent 服务"
        self.skills = ("工具调用", "参数校验")
        vectors = {"agent-workflow-boundary": (3, 4), "agent-json-schema": (0, 99),
                   "agent-tool-executor": (8, 6)}
        self.embedder = FixedVectors({
            document_text(question): vectors.get(question["id"], (1, 0))
            for question in list_questions(self.db)
        })
        self.query = "考察点：限制模型可以执行的操作\n岗位要求：实现有权限控制的 Agent 服务\n相关技能：工具调用、参数校验"
        self.embedder.vectors[self.query] = (10, 0)

    def search(self, *, exclude_ids=(), top_k=3):
        return search_questions(self.db, self.index, self.embedder, objective=self.objective,
                                jd=self.jd, skills=self.skills, dimension="agent", difficulty="basic",
                                exclude_ids=exclude_ids, top_k=top_k)

    def update_question(self, question_id, **fields):
        payload = self.bank.model_dump(mode="json")
        question = next(q for q in payload["questions"] if q["id"] == question_id)
        question["revision"] += 1
        question.update(fields)
        self.bank = SeedBank.model_validate(payload)
        import_seed(self.db, self.bank)

    def test_queries_include_all_three_components_and_allow_optional_context(self):
        self.assertEqual(query_text(self.objective, self.jd, self.skills), self.query)
        self.assertEqual(query_text(" 考察点 ", " ", ("",)), "考察点：考察点")
        with self.assertRaisesRegex(ValueError, "考察点不能为空"):
            query_text("  ", self.jd, self.skills)

    def test_cosine_ranking_uses_all_eligible_candidates_before_top_k(self):
        manifest = rebuild_index(self.db, self.index, self.embedder)
        result = self.search()
        self.assertEqual([row["question"]["id"] for row in result],
                         ["agent-tool-executor", "agent-workflow-boundary", "agent-json-schema"])
        for row, cosine in zip(result, (0.8, 0.6, 0.0)):
            self.assertAlmostEqual(row["score"], cosine, places=6)
            self.assertEqual(row["index_version"], manifest["index_version"])
            self.assertTrue(row["question"]["sources"])
        self.assertEqual(self.search(top_k=1), result[:1])

    def test_drafts_retired_and_seen_questions_never_rank(self):
        self.update_question("agent-tool-executor", status="retired")
        self.update_question("agent-json-schema", status="draft")
        manifest = rebuild_index(self.db, self.index, self.embedder)
        indexed = {row["id"] for row in manifest["questions"]}
        self.assertEqual(len(indexed), len(self.bank.questions) - 2)
        self.assertTrue({"agent-tool-executor", "agent-json-schema"}.isdisjoint(indexed))
        self.assertEqual(self.search()[0]["question"]["id"], "agent-workflow-boundary")
        self.assertEqual(self.search(exclude_ids=("agent-workflow-boundary",)), [])

    def test_changed_revision_requires_rebuild(self):
        original = rebuild_index(self.db, self.index, self.embedder)
        self.update_question("agent-tool-executor", objective="新的工具执行与回执规则")
        with self.assertRaisesRegex(IndexUnavailable, "不一致"):
            self.search()
        changed = next(row for row in list_questions(self.db) if row["id"] == "agent-tool-executor")
        self.embedder.vectors[document_text(changed)] = (8, 6)
        replaced = rebuild_index(self.db, self.index, self.embedder)
        self.assertNotEqual(replaced["index_version"], original["index_version"])
        self.assertEqual(self.search()[0]["question"]["revision"], changed["revision"])

    def test_model_or_approved_membership_change_rejects_stale_index(self):
        rebuild_index(self.db, self.index, self.embedder)
        self.embedder.model_id = "fixture-2"
        with self.assertRaisesRegex(IndexUnavailable, "不一致"):
            self.search()
        self.embedder.model_id = "fixture-1"
        self.update_question("rag-cosine-faiss", status="retired")
        with self.assertRaisesRegex(IndexUnavailable, "不一致"):
            self.search()

    def test_invalid_index_is_rejected_before_native_deserialization(self):
        with self.assertRaisesRegex(IndexUnavailable, "先重建"):
            self.search()
        manifest = rebuild_index(self.db, self.index, self.embedder)
        file = self.index / manifest["index_version"] / "questions.faiss"
        file.write_bytes(b"corrupted")
        with patch("app.kb.retrieval.faiss.deserialize_index") as deserialize:
            with self.assertRaisesRegex(IndexUnavailable, "校验失败"):
                self.search()
            deserialize.assert_not_called()
        (self.index / "current").write_text("../unexpected", encoding="ascii")
        with self.assertRaisesRegex(IndexUnavailable, "指针无效"):
            self.search()

    def test_failed_publication_keeps_previous_generation(self):
        manifest = rebuild_index(self.db, self.index, self.embedder)
        with patch("app.kb.retrieval.os.replace", side_effect=OSError("simulated failure")):
            with self.assertRaisesRegex(OSError, "simulated failure"):
                rebuild_index(self.db, self.index, self.embedder)
        self.assertEqual(self.search()[0]["index_version"], manifest["index_version"])

    def test_zero_vectors_are_rejected_without_publishing(self):
        texts = list(self.embedder.vectors)
        self.embedder.vectors[texts[0]] = (0, 0)
        with self.assertRaisesRegex(ValueError, "无法归一化"):
            rebuild_index(self.db, self.index, self.embedder)
        self.assertFalse((self.index / "current").exists())
