"""Allowlisted user-confirmed material fields."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

ShortText = Annotated[str, StringConstraints(min_length=1, max_length=120)]
ProjectText = Annotated[str, StringConstraints(min_length=1, max_length=1600)]
Mode = Literal["mixed", "knowledge_only"]
InputMode = Literal["text", "form"]


class ProjectInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True, hide_input_in_errors=True)

    action: ProjectText
    name: ShortText | None = None
    background: ProjectText | None = None
    result: ProjectText | None = None
    excerpt: Annotated[str, StringConstraints(min_length=1, max_length=1000)] | None = None


class MaterialInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True, hide_input_in_errors=True)

    jd_text: Annotated[str, StringConstraints(min_length=1, max_length=8000)] | None = None
    skills: list[ShortText] = Field(default_factory=list, max_length=20)
    years: int | None = Field(default=None, ge=0)
    education: ShortText | None = None
    projects: list[ProjectInput] = Field(default_factory=list, max_length=8)


class Confirmation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    material: MaterialInput
    mode: Mode

    @model_validator(mode="after")
    def check_project_requirement(self) -> Confirmation:
        if self.mode == "mixed" and not self.material.projects:
            raise ValueError("混合练习需要至少一项项目，请填写做过什么或明确选择知识专项")
        return self
