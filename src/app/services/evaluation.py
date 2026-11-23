"""Validate and persist one evaluation against the frozen question and answers."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from typing import Callable, TypeVar
from uuid import uuid4

from pydantic import ValidationError

from app.db.business import (
    OWNER_ID, SessionError, check_business_schema, ensure_session_idle, find_operation, find_replay,
    find_session, upgrade_business, validate_request_key,
)
from app.db.sqlite import canonical_json, connect, sha256
from app.schemas.scoring import (
    Assessment, DIMENSIONS, DimensionAssessment, EvidenceRef, ModelAssessment, ScoringInput, TaskType, Uncertainty,
)
from app.services.answers import ExchangeRecord, read_exchanges
from app.services.question_delivery import finish_if_all_closed
from app.services.scoring import AssessmentOutputError, ScoredAssessment, score_assessment

EVALUATE = "evaluate_answer"
_SAFE_FIELDS = (set(Assessment.model_fields) | set(DimensionAssessment.model_fields) |
                set(EvidenceRef.model_fields) | set(Uncertainty.model_fields) |
                set(DIMENSIONS["knowledge"]) | set(DIMENSIONS["project"]))


@dataclass(frozen=True)
class EvaluationContext:
    turn_id: str
    plan_item_id: str
    task_type: TaskType
    exchanges: tuple[ExchangeRecord, ...] = field(repr=False)
    question_snapshot: str = field(repr=False)
    rubric_snapshot: str = field(repr=False)
    source_snapshot: str = field(repr=False)


DemoEvaluator = Callable[[EvaluationContext], object]
ScoringSchema = TypeVar("ScoringSchema", bound=ScoringInput)


def _context(db: sqlite3.Connection, session_id: str, answer_operation: sqlite3.Row) -> EvaluationContext:
    refs = json.loads(answer_operation["result_refs_json"] or "{}")
    if answer_operation["status"] != "succeeded" or refs.get("decision") != "score_pending":
        raise SessionError("ILLEGAL_ACTION", "该回答尚未进入待评分状态")
    turn = db.execute("""SELECT t.* FROM turn t JOIN plan_item p ON p.id=t.plan_item_id
                        WHERE t.id=? AND t.session_id=? AND t.state='processing' AND p.status='active'""",
                      (refs["turn_id"], session_id)).fetchone()
    if turn is None:
        raise SessionError("ILLEGAL_ACTION", "题目已收尾或当前不能评分")
    exchanges = read_exchanges(db, turn["id"])
    if (not exchanges or refs.get("exchange_id") != exchanges[-1].id
            or [row.round_no for row in exchanges] != list(range(turn["probe_count"] + 1))
            or any(not row.answer or not row.answer.strip() for row in exchanges)):
        raise SessionError("ILLEGAL_ACTION", "评分需要匹配本题的完整已提交问答")
    return evaluation_context(turn, exchanges)


def evaluation_context(turn: sqlite3.Row, exchanges: tuple[ExchangeRecord, ...]) -> EvaluationContext:
    return EvaluationContext(turn["id"], turn["plan_item_id"], turn["task_type"], exchanges,
                             turn["question_snapshot_json"], turn["rubric_snapshot_json"], turn["source_snapshot_json"])


def accept_evaluation(path: Path, session_id: str, answer_operation_id: str,
                      expected_revision: int, key: str) -> tuple[str, bool]:
    validate_request_key(key)
    if type(expected_revision) is not int or expected_revision < 1:
        raise SessionError("INVALID_INPUT", "会话 revision 须为正整数")
    if not path.is_file():
        raise SessionError("RESOURCE_NOT_FOUND", "找不到待评分会话")
    request = {"session_id": session_id, "answer_operation_id": answer_operation_id, "expected_revision": expected_revision}
    digest = sha256(canonical_json(request))
    with closing(connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        version = check_business_schema(db)
        session = find_session(db, session_id)
        replay = find_replay(db, EVALUATE, key, digest) if version >= 4 else None
        if replay is not None:
            return replay["id"], False
        revision = session["revision"]
        if revision != expected_revision:
            raise SessionError("STALE_REVISION", "会话进度已更新，请刷新后再评分", current_revision=revision)
        if version < 3 or session["phase"] != "ACTIVE":
            raise SessionError("ILLEGAL_ACTION", "当前会话不能提交单题评分", current_revision=revision)
        ensure_session_idle(db, session)
        source = find_operation(db, session_id, answer_operation_id, "answer")
        if source["expected_epoch"] != session["control_epoch"]:
            raise SessionError("ILLEGAL_ACTION", "回答操作已过期，不能用于提交评分")
        context = _context(db, session_id, source)
        if version == 3:
            upgrade_business(db, path, 4)

        now = datetime.now(UTC).isoformat()
        evaluation_id, operation_id = uuid4().hex, uuid4().hex
        epoch = session["control_epoch"] + 1
        db.execute("""INSERT INTO evaluation(id,turn_id,status,model_context_json,created_at,updated_at)
                      VALUES (?,?,'pending','{}',?,?)""", (evaluation_id, context.turn_id, now, now))
        db.execute("""INSERT INTO operation(id,session_id,owner_id,kind,idempotency_key,request_hash,expected_epoch,
                      status,safe_request_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,'queued',?,?,?)""",
                   (operation_id, session_id, OWNER_ID, EVALUATE, key, digest, epoch,
                    canonical_json({**request, "evaluation_id": evaluation_id}), now, now))
        db.execute("UPDATE interview_session SET revision=revision+1,control_epoch=? WHERE id=?", (epoch, session_id))
        return operation_id, True


def _load_work(db: sqlite3.Connection, session_id: str, operation_id: str, epoch: int) -> tuple[EvaluationContext | None, dict]:
    check_business_schema(db)
    session = find_session(db, session_id)
    operation = find_operation(db, session_id, operation_id, EVALUATE)
    saved = db.execute("SELECT result_json FROM operation_step WHERE operation_id=? AND step_key=?",
                       (operation_id, EVALUATE)).fetchone()
    if saved is not None:
        return None, json.loads(saved["result_json"])
    if (session["phase"] != "ACTIVE" or operation["status"] != "queued"
            or operation["expected_epoch"] != epoch or session["control_epoch"] != epoch):
        raise SessionError("ILLEGAL_ACTION", "评分操作已失效，请读取当前进度")
    request = json.loads(operation["safe_request_json"])
    answer_op = find_operation(db, session_id, request["answer_operation_id"], "answer")
    context = _context(db, session_id, answer_op)
    pending = db.execute("SELECT 1 FROM evaluation WHERE id=? AND turn_id=? AND status='pending'",
                         (request["evaluation_id"], context.turn_id)).fetchone()
    if pending is None:
        raise SessionError("ILLEGAL_ACTION", "待评分记录与当前题目不一致")
    return context, {"turn_id": context.turn_id, "evaluation_id": request["evaluation_id"]}


def _validate_schema(schema: type[ScoringSchema], value: object) -> ScoringSchema:
    try:
        return schema.model_validate(value)
    except ValidationError as exc:
        error = exc.errors(include_input=False, include_context=False, include_url=False)[0]
        field = ".".join(part if part in _SAFE_FIELDS else "*" for part in error["loc"]) or "assessment"
        reason = {"missing": "MISSING_FIELD", "extra_forbidden": "UNEXPECTED_FIELD"}.get(error["type"], "INVALID_FIELD")
        if reason == "INVALID_FIELD" and field.endswith(".value"):
            reason = "INVALID_SCORE"
        raise AssessmentOutputError("schema", reason, field) from None


def complete_model_assessment(context: EvaluationContext, value: object) -> dict:
    model = _validate_schema(ModelAssessment, value)
    rubric = json.loads(context.rubric_snapshot)
    active = set(rubric["applicable_dimensions"])
    missing = sorted(active - model.dimensions.keys())
    unexpected = sorted(model.dimensions.keys() - active)
    if missing:
        raise AssessmentOutputError("schema", "MISSING_FIELD", f"dimensions.{missing[0]}")
    if unexpected:
        raise AssessmentOutputError("schema", "UNEXPECTED_FIELD", f"dimensions.{unexpected[0]}")
    return {
        "task_type": context.task_type,
        "rubric_version": rubric["rubric_version"],
        "dimensions": {
            name: ({"applicable": True, **model.dimensions[name].model_dump(mode="json")} if name in active else
                   {"applicable": False, "value": None, "reason": "本题未考察此维度。", "evidence": []})
            for name in DIMENSIONS[context.task_type]
        },
        "uncertainties": [item.model_dump(mode="json") for item in model.uncertainties],
    }


def validate_evaluation_response(context: EvaluationContext, value: object) -> ScoredAssessment:
    rubric = json.loads(context.rubric_snapshot)
    answers = {item.id: item.answer for item in context.exchanges if item.answer is not None}
    source_ids = {source["id"] for source in json.loads(context.source_snapshot)}
    assessment = _validate_schema(Assessment, value)
    return score_assessment(assessment, task_type=context.task_type, rubric_version=rubric["rubric_version"],
                            applicable_dimensions=rubric["applicable_dimensions"], answers=answers, source_refs=source_ids)


def score_context(context: EvaluationContext, evaluator: DemoEvaluator, *,
                  diagnostic: dict[str, str] | None = None) -> tuple[ScoredAssessment | None, str | None]:
    try:
        result = validate_evaluation_response(context, evaluator(context))
    except AssessmentOutputError as exc:
        if diagnostic is not None:
            diagnostic.update(exc.diagnostic)
        return None, "OUTPUT_INVALID"
    except SessionError as exc:
        if exc.code == "OUTPUT_INVALID" and diagnostic is not None:
            diagnostic.update(AssessmentOutputError("gateway", "INVALID_JSON", "response").diagnostic)
        if exc.code in {"BUDGET_EXHAUSTED", "TURN_BUDGET_EXHAUSTED", "MODEL_UNAVAILABLE", "MODEL_REQUEST_REJECTED",
                        "OUTPUT_INVALID", "CALL_ALREADY_RECORDED", "CLOUD_BUDGET_MISMATCH", "BUDGET_NOT_CONFIGURED",
                        "INPUT_TOO_LARGE", "USAGE_OVER_PLAN"}:
            return None, exc.code
        raise
    except OSError:
        return None, "MODEL_UNAVAILABLE"
    except ValueError:
        return None, "OUTPUT_INVALID"
    return result, None


def store_evaluation(db: sqlite3.Connection, context: EvaluationContext, evaluation_id: str,
                     result: ScoredAssessment | None, error_code: str | None, now: str, *,
                     evaluator_version: str | None = "local-demo-v1",
                     diagnostic: dict[str, str] | None = None) -> tuple[str, str]:
    question = json.loads(context.question_snapshot)
    rubric = json.loads(context.rubric_snapshot)
    version_context = canonical_json({"evaluator": evaluator_version, "rubric_version": rubric["rubric_version"],
                                      "question_id": question.get("id"), "question_revision": question.get("revision"),
                                      **({"validation_failure": diagnostic} if diagnostic else {})})
    status = "ready" if result is not None else "failed"
    normalized = canonical_json(result.assessment.model_dump(mode="json")) if result is not None else None
    db.execute("""UPDATE evaluation SET status=?,assessment_json=?,item_score=?,error_code=?,model_context_json=?,updated_at=?
                  WHERE id=? AND turn_id=? AND status='pending'""",
               (status, normalized, result.item_score if result is not None else None, error_code,
                version_context, now, evaluation_id, context.turn_id))
    return status, version_context


def process_evaluation(path: Path, session_id: str, operation_id: str, epoch: int, evaluator: DemoEvaluator | None, *,
                       model_version: str = "local-demo-v1") -> dict:
    with closing(connect(path, readonly=True)) as db, db:
        db.execute("BEGIN")
        context, refs = _load_work(db, session_id, operation_id, epoch)
    if context is None:
        return {"turn_id": refs["turn_id"], "evaluation_id": refs["evaluation_id"]}
    if evaluator is None:
        raise SessionError("CONFIGURATION_ERROR", "本批需显式提供本地评分样例")
    diagnostic: dict[str, str] = {}
    result, error_code = score_context(context, evaluator, diagnostic=diagnostic)

    with closing(connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        current, current_refs = _load_work(db, session_id, operation_id, epoch)
        if current is None:
            return {"turn_id": current_refs["turn_id"], "evaluation_id": current_refs["evaluation_id"]}
        if current != context or current_refs != refs:
            raise SessionError("ILLEGAL_ACTION", "评分依据已更新，请读取当前进度")
        now = datetime.now(UTC).isoformat()
        question = json.loads(context.question_snapshot)
        eval_status, version_context = store_evaluation(db, context, refs["evaluation_id"], result, error_code, now,
                                                       evaluator_version=model_version, diagnostic=diagnostic)
        op_status = "succeeded" if result is not None else "failed"
        refs_json = canonical_json({**refs, "evaluation_status": eval_status})
        db.execute("UPDATE turn SET state='closed',closed_at=? WHERE id=?", (now, context.turn_id))
        db.execute("UPDATE plan_item SET status='closed',outcome='answered' WHERE id=?", (context.plan_item_id,))
        if error_code == "BUDGET_EXHAUSTED":
            db.execute("""UPDATE plan_item SET status='closed',outcome='unanswered'
                          WHERE session_id=? AND plan_version=? AND status='pending'""", (session_id, question["plan_version"]))
            db.execute("""UPDATE interview_session SET phase='FINISHED',end_reason='budget_exhausted',ended_at=?
                          WHERE id=? AND phase='ACTIVE'""", (now, session_id))
        else:
            finish_if_all_closed(db, session_id, question["plan_version"], now)
        db.execute("INSERT INTO operation_step VALUES (?,?,?,?,1,?)", (operation_id, EVALUATE, op_status, refs_json, version_context))
        db.execute("UPDATE operation SET status=?,result_refs_json=?,error_code=?,updated_at=? WHERE id=?",
                   (op_status, refs_json, error_code, now, operation_id))
        db.execute("UPDATE interview_session SET revision=revision+1 WHERE id=?", (session_id,))
        return refs
