"""ATS match estimate reports as the API returns them (ADR 0006)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.ats.scoring import AtsResult, Comparison

ReportKind = Literal["master_baseline", "draft", "final_pdf"]
# "stale": a report exists but no longer matches the run (older rubric, older
# master, or a PDF made before the latest draft change). Only "complete" has a score.
ShownStatus = Literal["pending", "complete", "failed", "stale"]


class AtsReportOut(BaseModel):
    kind: ReportKind
    status: ShownStatus
    score: int | None
    result: AtsResult | None
    rubric_version: str | None
    draft_version: int | None = None
    artifact_id: uuid.UUID | None = None
    master_version_id: uuid.UUID | None = None
    checked_at: datetime | None
    message: str | None = None


class AtsOverview(BaseModel):
    rubric_version: str
    # None when there is nothing to check: no master, no draft, or no PDF yet.
    baseline: AtsReportOut | None
    draft: AtsReportOut | None
    final_pdf: AtsReportOut | None
    draft_vs_baseline: Comparison | None
    pdf_vs_baseline: Comparison | None
