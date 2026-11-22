"""Check synthetic M4 labels and measure material parsing and local retrieval."""
from __future__ import annotations

import argparse
from collections import defaultdict
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unicodedata import normalize
from xml.sax.saxutils import escape
from zipfile import ZipFile

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from app.kb.seed import read_seed
from app.kb.store import import_seed, list_questions
from app.schemas.scoring import DIMENSIONS
from app.services.document_text import DocumentError, extract_document
from app.services.materials import preview_text

DATASET = ROOT / "data" / "evals" / "m4"
SEED = ROOT / "data" / "seeds" / "knowledge_questions.json"


def load_samples(directory: Path) -> tuple[dict, dict]:
    sets = {}
    versions = {}
    for name in ("core", "retrieval", "materials"):
        raw = (directory / f"{name}.json").read_bytes()
        data = json.loads(raw)
        sets[name] = data["cases"]
        versions[name] = {"version": data["version"], "sha256": sha256(raw).hexdigest()}
    return sets, versions


def validate_labels(samples: dict, questions: dict) -> None:
    ids = set()
    families = {}
    for cases in samples.values():
        for case in cases:
            case_id, split = case["id"], case["split"]
            if case_id in ids or split not in {"debug", "holdout"}:
                raise ValueError(f"{case_id}: 样本编号重复或分组无效")
            ids.add(case_id)
            if families.setdefault(case["family"], split) != split:
                raise ValueError(f"{case_id}: 同一家族分布到了两个集合")

    for case in samples["core"]:
        expected = case["expected"]
        ranges = expected["score_ranges"]
        if not ranges or ranges.keys() - set(DIMENSIONS[case["task_type"]]):
            raise ValueError(f"{case['id']}: 分数区间必须使用对应题型的维度")
        if any(not 0 <= low <= high <= 4 for low, high in ranges.values()):
            raise ValueError(f"{case['id']}: 分数区间超出 0–4")
        if not expected["evidence"] or expected["evidence"] not in case["answer"]:
            raise ValueError(f"{case['id']}: 主回答没有标注引用")
        if len(case["prior_exchanges"]) > 2 or (len(case["prior_exchanges"]) == 2 and True in expected["probe"]):
            raise ValueError(f"{case['id']}: 超出两轮追问限制")
        if case["question_id"] is not None and case["question_id"] not in questions:
            raise ValueError(f"{case['id']}: 引用了未知的审核题")

    for case in samples["retrieval"]:
        eligible = {key for key, question in questions.items()
                    if question.dimension == case["direction"] and question.difficulty == case["difficulty"]}
        relevant, excluded = set(case["relevant_ids"]), set(case["exclude_ids"])
        if relevant & excluded or (relevant | excluded) - eligible or not eligible:
            raise ValueError(f"{case['id']}: 检索标注违反候选约束")
        if not relevant and eligible - excluded:
            raise ValueError(f"{case['id']}: 无候选样本仍有可用题")

    for case in samples["materials"]:
        columns, snippets = case["columns"], case["expected_snippets"]
        if not columns or not snippets or any(not snippet for snippet in snippets):
            raise ValueError(f"{case['id']}: 材料正文与核对片段不能为空")
        if case["format"] != "pdf" and len(columns) != 1:
            raise ValueError(f"{case['id']}: 只有 PDF 夹具支持多栏")


def _docx(paragraphs: list[str]) -> bytes:
    out = BytesIO()
    content = "".join(f"<w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p>" for text in paragraphs)
    with ZipFile(out, "w") as package:
        package.writestr("[Content_Types].xml", (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
            'officedocument.wordprocessingml.document.main+xml"/></Types>'
        ))
        package.writestr("_rels/.rels", (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/officeDocument" Target="word/document.xml"/></Relationships>'
        ))
        package.writestr("word/document.xml", (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{content}</w:body></w:document>"
        ))
    return out.getvalue()


def _pdf(columns: list[list[str]]) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }),
        }),
    })
    commands = []
    for column, lines in enumerate(columns):
        for row, line in enumerate(lines):
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            commands.append(f"BT /F1 10 Tf {35 + 275 * column} {780 - 20 * row} Td ({escaped}) Tj ET")
    stream = DecodedStreamObject()
    stream.set_data("\n".join(commands).encode("ascii"))
    page.replace_contents(stream)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def measure_materials(cases: list[dict]) -> dict:
    rows = []
    for case in cases:
        columns, kind = case["columns"], case["format"]
        error = None
        try:
            if kind == "pdf":
                text = extract_document(_pdf(columns), "pdf")
            elif kind == "docx":
                text = extract_document(_docx(columns[0]), "docx")
            elif kind == "text":
                text = preview_text("\n".join(columns[0]))
            else:
                raise ValueError(f"{case['id']}: 未知材料类型")
        except DocumentError as exc:
            text, error = "", exc.code
        missing = [snippet for snippet in case["expected_snippets"] if normalize("NFKC", snippet) not in text]
        rows.append({"id": case["id"], "format": kind, "columns": len(columns), "passed": not missing,
                     "missing_snippets": missing, "error": error})
    return {"passing": sum(row["passed"] for row in rows), "samples": len(rows), "cases": rows}


def summarize_rankings(rows: list[dict], ranking: str) -> dict:
    positives = [row for row in rows if row["relevant_ids"]]
    negatives = [row for row in rows if not row["relevant_ids"]]
    hit = recall = reciprocal = 0.0
    for row in positives:
        relevant = set(row["relevant_ids"])
        positions = [position for position, question_id in enumerate(row[ranking][:3], 1) if question_id in relevant]
        hit += bool(positions)
        recall += len(positions) / len(relevant)
        reciprocal += 1 / positions[0] if positions else 0
    total = len(positives)
    return {"positives": total, "hit_at_3": hit / total if total else None,
            "recall_at_3": recall / total if total else None, "mrr_at_3": reciprocal / total if total else None,
            "no_candidate_samples": len(negatives),
            "no_candidate_correct": sum(not row[ranking] for row in negatives)}


def measure_retrieval(cases: list[dict], model_dir: Path) -> dict:
    from app.kb.embedding import LocalChineseEmbedder
    from app.kb.retrieval import rebuild_index, search_questions

    embedder = LocalChineseEmbedder(model_dir)
    rows = []
    grouped = defaultdict(list)
    with TemporaryDirectory(prefix="m4-eval-") as temp:
        db, index = Path(temp) / "questions.db", Path(temp) / "faiss"
        import_seed(db, read_seed(SEED))
        manifest = rebuild_index(db, index, embedder)
        for case in cases:
            filters = dict(dimension=case["direction"], difficulty=case["difficulty"], exclude_ids=case["exclude_ids"])
            baseline = list_questions(db, **filters)
            ranked = search_questions(db, index, embedder, **filters, objective=case["objective"],
                                      jd=case["jd"], skills=case["skills"], top_k=3)
            row = {"id": case["id"], "direction": case["direction"], "candidates": len(baseline),
                   "relevant_ids": case["relevant_ids"], "sqlite": [item["id"] for item in baseline[:3]],
                   "faiss": [item["question"]["id"] for item in ranked]}
            rows.append(row)
            grouped[case["direction"]].append(row)
    return {
        "embedding_model": manifest["embedding_model"], "index_sha256": manifest["index_sha256"],
        "seed_sha256": sha256(SEED.read_bytes()).hexdigest(), "query_version": manifest["query_version"],
        "comparison": {name: summarize_rankings(rows, name) for name in ("sqlite", "faiss")},
        "directions": {name: {method: summarize_rankings(group, method) for method in ("sqlite", "faiss")}
                       for name, group in grouped.items()},
        "cases": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", default="debug", choices=("debug", "holdout"))
    parser.add_argument("--model-dir", type=Path, help="opt in to local FAISS comparison using prepared assets")
    args = parser.parse_args()
    samples, versions = load_samples(DATASET)
    bank = {question.id: question for question in read_seed(SEED).questions if question.status == "verified"}
    validate_labels(samples, bank)
    selected = {name: [case for case in batch if case["split"] == args.split] for name, batch in samples.items()}
    result = {"split": args.split, "datasets": versions,
              "sample_counts": {name: len(batch) for name, batch in samples.items()},
              "core": {"selected": len(selected["core"]), "status": "labels_checked_only"},
              "materials": measure_materials(selected["materials"]),
              "retrieval": measure_retrieval(selected["retrieval"], args.model_dir) if args.model_dir else None,
              "paid_model_calls": 0}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(result["materials"]["passing"] != result["materials"]["samples"])


if __name__ == "__main__":
    raise SystemExit(main())
