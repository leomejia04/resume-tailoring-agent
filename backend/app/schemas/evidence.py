from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ExperienceType = Literal[
    "professional", "internship", "academic", "personal", "other", "unclassified"
]
VerificationStatus = Literal["unverified", "verified", "rejected"]


class EvidenceOut(BaseModel):
    id: uuid.UUID
    master_version_id: uuid.UUID
    position: int
    version: int
    text: str
    source_text: str
    section: str | None
    entry: str | None
    date_text: str | None
    metrics: list[str]
    experience_type: ExperienceType
    suggested_type: ExperienceType
    verification_status: VerificationStatus
    change_source: Literal["extracted", "user"]
    updated_at: datetime


class EvidenceUpdate(BaseModel):
    """Creates a new immutable version. Omitted text keeps the current wording."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_version: int = Field(ge=1)
    text: str | None = Field(default=None, min_length=1, max_length=2000)
    experience_type: ExperienceType
    verification_status: VerificationStatus

    @model_validator(mode="after")
    def _verified_needs_type(self) -> EvidenceUpdate:
        if self.verification_status == "verified" and self.experience_type == "unclassified":
            raise ValueError("choose what kind of experience this is before confirming it")
        return self
