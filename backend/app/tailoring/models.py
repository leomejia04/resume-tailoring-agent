"""Validated domain shapes stored as JSON (analyses, drafts, validation reports)."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Category = Literal["required", "preferred", "responsibility", "terminology"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Requirement(_Model):
    id: str = Field(pattern=r"^R\d{1,3}$")
    category: Category
    text: str = Field(min_length=1, max_length=300)
    terms: list[str] = Field(max_length=5)
    mandatory: bool


class AnalysisContent(_Model):
    role_title: str = Field(min_length=1, max_length=120)
    seniority: str | None = Field(default=None, max_length=60)
    requirements: list[Requirement] = Field(max_length=40)


class DraftBullet(_Model):
    text: str = Field(min_length=1, max_length=350)
    evidence_ids: list[uuid.UUID] = Field(min_length=1)
    requirement_ids: list[str] = Field(default_factory=list)


class DraftEntry(_Model):
    ref: str
    section: str | None
    # Shown exactly as in the master resume (protected: never model-written).
    entry: str | None
    date_text: str | None
    experience_type: str
    bullets: list[DraftBullet]


class DraftSkill(_Model):
    text: str = Field(min_length=1, max_length=350)
    evidence_ids: list[uuid.UUID] = Field(min_length=1)


class OmittedFact(_Model):
    evidence_id: uuid.UUID
    reason: Literal["not_selected", "failed_validation"]


class DraftContent(_Model):
    headline: str = Field(min_length=1, max_length=120)
    summary: str | None = Field(default=None, max_length=600)
    entries: list[DraftEntry]
    skills: list[DraftSkill]
    omitted: list[OmittedFact]


class ValidationIssue(_Model):
    code: str
    severity: Literal["error", "warning"]
    where: str
    message: str


class ValidationReport(_Model):
    rule_version: str
    issues: list[ValidationIssue]
    removed: list[str]
    repaired: bool
    passed: bool
