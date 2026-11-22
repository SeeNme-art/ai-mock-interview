"""Local Web adapter for the existing interview workflow."""
from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
import json
from pathlib import Path
from threading import Lock
from typing import Literal

from app.core.logging_setup import log_event
from app.db.business import SessionError, check_business_schema, find_session
from app.db.sqlite import canonical_json, connect, sha256
from app.kb.seed import read_seed
from app.kb.store import import_seed
from app.schemas.materials import MaterialInput, ProjectInput
from app.schemas.scoring import DIMENSIONS
from app.services.evaluation import accept_evaluation
from app.services.first_question import read_state
from app.services.materials import MaterialDraft, preview_text, start_draft
from app.services.reports import generate_report

EXAMPLE = MaterialInput(
    jd_text="实现面向业务文档的检索问答工具，并能说明召回与故障处理。",
    skills=["Python", "FAISS", "SQLite", "检索评测"],
    projects=[ProjectInput(name="课程检索练习", action="实现文档分块并保存段落出处", result="用固定查询比较两种分块大小")],
)
LABELS = {"岗位描述": "jd_text", "技能": "skills", "项目名称": "name", "项目做法": "action", "项目结果": "result"}


def _candidate(text: str) -> MaterialInput:
    values = {}
    for line in preview_text(text).splitlines():
        tag, separator, value = line.replace("：", ":", 1).partition(":")
        if separator and tag.strip() in LABELS and value.strip():
            values[LABELS[tag.strip()]] = value.strip()
    skills = values.get("skills", "").replace("、", ",").replace("，", ",").split(",")
    project = {name: values[name] for name in ("name", "action", "result") if name in values}
    return MaterialInput(jd_text=values.get("jd_text"), skills=[skill.strip() for skill in skills if skill.strip()],
                         projects=[ProjectInput(**project)] if "action" in project else [])


def _example_assessment(context):
    rubric = json.loads(context.rubric_snapshot)
    dimensions = set(rubric["applicable_dimensions"])
    exchange = context.exchanges[-1]
    return {"task_type": context.task_type, "rubric_version": rubric["rubric_version"], "dimensions": {
        name: {"applicable": name in dimensions, "value": 3 if name in dimensions else None, "reason": "示例固定反馈",
               "evidence": [{"exchange_id": exchange.id, "quote": exchange.answer}] if name in dimensions else []}
        for name in DIMENSIONS[context.task_type]
    }}


def _local_follow_up(context, example: bool) -> dict:
    if example and len(context.asked_questions) == 1:
        exchange = context.exchanges[-1]
        return {"should_probe": True, "reason": "示例追问", "purpose": "deepen",
                "probe_question": "能举一个具体例子说明你会怎样验证这个判断吗？",
                "evidence_refs": [{"exchange_id": exchange.id, "quote": exchange.answer}]}
    return {"should_probe": False, "reason": "本地模式进入评估"}


def _unavailable(_):
    raise OSError("local review mode has no evaluator")


class Workspace:
    def __init__(self, directory: Path, seed: Path):
        self.db = directory / "interviews.db"
        self.bank = directory / "question_bank.db"
        self._seed = seed
        self._drafts: dict[str, tuple[str, MaterialDraft]] = {}
        self._lock = Lock()

    def preview(self, key: str, source: Literal["example", "text"], text: str) -> MaterialDraft:
        digest = sha256(canonical_json([source, text]))
        with self._lock:
            self._purge_drafts()
            saved = self._drafts.get(key)
            if saved is not None:
                if saved[0] != digest:
                    raise SessionError("IDEMPOTENCY_CONFLICT", "材料预览已使用该请求标识")
                return saved[1]
            material = EXAMPLE if source == "example" else _candidate(text)
            draft = start_draft(material, "form" if source == "example" else "text", web_source=source)
            if len(self._drafts) >= 64:
                raise SessionError("OPERATION_IN_PROGRESS", "暂存预览已满，请稍后再试")
            self._drafts[key] = (digest, draft)
            return draft

    def _purge_drafts(self) -> None:
        now = datetime.now(UTC)
        self._drafts = {key: value for key, value in self._drafts.items() if value[1].expires_at > now}

    def draft(self, draft_id: str) -> MaterialDraft | None:
        with self._lock:
            self._purge_drafts()
            return next((item for _, item in self._drafts.values() if item.id == draft_id), None)

    def forget(self, draft_id: str) -> None:
        with self._lock:
            self._drafts = {key: value for key, value in self._drafts.items() if value[1].id != draft_id}

    def prepare_bank(self) -> None:
        with self._lock:
            if not self.bank.is_file():
                import_seed(self.bank, read_seed(self._seed))

    def preferences(self, session_id: str) -> dict:
        with closing(connect(self.db, readonly=True)) as db, db:
            db.execute("BEGIN")
            version = check_business_schema(db)
            row = find_session(db, session_id)
            return {"source": json.loads(row["policy_snapshot_json"]).get("web_source"),
                    "settings": {"directions": json.loads(row["selected_dimensions_json"]),
                                 "difficulty": row["difficulty"] if version >= 2 else "basic",
                                 "question_count": row["question_count"]}}

    def process(self, session_id: str, operation_id: str) -> None:
        """Run an accepted job to its next saved boundary. A restart can resume it explicitly."""
        try:
            from app.workflow.interview import resume_interview, run_evaluation

            source = self.preferences(session_id)["source"]
            evaluator = _example_assessment if source == "example" else _unavailable
            state = resume_interview(self.db, self.bank, session_id, operation_id=operation_id,
                                     proposer=lambda context: _local_follow_up(context, source == "example"),
                                     evaluator=evaluator)
            operation = state["operation"]
            if (operation is not None and operation["operation_id"] == operation_id
                    and operation["kind"] == "answer" and operation.get("decision") == "score_pending"):
                score, _ = accept_evaluation(self.db, session_id, operation_id, state["revision"], f"web-score-{operation_id}")
                run_evaluation(self.db, session_id, score, evaluator)
                state = read_state(self.db, session_id)
            if state["phase"] == "FINISHED":
                generate_report(self.db, session_id)
        except Exception as exc:
            log_event("web.operation.pending", operation_id=operation_id, error_type=type(exc).__name__)
