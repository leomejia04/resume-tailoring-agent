"""Measure retrieval on the labeled fictional set (no paid calls).

Usage (from backend/, with the local Supabase stack running and the model fetched):
    .venv/Scripts/python -m scripts.eval_retrieval

Creates fictional evaluation accounts in the LOCAL database only (refuses a
non-local SUPABASE_URL), loads the labeled facts, runs exact, full-text,
semantic, and hybrid retrieval plus the selection policy, and writes:
- backend/out/eval/retrieval-<timestamp>.json (all rankings and metrics)
- a Markdown summary printed to stdout (paste into docs only from a real run).
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from app.config import get_settings
from app.retrieval.embedder import MODEL_ID, MODEL_REVISION, load_embedder
from evaluation.retrieval import (
    COMBINED,
    KS,
    METHODS,
    create_eval_user,
    load_dataset,
    load_profile,
    run_scenario,
    score,
)

OUT = Path(__file__).resolve().parents[1] / "out" / "eval"


def _git_commit() -> str:
    try:
        return subprocess.run(  # noqa: S603
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _fmt(value: object) -> str:
    return "n/a" if value is None else f"{value:.2f}" if isinstance(value, float) else str(value)


def markdown(report: dict) -> str:
    lines = [
        f"Run {report['run_at']} (commit {report['commit']}, dataset {report['dataset']}, "
        f"{report['model']}@{report['revision'][:12]}, top_k={report['top_k']}, "
        f"{report['platform']})",
        "",
        f"Model load: {report['model_load_ms']} ms. Indexing per profile (first / unchanged "
        f"re-run): {report['index_ms']}",
        "",
    ]
    metrics = report["metrics"]
    for scenario in ("own", COMBINED):
        counts = metrics["counts"].get(scenario, {})
        lines += [
            f"### Rankings, scenario: {scenario} "
            f"({counts.get('labeled', 0)} labeled requirements, "
            f"{counts.get('missing', 0)} with no evidence)",
            "",
            "| method | "
            + " | ".join(f"recall@{k}" for k in KS)
            + " | precision@3 | direct hit@1 | direct hit@3 | MRR |",
            "|---" * (len(KS) + 5) + "|",
        ]
        for method in METHODS:
            row = metrics["rank_metrics"][scenario][method]
            values = [row.get(f"recall@{k}") for k in KS] + [
                row.get("precision@3"),
                row.get("direct_hit@1"),
                row.get("direct_hit@3"),
                row.get("mrr"),
            ]
            lines.append(f"| {method} | " + " | ".join(_fmt(v) for v in values) + " |")
        noise = metrics["missing_requirement_results"].get(scenario, {})
        lines += [
            "",
            "Average facts returned for requirements with no evidence: "
            + ", ".join(f"{m} {_fmt(noise.get(m))}" for m in METHODS),
            "",
        ]
    lines += [
        "### Context the drafting model would read (full profile vs hybrid selection)",
        "",
        "| posting | scenario | facts full | facts hybrid | chars full | chars hybrid "
        "| labeled pairs kept | reqs with a direct fact kept |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in metrics["selection"]:
        lines.append(
            f"| {row['posting']} | {row['scenario']} | {row['facts_full']} | "
            f"{row['facts_hybrid']} | {row['chars_full']} | {row['chars_hybrid']} | "
            f"{_fmt(row['pair_coverage_hybrid'])} | {_fmt(row['direct_req_coverage_hybrid'])} |"
        )
    lines += ["", "Median timings per posting (ms):", ""]
    for scenario, timings in metrics["median_timings_ms_per_posting"].items():
        lines.append(f"- {scenario}: " + ", ".join(f"{k} {v}" for k, v in timings.items()))
    return "\n".join(lines)


def main() -> None:
    settings = get_settings()
    host = urlsplit(settings.supabase_url).hostname
    if host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("The evaluation only runs against the local Supabase stack.")
    dataset = load_dataset()

    started = time.perf_counter()
    embedder = load_embedder(settings)
    model_load_ms = int((time.perf_counter() - started) * 1000)

    accounts = {}
    for profile in [*dataset.profiles, COMBINED]:
        user = create_eval_user(settings)
        accounts[profile] = load_profile(settings, user, dataset.facts_for(profile), embedder)

    results = []
    for posting in dataset.postings:
        for scenario, profile in (("own", posting.profile), (COMBINED, COMBINED)):
            results.append(
                run_scenario(
                    settings,
                    accounts[profile],
                    posting,
                    scenario,
                    embedder,
                    top_k=settings.retrieval_top_k,
                    budget_chars=settings.max_context_chars,
                )
            )
    report = {
        "run_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "commit": _git_commit(),
        "dataset": dataset.version,
        "model": MODEL_ID,
        "revision": MODEL_REVISION,
        "top_k": settings.retrieval_top_k,
        "platform": f"{platform.system()} {platform.machine()}, Python {platform.python_version()}",
        "model_load_ms": model_load_ms,
        "index_ms": {
            name: f"{a.index_ms}/{a.reindex_ms} ms ({len(a.facts)} facts, "
            f"{a.reindex_embedded} re-embedded)"
            for name, a in accounts.items()
        },
        "metrics": score(dataset, results),
        "results": [r.__dict__ for r in results],
    }
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"retrieval-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(markdown(report))
    print(f"\nFull results: {path}")


if __name__ == "__main__":
    sys.exit(main())
