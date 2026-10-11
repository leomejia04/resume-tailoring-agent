"""Deterministic truthfulness checks for model-written drafts.

Valid IDs are necessary but not sufficient: a bullet must cite this user's
confirmed facts from the same entry, and every number, year, and requirement
term it states must appear in those cited facts. Anything that still fails
after the bounded repair attempt is removed and reported, never shown as
supported.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.ai.schemas import TailoredDraftOut
from app.tailoring.context import ProfileContext
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

RULE_VERSION = "draft-rules/v1"
MAX_BULLET_CHARS = 350
_NUMBER = re.compile(
    r"\$?\d[\d,]*(?:\.\d+)?\s?(?:%|percent\b|k\b|m\b|million\b|billion\b|x\b|\+)?", re.I
)
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")


def normalize_number(token: str) -> str:
    token = token.lower().replace(",", "").replace("$", "").replace(" ", "")
    return token.replace("percent", "%")


def numbers_in(text: str) -> set[str]:
    return {normalize_number(m.group(0)) for m in _NUMBER.finditer(text)}


def _bare(number: str) -> str:
    return re.sub(r"[^0-9.]", "", number)


def term_pattern(term: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", re.I)


def contains_term(text: str, term: str) -> bool:
    return bool(term_pattern(term).search(text))


@dataclass
class _Checked:
    issues: list[ValidationIssue]
    bad_bullets: set[tuple[int, int]]
    bad_skills: set[int]
    bad_summary: bool
    bad_headline: bool


def _unsupported_numbers(text: str, support: str) -> list[str]:
    supported = numbers_in(support)
    supported_bare = {_bare(n) for n in supported}
    missing = []
    for number in numbers_in(text):
        if _YEAR.fullmatch(number):
            continue  # years are checked by the date rule
        if number in supported or (_bare(number) in supported_bare and not number.endswith("%")):
            continue
        missing.append(number)
    return missing


def _requirement_terms(analysis: AnalysisContent) -> list[str]:
    seen: dict[str, str] = {}
    for requirement in analysis.requirements:
        for term in requirement.terms:
            seen.setdefault(term.lower(), term)
    return list(seen.values())


def _check_claim(
    text: str, support: str, where: str, terms: list[str], issues: list[ValidationIssue]
) -> bool:
    ok = True
    for number in _unsupported_numbers(text, support):
        issues.append(
            ValidationIssue(
                code="unsupported_number",
                severity="error",
                where=where,
                message=f"States {number!r}, which the cited facts do not contain.",
            )
        )
        ok = False
    for year in set(_YEAR.findall(text)) - set(_YEAR.findall(support)):
        issues.append(
            ValidationIssue(
                code="unsupported_date",
                severity="error",
                where=where,
                message=f"States the year {year}.",
            )
        )
        ok = False
    flagged: set[str] = set()
    for term in terms:
        if contains_term(text, term) and not contains_term(support, term):
            flagged.add(term.lower())
            issues.append(
                ValidationIssue(
                    code="unsupported_term",
                    severity="error",
                    where=where,
                    message=f"Mentions {term!r}, which the cited facts do not support.",
                )
            )
            ok = False
    # Names the job analysis never listed (tools, companies, certifications) are
    # caught by shape: acronyms, mixed case, or capitalized words mid-sentence.
    for name in _unsupported_names(text, support):
        if name.lower() in flagged:
            continue
        issues.append(
            ValidationIssue(
                code="unsupported_name",
                severity="error",
                where=where,
                message=f"Mentions {name!r}, which the cited facts do not contain.",
            )
        )
        ok = False
    return ok


_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#./-]*[A-Za-z0-9+#]|[A-Za-z]")
_SENTENCE_START = re.compile(r"(^|[.!?;:]\s+|\n)\s*$")
_ALWAYS_OK = {
    "I", "A", "An", "The", "And", "Or", "For", "With", "To", "Of", "In", "On", "At", "By",
    "January", "February", "March", "April", "May", "June", "July", "August", "September",
    "October", "November", "December", "Jan", "Feb", "Mar", "Apr", "Jun", "Jul", "Aug",
    "Sep", "Sept", "Oct", "Nov", "Dec", "Present",
}  # fmt: skip


def _looks_like_name(word: str, at_start: bool) -> bool:
    if word in _ALWAYS_OK:
        return False
    if len(word) >= 2 and word.isupper():
        return True  # acronym: SQL, AWS, PMP
    if (
        any(c.isupper() for c in word[1:])
        or any(c.isdigit() for c in word)
        or any(c in "+#" for c in word)
    ):
        return True  # PowerBI, iOS, C++, S3
    return word[0].isupper() and not at_start


def _unsupported_names(text: str, support: str) -> list[str]:
    support_lower = support.lower()
    missing = []
    for match in _WORD.finditer(text):
        word = match.group(0).rstrip(".")
        at_start = bool(_SENTENCE_START.search(text[: match.start()]))
        if (
            _looks_like_name(word, at_start)
            and not contains_term(support_lower, word.lower())
            and word not in missing
        ):
            missing.append(word)
    return missing


_HEADLINE_FILLER = {"and", "or", "of", "for", "the", "with", "in", "to", "a", "an", "&", "|", "-"}


def _headline_words_supported(headline: str, allowed: str) -> list[str]:
    """Every meaningful headline word must come from the role title or the user's facts."""
    allowed_lower = allowed.lower()
    return [
        w
        for w in re.findall(r"[A-Za-z][A-Za-z0-9+#.]*", headline)
        if w.lower() not in _HEADLINE_FILLER and not contains_term(allowed_lower, w.lower())
    ]


def check(draft: TailoredDraftOut, context: ProfileContext, analysis: AnalysisContent) -> _Checked:
    issues: list[ValidationIssue] = []
    bad_bullets: set[tuple[int, int]] = set()
    bad_skills: set[int] = set()
    terms = _requirement_terms(analysis)
    seen_texts: set[str] = set()
    used_entries: set[str] = set()

    for e_index, entry in enumerate(draft.entries):
        group = context.entries.get(entry.entry_ref)
        if group is None or entry.entry_ref in used_entries:
            issues.append(
                ValidationIssue(
                    code="unknown_entry",
                    severity="error",
                    where=f"entry {entry.entry_ref}",
                    message="Refers to an entry that is not in the confirmed facts.",
                )
            )
            bad_bullets.update((e_index, b) for b in range(len(entry.bullets)))
            continue
        used_entries.add(entry.entry_ref)
        for b_index, bullet in enumerate(entry.bullets):
            where = f"{entry.entry_ref} bullet {b_index + 1}"
            ok = True
            if not bullet.evidence_ids:
                issues.append(
                    ValidationIssue(
                        code="missing_citation",
                        severity="error",
                        where=where,
                        message="Cites no facts.",
                    )
                )
                ok = False
            cited = []
            for alias in bullet.evidence_ids:
                fact = context.facts.get(alias)
                if fact is None:
                    issues.append(
                        ValidationIssue(
                            code="unknown_evidence",
                            severity="error",
                            where=where,
                            message=f"Cites {alias!r}, which is not a confirmed fact.",
                        )
                    )
                    ok = False
                elif fact.entry_ref != entry.entry_ref:
                    issues.append(
                        ValidationIssue(
                            code="evidence_wrong_entry",
                            severity="error",
                            where=where,
                            message=f"Cites {alias!r} from a different entry.",
                        )
                    )
                    ok = False
                else:
                    cited.append(fact)
            support = "\n".join(
                [f.text for f in cited] + [group.entry or "", group.date_text or ""]
            )
            if cited and not _check_claim(bullet.text, support, where, terms, issues):
                ok = False
            if len(bullet.text) > MAX_BULLET_CHARS or not bullet.text.strip():
                issues.append(
                    ValidationIssue(
                        code="bad_length",
                        severity="error",
                        where=where,
                        message="Bullet is empty or too long.",
                    )
                )
                ok = False
            key = re.sub(r"\W+", " ", bullet.text.lower()).strip()
            if key in seen_texts:
                issues.append(
                    ValidationIssue(
                        code="duplicate",
                        severity="warning",
                        where=where,
                        message="Repeats another bullet.",
                    )
                )
            seen_texts.add(key)
            if not ok:
                bad_bullets.add((e_index, b_index))

    unattached = {f.alias: f for f in context.unattached}
    for s_index, skill in enumerate(draft.skills):
        where = f"skill {s_index + 1}"
        cited = [unattached[a] for a in skill.evidence_ids if a in unattached]
        if not skill.evidence_ids or len(cited) != len(skill.evidence_ids):
            issues.append(
                ValidationIssue(
                    code="unknown_evidence",
                    severity="error",
                    where=where,
                    message="Skills must cite confirmed skills or other unattached facts.",
                )
            )
            bad_skills.add(s_index)
            continue
        if not _check_claim(skill.text, "\n".join(f.text for f in cited), where, terms, issues):
            bad_skills.add(s_index)

    everything = context.all_text
    bad_summary = bool(draft.summary) and not _check_claim(
        draft.summary or "", everything, "summary", terms, issues
    )
    allowed_headline = everything + "\n" + analysis.role_title
    bad_headline = not _check_claim(draft.headline, allowed_headline, "headline", terms, issues)
    unsupported_words = _headline_words_supported(draft.headline, allowed_headline)
    if unsupported_words:
        issues.append(
            ValidationIssue(
                code="unsupported_headline",
                severity="error",
                where="headline",
                message=f"Uses {', '.join(unsupported_words)}; the role title and facts lack it.",
            )
        )
        bad_headline = True
    return _Checked(issues, bad_bullets, bad_skills, bad_summary, bad_headline)


def finalize(
    draft: TailoredDraftOut,
    context: ProfileContext,
    analysis: AnalysisContent,
    checked: _Checked,
    repaired: bool,
) -> tuple[DraftContent, ValidationReport]:
    """Drop anything that failed, map aliases to real IDs, and build the report."""
    removed: list[str] = []
    entries: list[DraftEntry] = []
    cited_ids: set = set()
    failed_ids: set = set()
    for e_index, entry in enumerate(draft.entries):
        group = context.entries.get(entry.entry_ref)
        bullets = []
        for b_index, bullet in enumerate(entry.bullets):
            facts = [context.facts[a] for a in bullet.evidence_ids if a in context.facts]
            if (e_index, b_index) in checked.bad_bullets or group is None:
                removed.append(f"{entry.entry_ref} bullet {b_index + 1}: {bullet.text[:80]}")
                failed_ids.update(f.evidence_id for f in facts)
                continue
            cited_ids.update(f.evidence_id for f in facts)
            bullets.append(
                DraftBullet(
                    text=bullet.text.strip(),
                    evidence_ids=[f.evidence_id for f in facts],
                    requirement_ids=[
                        r for r in bullet.requirement_ids if re.fullmatch(r"R\d{1,3}", r)
                    ],
                )
            )
        if group is not None and bullets and all(e.ref != group.ref for e in entries):
            entries.append(
                DraftEntry(
                    ref=group.ref,
                    section=group.section,
                    entry=group.entry,
                    date_text=group.date_text,
                    experience_type=group.experience_type,
                    bullets=bullets,
                )
            )
    skills = []
    for s_index, skill in enumerate(draft.skills):
        facts = [context.facts[a] for a in skill.evidence_ids if a in context.facts]
        if s_index in checked.bad_skills:
            removed.append(f"skill {s_index + 1}: {skill.text[:80]}")
            continue
        cited_ids.update(f.evidence_id for f in facts)
        skills.append(
            DraftSkill(text=skill.text.strip(), evidence_ids=[f.evidence_id for f in facts])
        )
    if checked.bad_summary:
        removed.append("summary")
    headline = analysis.role_title if checked.bad_headline else draft.headline.strip()[:120]
    if checked.bad_headline:
        removed.append("headline (replaced with the posting's role title)")

    omitted = [
        OmittedFact(
            evidence_id=f.evidence_id,
            reason="failed_validation" if f.evidence_id in failed_ids else "not_selected",
        )
        for f in context.facts.values()
        if f.evidence_id not in cited_ids
    ]
    content = DraftContent(
        headline=headline or analysis.role_title,
        summary=None if checked.bad_summary else (draft.summary or None),
        entries=entries,
        skills=skills,
        omitted=omitted,
    )
    errors = [i for i in checked.issues if i.severity == "error"]
    report = ValidationReport(
        rule_version=RULE_VERSION,
        issues=checked.issues,
        removed=removed,
        repaired=repaired,
        passed=not errors,
    )
    return content, report
