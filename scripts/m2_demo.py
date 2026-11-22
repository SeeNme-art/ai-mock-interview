"""Run reproducible M2 interviews with synthetic material and an offline model transport."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from app.config import Settings
from app.kb.seed import read_seed
from app.kb.store import import_seed
from app.schemas.materials import Confirmation, MaterialInput, ProjectInput
from app.schemas.planning import PlanChanges
from app.schemas.scoring import DIMENSIONS
from app.services.advancement import accept_advance
from app.services.answers import accept_answer
from app.services.budget import PriceSnapshot, configure_budget, read_budget
from app.services.evaluation import accept_evaluation
from app.services.first_question import accept_start, read_state
from app.services.materials import confirm_material, start_draft
from app.services.model_gateway import DeepSeekGateway
from app.services.model_steps import InterviewModel
from app.services.planning import rebuild_plan
from app.services.reports import generate_report
from app.workflow.interview import resume_interview, run_advance, run_evaluation, run_first_question

MODEL = "deepseek-flash"
PRICE = PriceSnapshot(MODEL, "m2-synthetic-v1", 10, 20, 2)
CASES = ("knowledge", "mixed", "budget", "provider-failure")
ANSWER = "我先保存处理条件，再用固定查询和对照样本复跑。"


def _fixture_transport(case: str):
    def send(body: bytes, _key: str, _timeout: int) -> tuple[int, bytes]:
        if case in ("provider-failure", "budget"):
            return 503, b""
        request = json.loads(body)
        context = json.loads(request["messages"][1]["content"])
        last = context["exchanges"][-1]
        evidence = [{"exchange_id": last["id"], "quote": last["answer"]}]
        if "rubric" in context:
            rubric = context["rubric"]
            task = context["task_type"]
            dimensions = set(rubric["applicable_dimensions"])
            result = {"task_type": task, "rubric_version": rubric["rubric_version"], "dimensions": {
                key: {"applicable": key in dimensions, "value": 3 if key in dimensions else None,
                      "reason": "固定评分仅用于验证问答、证据与报告贯通",
                      "evidence": evidence if key in dimensions else []}
                for key in DIMENSIONS[task]
            }}
        else:
            should_probe = last["round_no"] < 2 and context["asked_questions"] == [
                item["question"] for item in context["exchanges"]
            ]
            result = {"should_probe": should_probe, "reason": "固定决定用于验证轮次"}
            if should_probe:
                prompts = ("在这项检查中，你具体记录了哪些输入？", "如果对照出现相反结果，你会怎样定位原因？")
                result.update(purpose="deepen", probe_question=prompts[last["round_no"]], evidence_refs=evidence)
        response = {"model": MODEL, "choices": [
            {"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(result, ensure_ascii=False)}},
        ], "usage": {"prompt_tokens": len(body) // 3, "completion_tokens": 96,
                     "total_tokens": len(body) // 3 + 96}}
        return 200, json.dumps(response, ensure_ascii=False).encode()
    return send


def run_case(case: str, folder: Path) -> dict:
    bank, db = folder / "bank.db", folder / "session.db"
    import_seed(bank, read_seed(ROOT / "data" / "seeds" / "knowledge_questions.json"))
    projects = [ProjectInput(action="用 SQLite 保存检索样本", result="使用固定问题对比召回结果")] if case == "mixed" else []
    material = MaterialInput(jd_text="开发并验证智能体与检索服务", skills=["SQLite", "检索评测"], projects=projects)
    draft = start_draft(material, "form")
    session_id = confirm_material(db, draft.id, f"confirm-{case}",
                                  Confirmation(material=material, mode="mixed" if projects else "knowledge_only"), draft=draft)
    plan = rebuild_plan(db, bank, session_id, 1, f"plan-{case}", PlanChanges())
    configure_budget(db, session_id, hard_limit_nanos=1 if case == "budget" else 10_000_000,
                     closing_reserve_nanos=0 if case == "budget" else 100_000)
    settings = Settings(_env_file=None, deepseek_api_key="offline-demo", llm_model_main=MODEL, observe_store_io=False)
    model = InterviewModel(DeepSeekGateway(db, settings, PRICE, transport=_fixture_transport(case)), max_output_tokens=512)
    start, _ = accept_start(db, session_id, plan["revision"], f"start-{case}")
    run_first_question(db, bank, session_id, start)
    for number in range(1, plan["question_count"] + 1):
        state = read_state(db, session_id)
        for round_no in range(3):
            turn = state["current"]
            receipt = accept_answer(db, session_id, turn["turn_id"], turn["exchange_id"], state["revision"],
                                    f"answer-{number}-{round_no}", ANSWER)
            state = resume_interview(db, bank, session_id, operation_id=receipt["operation_id"], model=model)
            if state["operation"]["decision"] == "score_pending":
                break
        score, _ = accept_evaluation(db, session_id, receipt["operation_id"], state["revision"], f"score-{number}")
        run_evaluation(db, session_id, score, model=model)
        state = read_state(db, session_id)
        if state["phase"] == "FINISHED":
            break
        advance, _ = accept_advance(db, session_id, state["revision"], f"advance-{number}")
        run_advance(db, bank, session_id, advance)

    report = generate_report(db, session_id)
    budget = read_budget(db, session_id)
    return {
        "case": case, "synthetic": True, "end_reason": report["end_reason"], "report_kind": report["kind"],
        "counts": report["counts"], "category_counts": report["category_counts"],
        "knowledge_score": report["knowledge_score"], "project_score": report["project_score"],
        "probe_counts": [item["probe_count"] for item in report["items"]],
        "estimated_fixture_spend_nanos": budget["spent_nanos"], "reserved_nanos": budget["reserved_nanos"],
        "attempts": len(budget["attempts"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=(*CASES, "all"), default="all")
    args = parser.parse_args()
    summaries = []
    for case in CASES if args.case == "all" else (args.case,):
        with TemporaryDirectory(prefix="interview-m2-") as location:
            summaries.append(run_case(case, Path(location)))
    print(json.dumps(summaries, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
