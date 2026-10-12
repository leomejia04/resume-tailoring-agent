"""Draft-level comparison: full context vs hybrid retrieval (milestone 7, ADR 0010).

For every labeled posting and scenario (own role profile, 78-fact bank), the
production drafting code (``generate_draft``: model call, claim validation,
one bounded repair) runs twice from the same labeled requirements: once with
RETRIEVAL_MODE=full and once with RETRIEVAL_MODE=hybrid. The analysis is the
labeled one, stored as a job analysis, so both modes answer the same
requirements and no analysis call is paid for.

Measured per draft, all deterministically in Python:
- factual checks: whether the first output failed validation (repaired),
  claims still failing after the repair (removed), and final pass;
- labeled evidence used: share of labeled direct facts the draft cites;
- ATS match estimate of the draft (same rubric as the app);
- cost, tokens, and latency from the app's own usage records.
Writing preference is left to a person: a blind A/B file pairs the two
drafts of each posting, with the key stored separately.

Spending is bounded twice: the app's own reservations, and a hard cap here
that stops before the next draft's worst case (two calls at the output
limit) could exceed it.
"""

from __future__ import annotations

import random
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

from app.ai import prompts
from app.ai.client import LLMClient, LLMError
from app.ai.pricing import worst_case_cost_usd
from app.ats.passages import draft_passages
from app.ats.scoring import score
from app.auth.tokens import VerifiedUser
from app.config import Settings
from app.db.models import (
    Draft,
    DraftVersion,
    JobAnalysis,
    Retrieval,
    TailoringRun,
    UsageEvent,
)
from app.db.session import user_session
from app.tailoring import runs
from app.tailoring.models import AnalysisContent, DraftContent, ValidationReport
from evaluation.retrieval import COMBINED, Dataset, EvalUser, PostingIn

MODES = ("full", "hybrid")
CATEGORY_HEADINGS = {
    "required": "Required qualifications",
    "responsibility": "Responsibilities",
    "preferred": "Preferred qualifications",
    "terminology": "Tools and terms",
}


class BudgetCapReachedError(Exception):
    pass


@dataclass
class DraftResult:
    posting: str
    role: str
    scenario: str
    mode: str
    run_id: str
    status: str  # "ok" or an error code
    facts_sent: int = 0
    facts_total: int = 0
    chars_sent: int = 0
    repaired: bool = False
    removed: int = 0
    failed_validation: int = 0
    passed: bool = False
    issues: list[str] = field(default_factory=list)
    direct_used: float | None = None
    labeled_used: float | None = None
    ats_score: int | None = None
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    calls: int = 0
    seconds: float = 0.0
    content: dict[str, Any] | None = None


def job_description(posting: PostingIn) -> str:
    """A plain posting text built from the labeled requirements (for ATS negation checks)."""
    lines = [f"{posting.company} is hiring a {posting.title}."]
    for category, heading in CATEGORY_HEADINGS.items():
        items = [r for r in posting.requirements if r.category == category]
        if items:
            lines += ["", f"{heading}:"] + [f"- {r.text}" for r in items]
    return "\n".join(lines)


def labeled_analysis(posting: PostingIn) -> AnalysisContent:
    return AnalysisContent(
        role_title=posting.title,
        requirements=[r.as_requirement() for r in posting.requirements],
    )


def worst_case_per_draft(settings: Settings, chars: int) -> Decimal:
    """A draft is at most two calls (draft + repair), each at the output limit."""
    input_chars = len(prompts.TAILOR_INSTRUCTIONS) + len(prompts.REPAIR_SUFFIX) + 3 * chars + 4000
    return 2 * worst_case_cost_usd(input_chars, settings.draft_max_output_tokens, settings)


def prepare_run(
    settings: Settings, user: VerifiedUser, posting: PostingIn, analysis: AnalysisContent
) -> uuid.UUID:
    """A posting, the labeled analysis, and a run ready to draft (no model call)."""
    _, version = runs.create_job(
        settings,
        user,
        title=posting.title,
        company=posting.company,
        source_url=None,
        description=job_description(posting),
    )
    with user_session(settings, user.id) as db:
        analysis_row = JobAnalysis(
            id=uuid.uuid4(),
            owner_id=user.id,
            posting_version_id=version.id,
            model="labeled",
            prompt_version="retrieval-eval/v1",
            content=analysis.model_dump(mode="json"),
        )
        db.add(analysis_row)
        db.flush()
        run = TailoringRun(
            id=uuid.uuid4(),
            owner_id=user.id,
            posting_version_id=version.id,
            master_version_id=None,
            analysis_id=analysis_row.id,
            status="ready",
        )
        db.add(run)
        db.flush()
        return run.id


def _usage(settings: Settings, user: VerifiedUser, run_id: uuid.UUID) -> dict[str, Any]:
    with user_session(settings, user.id) as db:
        row = db.execute(
            select(
                func.coalesce(func.sum(UsageEvent.cost_usd), 0),
                func.coalesce(func.sum(UsageEvent.input_tokens), 0),
                func.coalesce(func.sum(UsageEvent.output_tokens), 0),
                func.coalesce(func.sum(UsageEvent.reasoning_tokens), 0),
                func.count(),
            ).where(UsageEvent.owner_id == user.id, UsageEvent.run_id == run_id)
        ).one()
    return {
        "cost_usd": float(row[0]),
        "input_tokens": int(row[1]),
        "output_tokens": int(row[2]),
        "reasoning_tokens": int(row[3]),
        "calls": int(row[4]),
    }


def _latest_version(settings: Settings, user: VerifiedUser, run_id: uuid.UUID):  # noqa: ANN202
    with user_session(settings, user.id) as db:
        row = db.execute(
            select(DraftVersion, Retrieval)
            .join(Draft, Draft.id == DraftVersion.draft_id)
            .outerjoin(Retrieval, Retrieval.id == DraftVersion.retrieval_id)
            .where(Draft.run_id == run_id, Draft.owner_id == user.id)
            .order_by(DraftVersion.version.desc())
            .limit(1)
        ).one()
        db.expunge_all()
        return row


def evaluate_draft(
    content: DraftContent,
    report: ValidationReport,
    posting: PostingIn,
    available: set[str],
    back: dict[str, str],
    analysis: AnalysisContent,
) -> dict[str, Any]:
    """Deterministic checks on one saved draft (pure: no database, no model)."""
    cited = {
        back[str(i)]
        for e in content.entries
        for b in e.bullets
        for i in b.evidence_ids
        if str(i) in back
    }
    cited |= {back[str(i)] for s in content.skills for i in s.evidence_ids if str(i) in back}
    direct = {f for r in posting.requirements for f in r.direct} & available
    labeled = direct | ({f for r in posting.requirements for f in r.related} & available)
    ats = score(analysis, draft_passages(content), job_text=job_description(posting))
    return {
        "repaired": report.repaired,
        "removed": len(report.removed),
        "failed_validation": sum(1 for o in content.omitted if o.reason == "failed_validation"),
        "passed": report.passed,
        "issues": sorted({i.code for i in report.issues if i.severity == "error"}),
        "direct_used": round(len(cited & direct) / len(direct), 4) if direct else None,
        "labeled_used": round(len(cited & labeled) / len(labeled), 4) if labeled else None,
        "ats_score": ats.score,
    }


def run_comparison(
    settings: Settings,
    client: LLMClient,
    dataset: Dataset,
    accounts: dict[str, EvalUser],
    *,
    budget_usd: Decimal,
    postings: Sequence[PostingIn] | None = None,
    scenarios: Sequence[str] = ("own", COMBINED),
    seed: int = 7,
    progress: Callable[[str], None] = print,
) -> tuple[list[DraftResult], Decimal, bool]:
    """Draft every (posting, scenario) in both modes. Returns results, spent, completed."""
    rng = random.Random(seed)  # noqa: S311 (reproducible ordering, not security)
    results: list[DraftResult] = []
    spent = Decimal(0)
    for posting in postings or dataset.postings:
        analysis = labeled_analysis(posting)
        for scenario in scenarios:
            profile = COMBINED if scenario == COMBINED else posting.profile
            account = accounts[profile]
            back = {str(v): k for k, v in account.fact_ids.items()}
            available = {f.id for f in dataset.facts_for(profile)}
            chars = sum(f.cost for f in account.facts)
            modes = list(MODES)
            rng.shuffle(modes)  # alternate which mode runs first
            for mode in modes:
                worst = worst_case_per_draft(settings, chars) if client.provider == "openai" else 0
                if spent + Decimal(worst) > budget_usd:
                    progress(f"stopping: next draft could exceed the ${budget_usd} cap")
                    return results, spent, False
                mode_settings = settings.model_copy(update={"retrieval_mode": mode})
                run_id = prepare_run(mode_settings, account.user, posting, analysis)
                result = DraftResult(posting.id, posting.role, scenario, mode, str(run_id), "ok")
                started = time.perf_counter()
                try:
                    runs.generate_draft(mode_settings, account.user, client, run_id)
                except LLMError as exc:
                    result.status = exc.code
                except Exception as exc:  # recorded, never hides spending
                    result.status = type(exc).__name__
                result.seconds = round(time.perf_counter() - started, 2)
                usage = _usage(settings, account.user, run_id)
                for key, value in usage.items():
                    setattr(result, key, value)
                spent += Decimal(str(usage["cost_usd"]))
                if result.status == "ok":
                    version, retrieval = _latest_version(settings, account.user, run_id)
                    content = DraftContent.model_validate(version.content)
                    report = ValidationReport.model_validate(version.validation)
                    for key, value in evaluate_draft(
                        content, report, posting, available, back, analysis
                    ).items():
                        setattr(result, key, value)
                    if retrieval is not None:
                        result.facts_sent = retrieval.facts_selected
                        result.facts_total = retrieval.facts_total
                        result.chars_sent = retrieval.chars_selected
                    result.content = version.content
                results.append(result)
                progress(
                    f"{posting.id} {scenario} {mode}: {result.status}, "
                    f"${result.cost_usd:.4f}, total ${spent:.4f}"
                )
    return results, spent, True


def summarize(results: Sequence[DraftResult]) -> dict[str, Any]:
    def avg(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 4) if values else None

    out: dict[str, Any] = {}
    for scenario in sorted({r.scenario for r in results}):
        for mode in MODES:
            rows = [r for r in results if r.scenario == scenario and r.mode == mode]
            ok = [r for r in rows if r.status == "ok"]
            out[f"{scenario}|{mode}"] = {
                "drafts": len(rows),
                "failed_runs": len(rows) - len(ok),
                "repaired": sum(r.repaired for r in ok),
                "claims_removed": sum(r.removed for r in ok),
                "passed": sum(r.passed for r in ok),
                "direct_used": avg([r.direct_used for r in ok if r.direct_used is not None]),
                "labeled_used": avg([r.labeled_used for r in ok if r.labeled_used is not None]),
                "ats_score": avg([float(r.ats_score) for r in ok if r.ats_score is not None]),
                "facts_sent": avg([float(r.facts_sent) for r in ok]),
                "input_tokens": avg([float(r.input_tokens) for r in rows]),
                "output_tokens": avg([float(r.output_tokens) for r in rows]),
                "cost_usd": round(sum(r.cost_usd for r in rows), 6),
                "cost_per_draft": avg([r.cost_usd for r in rows]),
                "seconds": avg([r.seconds for r in rows]),
            }
    return out


def blind_pairs(
    results: Sequence[DraftResult], dataset: Dataset, seed: int = 11
) -> tuple[str, dict[str, dict[str, str]]]:
    """Markdown pairing each posting's two drafts as A/B (random order) and the key."""
    rng = random.Random(seed)  # noqa: S311 (reproducible ordering, not security)
    by_key: dict[tuple[str, str], dict[str, DraftResult]] = {}
    for r in results:
        if r.status == "ok" and r.content is not None:
            by_key.setdefault((r.posting, r.scenario), {})[r.mode] = r
    postings = {p.id: p for p in dataset.postings}
    lines = [
        "# Blind comparison: which draft would you rather send?",
        "",
        "Each pair answers the same fictional posting from the same confirmed facts.",
        "Note A, B, or tie for each pair before opening the key file.",
        "",
    ]
    key: dict[str, dict[str, str]] = {}
    for (posting_id, scenario), pair in sorted(by_key.items()):
        if set(pair) != set(MODES):
            continue
        order = list(MODES)
        rng.shuffle(order)
        name = f"{posting_id} / {scenario}"
        key[name] = {"A": order[0], "B": order[1]}
        posting = postings[posting_id]
        lines += [f"## {name}: {posting.title} at {posting.company}", ""]
        lines += [f"- {r.text}" for r in posting.requirements] + [""]
        for label, mode in zip("AB", order, strict=True):
            content = DraftContent.model_validate(pair[mode].content)
            lines += [f"### Draft {label}", "", f"**{content.headline}**", ""]
            if content.summary:
                lines += [content.summary, ""]
            for entry in content.entries:
                if entry.bullets:
                    lines.append(f"*{entry.entry or entry.section or ''}*")
                    lines += [f"- {b.text}" for b in entry.bullets] + [""]
            if content.skills:
                lines += ["Skills: " + "; ".join(s.text for s in content.skills), ""]
    return "\n".join(lines), key
