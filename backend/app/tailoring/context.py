"""Build the model's view of a user's verified facts (full-profile baseline).

Only confirmed facts are included. Real UUIDs are replaced by short aliases
(E1, E2...) and entries by refs (J1...), so the model never sees internal IDs
and every alias it returns can be mapped back and checked. Identity details
(name, email, phone) are never part of evidence and are never sent.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from app.db.models import EvidenceRecord, EvidenceVersion
from app.tailoring.models import AnalysisContent

LIST_SECTIONS = {"skills", "certifications", "awards", "summary"}


@dataclass(frozen=True)
class Fact:
    alias: str
    evidence_id: uuid.UUID
    text: str
    section: str | None
    entry: str | None
    date_text: str | None
    experience_type: str
    entry_ref: str | None


@dataclass
class EntryGroup:
    ref: str
    section: str | None
    entry: str | None
    date_text: str | None
    experience_type: str
    facts: list[Fact] = field(default_factory=list)


@dataclass
class ProfileContext:
    facts: dict[str, Fact]
    entries: dict[str, EntryGroup]
    unattached: list[Fact]
    truncated: bool

    @property
    def all_text(self) -> str:
        parts = [f.text for f in self.facts.values()]
        parts += [e.entry or "" for e in self.entries.values()]
        return "\n".join(parts)


def build_context(
    rows: list[tuple[EvidenceRecord, EvidenceVersion]], max_chars: int
) -> ProfileContext:
    verified = [(r, v) for r, v in rows if v.verification_status == "verified"]
    verified.sort(
        key=lambda rv: (rv[0].origin != "master", str(rv[0].master_version_id), rv[0].position)
    )

    facts: dict[str, Fact] = {}
    entries: dict[str, EntryGroup] = {}
    entry_keys: dict[tuple, str] = {}
    unattached: list[Fact] = []
    used = 0
    truncated = False
    for record, version in verified:
        cost = len(version.text) + len(version.entry or "") + 40
        if used + cost > max_chars:
            truncated = True
            break
        used += cost
        alias = f"E{len(facts) + 1}"
        attach = version.entry is not None and version.section not in LIST_SECTIONS
        ref = None
        if attach:
            key = (version.section, version.entry, version.date_text)
            ref = entry_keys.get(key)
            if ref is None:
                ref = f"J{len(entries) + 1}"
                entry_keys[key] = ref
                entries[ref] = EntryGroup(
                    ref, version.section, version.entry, version.date_text, version.experience_type
                )
        fact = Fact(
            alias,
            record.id,
            version.text,
            version.section,
            version.entry,
            version.date_text,
            version.experience_type,
            ref,
        )
        facts[alias] = fact
        if ref:
            entries[ref].facts.append(fact)
        else:
            unattached.append(fact)
    return ProfileContext(facts, entries, unattached, truncated)


def draft_payload(
    context: ProfileContext, analysis: AnalysisContent, title: str | None, company: str | None
) -> dict[str, Any]:
    return {
        "job_title": title,
        "company": company,
        "role_title": analysis.role_title,
        "requirements": [
            {
                "id": r.id,
                "category": r.category,
                "text": r.text,
                "terms": r.terms,
                "mandatory": r.mandatory,
            }
            for r in analysis.requirements
        ],
        "entries": [
            {
                "ref": e.ref,
                "section": e.section,
                "kind_of_experience": e.experience_type,
                "facts": [{"id": f.alias, "text": f.text} for f in e.facts],
            }
            for e in context.entries.values()
        ],
        "unattached_facts": [
            {"id": f.alias, "section": f.section, "text": f.text} for f in context.unattached
        ],
    }
