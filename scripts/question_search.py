"""Prepare a local model, rebuild the public index offline, or inspect ranked candidates."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from app.kb.embedding import DOWNLOAD_URLS, LocalChineseEmbedder, prepare_model
from app.kb.retrieval import rebuild_index, search_questions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "question_bank.db")
    parser.add_argument("--index-dir", type=Path, default=ROOT / "data" / "index" / "questions")
    parser.add_argument("--model-dir", type=Path, default=ROOT / "data" / "index" / "bge-small-zh-v1.5")
    actions = parser.add_subparsers(dest="action", required=True)
    download = actions.add_parser("prepare-model", help="download and verify public model assets")
    download.add_argument("--source", choices=DOWNLOAD_URLS, default="huggingface")
    actions.add_parser("rebuild", help="stop interview service and seed imports before rebuilding")
    search = actions.add_parser("search")
    search.add_argument("--dimension", required=True, choices=("agent", "rag", "backend"))
    search.add_argument("--difficulty", required=True, choices=("basic", "applied"))
    search.add_argument("--objective", required=True)
    search.add_argument("--jd", default="")
    search.add_argument("--skill", action="append", default=[])
    search.add_argument("--exclude", action="append", default=[], metavar="QUESTION_ID")
    search.add_argument("--top-k", type=int, default=3)
    args = parser.parse_args()
    try:
        if args.action == "prepare-model":
            prepare_model(args.model_dir, args.source)
            result = {"model_ready": True}
        else:
            embedder = LocalChineseEmbedder(args.model_dir)
            if args.action == "rebuild":
                result = rebuild_index(args.db, args.index_dir, embedder)
            else:
                result = search_questions(args.db, args.index_dir, embedder, dimension=args.dimension,
                                          difficulty=args.difficulty, objective=args.objective, jd=args.jd,
                                          skills=args.skill, exclude_ids=args.exclude, top_k=args.top_k)
    except (OSError, ValueError, sqlite3.Error, RuntimeError) as exc:
        print(f"题库检索失败：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
