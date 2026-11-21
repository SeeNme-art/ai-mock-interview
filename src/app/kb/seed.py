"""Validation for the versioned, local question bank seed file."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Annotated, Literal, Self
from unicodedata import normalize
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{1,63}$")]
PositiveInt = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
Dimension = Literal["agent", "rag", "backend"]
Difficulty = Literal["basic", "applied"]
RatingDimension = Literal["correctness", "depth", "structure", "relevance"]


def prompt_key(text: str) -> str:
    return " ".join(normalize("NFKC", text).casefold().split())


class SeedRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Source(SeedRecord):
    id: Identifier
    title: Text
    locator: Text
    section: Text
    source_type: Literal["official_docs", "paper"]
    source_version: Text | None = None
    checked_at: date

    @field_validator("locator")
    @classmethod
    def require_public_https(cls, value: str) -> str:
        url = urlsplit(value)
        if url.scheme != "https" or not url.hostname or url.username is not None or url.password is not None:
            raise ValueError("来源需要公开 HTTPS 链接")
        return value


class Question(SeedRecord):
    id: Identifier
    stable_faiss_id: PositiveInt
    revision: PositiveInt
    dimension: Dimension
    difficulty: Difficulty
    content: Text
    objective: Text
    answer_points: Annotated[list[Text], Field(min_length=1)]
    acceptable_alternatives: Annotated[list[Text], Field(min_length=1)]
    common_errors: Annotated[list[Text], Field(min_length=1)]
    applicable_dimensions: Annotated[list[RatingDimension], Field(min_length=1)]
    tags: Annotated[list[Text], Field(min_length=1)]
    status: Literal["draft", "verified", "retired"]
    checked_at: date
    source_refs: Annotated[list[Identifier], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_rating_and_sources(self) -> Self:
        if len(set(self.source_refs)) != len(self.source_refs):
            raise ValueError("同一题的来源不能重复")
        if len(set(self.applicable_dimensions)) != len(self.applicable_dimensions):
            raise ValueError("评分维度不能重复")
        if not {"correctness", "depth"}.intersection(self.applicable_dimensions):
            raise ValueError("知识题至少考察一个技术内容维度")
        return self


class SeedBank(SeedRecord):
    sources: Annotated[list[Source], Field(min_length=1)]
    questions: Annotated[list[Question], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_bank(self) -> Self:
        source_ids = {source.id for source in self.sources}
        if len(source_ids) != len(self.sources):
            raise ValueError("来源 ID 重复")
        for keys in (
            {question.id for question in self.questions},
            {question.stable_faiss_id for question in self.questions},
            {prompt_key(question.content) for question in self.questions},
        ):
            if len(keys) != len(self.questions):
                raise ValueError("题目 ID、向量 ID 或规范化题面重复")
        for question in self.questions:
            if not source_ids.issuperset(question.source_refs):
                raise ValueError(f"{question.id} 引用了不存在的来源")
        return self


def read_seed(path: Path) -> SeedBank:
    return SeedBank.model_validate_json(path.read_text(encoding="utf-8"))
