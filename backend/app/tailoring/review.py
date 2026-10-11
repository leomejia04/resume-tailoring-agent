"""Review, revision, rendering, approval, and download of drafts.

Every change creates a new immutable draft version on top of the version the
client last saw (409 otherwise). Direct edits, restores, section regeneration,
and chat revisions pass the same deterministic checks as generation. Approval
binds one version to the SHA-256 of its rendered PDF; any later version is
unapproved until approved again. Every new version and every rendered PDF gets
an ATS report, and approval requires a complete report for that exact PDF.

Model calls, rendering, and file-based checks run in the durable worker
(ADR 0007). They read and write private files only through the signed URLs
their job was granted, never with a session or service key.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, select, update
from sqlalchemy.orm import Session

from app.ai import prompts
from app.ai.client import LLMClient
from app.ai.schemas import ChatPatchOut, EntryOut, TailoredDraftOut
from app.api.errors import ApiError
from app.ats import reports
from app.auth.dependencies import unavailable, user_db
from app.auth.tokens import VerifiedUser
from app.config import Settings
from app.db.models import (
    Approval,
    Artifact,
    ChatMessage,
    Draft,
    DraftVersion,
    EvidenceRecord,
    EvidenceVersion,
    JobAnalysis,
    JobPosting,
    JobPostingVersion,
    Profile,
    TailoringRun,
)
from app.documents.extract import extract_docx, extract_pdf
from app.documents.pdf_inspect import pdf_text
from app.jobs import queue
from app.render.filename import download_filename
from app.render.format import FormatPreferences
from app.render.pipeline import render_draft
from app.render.reconstructed_docx import ExtraEntry
from app.render.template_docx import FactLocation
from app.schemas.ats import ShownStatus
from app.schemas.errors import FieldError
from app.storage import (
    SignedObject,
    StorageObjectMissingError,
    StorageRejectedError,
    StorageUnavailableError,
    UserStorage,
)
from app.tailoring.context import ProfileContext, build_context, draft_payload
from app.tailoring.models import (
    AnalysisContent,
    DraftBullet,
    DraftContent,
    DraftEntry,
    DraftSkill,
    OmittedFact,
    ValidationIssue,
    ValidationReport,
)
from app.tailoring.runs import (
    _owned_run,
    _verified_rows,
    call_model,
    ensure_baseline,
    run_master,
    transition,
)
from app.tailoring.validate import (
    RULE_VERSION,
    check,
    check_text,
    finalize,
    headline_issues,
    requirement_terms,
)

logger = logging.getLogger("app.review")


# --- Edit operations (validated request shapes) ---------------------------------


class _Op(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class UpdateBullet(_Op):
    op: Literal["update_bullet"]
    entry_ref: str = Field(max_length=10)
    index: int = Field(ge=0, le=20)
    text: str = Field(min_length=1, max_length=350)


class RemoveBullet(_Op):
    op: Literal["remove_bullet"]
    entry_ref: str = Field(max_length=10)
    index: int = Field(ge=0, le=20)


class MoveBullet(_Op):
    op: Literal["move_bullet"]
    entry_ref: str = Field(max_length=10)
    index: int = Field(ge=0, le=20)
    to: int = Field(ge=0, le=20)


class AddFact(_Op):
    op: Literal["add_fact"]
    evidence_id: uuid.UUID


class UpdateSkill(_Op):
    op: Literal["update_skill"]
    index: int = Field(ge=0, le=30)
    text: str = Field(min_length=1, max_length=350)


class RemoveSkill(_Op):
    op: Literal["remove_skill"]
    index: int = Field(ge=0, le=30)


class UpdateHeadline(_Op):
    op: Literal["update_headline"]
    text: str = Field(min_length=1, max_length=120)


class UpdateSummary(_Op):
    op: Literal["update_summary"]
    text: str | None = Field(default=None, max_length=600)


class UpdateFormat(_Op):
    op: Literal["update_format"]
    format: FormatPreferences


EditOperation = Annotated[
    UpdateBullet
    | RemoveBullet
    | MoveBullet
    | AddFact
    | UpdateSkill
    | RemoveSkill
    | UpdateHeadline
    | UpdateSummary
    | UpdateFormat,
    Field(discriminator="op"),
]


# --- Shared helpers -------------------------------------------------------------


def _draft_state(
    db: Session, user: VerifiedUser, run: TailoringRun
) -> tuple[Draft, DraftVersion, DraftContent]:
    draft = db.scalars(
        select(Draft).where(Draft.run_id == run.id, Draft.owner_id == user.id)
    ).first()
    if draft is None:
        raise ApiError(409, "invalid_state", "Generate a draft first.")
    version = db.scalars(
        select(DraftVersion).where(
            DraftVersion.draft_id == draft.id, DraftVersion.version == draft.current_version
        )
    ).one()
    return draft, version, DraftContent.model_validate(version.content)


def _analysis(db: Session, run: TailoringRun) -> AnalysisContent:
    return AnalysisContent.model_validate(
        db.scalars(select(JobAnalysis.content).where(JobAnalysis.id == run.analysis_id)).one()
    )


def _facts_by_id(context: ProfileContext) -> dict[uuid.UUID, object]:
    return {fact.evidence_id: fact for fact in context.facts.values()}


def _recompute_omitted(content: DraftContent, context: ProfileContext) -> None:
    cited = {i for e in content.entries for b in e.bullets for i in b.evidence_ids}
    cited |= {i for s in content.skills for i in s.evidence_ids}
    content.omitted = [
        OmittedFact(evidence_id=f.evidence_id, reason="not_selected")
        for f in context.facts.values()
        if f.evidence_id not in cited
    ]


def save_version(
    db: Session,
    user: VerifiedUser,
    run: TailoringRun,
    draft: Draft,
    expected_version: int,
    content: DraftContent,
    report: ValidationReport,
    created_by: str,
    model: str | None = None,
    prompt_version: str | None = None,
) -> int:
    """Append a version on top of ``expected_version``; a newer version means 409."""
    moved = db.execute(
        update(Draft)
        .where(Draft.id == draft.id, Draft.current_version == expected_version)
        .values(current_version=expected_version + 1)
    )
    if moved.rowcount != 1:
        raise ApiError(
            409, "conflict", "This draft changed in another tab. Reload to see the latest version."
        )
    version_id = uuid.uuid4()
    db.add(
        DraftVersion(
            id=version_id,
            draft_id=draft.id,
            owner_id=user.id,
            version=expected_version + 1,
            content=content.model_dump(mode="json"),
            validation=report.model_dump(mode="json"),
            created_by=created_by,
            model=model,
            prompt_version=prompt_version,
        )
    )
    db.flush()
    inputs = reports.scoring_inputs(db, run)
    if inputs is not None:
        reports.record_draft(db, inputs, version_id, content)
    if run.status == "approved":
        transition(db, run, "awaiting_review")  # the new version is not approved
    return expected_version + 1


def _issues_error(issues: list[ValidationIssue]) -> ApiError:
    return ApiError(
        422,
        "unsupported_edit",
        "Some changes are not supported by your confirmed facts. "
        "If they are true, add them on the Experience page first.",
        fields=[FieldError(loc=["body", i.where], message=i.message, type=i.code) for i in issues][
            :20
        ],
    )


def _passing_report() -> ValidationReport:
    return ValidationReport(
        rule_version=RULE_VERSION, issues=[], removed=[], repaired=False, passed=True
    )


# --- Direct edits and restore ---------------------------------------------------


def apply_edits(
    settings: Settings,
    user: VerifiedUser,
    run_id: uuid.UUID,
    expected_version: int,
    operations: list,
) -> int:
    with user_db(settings, user) as db:
        run = _owned_run(db, user, run_id)
        draft, _, content = _draft_state(db, user, run)
        if draft.current_version != expected_version:
            raise ApiError(
                409,
                "conflict",
                "This draft changed in another tab. Reload to see the latest version.",
            )
        analysis = _analysis(db, run)
        context = build_context(_verified_rows(db, user), settings.max_context_chars)
        facts = _facts_by_id(context)
        terms = requirement_terms(analysis)
        content = content.model_copy(deep=True)
        issues: list[ValidationIssue] = []

        def entry(ref: str) -> DraftEntry:
            found = next((e for e in content.entries if e.ref == ref), None)
            if found is None:
                raise ApiError(404, "not_found", "That section of the draft was not found.")
            return found

        for number, op in enumerate(operations, start=1):
            where = f"operations.{number}"
            if isinstance(op, UpdateBullet):
                target = entry(op.entry_ref)
                if op.index >= len(target.bullets):
                    raise ApiError(404, "not_found", "That bullet was not found.")
                bullet = target.bullets[op.index]
                cited = [facts.get(i) for i in bullet.evidence_ids]
                if any(f is None for f in cited):
                    issues.append(
                        ValidationIssue(
                            code="evidence_not_confirmed",
                            severity="error",
                            where=where,
                            message="This bullet cites a fact that is no longer confirmed.",
                        )
                    )
                    continue
                support = "\n".join(
                    [f.text for f in cited] + [target.entry or "", target.date_text or ""]
                )  # type: ignore[union-attr]
                found = check_text(op.text, support, where, terms)
                issues.extend(found)
                if not [i for i in found if i.severity == "error"]:
                    bullet.text = op.text
            elif isinstance(op, RemoveBullet):
                target = entry(op.entry_ref)
                if op.index >= len(target.bullets):
                    raise ApiError(404, "not_found", "That bullet was not found.")
                target.bullets.pop(op.index)
                if not target.bullets:
                    content.entries.remove(target)
            elif isinstance(op, MoveBullet):
                target = entry(op.entry_ref)
                if op.index >= len(target.bullets) or op.to >= len(target.bullets):
                    raise ApiError(404, "not_found", "That bullet was not found.")
                target.bullets.insert(op.to, target.bullets.pop(op.index))
            elif isinstance(op, AddFact):
                fact = facts.get(op.evidence_id)
                if fact is None:
                    raise ApiError(404, "not_found", "That confirmed fact was not found.")
                if fact.entry_ref is None:  # type: ignore[attr-defined]
                    content.skills.append(
                        DraftSkill(text=fact.text, evidence_ids=[fact.evidence_id])
                    )  # type: ignore[attr-defined]
                    continue
                target = next(
                    (
                        e
                        for e in content.entries
                        if (e.section, e.entry, e.date_text)
                        == (fact.section, fact.entry, fact.date_text)  # type: ignore[attr-defined]
                    ),
                    None,
                )
                if target is None:
                    used = {e.ref for e in content.entries}
                    ref = next(f"J{n}" for n in range(1, 200) if f"J{n}" not in used)
                    target = DraftEntry(
                        ref=ref,
                        section=fact.section,  # type: ignore[attr-defined]
                        entry=fact.entry,  # type: ignore[attr-defined]
                        date_text=fact.date_text,  # type: ignore[attr-defined]
                        experience_type=fact.experience_type,  # type: ignore[attr-defined]
                        bullets=[],
                    )
                    content.entries.append(target)
                if any(fact.evidence_id in b.evidence_ids for b in target.bullets):  # type: ignore[attr-defined]
                    continue
                target.bullets.append(DraftBullet(text=fact.text, evidence_ids=[fact.evidence_id]))  # type: ignore[attr-defined]
            elif isinstance(op, UpdateSkill):
                if op.index >= len(content.skills):
                    raise ApiError(404, "not_found", "That skill line was not found.")
                skill = content.skills[op.index]
                cited = [facts.get(i) for i in skill.evidence_ids]
                support = "\n".join(f.text for f in cited if f is not None)  # type: ignore[union-attr]
                found = check_text(op.text, support, where, terms)
                issues.extend(found)
                if not [i for i in found if i.severity == "error"]:
                    skill.text = op.text
            elif isinstance(op, RemoveSkill):
                if op.index >= len(content.skills):
                    raise ApiError(404, "not_found", "That skill line was not found.")
                content.skills.pop(op.index)
            elif isinstance(op, UpdateHeadline):
                found = headline_issues(
                    op.text, context.all_text + "\n" + analysis.role_title, terms
                )
                issues.extend(found)
                if not found:
                    content.headline = op.text
            elif isinstance(op, UpdateSummary):
                if op.text:
                    found = check_text(op.text, context.all_text, where, terms)
                    issues.extend(found)
                    if not [i for i in found if i.severity == "error"]:
                        content.summary = op.text
                else:
                    content.summary = None
            elif isinstance(op, UpdateFormat):
                content.format = op.format

        errors = [i for i in issues if i.severity == "error"]
        if errors:
            raise _issues_error(errors)
        _recompute_omitted(content, context)
        return save_version(
            db, user, run, draft, expected_version, content, _passing_report(), "edit"
        )


def restore_version(
    settings: Settings,
    user: VerifiedUser,
    run_id: uuid.UUID,
    version_number: int,
    expected_version: int,
) -> int:
    with user_db(settings, user) as db:
        run = _owned_run(db, user, run_id)
        draft, _, _ = _draft_state(db, user, run)
        old = db.scalars(
            select(DraftVersion).where(
                DraftVersion.draft_id == draft.id, DraftVersion.version == version_number
            )
        ).first()
        if old is None:
            raise ApiError(404, "not_found", "That draft version was not found.")
        content = DraftContent.model_validate(old.content)
        context = build_context(_verified_rows(db, user), settings.max_context_chars)
        confirmed = _facts_by_id(context)
        cited = {i for e in content.entries for b in e.bullets for i in b.evidence_ids}
        cited |= {i for s in content.skills for i in s.evidence_ids}
        if cited - set(confirmed):
            raise ApiError(
                409,
                "facts_changed",
                "That version uses facts you have since un-confirmed. "
                "Edit the current version instead.",
            )
        _recompute_omitted(content, context)
        report = ValidationReport.model_validate(old.validation)
        return save_version(db, user, run, draft, expected_version, content, report, "edit")


def list_versions(
    settings: Settings, user: VerifiedUser, run_id: uuid.UUID
) -> list[tuple[DraftVersion, ShownStatus, int | None]]:
    """Versions, newest first, with the status and score of each one's ATS report."""
    with user_db(settings, user) as db:
        run = _owned_run(db, user, run_id)
        draft = db.scalars(
            select(Draft).where(Draft.run_id == run.id, Draft.owner_id == user.id)
        ).first()
        if draft is None:
            return []
        versions = db.scalars(
            select(DraftVersion)
            .where(DraftVersion.draft_id == draft.id, DraftVersion.owner_id == user.id)
            .order_by(DraftVersion.version.desc())
        ).all()
        scores = reports.draft_scores(db, run)
        return [(v, *scores.get(v.id, ("pending", None))) for v in versions]


# --- Model-assisted revisions ---------------------------------------------------


def _alias_draft(content: DraftContent, context: ProfileContext) -> dict:
    alias = {f.evidence_id: f.alias for f in context.facts.values()}
    return {
        "headline": content.headline,
        "summary": content.summary,
        "entries": [
            {
                "entry_ref": e.ref,
                "bullets": [
                    {
                        "text": b.text,
                        "evidence_ids": [alias[i] for i in b.evidence_ids if i in alias],
                    }
                    for b in e.bullets
                ],
            }
            for e in content.entries
        ],
    }


def _to_context_refs(
    content: DraftContent, context: ProfileContext
) -> tuple[DraftContent, dict[str, str]]:
    """Re-key draft entries to the current context refs (J-numbers can shift as facts change).

    Returns the re-keyed copy and a map from context ref back to the draft's ref.
    """
    keyed = content.model_copy(deep=True)
    back: dict[str, str] = {}
    for entry in keyed.entries:
        group = next(
            (
                g
                for g in context.entries.values()
                if (g.section, g.entry, g.date_text)
                == (entry.section, entry.entry, entry.date_text)
            ),
            None,
        )
        if group is not None:
            back[group.ref] = entry.ref
            entry.ref = group.ref
    return keyed, back


def _from_context_refs(content: DraftContent, back: dict[str, str]) -> DraftContent:
    used = set(back.values())
    for entry in content.entries:
        if entry.ref in back:
            entry.ref = back[entry.ref]
        elif entry.ref in used:  # a new entry colliding with an existing draft ref
            entry.ref = next(f"J{n}" for n in range(100, 400) if f"J{n}" not in used)
        used.add(entry.ref)
    return content


def _merge_entries(
    content: DraftContent,
    patch_entries: list[EntryOut],
    context: ProfileContext,
    analysis: AnalysisContent,
    headline: str | None,
    summary: str | None,
    change_summary: bool,
) -> tuple[DraftContent, ValidationReport]:
    """Validate patched entries like a draft, then splice survivors into the content."""
    trial = TailoredDraftOut(
        headline=headline or content.headline,
        summary=summary if change_summary else None,
        entries=patch_entries,
        skills=[],
        omitted_evidence_ids=[],
    )
    checked = check(trial, context, analysis)
    patched, report = finalize(trial, context, analysis, checked, repaired=False)
    merged = content.model_copy(deep=True)
    by_ref = {e.ref: e for e in patched.entries}
    touched = {e.entry_ref for e in patch_entries}
    new_entries = []
    for existing in merged.entries:
        if existing.ref in touched:
            if existing.ref in by_ref:
                new_entries.append(by_ref[existing.ref])
            # an entry whose every new bullet failed keeps its previous bullets
            else:
                new_entries.append(existing)
        else:
            new_entries.append(existing)
    for ref, entry in by_ref.items():
        if ref not in {e.ref for e in merged.entries}:
            new_entries.append(entry)
    merged.entries = new_entries
    if headline and not checked.bad_headline:
        merged.headline = patched.headline
    if change_summary and not checked.bad_summary:
        merged.summary = patched.summary
    _recompute_omitted(merged, context)
    return merged, report


def regenerate_entry(
    settings: Settings,
    user: VerifiedUser,
    client: LLMClient,
    run_id: uuid.UUID,
    entry_ref: str,
    expected_version: int,
) -> int:
    with user_db(settings, user) as db:
        run = _owned_run(db, user, run_id)
        draft, _, content = _draft_state(db, user, run)
        if draft.current_version != expected_version:
            raise ApiError(
                409,
                "conflict",
                "This draft changed in another tab. Reload to see the latest version.",
            )
        analysis = _analysis(db, run)
        context = build_context(_verified_rows(db, user), settings.max_context_chars)
        posting = db.execute(
            select(JobPosting.title, JobPosting.company)
            .join(JobPostingVersion, JobPostingVersion.posting_id == JobPosting.id)
            .where(JobPostingVersion.id == run.posting_version_id)
        ).one()
    keyed, back = _to_context_refs(content, context)
    group_ref = next((ctx for ctx, draft_ref in back.items() if draft_ref == entry_ref), None)
    if group_ref is None:
        raise ApiError(404, "not_found", "That section of the draft was not found.")
    payload = draft_payload(context, analysis, posting.title, posting.company)
    payload["entries"] = [e for e in payload["entries"] if e["ref"] == group_ref]
    payload["unattached_facts"] = []
    # Model errors propagate: the job runner retries timeouts and rate limits.
    result = call_model(
        settings,
        user,
        client,
        run_id=run_id,
        operation="regenerate",
        prompt_version=prompts.REGENERATE_VERSION,
        instructions=prompts.TAILOR_INSTRUCTIONS + prompts.REGENERATE_SUFFIX,
        payload=payload,
        schema=TailoredDraftOut,
        max_output_tokens=settings.draft_max_output_tokens,
    )
    entries = [e for e in result.output.entries if e.entry_ref == group_ref][:1]
    merged, report = _merge_entries(keyed, entries, context, analysis, None, None, False)
    merged = _from_context_refs(merged, back)
    with user_db(settings, user) as db:
        run = _owned_run(db, user, run_id)
        draft, _, _ = _draft_state(db, user, run)
        return save_version(
            db,
            user,
            run,
            draft,
            expected_version,
            merged,
            report,
            "generation",
            result.model,
            prompts.REGENERATE_VERSION,
        )


def chat_revision(
    settings: Settings,
    user: VerifiedUser,
    client: LLMClient,
    run_id: uuid.UUID,
    message: str,
    expected_version: int,
) -> tuple[int | None, str]:
    with user_db(settings, user) as db:
        run = _owned_run(db, user, run_id)
        draft, _, content = _draft_state(db, user, run)
        if draft.current_version != expected_version:
            raise ApiError(
                409,
                "conflict",
                "This draft changed in another tab. Reload to see the latest version.",
            )
        analysis = _analysis(db, run)
        context = build_context(_verified_rows(db, user), settings.max_context_chars)
        posting = db.execute(
            select(JobPosting.title, JobPosting.company)
            .join(JobPostingVersion, JobPostingVersion.posting_id == JobPosting.id)
            .where(JobPostingVersion.id == run.posting_version_id)
        ).one()
    # Send the draft keyed by the current context refs (the model cites those).
    keyed, back = _to_context_refs(content, context)
    aliased = _alias_draft(keyed, context)
    payload = draft_payload(context, analysis, posting.title, posting.company)
    payload["current_draft"] = aliased
    payload["instruction"] = message
    result = call_model(
        settings,
        user,
        client,
        run_id=run_id,
        operation="chat",
        prompt_version=prompts.CHAT_VERSION,
        instructions=prompts.CHAT_INSTRUCTIONS,
        payload=payload,
        schema=ChatPatchOut,
        max_output_tokens=settings.draft_max_output_tokens,
    )
    patch = result.output
    reply = re.sub(r"\s+", " ", patch.reply or "").strip()[:500] or "Done."
    changed = bool(patch.entries) or patch.headline is not None or patch.change_summary
    new_version = None
    if changed:
        merged, report = _merge_entries(
            keyed,
            patch.entries,
            context,
            analysis,
            patch.headline,
            patch.summary,
            patch.change_summary,
        )
        merged = _from_context_refs(merged, back)
        if report.removed:
            reply += " Some suggested changes were not supported by your facts and were left out."
        with user_db(settings, user) as db:
            run = _owned_run(db, user, run_id)
            draft, _, _ = _draft_state(db, user, run)
            new_version = save_version(
                db,
                user,
                run,
                draft,
                expected_version,
                merged,
                report,
                "chat",
                result.model,
                prompts.CHAT_VERSION,
            )
    with user_db(settings, user) as db:
        db.add(
            ChatMessage(
                owner_id=user.id,
                run_id=run_id,
                role="assistant",
                content=reply,
                draft_version=new_version,
            )
        )
    return new_version, reply


# --- Rendering, approval, downloads ---------------------------------------------


def _prefix(profile: Profile | None) -> str | None:
    if profile is None:
        return None
    if profile.filename_prefix:
        return profile.filename_prefix
    if profile.display_name:
        return re.sub(r"[^A-Za-z0-9]", "", profile.display_name) or None
    return None


def render_version(
    settings: Settings,
    user: VerifiedUser,
    run_id: uuid.UUID,
    version_number: int,
    artifact_id: uuid.UUID,
    master_grant: queue.Grant | None,
    pdf_grant: queue.Grant,
    docx_grant: queue.Grant,
) -> uuid.UUID:
    """Worker side: render one draft version to the PDF the user will review and approve."""
    with user_db(settings, user) as db:
        run = _owned_run(db, user, run_id)
        draft = db.scalars(
            select(Draft).where(Draft.run_id == run.id, Draft.owner_id == user.id)
        ).first()
        version = (
            db.scalars(
                select(DraftVersion).where(
                    DraftVersion.draft_id == draft.id, DraftVersion.version == version_number
                )
            ).first()
            if draft
            else None
        )
        if version is None:
            raise ApiError(404, "not_found", "That draft version was not found.")
        content = DraftContent.model_validate(version.content)
        inputs = reports.scoring_inputs(db, run)
        master = run_master(db, user, run)
        rows = db.execute(
            select(EvidenceRecord, EvidenceVersion)
            .join(
                EvidenceVersion,
                and_(
                    EvidenceVersion.evidence_id == EvidenceRecord.id,
                    EvidenceVersion.version == EvidenceRecord.current_version,
                ),
            )
            .where(EvidenceRecord.owner_id == user.id)
        ).all()
        profile = db.scalars(select(Profile).where(Profile.user_id == user.id)).first()
        posting = db.execute(
            select(JobPosting.title, JobPosting.company)
            .join(JobPostingVersion, JobPostingVersion.posting_id == JobPosting.id)
            .where(JobPostingVersion.id == run.posting_version_id)
        ).one()
        version_id = version.id
        master_info = (master.storage_path, master.file_kind, master.id) if master else None
        filename = download_filename(_prefix(profile), posting.title, posting.company)
    if master_info and (master_grant is None or master_grant.object_path != master_info[0]):
        # The run's master changed after this job was queued; never fall back silently.
        raise ApiError(409, "master_changed", "Your master resume changed. Create the PDF again.")

    facts = {
        record.id: FactLocation(
            position=record.position
            if master_info and record.master_version_id == master_info[2]
            else None,
            entry=ev.entry,
        )
        for record, ev in rows
    }
    extras = [
        ExtraEntry(section="education", entry=ev.entry or ev.text, date_text=ev.date_text)
        for record, ev in rows
        if ev.verification_status == "verified" and ev.section == "education"
    ]
    master_bytes, header = None, []
    if master_info and master_grant:
        master_bytes = SignedObject(settings, master_grant.url).download()
        extraction = (
            extract_docx(master_bytes) if master_info[1] == "docx" else extract_pdf(master_bytes)
        )
        header = extraction.header_lines
    outcome = render_draft(
        master=master_bytes,
        master_kind=master_info[1] if master_info else None,
        header_lines=header,
        content=content,
        facts=facts,
        prefs=content.format,
        extras=extras,
    )

    sha = hashlib.sha256(outcome.pdf).hexdigest()
    with user_db(settings, user) as db:
        db.add(
            Artifact(
                id=artifact_id,
                owner_id=user.id,
                draft_version_id=version_id,
                storage_path=pdf_grant.object_path,
                docx_storage_path=docx_grant.object_path,
                sha256=sha,
                size_bytes=len(outcome.pdf),
                page_count=outcome.checks.page_count,
                layout=outcome.checks.layout,
                checks=outcome.checks.model_dump(mode="json")
                | {
                    "draft_version": version_number,
                    "rendered_bullets": sum(len(e.bullets) for e in outcome.content.entries),
                },
                passed=outcome.checks.passed,
                filename=filename,
            )
        )
        db.flush()
        if inputs is not None:  # the ATS check of this exact PDF
            reports.record_pdf(db, inputs, artifact_id, outcome.content, outcome.pdf_text)
        # Uploads go last, inside the transaction: if either fails, no artifact row exists.
        SignedObject(settings, pdf_grant.url).upload(outcome.pdf, "application/pdf")
        SignedObject(settings, docx_grant.url).upload(
            outcome.docx,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    return artifact_id


def approve(
    settings: Settings,
    user: VerifiedUser,
    run_id: uuid.UUID,
    draft_version: int,
    artifact_id: uuid.UUID,
    artifact_sha256: str,
) -> uuid.UUID:
    with user_db(settings, user) as db:
        run = _owned_run(db, user, run_id)
        draft, version, _ = _draft_state(db, user, run)
        stale = ApiError(
            409,
            "stale_approval",
            "The draft or PDF changed. Review the latest PDF before approving.",
        )
        if draft.current_version != draft_version:
            raise stale
        artifact = db.scalars(
            select(Artifact).where(Artifact.id == artifact_id, Artifact.owner_id == user.id)
        ).first()
        if artifact is None:
            raise ApiError(404, "not_found", "That PDF was not found.")
        if artifact.draft_version_id != version.id or artifact.sha256 != artifact_sha256:
            raise stale
        if not artifact.passed:
            raise ApiError(
                409,
                "checks_failed",
                "This PDF did not pass the layout checks, so it cannot be approved.",
            )
        if db.scalar(select(Approval.id).where(Approval.artifact_id == artifact.id)) is not None:
            raise ApiError(409, "conflict", "This PDF is already approved.")
        check = reports.subject_status(db, run, "final_pdf", artifact.id)
        if check != "complete":
            raise ApiError(409, f"ats_check_{check}", reports.APPROVAL_BLOCKED[check])
        approval = Approval(
            id=uuid.uuid4(),
            owner_id=user.id,
            draft_id=draft.id,
            draft_version=draft_version,
            artifact_id=artifact.id,
            artifact_sha256=artifact.sha256,
            filename=artifact.filename,
        )
        db.add(approval)
        transition(db, run, "approved")
        return approval.id


def run_checks(
    settings: Settings,
    user: VerifiedUser,
    run_id: uuid.UUID,
    master_grant: queue.Grant | None,
    artifact_grant: queue.Grant | None,
) -> None:
    """Worker side: check the run's current master, draft version, and PDF where no
    current report exists (retry after a failure, or re-check after a rubric upgrade).

    Existing complete reports are kept; nothing is scored twice.
    """
    with user_db(settings, user) as db:
        run = _owned_run(db, user, run_id)
        if run.analysis_id is None:
            raise ApiError(409, "invalid_state", "Analyze the job posting first.")
    ensure_baseline(settings, user, run_id, master_grant)
    with user_db(settings, user) as db:
        run = _owned_run(db, user, run_id)
        inputs = reports.scoring_inputs(db, run)
        draft = db.scalars(
            select(Draft).where(Draft.run_id == run.id, Draft.owner_id == user.id)
        ).first()
        if inputs is None or draft is None:
            return
        _, version, content = _draft_state(db, user, run)
        if reports.needs_check(db, run, "draft", version.id):
            reports.record_draft(db, inputs, version.id, content)
        artifact = db.scalars(
            select(Artifact)
            .where(Artifact.draft_version_id == version.id, Artifact.owner_id == user.id)
            .order_by(Artifact.created_at.desc())
        ).first()
        if artifact is None or not reports.needs_check(db, run, "final_pdf", artifact.id):
            return
        artifact_id, path, expected_sha = artifact.id, artifact.storage_path, artifact.sha256
    text, error = None, None
    if artifact_grant is None or artifact_grant.object_path != path:
        return  # this job was not granted that PDF (it is newer than the request)
    try:
        data = SignedObject(settings, artifact_grant.url).download()
        if hashlib.sha256(data).hexdigest() != expected_sha:
            logger.error("artifact hash mismatch artifact_id=%s", artifact_id)
            error = "storage_unavailable"
        else:
            text = pdf_text(data)
    except (StorageUnavailableError, StorageRejectedError):
        error = "storage_unavailable"
    with user_db(settings, user) as db:
        reports.record_pdf(db, inputs, artifact_id, content, text, error)


def artifact_bytes(
    settings: Settings, user: VerifiedUser, artifact_id: uuid.UUID
) -> tuple[bytes, Artifact]:
    with user_db(settings, user) as db:
        artifact = db.scalars(
            select(Artifact).where(Artifact.id == artifact_id, Artifact.owner_id == user.id)
        ).first()
        if artifact is None:
            raise ApiError(404, "not_found", "That PDF was not found.")
        db.expunge(artifact)
    try:
        data = UserStorage(settings, user.access_token, settings.artifacts_bucket).download(
            artifact.storage_path
        )
    except StorageObjectMissingError as exc:
        raise ApiError(404, "not_found", "That PDF is no longer available.") from exc
    except (StorageUnavailableError, StorageRejectedError) as exc:
        raise unavailable("File storage") from exc
    if hashlib.sha256(data).hexdigest() != artifact.sha256:
        logger.error("artifact hash mismatch artifact_id=%s", artifact_id)
        raise unavailable("File storage")
    return data, artifact


def approved_download(
    settings: Settings, user: VerifiedUser, approval_id: uuid.UUID
) -> tuple[bytes, str]:
    with user_db(settings, user) as db:
        approval = db.scalars(
            select(Approval).where(Approval.id == approval_id, Approval.owner_id == user.id)
        ).first()
        if approval is None:
            raise ApiError(404, "not_found", "That approved resume was not found.")
        artifact_id, expected_sha, filename = (
            approval.artifact_id,
            approval.artifact_sha256,
            approval.filename,
        )
    data, artifact = artifact_bytes(settings, user, artifact_id)
    if artifact.sha256 != expected_sha:
        raise unavailable("File storage")
    return data, filename
