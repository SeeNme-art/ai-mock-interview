"""The CLI interview graph. Business operations remain the recovery source."""
from __future__ import annotations

from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context

from app.services.first_question import prepare_first_question, start_epoch


class InterviewState(TypedDict, total=False):
    session_id: str
    operation_id: str
    control_epoch: int
    turn_id: str | None
    exchange_id: str | None


def run_first_question(path: Path, bank: Path, session_id: str, operation_id: str) -> InterviewState:
    epoch = start_epoch(path, session_id, operation_id)

    def prepare(state: InterviewState) -> dict:
        return prepare_first_question(path, bank, state["session_id"], state["operation_id"], state["control_epoch"])

    builder = StateGraph(InterviewState)
    builder.add_node("prepare_first_question", prepare)
    builder.add_edge(START, "prepare_first_question")
    builder.add_edge("prepare_first_question", END)
    with tracing_context(enabled=False):
        return builder.compile().invoke({"session_id": session_id, "operation_id": operation_id, "control_epoch": epoch})
