"""Model clients behind one small interface.

``OpenAIResponsesClient`` calls the official SDK's Responses API with
schema-constrained output. ``DemoClient`` is a transparent, deterministic
stand-in for local development and tests: it never pretends to be AI (the
API reports the provider) and is refused in production by Settings.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from app.ai.pricing import Usage
from app.ai.schemas import (
    BulletOut,
    EntryOut,
    JobAnalysisOut,
    RequirementOut,
    SkillOut,
    TailoredDraftOut,
)
from app.config import Settings
from app.services.job_text import find_headings

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """A failed model call. ``usage`` is set when tokens were still billed."""

    def __init__(self, code: str, usage: Usage | None = None) -> None:
        super().__init__(code)
        self.code = code  # unavailable | timeout | rate_limited | refused | invalid_output
        self.usage = usage


@dataclass(frozen=True)
class LLMResult[T]:
    output: T
    usage: Usage
    latency_ms: int
    model: str


class LLMClient(Protocol):
    provider: str
    model: str

    def parse(
        self,
        *,
        instructions: str,
        payload: dict[str, Any],
        schema: type[T],
        max_output_tokens: int,
        user_hash: str,
    ) -> LLMResult[T]: ...


def user_hash(user_id: object) -> str:
    """Stable pseudonymous ID for provider abuse monitoring (never an email)."""
    return hashlib.sha256(f"resume-tailor:{user_id}".encode()).hexdigest()[:32]


class OpenAIResponsesClient:
    provider = "openai"

    def __init__(self, settings: Settings) -> None:
        from openai import OpenAI  # imported lazily so tests never need network setup

        if settings.openai_api_key is None:
            raise LLMError("unavailable")
        self.model = settings.openai_model
        self._effort = settings.openai_reasoning_effort
        self._client = OpenAI(
            api_key=settings.openai_api_key.get_secret_value(),
            timeout=settings.openai_timeout_seconds,
            max_retries=settings.openai_max_retries,
        )

    def parse(
        self,
        *,
        instructions: str,
        payload: dict[str, Any],
        schema: type[T],
        max_output_tokens: int,
        user_hash: str,
    ) -> LLMResult[T]:
        import openai

        started = time.perf_counter()
        try:
            response = self._client.responses.parse(
                model=self.model,
                instructions=instructions,
                input=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                text_format=schema,
                reasoning={"effort": self._effort},
                max_output_tokens=max_output_tokens,
                store=False,  # do not keep application state at the provider
                safety_identifier=user_hash,
            )
        except openai.APITimeoutError as exc:
            raise LLMError("timeout") from exc
        except openai.RateLimitError as exc:
            raise LLMError("rate_limited") from exc
        except (openai.APIConnectionError, openai.APIStatusError) as exc:
            raise LLMError("unavailable") from exc
        except ValidationError as exc:
            raise LLMError("invalid_output") from exc
        latency = int((time.perf_counter() - started) * 1000)
        usage = _usage_of(response)

        if getattr(response, "status", None) == "incomplete":
            raise LLMError("invalid_output", usage)
        for item in getattr(response, "output", []) or []:
            for content in getattr(item, "content", None) or []:
                if getattr(content, "type", None) == "refusal":
                    raise LLMError("refused", usage)
        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            raise LLMError("invalid_output", usage)
        return LLMResult(output=parsed, usage=usage, latency_ms=latency, model=response.model)


def _usage_of(response: Any) -> Usage:
    usage = getattr(response, "usage", None)
    if usage is None:
        return Usage()
    input_details = getattr(usage, "input_tokens_details", None)
    output_details = getattr(usage, "output_tokens_details", None)
    return Usage(
        input_tokens=usage.input_tokens or 0,
        cached_input_tokens=getattr(input_details, "cached_tokens", 0) or 0,
        cache_write_tokens=getattr(input_details, "cache_write_tokens", 0) or 0,
        output_tokens=usage.output_tokens or 0,
        reasoning_tokens=getattr(output_details, "reasoning_tokens", 0) or 0,
    )


# --- Deterministic demo provider ----------------------------------------------

_TERM = re.compile(
    r"\b(?:[A-Z][A-Za-z0-9+#.]*(?:\s+[A-Z][A-Za-z0-9+#.]*)*|[A-Z]{2,}[A-Za-z0-9+#]*)\b"
)
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
_SKIP_TERMS = {
    "Experience",
    "Strong",
    "Ability",
    "Excellent",
    "Bachelor",
    "We",
    "You",
    "Our",
    "The",
}


def _demo_terms(text: str) -> list[str]:
    terms = []
    for match in _TERM.finditer(_BULLET.sub("", text)):
        term = match.group(0).strip(" .")
        if term and term.split()[0] not in _SKIP_TERMS and term not in terms:
            terms.append(term)
    return terms[:5]


class DemoClient:
    """Builds outputs from the input with simple rules, at zero cost."""

    provider = "demo"
    model = "demo-deterministic-v1"

    def parse(
        self,
        *,
        instructions: str,
        payload: dict[str, Any],
        schema: type[T],
        max_output_tokens: int,
        user_hash: str,
    ) -> LLMResult[T]:
        if schema is JobAnalysisOut:
            output: BaseModel = self._analysis(payload)
        elif schema is TailoredDraftOut:
            output = self._draft(payload)
        else:  # pragma: no cover - programming error
            raise LLMError("invalid_output")
        return LLMResult(output=output, usage=Usage(), latency_ms=0, model=self.model)  # type: ignore[arg-type]

    @staticmethod
    def _analysis(payload: dict[str, Any]) -> JobAnalysisOut:
        text: str = payload["job_description"]
        categories = {
            "required": "required",
            "preferred": "preferred",
            "responsibilities": "responsibility",
        }
        current: str | None = None
        requirements = []
        headings = {h.line: h.kind for h in find_headings(text)}
        for number, line in enumerate(text.split("\n"), start=1):
            if number in headings:
                current = categories.get(headings[number])
                continue
            if current and _BULLET.match(line):
                requirements.append(
                    RequirementOut(
                        category=current,  # type: ignore[arg-type]
                        text=_BULLET.sub("", line).strip(),
                        terms=_demo_terms(line),
                        mandatory=current == "required",
                    )
                )
        title = payload.get("job_title") or "Target role"
        return JobAnalysisOut(role_title=title, seniority=None, requirements=requirements)

    @staticmethod
    def _draft(payload: dict[str, Any]) -> TailoredDraftOut:
        requirements = payload["requirements"]
        entries = []
        for entry in payload["entries"]:
            bullets = []
            for fact in entry["facts"]:
                matched = [
                    r["id"]
                    for r in requirements
                    if any(t.lower() in fact["text"].lower() for t in r["terms"])
                ]
                bullets.append(
                    BulletOut(text=fact["text"], evidence_ids=[fact["id"]], requirement_ids=matched)
                )
            entries.append(EntryOut(entry_ref=entry["ref"], bullets=bullets))
        skills = [
            SkillOut(text=f["text"], evidence_ids=[f["id"]]) for f in payload["unattached_facts"]
        ]
        return TailoredDraftOut(
            headline=payload["role_title"],
            summary=None,
            entries=entries,
            skills=skills,
            omitted_evidence_ids=[],
        )


def client_for(settings: Settings) -> LLMClient | None:
    provider = settings.effective_llm_provider
    if provider == "openai":
        return OpenAIResponsesClient(settings)
    if provider == "demo":
        return DemoClient()
    return None
