"""Bounded automatic improvement of a draft's ATS match estimate (milestone 6d, ADR 0008).

Runs in the worker as the ``improve`` job, queued automatically when a draft
job succeeds (or retried by the user). Each attempt:

1. scores the current draft and the user's confirmed facts with the same rubric
   and targets only requirements the facts support better than the draft;
2. claims an attempt slot (a row; the database allows attempts 1 and 2 per
   run) before any paid work, so retries and restarts cannot exceed the cap;
3. adds confirmed skill lines word for word, and asks the model for a concise
   patch of only the entries holding supporting facts;
4. runs the same checks as every edit, rejecting the attempt if anything was
   unsupported or an entry grew by more than one bullet;
5. saves a new version only if some requirement gains credit, none loses any,
   and no term is repeated more than before.

It stops, recording why, when no supported gap is left, an attempt does not
improve the estimate, the draft changed, or both attempts are used. It never
chases a target score.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.ai import prompts
from app.ai.client import LLMClient, LLMError
from app.ai.schemas import EntryOut, ImprovePatchOut
from app.api.errors import ApiError
from app.ats import improve as rules
from app.ats import reports
from app.ats.scoring import AtsResult
from app.auth.dependencies import user_db
from app.auth.tokens import VerifiedUser
from app.config import Settings
from app.db.models import MAX_IMPROVEMENT_ATTEMPTS, ImprovementAttempt
from app.tailoring.context import ProfileContext, build_context
from app.tailoring.models import AnalysisContent, DraftContent, DraftSkill, ValidationReport
from app.tailoring.review import (
    _draft_state,
    _from_context_refs,
    _merge_entries,
    _passing_report,
    _recompute_omitted,
    _to_context_refs,
    save_version,
)
from app.tailoring.runs import _owned_run, _verified_rows, call_model

logger = logging.getLogger("app.improve")

# Why improvement stopped (job result "stopped"; attempt rows use the per-attempt ones).
STOP_REASONS = (
    "no_gaps",  # every scored requirement already has full credit
    "exhausted",  # remaining gaps are not supported by confirmed facts
    "limit_reached",  # both attempts for this run are used
    "no_improvement",  # the attempt would not raise the estimate without losing credit
    "new_repetition",  # the attempt would repeat terms more often
    "unsupported_changes",  # the attempt failed the fact checks
    "too_long",  # the attempt grew an entry by more than one bullet
    "draft_changed",  # the draft changed while this was running
    "not_checked",  # the draft could not be scored
)


@dataclass
class _Prepared:
    attempt_id: uuid.UUID
    content: DraftContent
    context: ProfileContext
    analysis: AnalysisContent
    job_text: str
    plan: rules.Plan


def _finish_attempt(
    settings: Settings,
    user: VerifiedUser,
    attempt_id: uuid.UUID,
    status: str,
    outcome: str,
    *,
    new_version: int | None = None,
    score_after: int | None = None,
) -> None:
    with user_db(settings, user) as db:
        db.execute(
            update(ImprovementAttempt)
            .where(ImprovementAttempt.id == attempt_id, ImprovementAttempt.status == "running")
            .values(
                status=status,
                outcome=outcome[:32],
                new_version=new_version,
                score_after=score_after,
                finished_at=func.now(),
            )
        )


def _abandon(settings: Settings, user: VerifiedUser, attempt_id: uuid.UUID, code: str) -> None:
    """Best effort while an error propagates; a retried job marks leftovers interrupted."""
    try:
        _finish_attempt(settings, user, attempt_id, "failed", code)
    except Exception as exc:
        logger.warning("improve attempt not closed error=%s", type(exc).__name__)


def _prepare(
    settings: Settings,
    user: VerifiedUser,
    run_id: uuid.UUID,
    job_id: uuid.UUID,
    expected: int,
) -> _Prepared | str:
    """Decide whether another attempt is useful and claim its slot; else a stop reason."""
    with user_db(settings, user) as db:
        run = _owned_run(db, user, run_id)
        if run.status == "cancelled":
            raise ApiError(409, "invalid_state", "This run was cancelled.")
        draft, _, content = _draft_state(db, user, run)
        if draft.current_version != expected:
            return "draft_changed"
        inputs = reports.scoring_inputs(db, run)
        if inputs is None:
            return "not_checked"
        context = build_context(_verified_rows(db, user), settings.max_context_chars)
        try:
            plan = rules.plan(inputs.analysis, content, context.facts.values(), inputs.job_text)
        except Exception as exc:  # deterministic code: report, never guess
            logger.error("improve plan failed run_id=%s error=%s", run_id, type(exc).__name__)
            return "not_checked"
        if plan.open_gaps == 0:
            return "no_gaps"
        if not plan.targets:
            return "exhausted"
        used = db.scalar(select(func.count()).where(ImprovementAttempt.run_id == run_id)) or 0
        if used >= MAX_IMPROVEMENT_ATTEMPTS:
            return "limit_reached"
        attempt = ImprovementAttempt(
            id=uuid.uuid4(),
            owner_id=user.id,
            run_id=run_id,
            job_id=job_id,
            attempt=used + 1,
            base_version=expected,
            targets=[t.requirement_id for t in plan.targets],
            score_before=plan.before.score,
        )
        try:
            with db.begin_nested():
                db.add(attempt)
                db.flush()
        except IntegrityError:  # another job claimed the last slot first
            return "limit_reached"
        return _Prepared(attempt.id, content, context, inputs.analysis, inputs.job_text, plan)


def _payload(prepared: _Prepared, keyed: DraftContent, refs: list[str]) -> dict[str, Any]:
    context = prepared.context
    alias = {f.evidence_id: f.alias for f in context.facts.values()}
    allowed = set(refs)
    return {
        "role_title": prepared.analysis.role_title,
        "targets": [
            {
                "requirement_id": t.requirement_id,
                "text": t.text,
                "terms": t.terms,
                "supporting_fact_ids": [
                    a for a in t.fact_aliases if context.facts[a].entry_ref in allowed
                ],
            }
            for t in prepared.plan.targets
        ],
        "entries": [
            {
                "ref": ref,
                "section": context.entries[ref].section,
                "kind_of_experience": context.entries[ref].experience_type,
                "facts": [{"id": f.alias, "text": f.text} for f in context.entries[ref].facts],
            }
            for ref in refs
        ],
        "current_draft": {
            "entries": [
                {
                    "entry_ref": e.ref,
                    "bullets": [
                        {
                            "text": b.text,
                            "evidence_ids": [alias[i] for i in b.evidence_ids if i in alias],
                            "requirement_ids": b.requirement_ids,
                        }
                        for b in e.bullets
                    ],
                }
                for e in keyed.entries
                if e.ref in allowed
            ]
        },
    }


def _keep_links(patch: list[EntryOut], keyed: DraftContent) -> None:
    """A bullet returned unchanged keeps the requirement links it already had."""
    existing = {
        e.ref: {b.text.strip(): b.requirement_ids for b in e.bullets} for e in keyed.entries
    }
    for entry in patch:
        for bullet in entry.bullets:
            kept = existing.get(entry.entry_ref, {}).get(bullet.text.strip())
            if kept:
                bullet.requirement_ids = list(dict.fromkeys([*bullet.requirement_ids, *kept]))


def _candidate(
    settings: Settings,
    user: VerifiedUser,
    client: LLMClient,
    run_id: uuid.UUID,
    prepared: _Prepared,
) -> tuple[DraftContent, ValidationReport, str | None, str | None] | str:
    """Build the improved draft, or return why this attempt is rejected."""
    context = prepared.context
    keyed, back = _to_context_refs(prepared.content, context)
    aliases = [a for t in prepared.plan.targets for a in t.fact_aliases]
    refs = list(dict.fromkeys(r for a in aliases if (r := context.facts[a].entry_ref)))
    report, model, prompt_version = _passing_report(), None, None
    merged = keyed.model_copy(deep=True)
    if refs:
        result = call_model(
            settings,
            user,
            client,
            run_id=run_id,
            operation="improve",
            prompt_version=prompts.IMPROVE_VERSION,
            instructions=prompts.IMPROVE_INSTRUCTIONS,
            payload=_payload(prepared, keyed, refs),
            schema=ImprovePatchOut,
            max_output_tokens=settings.draft_max_output_tokens,
        )
        model, prompt_version = result.model, prompts.IMPROVE_VERSION
        patch = [e for e in result.output.entries if e.entry_ref in refs]
        patch = list({e.entry_ref: e for e in patch}.values())  # one patch per entry
        _keep_links(patch, keyed)
        before = {e.ref: len(e.bullets) for e in keyed.entries}
        for entry in patch:
            old = before.get(entry.entry_ref, 0)
            if len(entry.bullets) > old + 1 or len(entry.bullets) > max(
                old, prompts.MAX_BULLETS_PER_ENTRY
            ):
                return "too_long"
        if patch:
            merged, report = _merge_entries(
                keyed, patch, context, prepared.analysis, None, None, False
            )
            if report.removed or not report.passed:
                return "unsupported_changes"
    # Confirmed skill lines that support a target are added exactly as confirmed.
    in_skills = {i for s in merged.skills for i in s.evidence_ids}
    for alias in dict.fromkeys(aliases):
        fact = context.facts[alias]
        if fact.entry_ref is None and fact.evidence_id not in in_skills:
            merged.skills.append(DraftSkill(text=fact.text, evidence_ids=[fact.evidence_id]))
            in_skills.add(fact.evidence_id)
    _recompute_omitted(merged, context)
    return _from_context_refs(merged, back), report, model, prompt_version


def improve_draft(
    settings: Settings,
    user: VerifiedUser,
    client: LLMClient,
    run_id: uuid.UUID,
    job_id: uuid.UUID,
    expected_version: int,
    check_cancelled: Callable[[], None] = lambda: None,
) -> dict[str, Any]:
    """Worker side: up to the remaining attempts for this run; returns what happened."""
    with user_db(settings, user) as db:
        # A crashed attempt of this job keeps its slot: it may already have called the model.
        db.execute(
            update(ImprovementAttempt)
            .where(ImprovementAttempt.job_id == job_id, ImprovementAttempt.status == "running")
            .values(status="failed", outcome="interrupted", finished_at=func.now())
        )
        saved = list(
            db.scalars(
                select(ImprovementAttempt.new_version)
                .where(ImprovementAttempt.job_id == job_id, ImprovementAttempt.status == "saved")
                .order_by(ImprovementAttempt.attempt)
            )
        )
    expected = max([expected_version, *[v for v in saved if v is not None]])
    first: AtsResult | None = None
    last_score: int | None = None
    while True:
        check_cancelled()
        prepared = _prepare(settings, user, run_id, job_id, expected)
        if isinstance(prepared, str):
            stopped = prepared
            break
        first = first or prepared.plan.before
        try:
            built = _candidate(settings, user, client, run_id, prepared)
        except (LLMError, ApiError) as exc:  # the runner decides about retrying
            _abandon(settings, user, prepared.attempt_id, exc.code)
            raise
        except Exception:
            _abandon(settings, user, prepared.attempt_id, "internal_error")
            raise
        if isinstance(built, str):
            _finish_attempt(settings, user, prepared.attempt_id, "rejected", built)
            stopped = built
            break
        candidate, report, model, prompt_version = built
        verdict, after, _ = rules.judge(
            prepared.analysis, prepared.plan.before, candidate, prepared.job_text
        )
        if verdict != "improved":
            _finish_attempt(
                settings, user, prepared.attempt_id, "rejected", verdict, score_after=after.score
            )
            stopped = verdict
            break
        try:
            check_cancelled()
        except Exception:
            _abandon(settings, user, prepared.attempt_id, "cancelled")
            raise
        try:
            with user_db(settings, user) as db:
                run = _owned_run(db, user, run_id)
                draft, _, _ = _draft_state(db, user, run)
                expected = save_version(
                    db, user, run, draft, expected, candidate, report, "improve", model,
                    prompt_version,
                )  # fmt: skip
                db.execute(
                    update(ImprovementAttempt)
                    .where(ImprovementAttempt.id == prepared.attempt_id)
                    .values(
                        status="saved",
                        outcome="improved",
                        new_version=expected,
                        score_after=after.score,
                        finished_at=func.now(),
                    )
                )
        except ApiError as exc:
            if exc.code != "conflict":
                _abandon(settings, user, prepared.attempt_id, exc.code)
                raise
            _finish_attempt(settings, user, prepared.attempt_id, "rejected", "draft_changed")
            stopped = "draft_changed"
            break
        saved.append(expected)
        last_score = after.score
    return {
        "stopped": stopped,
        "version": expected,  # the draft version the stop reason applies to
        "saved_versions": [v for v in saved if v is not None],
        "score_before": first.score if first else None,
        "score_after": last_score,
    }
