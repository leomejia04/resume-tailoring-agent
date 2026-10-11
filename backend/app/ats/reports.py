"""Saved ATS reports: score a subject, store an insert-only row, decide what is current.

Scoring is deterministic and fast, so until the durable worker (milestone 6c)
it runs inline, in the same transaction that creates its subject: a draft
version or a rendered artifact. A scoring error is stored as a failed report
and never blocks saving the draft; approval is what requires a complete report
for the exact PDF.

Reports never change. Whether one still applies is decided here when reading:
it must be for the run's current subject (master version, draft version, or
artifact), its analysis, and the current rubric version; otherwise the UI shows
it as stale or pending, without a number.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.ats.passages import Passage, draft_passages, pdf_passages, text_passages
from app.ats.scoring import RUBRIC_VERSION, AtsResult, analysis_fingerprint, compare, score
from app.db.models import AtsReport, JobAnalysis, JobPostingVersion, TailoringRun
from app.schemas.ats import AtsOverview, AtsReportOut, ReportKind, ShownStatus
from app.tailoring.models import AnalysisContent, DraftContent

logger = logging.getLogger("app.ats")

FAILURE_MESSAGES = {
    "scoring_error": "The check could not be completed.",
    "storage_unavailable": "Your file could not be read right now.",
    "no_text": "No selectable text was found to check.",
}
APPROVAL_BLOCKED = {
    "pending": "The ATS check for this PDF has not finished. Run the check, then approve.",
    "failed": "The ATS check for this PDF failed. Run the check again before approving.",
    "stale": "The ATS check for this PDF is out of date. Run the check again before approving.",
}


@dataclass(frozen=True)
class ScoringInputs:
    owner_id: uuid.UUID
    run_id: uuid.UUID
    analysis_id: uuid.UUID
    analysis: AnalysisContent
    fingerprint: str
    job_text: str


def scoring_inputs(db: Session, run: TailoringRun) -> ScoringInputs | None:
    """What every report of this run is scored against; None until the posting is analyzed."""
    if run.analysis_id is None:
        return None
    content = db.scalars(
        select(JobAnalysis.content).where(
            JobAnalysis.id == run.analysis_id, JobAnalysis.owner_id == run.owner_id
        )
    ).one()
    job_text = db.scalars(
        select(JobPostingVersion.description).where(
            JobPostingVersion.id == run.posting_version_id,
            JobPostingVersion.owner_id == run.owner_id,
        )
    ).one()
    analysis = AnalysisContent.model_validate(content)
    return ScoringInputs(
        owner_id=run.owner_id,
        run_id=run.id,
        analysis_id=run.analysis_id,
        analysis=analysis,
        fingerprint=analysis_fingerprint(analysis),
        job_text=job_text,
    )


def _record(
    db: Session,
    inputs: ScoringInputs,
    kind: ReportKind,
    passages: Callable[[], Sequence[Passage]],
    *,
    error_code: str | None = None,
    **subject: uuid.UUID,
) -> None:
    values: dict[str, Any] = {
        "owner_id": inputs.owner_id,
        "run_id": inputs.run_id,
        "analysis_id": inputs.analysis_id,
        "kind": kind,
        "rubric_version": RUBRIC_VERSION,
        "analysis_fingerprint": inputs.fingerprint,
        **subject,
    }
    result: AtsResult | None = None
    if error_code is None:
        try:
            result = score(inputs.analysis, passages(), job_text=inputs.job_text)
        except Exception as exc:  # deterministic code: a bug must not lose the user's edit
            # Only the exception type: messages can quote resume text.
            logger.error(
                "ats scoring failed kind=%s run_id=%s error=%s",
                kind,
                inputs.run_id,
                type(exc).__name__,
            )
            error_code = "scoring_error"
    if result is not None:
        values |= {
            "status": "complete",
            "score": result.score,
            "result": result.model_dump(mode="json"),
        }
    else:
        values |= {"status": "failed", "error_code": error_code}
    # A concurrent check of the same subject may have stored it first; keep that one.
    db.execute(insert(AtsReport).values(**values).on_conflict_do_nothing())


def record_draft(
    db: Session, inputs: ScoringInputs, draft_version_id: uuid.UUID, content: DraftContent
) -> None:
    _record(db, inputs, "draft", lambda: draft_passages(content), draft_version_id=draft_version_id)


def record_pdf(
    db: Session,
    inputs: ScoringInputs,
    artifact_id: uuid.UUID,
    content: DraftContent,
    pdf_text: str | None,
    error_code: str | None = None,
) -> None:
    """Score the text extracted from the exact PDF; ``content`` only attributes passages."""
    if error_code is None and not (pdf_text or "").strip():
        error_code = "no_text"
    _record(
        db,
        inputs,
        "final_pdf",
        lambda: pdf_passages(content, pdf_text or ""),
        error_code=error_code,
        artifact_id=artifact_id,
    )


def record_master(
    db: Session,
    inputs: ScoringInputs,
    master_version_id: uuid.UUID,
    master_text: str | None,
    error_code: str | None = None,
) -> None:
    if error_code is None and not (master_text or "").strip():
        error_code = "no_text"
    _record(
        db,
        inputs,
        "master_baseline",
        lambda: text_passages(master_text or "", "Master"),
        error_code=error_code,
        master_version_id=master_version_id,
    )


# --- Reading: which report is current ------------------------------------------


@dataclass(frozen=True)
class _Row:
    id: uuid.UUID
    kind: str
    subject: uuid.UUID
    analysis_id: uuid.UUID
    status: str
    rubric_version: str
    error_code: str | None
    created_at: Any


_SUBJECT = {
    "master_baseline": AtsReport.master_version_id,
    "draft": AtsReport.draft_version_id,
    "final_pdf": AtsReport.artifact_id,
}


def _rows(db: Session, run: TailoringRun, kinds: Sequence[ReportKind]) -> list[_Row]:
    """Report metadata for this run (results are loaded only for the chosen reports)."""
    rows = db.execute(
        select(
            AtsReport.id,
            AtsReport.kind,
            AtsReport.master_version_id,
            AtsReport.draft_version_id,
            AtsReport.artifact_id,
            AtsReport.analysis_id,
            AtsReport.status,
            AtsReport.rubric_version,
            AtsReport.error_code,
            AtsReport.created_at,
        )
        .where(
            AtsReport.run_id == run.id,
            AtsReport.owner_id == run.owner_id,
            AtsReport.kind.in_(kinds),
        )
        .order_by(AtsReport.created_at.desc())
    ).all()
    return [
        _Row(
            id=r.id,
            kind=r.kind,
            subject=r.master_version_id or r.draft_version_id or r.artifact_id,
            analysis_id=r.analysis_id,
            status=r.status,
            rubric_version=r.rubric_version,
            error_code=r.error_code,
            created_at=r.created_at,
        )
        for r in rows
    ]


def _pick(
    rows: Sequence[_Row], run: TailoringRun, kind: str, subject: uuid.UUID
) -> tuple[ShownStatus, _Row | None]:
    """The report to show for one subject: any complete current one, else the latest."""
    mine = [r for r in rows if r.kind == kind and r.subject == subject]
    current = [
        r for r in mine if r.analysis_id == run.analysis_id and r.rubric_version == RUBRIC_VERSION
    ]
    complete = next((r for r in current if r.status == "complete"), None)
    if complete is not None:
        return "complete", complete
    if current:
        return current[0].status, current[0]  # type: ignore[return-value]
    if mine:
        return "stale", mine[0]  # checked with an older rubric or analysis
    return "pending", None


def subject_status(
    db: Session, run: TailoringRun, kind: ReportKind, subject: uuid.UUID
) -> ShownStatus:
    return _pick(_rows(db, run, [kind]), run, kind, subject)[0]


def needs_check(db: Session, run: TailoringRun, kind: ReportKind, subject: uuid.UUID) -> bool:
    return subject_status(db, run, kind, subject) != "complete"


def _out(
    kind: ReportKind,
    status: ShownStatus,
    row: _Row | None,
    results: dict[uuid.UUID, AtsResult],
    **extra: Any,
) -> AtsReportOut:
    result = results.get(row.id) if row is not None and status == "complete" else None
    message = None
    if status == "failed" and row is not None:
        message = FAILURE_MESSAGES.get(row.error_code or "", FAILURE_MESSAGES["scoring_error"])
    elif status == "stale":
        message = "Checked with an older version of the rubric or job analysis."
    return AtsReportOut(
        kind=kind,
        status=status,
        score=result.score if result else None,
        result=result,
        rubric_version=row.rubric_version if row else None,
        checked_at=row.created_at if row else None,
        message=message,
        **extra,
    )


def overview(
    db: Session,
    run: TailoringRun,
    *,
    master_version_id: uuid.UUID | None,
    draft_version: tuple[uuid.UUID, int] | None,
    artifact_id: uuid.UUID | None,
    older_pdf_version: int | None = None,
) -> AtsOverview | None:
    """Reports for the run's current master, draft version, and PDF.

    ``older_pdf_version`` is the draft version of the latest PDF when no PDF
    exists for the current version: that PDF's check no longer applies.
    """
    if run.analysis_id is None:
        return None
    rows = _rows(db, run, ["master_baseline", "draft", "final_pdf"])
    picks: dict[str, tuple[ShownStatus, _Row | None]] = {}
    if master_version_id is not None:
        status, row = _pick(rows, run, "master_baseline", master_version_id)
        if status == "pending" and any(r.kind == "master_baseline" for r in rows):
            status = "stale"  # only an older master version was checked
        picks["master_baseline"] = (status, row)
    if draft_version is not None:
        picks["draft"] = _pick(rows, run, "draft", draft_version[0])
    if artifact_id is not None:
        picks["final_pdf"] = _pick(rows, run, "final_pdf", artifact_id)

    wanted = [row.id for status, row in picks.values() if status == "complete" and row]
    results = {
        report_id: AtsResult.model_validate(result)
        for report_id, result in db.execute(
            select(AtsReport.id, AtsReport.result).where(AtsReport.id.in_(wanted))
        ).all()
    }

    baseline = draft = final_pdf = None
    if "master_baseline" in picks:
        status, row = picks["master_baseline"]
        baseline = _out(
            "master_baseline", status, row, results, master_version_id=master_version_id
        )
        if status == "stale" and row is None:
            baseline.message = "Your master resume changed since it was last checked."
    if draft_version is not None:
        status, row = picks["draft"]
        draft = _out("draft", status, row, results, draft_version=draft_version[1])
    if artifact_id is not None and draft_version is not None:
        status, row = picks["final_pdf"]
        final_pdf = _out(
            "final_pdf",
            status,
            row,
            results,
            draft_version=draft_version[1],
            artifact_id=artifact_id,
        )
    elif older_pdf_version is not None:
        final_pdf = AtsReportOut(
            kind="final_pdf",
            status="stale",
            score=None,
            result=None,
            rubric_version=None,
            draft_version=older_pdf_version,
            checked_at=None,
            message="The draft changed after the last PDF. Create the PDF again to check it.",
        )

    def versus(other: AtsReportOut | None):
        if baseline and baseline.result and other and other.result:
            return compare(baseline.result, other.result)
        return None

    return AtsOverview(
        rubric_version=RUBRIC_VERSION,
        baseline=baseline,
        draft=draft,
        final_pdf=final_pdf,
        draft_vs_baseline=versus(draft),
        pdf_vs_baseline=versus(final_pdf),
    )


def draft_scores(db: Session, run: TailoringRun) -> dict[uuid.UUID, tuple[ShownStatus, int | None]]:
    """Status and score of each draft version's report, for the version history."""
    rows = _rows(db, run, ["draft"])
    picked = {subject: _pick(rows, run, "draft", subject) for subject in {r.subject for r in rows}}
    complete = [row.id for status, row in picked.values() if status == "complete" and row]
    scores = dict(
        db.execute(select(AtsReport.id, AtsReport.score).where(AtsReport.id.in_(complete))).all()
    )
    return {
        subject: (status, scores.get(row.id) if row and status == "complete" else None)
        for subject, (status, row) in picked.items()
    }
