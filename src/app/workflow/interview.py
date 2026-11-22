"""The CLI interview graph. Business operations remain the recovery source."""
from __future__ import annotations

from pathlib import Path
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context

from app.db.business import SessionError, operation_epoch
from app.services.advancement import ADVANCE, SKIP, STEP as ADVANCE_STEP, advance_plan
from app.services.evaluation import EVALUATE, DemoEvaluator, process_evaluation
from app.services.finalization import FINALIZE, finalize_interview
from app.services.first_question import prepare_first_question, read_state
from app.services.model_steps import PROMPT_VERSION, InterviewModel
from app.services.probing import DemoProposer, process_answer
from app.services.reports import generate_report


InterviewCommand = Literal["start", "answer", "evaluate_answer", "advance", "skip", "finalize"]


class InterviewState(TypedDict, total=False):
    session_id: str
    operation_id: str
    control_epoch: int
    turn_id: str | None
    exchange_id: str | None
    evaluation_id: str | None


def _run_interview(path: Path, initial: InterviewState, command: InterviewCommand, *,
                   bank: Path | None = None, proposer: DemoProposer | None = None,
                   evaluator: DemoEvaluator | None = None, model_version: str = "local-demo-v1") -> InterviewState:
    def prepare(state: InterviewState) -> dict:
        if bank is None:
            raise RuntimeError("首题节点缺少审核题库配置")
        return prepare_first_question(path, bank, state["session_id"], state["operation_id"], state["control_epoch"])

    def decide(state: InterviewState) -> dict:
        return process_answer(path, state["session_id"], state["operation_id"], state["control_epoch"], proposer,
                              model_version=model_version)

    def evaluate(state: InterviewState) -> dict:
        return process_evaluation(path, state["session_id"], state["operation_id"], state["control_epoch"], evaluator,
                                  model_version=model_version)

    def advance(state: InterviewState) -> dict:
        if bank is None:
            raise RuntimeError("推进节点缺少审核题库配置")
        return advance_plan(path, bank, state["session_id"], state["operation_id"], state["control_epoch"],
                            kind=SKIP if command == SKIP else ADVANCE)

    def finalize(state: InterviewState) -> dict:
        return finalize_interview(path, state["session_id"], state["operation_id"], state["control_epoch"], evaluator,
                                  model_version=model_version)

    builder = StateGraph(InterviewState)
    builder.add_node("prepare_first_question", prepare)
    builder.add_node("decide_follow_up", decide)
    builder.add_node(EVALUATE, evaluate)
    builder.add_node(ADVANCE_STEP, advance)
    builder.add_node(FINALIZE, finalize)
    builder.add_conditional_edges(START, lambda _: command,
                                  {"start": "prepare_first_question", "answer": "decide_follow_up",
                                   EVALUATE: EVALUATE, ADVANCE: ADVANCE_STEP, SKIP: ADVANCE_STEP, FINALIZE: FINALIZE})
    builder.add_edge("prepare_first_question", END)
    builder.add_edge("decide_follow_up", END)
    builder.add_edge(EVALUATE, END)
    builder.add_edge(ADVANCE_STEP, END)
    builder.add_edge(FINALIZE, END)
    with tracing_context(enabled=False):
        return builder.compile().invoke(initial)


def _run_operation(path: Path, session_id: str, operation_id: str, kind: InterviewCommand, *,
                   bank: Path | None = None, proposer: DemoProposer | None = None,
                   evaluator: DemoEvaluator | None = None, model: InterviewModel | None = None) -> InterviewState:
    if model is not None:
        if proposer is not None or evaluator is not None:
            raise SessionError("INVALID_INPUT", "样例和模型入口只能选一个")
        if kind == "answer":
            proposer = lambda context: model.propose(session_id, operation_id, context)
        elif kind in (EVALUATE, FINALIZE):
            evaluator = lambda context: model.evaluate(session_id, operation_id, context)
    initial = {"session_id": session_id, "operation_id": operation_id,
               "control_epoch": operation_epoch(path, session_id, operation_id, kind)}
    result = _run_interview(path, initial, kind, bank=bank, proposer=proposer, evaluator=evaluator,
                            model_version=PROMPT_VERSION if model is not None else "local-demo-v1")
    if kind == FINALIZE:
        generate_report(path, session_id)
    return result


def run_first_question(path: Path, bank: Path, session_id: str, operation_id: str) -> InterviewState:
    return _run_operation(path, session_id, operation_id, "start", bank=bank)


def run_answer(path: Path, session_id: str, operation_id: str, proposer: DemoProposer | None = None, *,
               model: InterviewModel | None = None) -> InterviewState:
    return _run_operation(path, session_id, operation_id, "answer", proposer=proposer, model=model)


def run_evaluation(path: Path, session_id: str, operation_id: str, evaluator: DemoEvaluator | None = None, *,
                   model: InterviewModel | None = None) -> InterviewState:
    return _run_operation(path, session_id, operation_id, EVALUATE, evaluator=evaluator, model=model)


def run_advance(path: Path, bank: Path, session_id: str, operation_id: str, *,
                kind: Literal["advance", "skip"] = ADVANCE) -> InterviewState:
    return _run_operation(path, session_id, operation_id, kind, bank=bank)


def run_finalize(path: Path, session_id: str, operation_id: str, evaluator: DemoEvaluator | None = None, *,
                 model: InterviewModel | None = None) -> InterviewState:
    return _run_operation(path, session_id, operation_id, FINALIZE, evaluator=evaluator, model=model)


def resume_interview(path: Path, bank: Path, session_id: str, *, expected_revision: int | None = None,
                     operation_id: str | None = None, proposer: DemoProposer | None = None,
                     evaluator: DemoEvaluator | None = None, model: InterviewModel | None = None) -> dict:
    """Resume only work already registered for this session."""
    if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1):
        raise SessionError("INVALID_INPUT", "会话 revision 须为正整数")
    if not path.is_file():
        raise SessionError("RESOURCE_NOT_FOUND", "找不到可访问的会话")
    state = read_state(path, session_id)
    revision = state["revision"]
    if expected_revision is not None and revision != expected_revision:
        raise SessionError("STALE_REVISION", "会话进度已更新，请刷新后再继续", current_revision=revision)
    current = state["operation"]
    if operation_id is not None and (current is None or current["operation_id"] != operation_id):
        raise SessionError("ILLEGAL_ACTION", "该会话的当前操作与指定操作不匹配", current_revision=revision)
    target = state["resume"]
    if target is None:
        return state
    if target["reason"] == "report_pending":
        generate_report(path, session_id)
    else:
        _run_operation(path, session_id, target["operation_id"], target["kind"], bank=bank,
                       proposer=proposer, evaluator=evaluator, model=model)
    return read_state(path, session_id)
