"""Build and save a deterministic, reviewable plan for a confirmed session."""
from __future__ import annotations

from collections import deque
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import zip_longest
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from app.db.business import OWNER_ID, check_business_schema, upgrade_business_v1, validate_request_key
from app.db.sqlite import canonical_json, connect, sha256
from app.kb.seed import prompt_key
from app.kb.store import list_questions
from app.schemas.planning import PlanChanges

_PROJECT_TERMS = {
    "agent": r"(?<![a-z0-9])(?:agent|langgraph)(?![a-z0-9])|智能体|工具调用",
    "rag": r"(?<![a-z0-9])(?:rag|faiss)(?![a-z0-9])|检索|召回|分块|向量",
    "backend": r"(?<![a-z0-9])(?:fastapi|sqlite|pydantic)(?![a-z0-9])|后端|数据库|接口",
}
_PROJECT_TARGETS = {
    "action": ("实施与个人贡献", ("implementation_ownership",)),
    "result": ("验证方法与证据", ("validation_evidence",)),
}


class PlanningError(ValueError):
    def __init__(self, code: str, message: str, *, current_revision: int | None = None):
        super().__init__(message)
        self.code = code
        self.current_revision = current_revision


@dataclass(frozen=True)
class PlanSlot:
    task_type: str
    dimension: str
    topic: str
    generation_method: str
    scoring_targets: tuple[str, ...]
    question_id: str | None = None
    question_revision: int | None = None
    project_ref: str | None = None
    project_field: str | None = None
    form_reason: str | None = None


def _project_quota(mode: str, count: int) -> int:
    return {5: 2, 8: 3}[count] if mode == "mixed" else 0


def _project_slots(projects: list[dict], remaining: dict[str, deque], quota: int) -> list[PlanSlot]:
    if not quota:
        return []
    project_directions = []
    for project in projects:
        text = prompt_key(" ".join(project.get(field) or "" for field in ("name", "action", "background", "result", "excerpt")))
        project_directions.append([key for key in remaining if re.search(_PROJECT_TERMS[key], text)])

    counts = dict.fromkeys(remaining, 0)
    selected = []
    used_evidence = set()
    for field, (topic, scoring_targets) in _PROJECT_TARGETS.items():
        for project, directions in zip(projects, project_directions):
            evidence = prompt_key(project.get(field) or "")
            if not directions or not evidence or evidence in used_evidence:
                continue
            direction = min(directions, key=lambda key: (counts[key], len(remaining[key])))
            selected.append(PlanSlot("project", direction, topic, "generated", scoring_targets,
                                     project_ref=project["id"], project_field=field, form_reason="confirmed_project_field"))
            counts[direction] += 1
            used_evidence.add(evidence)
            if len(selected) == quota:
                return selected
    return selected


def _build_slots(questions: list[dict], projects: list[dict], directions: list[str], count: int,
                 mode: str) -> list[PlanSlot]:
    remaining = {key: deque() for key in directions}
    seen_topics = set()
    for row in questions:
        topic = prompt_key(row["objective"])
        if row["dimension"] in remaining and topic not in seen_topics:
            remaining[row["dimension"]].append(row)
            seen_topics.add(topic)

    project = _project_slots(projects, remaining, _project_quota(mode, count))
    needed = count - len(project)
    available = sum(map(len, remaining.values()))
    if available < needed:
        raise PlanningError("QUESTION_UNAVAILABLE", f"需要 {needed} 道审核知识题，当前条件只有 {available} 道。请调整方向、难度或题数。")

    counts = dict.fromkeys(directions, 0)
    for slot in project:
        counts[slot.dimension] += 1
    knowledge = []
    for _ in range(needed):
        direction = min((key for key in directions if remaining[key]), key=counts.__getitem__)
        row = remaining[direction].popleft()
        knowledge.append(PlanSlot("knowledge", direction, row["objective"], "original",
                                  tuple(row["applicable_dimensions"]), question_id=row["id"], question_revision=row["revision"],
                                  form_reason="verified_bank"))
        counts[direction] += 1
    return [slot for pair in zip_longest(knowledge, project) for slot in pair if slot is not None]


def _find_session(db: sqlite3.Connection, session_id: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM interview_session WHERE id=? AND owner_id=? AND delete_state='none'",
                     (session_id, OWNER_ID)).fetchone()
    if row is None:
        raise PlanningError("RESOURCE_NOT_FOUND", "找不到可访问的会话")
    return row


def _summary(db: sqlite3.Connection, session: sqlite3.Row) -> dict:
    directions = json.loads(session["selected_dimensions_json"])
    coverage = {key: {"knowledge": 0, "project": 0} for key in directions}
    items = [dict(row) for row in db.execute("""SELECT seq, task_type, dimension, difficulty FROM plan_item
                                               WHERE session_id=? AND plan_version=? ORDER BY seq""",
                                            (session["id"], session["plan_version"]))]
    counts = {"knowledge": 0, "project": 0}
    for item in items:
        counts[item["task_type"]] += 1
        coverage[item["dimension"]][item["task_type"]] += 1
    hints = []
    replaced = _project_quota(session["mode"], session["question_count"]) - counts["project"]
    if replaced > 0:
        hints.append(f"当前材料支撑 {counts['project']} 道项目题，原计划中 {replaced} 道项目题由审核知识题补足。")
    uncovered = [key for key, values in coverage.items() if not any(values.values())]
    if uncovered:
        hints.append("本次规划未覆盖 " + "、".join(uncovered) + "。可调整方向、难度或题数。")
    return {
        "session_id": session["id"], "phase": session["phase"], "revision": session["revision"],
        "plan_version": session["plan_version"], "mode": session["mode"], "input_mode": session["input_mode"],
        "question_count": session["question_count"], "difficulty": session["difficulty"], "directions": directions,
        "counts": counts, "coverage": coverage, "items": items, "hints": hints,
    }


def show_plan(path: Path, session_id: str) -> dict:
    with closing(connect(path, readonly=True)) as db, db:
        db.execute("BEGIN")
        version = check_business_schema(db)
        session = _find_session(db, session_id)
        if version < 2 or not session["plan_version"]:
            raise PlanningError("ILLEGAL_ACTION", "会话还没有计划，请先生成计划", current_revision=session["revision"])
        return _summary(db, session)


def rebuild_plan(path: Path, bank: Path, session_id: str, expected_revision: int, key: str,
                 changes: PlanChanges) -> dict:
    validate_request_key(key)
    if type(expected_revision) is not int or expected_revision < 1:
        raise ValueError("会话 revision 须为正整数")
    if not path.is_file():
        raise PlanningError("RESOURCE_NOT_FOUND", "业务库尚未创建，请先确认材料")
    options = changes.model_dump(exclude_none=True, mode="json")
    request = canonical_json({"session_id": session_id, "expected_revision": expected_revision, **options})
    digest = sha256(request)
    with closing(connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        version = check_business_schema(db)
        session = _find_session(db, session_id)
        if version >= 2:
            operation = db.execute("""SELECT request_hash, result_refs_json FROM operation
                                      WHERE owner_id=? AND kind='rebuild_plan' AND idempotency_key=?""",
                                   (OWNER_ID, key)).fetchone()
            if operation:
                if operation["request_hash"] != digest:
                    raise PlanningError("IDEMPOTENCY_CONFLICT", "该请求标识已经用于其他规划内容")
                return json.loads(operation["result_refs_json"])
        if session["phase"] != "PLANNING":
            raise PlanningError("ILLEGAL_ACTION", "面试开始后计划已冻结", current_revision=session["revision"])
        if session["revision"] != expected_revision:
            raise PlanningError("STALE_REVISION", "会话进度已更新，请刷新后再生成计划", current_revision=session["revision"])
        if version >= 2 and db.execute("""SELECT 1 FROM operation WHERE session_id=?
                                         AND status IN ('queued','running','recovery_required')""", (session_id,)).fetchone():
            raise PlanningError("OPERATION_IN_PROGRESS", "该会话已有待处理操作", current_revision=session["revision"])

        directions = changes.directions or json.loads(session["selected_dimensions_json"])
        difficulty = changes.difficulty or (session["difficulty"] if version >= 2 else "basic")
        count = changes.question_count or session["question_count"]
        projects = json.loads(session["resume_profile_json"])["projects"]
        if not bank.is_file():
            raise PlanningError("QUESTION_UNAVAILABLE", "审核题库尚未导入，请先初始化题库")
        slots = _build_slots(list_questions(bank, difficulty=difficulty), projects, directions, count, session["mode"])
        if version == 1:
            upgrade_business_v1(db, path)

        plan_version = (session["plan_version"] if version >= 2 else 0) + 1
        db.execute("DELETE FROM plan_item WHERE session_id=?", (session_id,))
        db.executemany("""INSERT INTO plan_item (id, session_id, seq, plan_version, task_type, dimension, difficulty,
                      topic, generation_method, scoring_targets_json, question_id, question_revision,
                      project_ref, project_field, form_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", [
            (uuid4().hex, session_id, seq, plan_version, slot.task_type, slot.dimension, difficulty, slot.topic,
             slot.generation_method, canonical_json(slot.scoring_targets), slot.question_id, slot.question_revision,
             slot.project_ref, slot.project_field, slot.form_reason)
            for seq, slot in enumerate(slots, start=1)
        ])
        db.execute("""UPDATE interview_session SET revision=revision+1, plan_version=?, difficulty=?,
                      question_count=?, selected_dimensions_json=? WHERE id=? AND owner_id=?""",
                   (plan_version, difficulty, count, canonical_json(directions), session_id, OWNER_ID))
        result = _summary(db, _find_session(db, session_id))
        now = datetime.now(UTC).isoformat()
        db.execute("""INSERT INTO operation (id, session_id, owner_id, kind, idempotency_key, request_hash,
                      expected_epoch, status, safe_request_json, result_refs_json, created_at, updated_at)
                      VALUES (?,?,?,'rebuild_plan',?,?,?,'succeeded',?,?,?,?)""",
                   (uuid4().hex, session_id, OWNER_ID, key, digest, session["control_epoch"], request,
                    canonical_json(result), now, now))
        return result
