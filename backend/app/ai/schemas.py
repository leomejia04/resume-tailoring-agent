"""Shapes the model must return (Structured Outputs).

These deliberately avoid length/pattern constraints so they stay compatible
with strict JSON-schema mode; every result is re-validated by our own code
before anything is stored or shown.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

Category = Literal["required", "preferred", "responsibility", "terminology"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RequirementOut(_Strict):
    category: Category
    text: str
    terms: list[str]
    mandatory: bool


class JobAnalysisOut(_Strict):
    role_title: str
    seniority: str | None
    requirements: list[RequirementOut]


class BulletOut(_Strict):
    text: str
    evidence_ids: list[str]
    requirement_ids: list[str]


class EntryOut(_Strict):
    entry_ref: str
    bullets: list[BulletOut]


class SkillOut(_Strict):
    text: str
    evidence_ids: list[str]


class TailoredDraftOut(_Strict):
    headline: str
    summary: str | None
    entries: list[EntryOut]
    skills: list[SkillOut]
    omitted_evidence_ids: list[str]


class ChatPatchOut(_Strict):
    """A concise revision: only the entries that change, plus optional headline/summary."""

    reply: str
    entries: list[EntryOut]
    headline: str | None
    change_summary: bool
    summary: str | None


class ImprovePatchOut(_Strict):
    """Entries rewritten to show targeted requirements (complete bullet lists)."""

    entries: list[EntryOut]
