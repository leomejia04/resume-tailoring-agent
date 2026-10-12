"""Formatting preferences applied when rendering (stored with each draft version)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def is_entry_line(text: str, entry: str | None) -> bool:
    """A fact that is just its entry line (e.g. a degree) is shown as the line, not a bullet."""

    def key(value: str) -> str:
        return " ".join(value.lower().split()).strip(" .")

    return entry is not None and key(text) == key(entry)


class FormatPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # "standard": the shared layout (ADR 0011); "master": edit a DOCX master in
    # place, keeping its own design (PDF masters always use the standard layout).
    layout: Literal["standard", "master"] = "standard"
    max_bullets_per_entry: int = Field(default=4, ge=1, le=8)
    project_bullets: int = Field(default=3, ge=1, le=6)
    bold_project_technologies: bool = True
    languages_on_own_line: bool = True
    show_headline: bool = False
    # Entry lines exactly as in the master; locked entries keep their master bullets.
    locked_entries: list[str] = Field(default_factory=list, max_length=30)
