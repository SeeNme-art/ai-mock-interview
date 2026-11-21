"""Supported first-release planning configuration."""
from __future__ import annotations

from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.kb.seed import Difficulty, Dimension

DIRECTIONS = get_args(Dimension)
DirectionList = Annotated[list[Dimension], Field(min_length=1, max_length=3)]


class PlanChanges(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    directions: DirectionList | None = None
    difficulty: Difficulty | None = None
    question_count: Literal[5, 8] | None = None

    @field_validator("directions")
    @classmethod
    def unique_directions(cls, values: list[Dimension] | None) -> list[Dimension] | None:
        if values is not None and len(set(values)) != len(values):
            raise ValueError("考察方向不能重复")
        return values

    @field_validator("question_count", mode="before")
    @classmethod
    def integer_count(cls, value: object) -> object:
        if value is not None and type(value) is not int:
            raise ValueError("题数必须为整数")
        return value
