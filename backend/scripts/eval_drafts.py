"""Live draft comparison, full context vs hybrid retrieval (PAID; ADR 0010).

Usage (from backend/, local Supabase stack running, model fetched, OPENAI_API_KEY in .env):
    .venv/Scripts/python -m scripts.eval_drafts --budget-usd 1.00
    .venv/Scripts/python -m scripts.eval_drafts --demo        # free plumbing check

Refuses to run without an explicit --budget-usd (or --demo), refuses a
non-local database, and stops before the next draft's worst case could pass
the budget. Uses fictional evaluation accounts only. Writes to git-ignored
backend/out/eval/: drafts-<ts>.json (results and metrics), drafts-<ts>-blind.md
(pairs for a person to judge), drafts-<ts>-key.json (which draft was which).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from app.ai.client import client_for
from app.config import get_settings
from app.retrieval.embedder import MODEL_REVISION, load_embedder
from evaluation.drafts import blind_pairs, run_comparison, summarize
from evaluation.retrieval import COMBINED, create_eval_user, load_dataset, load_profile
from scripts.eval_retrieval import _git_commit

OUT = Path(__file__).resolve().parents[1] / "out" / "eval"
MAX_BUDGET = Decimal("5.00")  # a typo guard; the owner's authorization sets the real cap


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--budget-usd", type=Decimal, help="hard spending cap for this run")
    group.add_argument("--demo", action="store_true", help="built-in demo rules, no AI calls")
    parser.add_argument("--limit", type=int, default=None, help="only the first N postings")
    args = parser.parse_args(argv)

    base = get_settings()
    if urlsplit(base.supabase_url).hostname not in {"127.0.0.1", "localhost"}:
        raise SystemExit("The evaluation only runs against the local Supabase stack.")
    if args.demo:
        settings = base.model_copy(update={"llm_provider": "demo"})
        budget = Decimal(0)
    else:
        if not Decimal(0) < args.budget_usd <= MAX_BUDGET:
            raise SystemExit(f"--budget-usd must be above 0 and at most {MAX_BUDGET}.")
        if base.openai_api_key is None:
            raise SystemExit("OPENAI_API_KEY is not set.")
        settings = base.model_copy(update={"llm_provider": "openai"})
        budget = args.budget_usd
    client = client_for(settings)
    if client is None:
        raise SystemExit("No AI client is configured.")

    dataset = load_dataset()
    postings = dataset.postings[: args.limit] if args.limit else dataset.postings
    embedder = load_embedder(settings)
    accounts = {
        profile: load_profile(
            settings, create_eval_user(settings), dataset.facts_for(profile), embedder
        )
        for profile in [*dataset.profiles, COMBINED]
    }
    print(f"Provider {client.provider} model {client.model}, budget ${budget}")
    results, spent, completed = run_comparison(
        settings, client, dataset, accounts, budget_usd=budget, postings=postings
    )
    summary = summarize(results)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    OUT.mkdir(parents=True, exist_ok=True)
    report = {
        "run_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "commit": _git_commit(),
        "dataset": dataset.version,
        "provider": client.provider,
        "model": client.model,
        "reasoning_effort": settings.openai_reasoning_effort,
        "embedding_revision": MODEL_REVISION,
        "top_k": settings.retrieval_top_k,
        "budget_usd": str(budget),
        "spent_usd": str(spent),
        "completed": completed,
        "summary": summary,
        "results": [asdict(r) for r in results],
    }
    (OUT / f"drafts-{stamp}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    markdown, key = blind_pairs(results, dataset)
    (OUT / f"drafts-{stamp}-blind.md").write_text(markdown, encoding="utf-8")
    (OUT / f"drafts-{stamp}-key.json").write_text(json.dumps(key, indent=2), encoding="utf-8")

    print(f"\nSpent ${spent} of ${budget}; completed: {completed}\n")
    header = (
        "| scenario | mode | drafts | failed | repaired | claims removed | passed "
        "| direct facts used | labeled facts used | ATS | facts sent | input tokens "
        "| output tokens | cost/draft | seconds |"
    )
    print(header)
    print("|---" * (header.count("|") - 1) + "|")
    for name, row in summary.items():
        scenario, mode = name.split("|")
        cells = [
            scenario,
            mode,
            row["drafts"],
            row["failed_runs"],
            row["repaired"],
            row["claims_removed"],
            row["passed"],
            row["direct_used"],
            row["labeled_used"],
            row["ats_score"],
            row["facts_sent"],
            row["input_tokens"],
            row["output_tokens"],
            row["cost_per_draft"],
            row["seconds"],
        ]
        print("| " + " | ".join("n/a" if c is None else str(c) for c in cells) + " |")
    print(f"\nFiles: {OUT / f'drafts-{stamp}.json'} (+ -blind.md, -key.json)")


if __name__ == "__main__":
    sys.exit(main())
