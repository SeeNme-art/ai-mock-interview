"""Run the fixed synthetic M5 sample once through the real gateway and a durable budget."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import sys
from time import monotonic

from evaluate_offline import DATASET, ROOT, SEED, load_samples, validate_labels
from app.config import Settings
from app.db.business import SessionError
from app.db.sqlite import canonical_json
from app.kb.seed import read_seed
from app.schemas.materials import Confirmation, MaterialInput
from app.services.answers import ExchangeRecord
from app.services.budget import PriceSnapshot, configure_budget, read_budget
from app.services.evaluation import EvaluationContext, score_context
from app.services.first_question import accept_start
from app.services.materials import confirm_material, start_draft
from app.services.model_gateway import DeepSeekGateway
from app.services.model_steps import InterviewModel
from app.services.planning import PlanChanges, rebuild_plan
from app.services.probing import ProbeContext, _decide

GATES = DATASET.parent / "m5-release-gates.json"
DEFAULT_RUN = ROOT / "data" / "experiments" / "EXP-005-live"
STOP_CODES = {"CONFIGURATION_ERROR", "BUDGET_EXHAUSTED", "USAGE_OVER_PLAN", "INPUT_TOO_LARGE"}


class CaseGateway:
    def __init__(self, gateway, case_id: str):
        self.gateway = gateway
        self.case_id = case_id

    def invoke_json(self, session_id, operation_id, step_key, **kwargs):
        return self.gateway.invoke_json(session_id, operation_id, f"{self.case_id}-{step_key}", **kwargs)


def _fixture(case: dict, bank: dict) -> tuple[ProbeContext, EvaluationContext]:
    question = bank.get(case["question_id"])
    prior = [*case["prior_exchanges"], {"question": case["question"], "answer": case["answer"]}]
    exchanges = tuple(ExchangeRecord(f"e{round_no}", round_no, pair["question"], pair["answer"])
                      for round_no, pair in enumerate(prior))
    refs = question.source_refs if question else []
    points = question.answer_points if question else []
    assessment = {
        "rubric_version": "knowledge-v1" if case["task_type"] == "knowledge" else "project-v1",
        "applicable_dimensions": list(case["expected"]["score_ranges"]),
        "answer_points": points,
        "acceptable_alternatives": question.acceptable_alternatives if question else [],
        "common_errors": question.common_errors if question else [],
    }
    probe = ProbeContext(case["id"], case["question"], exchanges, tuple(pair["question"] for pair in prior), tuple(points))
    evaluate = EvaluationContext(case["id"], case["id"], case["task_type"], exchanges,
                                 canonical_json({"objective": case["question"], "confirmed_material": case["material"]}),
                                 canonical_json(assessment), canonical_json([{"id": ref} for ref in refs]))
    return probe, evaluate


def _setup(directory: Path, limit: int, seed) -> dict:
    from app.kb.store import import_seed

    directory.mkdir(parents=True, exist_ok=False)
    path = directory / "meter.db"
    bank_path = directory / "bank.db"
    import_seed(bank_path, seed)
    material = MaterialInput()
    draft = start_draft(material, "form")
    session = confirm_material(path, draft.id, "m5-confirm", Confirmation(material=material, mode="knowledge_only"),
                               draft=draft)
    configure_budget(path, session, hard_limit_nanos=limit)
    plan = rebuild_plan(path, bank_path, session, 1, "m5-plan", PlanChanges())
    operation, _ = accept_start(path, session, plan["revision"], "m5-start")
    return {"session": session, "operation": operation}


def _model_result(case, bank, model, session, operation):
    probe, evaluation = _fixture(case, bank)
    stop = None

    def call(function, context):
        nonlocal stop
        try:
            return function(session, operation, context)
        except SessionError as exc:
            if exc.code in STOP_CODES:
                stop = exc.code
            raise

    start = monotonic()
    try:
        proposal, reason, probe_error = _decide(probe, lambda context: call(model.propose, context))
    except SessionError as exc:
        if exc.code not in STOP_CODES:
            raise
        proposal, reason, probe_error = None, "stopped", exc.code
    probe_time = round(monotonic() - start, 3)
    decision = proposal.should_probe if proposal is not None else False
    start = monotonic()
    try:
        score, score_error = (None, stop) if stop else score_context(
            evaluation, lambda context: call(model.evaluate, context))
    except SessionError as exc:
        if exc.code not in STOP_CODES:
            raise
        score, score_error = None, exc.code
    duration = round(monotonic() - start, 3)
    values = dict(score.dimension_values) if score else {}
    expected = case["expected"]
    return {
        "id": case["id"], "task_type": case["task_type"], "stop": stop,
        "probe": {"valid": proposal is not None or reason == "round_limit",
                  "acceptable": (proposal is not None or reason == "round_limit") and decision in expected["probe"],
                  "reason": reason, "error": probe_error, "elapsed_s": probe_time,
                  "output": proposal.model_dump(mode="json") if proposal is not None else None},
        "score": {"valid": score is not None,
                  "acceptable": bool(values) and all(low <= values.get(key, -1) <= high
                                                      for key, (low, high) in expected["score_ranges"].items()),
                  "error": score_error, "elapsed_s": duration,
                  "output": score.assessment.model_dump(mode="json") if score else None},
    }


def _write(path: Path, value: dict):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def run(split: str, directory: Path, settings: Settings, *, transport=None) -> dict:
    gates = json.loads(GATES.read_text(encoding="utf-8"))
    limits = gates["trial"]
    samples, labels = load_samples(DATASET)
    seed = read_seed(SEED)
    bank = {question.id: question for question in seed.questions if question.status == "verified"}
    validate_labels(samples, bank)
    cases = samples["core"]
    price = PriceSnapshot(model=gates["model"], **gates["pricing"])
    highest = (limits["input_limit"] * price.input_nanos_per_token
               + limits["output_limit"] * price.output_nanos_per_token)
    planned = 2 * len(cases)
    if (planned > limits["max_requests"] or limits["max_requests"] > 40
            or highest * planned > limits["budget_nanos"] or limits["budget_nanos"] > 120_000_000):
        raise ValueError("试测计划超出已批准的预算或调用次数")
    fingerprint = {"labels": labels["core"], "price": asdict(price), "limits": limits,
                   "gate_version": gates["version"], "seed_sha256": sha256(SEED.read_bytes()).hexdigest(),
                   "gates_sha256": sha256(GATES.read_bytes()).hexdigest(),
                   "code_sha256": {path.name: sha256(path.read_bytes()).hexdigest() for path in (
                       Path(__file__), ROOT / "src/app/services/model_steps.py",
                       ROOT / "src/app/services/model_gateway.py", ROOT / "src/app/services/budget.py",
                       ROOT / "src/app/services/probing.py", ROOT / "src/app/services/scoring.py",
                       ROOT / "src/app/services/evaluation.py")}}
    path = directory / "meter.db"
    gateway = DeepSeekGateway(path, settings, price, max_input_tokens=limits["input_limit"],
                              **({"transport": transport} if transport else {}))
    if split == "debug":
        run_scope = _setup(directory, limits["budget_nanos"], seed)
        _write(directory / "scope.json", {**run_scope, "fingerprint": fingerprint})
    else:
        scope = json.loads((directory / "scope.json").read_text(encoding="utf-8"))
        debug = json.loads((directory / "debug.json").read_text(encoding="utf-8"))
        if scope["fingerprint"] != fingerprint or debug["stop"] is not None:
            raise ValueError("保留集需使用已经完成调试的固定配置")
        run_scope = scope

    # A partial or interrupted paid split remains inspectable and cannot be started again.
    marker = directory / f"{split}.started"
    marker.touch(exist_ok=False)
    session, operation = run_scope["session"], run_scope["operation"]
    rows = []
    stop = None
    for case in (row for row in cases if row["split"] == split):
        model = InterviewModel(CaseGateway(gateway, case["id"]), max_output_tokens=limits["output_limit"], retries=0)
        row = _model_result(case, bank, model, session, operation)
        _write(directory / f"{case['id']}.json", row)
        rows.append(row)
        if row["stop"]:
            stop = row["stop"]
            break
    budget = read_budget(path, session)
    result = {"split": split, "fingerprint": fingerprint, "cases": rows, "stop": stop,
              "metrics": {f"{section}_{metric}": sum(row[section][metric] for row in rows)
                          for section in ("probe", "score") for metric in ("valid", "acceptable")},
              "ledger": {name: budget[name] for name in ("hard_limit_nanos", "spent_nanos", "reserved_nanos")},
              "attempts": [{key: row[key] for key in ("step_key", "status", "estimated_input_tokens", "reserved_nanos",
                                                     "actual_input_tokens", "actual_output_tokens", "actual_nanos")}
                           for row in budget["attempts"]]}
    _write(directory / f"{split}.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("split", choices=("debug", "holdout"))
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--approved", action="store_true", help="explicitly allow the budgeted synthetic calls")
    args = parser.parse_args()
    if not args.approved:
        parser.error("真实试测需要 --approved")
    try:
        result = run(args.split, args.run_dir, Settings())
    except (ValueError, OSError) as exc:
        print(f"试测停止：{getattr(exc, 'code', type(exc).__name__)}", file=sys.stderr)
        return 1
    print(json.dumps({"split": result["split"], "metrics": result["metrics"], "ledger": result["ledger"],
                      "attempt_count": len(result["attempts"]), "stop": result["stop"]}, ensure_ascii=False))
    return int(result["stop"] is not None)


if __name__ == "__main__":
    raise SystemExit(main())
