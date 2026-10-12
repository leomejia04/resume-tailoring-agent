"""Manual application tracker (milestone 6e): request and response bodies."""

from __future__ import annotations

import unicodedata
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from app.schemas.jobs import plain_http_url

ApplicationStatus = Literal[
    "planned", "applied", "interviewing", "offer", "accepted", "rejected", "withdrawn"
]
EARLIEST_APPLIED_ON = date(2000, 1, 1)
MAX_NOTES_CHARS = 4000


def _check_text(value: str | None, *, multiline: bool) -> str | None:
    if value is None:
        return None
    allowed = {"\n", "\t"} if multiline else set()
    if any(unicodedata.category(ch) == "Cc" and ch not in allowed for ch in value):
        raise ValueError("remove line breaks or other control characters")
    return value


class ApplicationInput(BaseModel):
    """What the user types into the tracker. Optional fields may be blank or omitted."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    company: str = Field(min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=200, description="The job title applied for")
    job_url: HttpUrl | None = Field(
        default=None, description="Link to the posting. Stored as a label; never fetched."
    )
    approval_id: uuid.UUID | None = Field(
        default=None, description="One of your approved resumes: the exact version you sent"
    )
    applied_on: date | None = None
    status: ApplicationStatus = "applied"
    notes: str | None = Field(default=None, max_length=MAX_NOTES_CHARS)

    @field_validator("job_url", "approval_id", "applied_on", "notes", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("company", "role")
    @classmethod
    def _single_line(cls, value: str) -> str:
        return _check_text(value, multiline=False)  # type: ignore[return-value]

    @field_validator("notes")
    @classmethod
    def _notes_text(cls, value: str | None) -> str | None:
        return _check_text(value, multiline=True)

    @field_validator("job_url")
    @classmethod
    def _plain_url(cls, url: HttpUrl | None) -> HttpUrl | None:
        return plain_http_url(url)

    @field_validator("applied_on")
    @classmethod
    def _plausible_date(cls, value: date | None) -> date | None:
        # One day of slack: the user's calendar may already be on tomorrow (UTC+14).
        latest = datetime.now(UTC).date() + timedelta(days=1)
        if value is not None and not EARLIEST_APPLIED_ON <= value <= latest:
            raise ValueError("enter the date you applied (not in the future)")
        return value


class ApplicationUpdate(ApplicationInput):
    """A full replacement of the entry the client last saw (optimistic concurrency)."""

    expected_version: int = Field(ge=1, description="lock_version the client last saw")


class TrackedResume(BaseModel):
    """The approved resume an entry points at."""

    approval_id: uuid.UUID
    run_id: uuid.UUID
    filename: str
    draft_version: int
    approved_at: datetime


class ApplicationOut(BaseModel):
    id: uuid.UUID
    company: str
    role: str
    job_url: str | None
    applied_on: date | None
    status: ApplicationStatus
    notes: str | None
    resume: TrackedResume | None
    lock_version: int
    created_at: datetime
    updated_at: datetime
