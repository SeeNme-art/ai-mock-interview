"""Structured output used by the answer follow-up step."""
from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from .scoring import EvidenceRef, ScoringInput

ProbePurpose = Literal["clarify", "deepen", "check_consistency"]
ProbeText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=600)]


class ProbeProposal(ScoringInput):
    should_probe: bool
    purpose: ProbePurpose | None = None
    reason: ProbeText
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=8)
    probe_question: ProbeText | None = None

    @model_validator(mode="after")
    def match_decision(self) -> Self:
        if self.should_probe:
            if self.purpose is None or self.probe_question is None or not self.evidence_refs:
                raise ValueError("追问建议缺少目的、题面或证据")
        elif self.purpose is not None or self.probe_question is not None:
            raise ValueError("收尾建议不得附带追问题面或目的")
        return self
