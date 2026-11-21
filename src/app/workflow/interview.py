"""The CLI interview graph. Business operations remain the recovery source."""
from __future__ import annotations

from pathlib import Path
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context

from app.db.business import operation_epoch
from app.services.evaluation import EVALUATE, DemoEvaluator, process_evaluation
from app.services.first_question import prepare_first_question, start_epoch
from app.services.probing import DemoProposer, process_answer


class InterviewState(TypedDict, total=False):
    session_id: str
    operation_id: str
    control_epoch: int
    turn_id: str | None
    exchange_id: str | None
    evaluation_id: str


def _run_interview(path: Path, initial: InterviewState, command: Literal["start", "answer", "evaluate_answer"], *,
                   bank: Path | None = None, proposer: DemoProposer | None = None,
                   evaluator: DemoEvaluator | None = None) -> InterviewState:
    def prepare(state: InterviewState) -> dict:
        if bank is None:
            raise RuntimeError("首题节点缺少审核题库配置")
        return prepare_first_question(path, bank, state["session_id"], state["operation_id"], state["control_epoch"])

    def decide(state: InterviewState) -> dict:
        return process_answer(path, state["session_id"], state["operation_id"], state["control_epoch"], proposer)

    def evaluate(state: InterviewState) -> dict:
        return process_evaluation(path, state["session_id"], state["operation_id"], state["control_epoch"], evaluator)

    builder = StateGraph(InterviewState)
    builder.add_node("prepare_first_question", prepare)
    builder.add_node("decide_follow_up", decide)
    builder.add_node(EVALUATE, evaluate)
    builder.add_conditional_edges(START, lambda _: command,
                                  {"start": "prepare_first_question", "answer": "decide_follow_up", EVALUATE: EVALUATE})
    builder.add_edge("prepare_first_question", END)
    builder.add_edge("decide_follow_up", END)
    builder.add_edge(EVALUATE, END)
    with tracing_context(enabled=False):
        return builder.compile().invoke(initial)


def run_first_question(path: Path, bank: Path, session_id: str, operation_id: str) -> InterviewState:
    initial = {"session_id": session_id, "operation_id": operation_id,
               "control_epoch": start_epoch(path, session_id, operation_id)}
    return _run_interview(path, initial, "start", bank=bank)


def run_answer(path: Path, session_id: str, operation_id: str, proposer: DemoProposer | None = None) -> InterviewState:
    initial = {"session_id": session_id, "operation_id": operation_id,
               "control_epoch": operation_epoch(path, session_id, operation_id, "answer")}
    return _run_interview(path, initial, "answer", proposer=proposer)


def run_evaluation(path: Path, session_id: str, operation_id: str, evaluator: DemoEvaluator | None = None) -> InterviewState:
    initial = {"session_id": session_id, "operation_id": operation_id,
               "control_epoch": operation_epoch(path, session_id, operation_id, EVALUATE)}
    return _run_interview(path, initial, EVALUATE, evaluator=evaluator)
