"""Initial rubric keys and strict model-output fields used by scoring."""
from __future__ import annotations

from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

TaskType = Literal["knowledge", "project"]
KnowledgeDimension = Literal["correctness", "depth", "structure", "relevance"]
ProjectDimension = Literal["technical_reasoning", "implementation_ownership", "validation_evidence", "communication"]

DIMENSIONS = {"knowledge": get_args(KnowledgeDimension), "project": get_args(ProjectDimension)}
CONTENT_DIMENSIONS = {
    "knowledge": frozenset({"correctness", "depth"}),
    "project": frozenset({"technical_reasoning", "implementation_ownership", "validation_evidence"}),
}

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Rating = Annotated[int, Field(ge=0, le=4)]
Offset = Annotated[int, Field(ge=0)]


class ScoringInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class EvidenceRef(ScoringInput):
    exchange_id: NonBlank
    quote: Annotated[str, StringConstraints(min_length=1)]
    start: Offset | None = None
    end: Offset | None = None


class ModelDimensionAssessment(ScoringInput):
    value: Rating
    reason: NonBlank
    evidence: list[EvidenceRef]
    evidence_absence_reason: NonBlank | None = None
    source_refs: list[NonBlank] = Field(default_factory=list)


class DimensionAssessment(ModelDimensionAssessment):
    value: Rating | None
    applicable: bool


class Uncertainty(ScoringInput):
    type: NonBlank
    message: NonBlank


class ModelAssessment(ScoringInput):
    dimensions: dict[KnowledgeDimension | ProjectDimension, ModelDimensionAssessment]
    uncertainties: list[Uncertainty] = Field(default_factory=list)


class Assessment(ScoringInput):
    task_type: TaskType
    rubric_version: NonBlank
    dimensions: dict[KnowledgeDimension | ProjectDimension, DimensionAssessment]
    uncertainties: list[Uncertainty] = Field(default_factory=list)
    comment: NonBlank | None = None
    improvements: list[NonBlank] = Field(default_factory=list)
