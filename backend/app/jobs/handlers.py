"""What each job kind does, and what its run shows when the job finally fails."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.ai import client as ai_client
from app.ai.client import LLMClient
from app.api.errors import ApiError
from app.auth.dependencies import user_db
from app.config import Settings
from app.db.models import ChatMessage
from app.jobs.queue import FollowUp
from app.tailoring import review, runs
from app.tailoring.improve import improve_draft

if TYPE_CHECKING:
    from app.jobs.runner import JobContext


def _model(settings: Settings) -> LLMClient:
    client = ai_client.client_for(settings)
    if client is None:
        raise ApiError(503, "ai_unavailable", "AI features are not configured.")
    return client


def analyze(ctx: JobContext) -> dict[str, Any]:
    client = _model(ctx.settings)
    status = runs.analyze_run(ctx.settings, ctx.owner, client, ctx.run_id, ctx.grant("master_read"))
    return {"status": status}


def draft(ctx: JobContext) -> dict[str, Any]:
    version = runs.generate_draft(ctx.settings, ctx.owner, _model(ctx.settings), ctx.run_id)
    return {"version": version}


def regenerate(ctx: JobContext) -> dict[str, Any]:
    version = review.regenerate_entry(
        ctx.settings,
        ctx.owner,
        _model(ctx.settings),
        ctx.run_id,
        str(ctx.params["entry_ref"]),
        int(ctx.params["expected_version"]),
    )
    return {"new_version": version}


def chat(ctx: JobContext) -> dict[str, Any]:
    with user_db(ctx.settings, ctx.owner) as db:
        message = db.scalar(
            select(ChatMessage.content).where(
                ChatMessage.id == uuid.UUID(ctx.params["message_id"]),
                ChatMessage.run_id == ctx.run_id,
                ChatMessage.role == "user",
            )
        )
    if message is None:
        raise ApiError(404, "not_found", "That chat message was not found.")
    version, _ = review.chat_revision(
        ctx.settings,
        ctx.owner,
        _model(ctx.settings),
        ctx.run_id,
        message,
        int(ctx.params["expected_version"]),
    )
    return {"new_version": version}


def improve(ctx: JobContext) -> dict[str, Any]:
    return improve_draft(
        ctx.settings,
        ctx.owner,
        _model(ctx.settings),
        ctx.run_id,
        ctx.job_id,
        int(ctx.params["expected_version"]),
        ctx.check_cancelled,
    )


def render(ctx: JobContext) -> dict[str, Any]:
    # Signed uploads never overwrite, so every attempt has its own artifact ID and paths.
    artifact_id = uuid.UUID(ctx.params["artifact_ids"][ctx.attempt - 1])
    pdf, docx = ctx.grant("pdf_upload", ctx.attempt), ctx.grant("docx_upload", ctx.attempt)
    if pdf is None or docx is None:
        raise ApiError(409, "grant_missing", "This PDF request is incomplete. Try again.")
    review.render_version(
        ctx.settings,
        ctx.owner,
        ctx.run_id,
        int(ctx.params["version"]),
        artifact_id,
        ctx.grant("master_read"),
        pdf,
        docx,
    )
    return {"artifact_id": str(artifact_id), "version": int(ctx.params["version"])}


def ats_check(ctx: JobContext) -> dict[str, Any]:
    review.run_checks(
        ctx.settings, ctx.owner, ctx.run_id, ctx.grant("master_read"), ctx.grant("artifact_read")
    )
    return {}


HANDLERS: dict[str, Callable[[JobContext], dict[str, Any]]] = {
    "analyze": analyze,
    "draft": draft,
    "regenerate": regenerate,
    "chat": chat,
    "improve": improve,
    "render": render,
    "ats_check": ats_check,
}


def follow_up(ctx: JobContext, result: dict[str, Any]) -> FollowUp | None:
    """Work queued in the same transaction that records this job's success."""
    if ctx.kind == "draft" and result.get("version") and ctx.settings.auto_improve:
        # Bounded automatic improvement of the new draft (ADR 0008).
        return FollowUp(ctx.run_id, "improve", {"expected_version": int(result["version"])})
    return None


_CHAT_FAILURES = {
    "budget_exceeded": "Your daily AI spending limit was reached, so nothing was changed.",
    "conflict": "The draft changed while I was working, so nothing was changed. Ask again.",
}


def on_failure(ctx: JobContext, code: str) -> None:
    """Leave the run in an honest state once a job has used up its attempts."""
    if ctx.kind == "analyze":
        runs.fail_run(ctx.settings, ctx.owner, ctx.run_id, f"analysis_{code}")
    elif ctx.kind == "draft":
        runs.fail_run(ctx.settings, ctx.owner, ctx.run_id, f"draft_{code}")
    elif ctx.kind == "chat":
        reply = _CHAT_FAILURES.get(
            code, "I couldn't revise the draft this time, so nothing was changed. Try again."
        )
        with user_db(ctx.settings, ctx.owner) as db:
            db.add(
                ChatMessage(
                    owner_id=ctx.owner_id, run_id=ctx.run_id, role="assistant", content=reply
                )
            )
    # regenerate, improve, render, ats_check: the run is unchanged; the UI shows the job.
