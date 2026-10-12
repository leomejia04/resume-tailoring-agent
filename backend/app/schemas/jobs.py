from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from app.services.job_text import normalize_job_text

MIN_DESCRIPTION_CHARS = 40
MAX_DESCRIPTION_CHARS = 50_000
_URL_ONLY = re.compile(r"https?://\S+")
MAX_URL_CHARS = 2048


def plain_http_url(url: HttpUrl | None) -> HttpUrl | None:
    """A stored link label: http(s) only (HttpUrl), no embedded credentials, bounded length."""
    if url is not None:
        if url.username or url.password:
            raise ValueError("links must not contain a username or password")
        if len(str(url)) > MAX_URL_CHARS:
            raise ValueError("link is too long")
    return url


class JobInputRequest(BaseModel):
    """A pasted job posting. Optional fields may be omitted or sent as empty strings."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str | None = Field(default=None, max_length=200)
    company: str | None = Field(default=None, max_length=200)
    source_url: HttpUrl | None = Field(
        default=None, description="Where the posting was found. Stored as a label; not fetched."
    )
    description: str = Field(max_length=MAX_DESCRIPTION_CHARS)

    @field_validator("title", "company", "source_url", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("source_url")
    @classmethod
    def _plain_url(cls, url: HttpUrl | None) -> HttpUrl | None:
        return plain_http_url(url)

    @field_validator("description")
    @classmethod
    def _normalized_description(cls, value: str) -> str:
        text = normalize_job_text(value)
        if _URL_ONLY.fullmatch(text):
            raise ValueError("paste the job description text; opening links is not available yet")
        if len(text) < MIN_DESCRIPTION_CHARS:
            raise ValueError(
                f"paste the full job description (at least {MIN_DESCRIPTION_CHARS} characters)"
            )
        return text


class SectionKind(StrEnum):
    REQUIRED = "required"
    PREFERRED = "preferred"
    RESPONSIBILITIES = "responsibilities"
    ABOUT_ROLE = "about_role"
    ABOUT_COMPANY = "about_company"
    BENEFITS = "benefits"


class DetectedSection(BaseModel):
    kind: SectionKind
    heading: str = Field(max_length=120)
    line: int = Field(ge=1, description="1-based line number in the normalized description")


class PreviewWarning(BaseModel):
    code: Literal["short_description", "no_requirements_heading"]
    message: str


class JobInputPreview(BaseModel):
    title: str | None
    company: str | None
    source_url: HttpUrl | None
    character_count: int = Field(ge=0)
    word_count: int = Field(ge=0)
    sections: list[DetectedSection]
    warnings: list[PreviewWarning]
    saved: Literal[False] = Field(
        default=False, description="Previews are never stored; saving requires an account."
    )
