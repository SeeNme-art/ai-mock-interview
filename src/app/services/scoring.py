"""Validate evidence and derive deterministic report numbers from question facts."""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Collection, Literal, Mapping, Sequence

from app.schemas.scoring import CONTENT_DIMENSIONS, DIMENSIONS, Assessment, EvidenceRef, TaskType

Outcome = Literal["answered", "skipped", "unanswered", "generation_failed"]
EvaluationStatus = Literal["pending", "ready", "failed"]


@dataclass(frozen=True)
class ScoredAssessment:
    assessment: Assessment
    dimension_values: tuple[tuple[str, int], ...]

    @property
    def score(self) -> Fraction:
        total = sum(value for _, value in self.dimension_values)
        return round(Fraction(25 * total, len(self.dimension_values)), 1)

    @property
    def item_score(self) -> float:
        return float(self.score)


def locate_evidence(ref: EvidenceRef, answers: Mapping[str, str]) -> EvidenceRef:
    answer = answers.get(ref.exchange_id, "")
    start = answer.find(ref.quote)
    if not ref.quote.strip() or start < 0:
        raise ValueError("评分证据未在该题的已提交答案中找到")
    return ref.model_copy(update={"start": start, "end": start + len(ref.quote)})


def score_assessment(assessment: Assessment, *, task_type: TaskType, rubric_version: str,
                     applicable_dimensions: Collection[str], answers: Mapping[str, str],
                     source_refs: Collection[str] = ()) -> ScoredAssessment:
    if assessment.task_type != task_type or assessment.rubric_version != rubric_version:
        raise ValueError("题型或评分规则版本与正式题目不符")
    keys = set(DIMENSIONS[task_type])
    if assessment.dimensions.keys() != keys:
        raise ValueError("评分结果必须包含该题型的全部维度")
    active = set(applicable_dimensions)
    if not active <= keys or not active & CONTENT_DIMENSIONS[task_type]:
        raise ValueError("冻结的评分目标缺少合法的内容维度")
    if not any(answer.strip() for answer in answers.values()):
        raise ValueError("评分需要至少一条已提交答案")

    verified = {}
    dimension_values = []
    for key, dimension in assessment.dimensions.items():
        if dimension.applicable != (key in active):
            raise ValueError("评分适用性与冻结的考察维度不一致")
        if not set(dimension.source_refs).issubset(source_refs):
            raise ValueError("评分引用了不在本题快照中的来源")
        if dimension.applicable:
            if dimension.value is None:
                raise ValueError("已考察的维度须给出分值")
            if not dimension.evidence and dimension.evidence_absence_reason is None:
                raise ValueError("已考察维度缺少证据或缺证说明")
            dimension_values.append((key, dimension.value))
        elif dimension.value is not None or dimension.evidence:
            raise ValueError("未考察的维度不能携带分值或答案引用")
        verified[key] = dimension.model_copy(deep=True, update={
            "evidence": [locate_evidence(ref, answers) for ref in dimension.evidence],
        })
    normalized = assessment.model_copy(deep=True, update={"dimensions": verified})
    return ScoredAssessment(normalized, tuple(dimension_values))


@dataclass(frozen=True)
class ReportItem:
    plan_item_id: str
    task_type: TaskType
    displayed: bool = False
    has_answer: bool = False
    outcome: Outcome | None = None
    evaluation_status: EvaluationStatus | None = None
    scored: ScoredAssessment | None = None

    def __post_init__(self) -> None:
        if self.has_answer and not self.displayed:
            raise ValueError("有答案的题目必须已展示")
        if self.outcome == "answered" and not self.has_answer:
            raise ValueError("已回答题目缺少提交记录")
        if self.outcome == "unanswered" and self.has_answer:
            raise ValueError("未回答题目不能包含答案")
        if self.outcome == "generation_failed" and self.displayed:
            raise ValueError("生成失败的题目没有正式题面")
        if self.evaluation_status is not None and self.outcome != "answered":
            raise ValueError("评分状态必须对应已回答并收尾的题目")
        if (self.scored is not None) != (self.evaluation_status == "ready"):
            raise ValueError("只有评分成功才能参与报告均分")
        if self.scored is not None and self.scored.assessment.task_type != self.task_type:
            raise ValueError("汇总题型必须与实际评分题型一致")


def _mean(values: Sequence[Fraction | int]) -> float | None:
    return float(round(Fraction(sum(values), len(values)), 1)) if values else None


def aggregate_scores(items: Sequence[ReportItem], *, ended_early: bool) -> dict:
    if len({item.plan_item_id for item in items}) != len(items):
        raise ValueError("报告中的计划题目重复")
    category_scores: dict[TaskType, list[Fraction]] = {"knowledge": [], "project": []}
    dimension_scores: dict[str, list[int]] = {key: [] for keys in DIMENSIONS.values() for key in keys}
    counts = {"planned": len(items), "displayed": 0, "answered": 0, "scored": 0, "skipped": 0, "system_failed": 0}
    for item in items:
        counts["displayed"] += item.displayed
        counts["answered"] += item.has_answer
        counts["skipped"] += item.outcome == "skipped"
        counts["system_failed"] += item.outcome == "generation_failed" or item.evaluation_status == "failed"
        if item.scored is not None:
            counts["scored"] += 1
            category_scores[item.task_type].append(item.scored.score)
            for key, value in item.scored.dimension_values:
                dimension_scores[key].append(value)
    return {
        "knowledge_score": _mean(category_scores["knowledge"]),
        "project_score": _mean(category_scores["project"]),
        "counts": counts,
        "category_counts": {key: len(values) for key, values in category_scores.items()},
        "dimension_summary": {key: {"mean": _mean(values), "count": len(values)}
                              for key, values in dimension_scores.items()},
        "is_partial": ended_early or counts["scored"] < counts["planned"],
    }
