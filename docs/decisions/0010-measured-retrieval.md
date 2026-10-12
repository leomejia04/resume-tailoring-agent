# ADR 0010: Measured retrieval (RAG)

Status: accepted (milestone 7). Retrieval-level results are measured below;
draft-level comparisons (factual errors, writing preference, model cost)
need a live budget and are not yet measured.

## Context

Until now the drafting model received every confirmed fact (the
full-verified-profile baseline, ADR 0004), cut off by a character budget.
That is simple and complete for a normal one- or two-page history, but it
grows with the experience bank, pays for facts unrelated to the posting, and
the budget cut drops facts by position, not by relevance. Milestone 7 adds
retrieval per job requirement, measured against that baseline on a labeled
set, with the baseline kept as the fallback.

## Decisions

1. **Local embeddings, pinned and verified.**
   `sentence-transformers/all-MiniLM-L6-v2` (Apache-2.0) at revision
   `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`, 384 dimensions, L2-normalized,
   256-token inputs. `scripts.fetch_embedding_model` downloads exactly that
   revision; every file (safetensors weights only, no pickle) is checked
   against a SHA-256 in `app/retrieval/embedder.py` before every load. The
   model loads with `local_files_only=True` and `trust_remote_code=False`,
   on CPU (the CPU-only torch build), once per process. Embedding is local,
   so retrieval sends no resume text to any provider and costs nothing per
   call. Dependencies were re-locked with hashes; all new packages are
   permissively licensed (Apache-2.0, BSD, MIT, ISC; tqdm MPL-2.0/MIT).
2. **pgvector in the existing database, exact search.** Migration `0008`
   enables `vector` in Supabase's `extensions` schema. `app.evidence_chunks`
   holds one or more chunks per confirmed fact version: parent context
   (`entry | section (experience type)`) plus the fact, split into
   overlapping token windows only if it exceeds the model limit. Each row
   records the fact version, a SHA-256 of the embedded text, token count,
   model, revision, dimension, and normalization. No HNSW/IVFFlat index:
   search is filtered to one owner's few dozen to few hundred rows, so an
   exact scan is fast (timings below) and exact. Approximate indexes wait
   for a measured need.
3. **Re-embed changes only.** Indexing compares (fact, version, model
   revision) with stored chunks: new versions are embedded, unchanged ones
   are left alone, identical text the same owner already embedded reuses
   its vector, and chunks of old versions, rejected facts, or another model
   revision are deleted. It runs in three steps (read plan, embed with no
   transaction open, idempotent write), so a crash or a concurrent job is
   harmless. Chunks cascade from the fact and its version.
4. **Isolation like every other table.** `evidence_chunks` and `retrievals`
   have forced row-level security on `owner_id = app.current_user_id()`,
   composite owner foreign keys, explicit owner filters in every query, and
   no access for Supabase's API roles. `app_backend` may select, insert and
   delete chunks (never update) and only select and insert retrievals. Its
   search path gains `extensions` so the `vector` operators resolve; only
   the schema owner can create objects there.
5. **Hybrid search per requirement.** For each requirement in the saved job
   analysis: (a) exact terms, using the ATS matcher's whole-word and
   negation rules (ADR 0006); (b) Postgres full-text search
   (`websearch_to_tsquery('english')` over an OR of the requirement's
   meaningful words, ranked by `ts_rank_cd`; bound parameters only); (c)
   semantic search (cosine similarity of the requirement sentence plus its
   terms against each fact's best chunk). The three rankings are fused with
   Reciprocal Rank Fusion (k = 60), which needs no score calibration.
6. **Selection policy (`retrieval/v1`).** Keep education and certification
   facts always; the top `RETRIEVAL_TOP_K` (4) fused facts per requirement;
   an exact-match fallback (if a requirement's term appears in some fact but
   no selected fact contains it, add those facts); and the best fact of
   every job or internship so no employer disappears. If the result still
   exceeds the character budget, the lowest-ranked non-essential facts go
   first.
7. **Modes and fallback.** `RETRIEVAL_MODE=auto` (default) sends the full
   profile whenever it fits both `RETRIEVAL_FULL_CONTEXT_MAX_CHARS` and the
   drafting budget `MAX_CONTEXT_CHARS` (both 60,000 characters by default),
   and retrieves only when the profile would otherwise be cut by position.
   This default follows the measurements below; an earlier draft used
   12,000. `full` always sends everything; `hybrid` always retrieves. If the model is missing or fails to load, drafting
   falls back to the full profile (reason `embeddings_unavailable`) instead
   of failing. Every draft records its mode and reason.
8. **Provenance and cache.** Each decision is an insert-only
   `app.retrievals` row: mode, reason, strategy version, model and revision,
   counts and characters selected, runtime, and per-requirement top-10
   rankings with scores (evidence IDs and numbers only, never fact text).
   Generated draft versions point at it (`draft_versions.retrieval_id`).
   The unique key (owner, analysis, profile fingerprint of fact
   id:version pairs, configuration fingerprint) is the tenant-scoped cache:
   re-drafting with nothing changed reuses the stored selection.
9. **Facts the model never saw are still reported.** Omitted facts are
   computed against the whole verified profile, not the retrieved subset,
   so "Facts left out" stays complete. The draft editor shows a short note
   saying whether the full profile or a selection was used.

## Evaluation method

`backend/evaluation/data/retrieval_v1.json` is a fictional labeled set:
four profiles (Data 22 facts, GRC 20, Business Analyst 19, Tech Sales 17;
78 total) and two postings per role. Every requirement lists its direct and
related facts. The set includes traps: "R&D" vs the R language,
"JavaScript" vs Java, synonyms without shared words, negated mentions, and
requirements with no supporting evidence at all. The labels were written by
the assistant and should be spot-checked by the owner; they are not
independent human judgments.

`.venv/Scripts/python -m scripts.eval_retrieval` (from `backend/`) runs the
production code under row-level security against the local database as
fictional accounts, with the real pinned model, in two scenarios: each
posting against its own role's profile, and against all 78 facts in one
bank (a distractor-heavy stress test). It writes all rankings to
`backend/out/eval/` and prints the tables below. It refuses a non-local
database and makes no paid calls.

## Measured results

Run 2026-10-12 05:27 UTC, commit 2d05a49, dataset `retrieval-eval/v1`,
`all-MiniLM-L6-v2@1110a243fdf4`, top_k 4, Windows AMD64 laptop CPU, Python
3.13, local Supabase in Docker. Raw output:
`backend/out/eval/retrieval-20261012T052758Z.json` (git-ignored). 65 labeled
requirements and 9 with no supporting evidence, per scenario.

Rankings, each posting against its own role's profile (17-22 facts):

| method | recall@1 | recall@3 | recall@5 | recall@10 | precision@3 | direct hit@1 | direct hit@3 | MRR |
|---|---|---|---|---|---|---|---|---|
| exact | 0.35 | 0.49 | 0.50 | 0.50 | 0.38 | 0.69 | 0.73 | 0.71 |
| full-text | 0.40 | 0.65 | 0.70 | 0.72 | 0.50 | 0.74 | 0.87 | 0.81 |
| semantic | 0.40 | 0.67 | 0.75 | 0.89 | 0.54 | 0.74 | 0.94 | 0.84 |
| hybrid (RRF) | 0.46 | 0.75 | 0.82 | 0.92 | 0.60 | 0.85 | 0.95 | 0.91 |

Rankings, every posting against all 78 facts in one bank (stress test):

| method | recall@1 | recall@3 | recall@5 | recall@10 | precision@3 | direct hit@1 | direct hit@3 | MRR |
|---|---|---|---|---|---|---|---|---|
| exact | 0.32 | 0.46 | 0.48 | 0.49 | 0.40 | 0.70 | 0.73 | 0.71 |
| full-text | 0.32 | 0.53 | 0.62 | 0.71 | 0.45 | 0.67 | 0.79 | 0.75 |
| semantic | 0.34 | 0.57 | 0.63 | 0.74 | 0.51 | 0.68 | 0.86 | 0.77 |
| hybrid (RRF) | 0.40 | 0.64 | 0.73 | 0.81 | 0.57 | 0.86 | 0.90 | 0.89 |

Context the drafting model would read. Full context keeps every labeled
fact by definition (coverage 1.00); the hybrid selection keeps:

| profile | facts sent (full -> hybrid) | characters (full -> hybrid) | labeled facts kept | requirements keeping a direct fact |
|---|---|---|---|---|
| own role, 8 postings | 17-22 -> 16-20 | 2,635-3,336 -> 2,307-2,993 | 0.82-1.00 | 1.00 |
| 78-fact bank, 8 postings | 78 -> 32-40 | 11,792 -> 4,560-5,917 | 0.62-1.00 | 0.86-1.00 |

Cost and runtime (this machine): model load 20.6 s once per worker process
(about 26 s on a cold import); indexing 227-302 ms for a 17-22-fact profile
and 860 ms for 78 facts; an unchanged re-index 11-28 ms with 0 facts
re-embedded. Per posting (median): query embeddings 50-60 ms, full-text
44-59 ms, exact vector search 51-56 ms, selection under 1 ms. Embeddings
cost no API money. The saving retrieval buys is input tokens: the largest
cut measured (about 7,200 characters, roughly 1,800 tokens) is worth about
$0.0004 per draft at the configured `gpt-5.6-luna` input price ($0.20 per
million tokens).

What the numbers say:

- **Hybrid is the best ranking on every metric in both scenarios.** Fusion
  beats the best single method by 7-10 points of recall@5 and lifts direct
  hit@1 from 0.74 to 0.85 (own) and 0.70 to 0.86 (bank). Semantic search
  has the best recall@10 of the single methods (it finds facts worded
  differently from the posting); exact and full-text put precise term
  matches first. Exact matching alone finds only half of
  the labeled evidence because it is deliberately strict (whole terms,
  plurals only: "mentor" does not match "Mentored"); full-text stemming
  covers those variants.
- **On realistic profiles, retrieval saves almost nothing and can lose
  evidence.** Own-role profiles shrink by at most about a quarter of
  roughly 3,000 characters, yet up to 18% of labeled supporting facts are left out
  (grc-2). Full context is complete, and costs well under a tenth of a cent.
- **On a large bank, retrieval halves the context but misses real
  evidence.** Examples: "Strong outbound prospecting skills" never reached
  the cold-calling SDR fact (no shared word, and the semantic model ranked a
  sales-methods line first); "Coach and mentor junior team members" ranked
  "Mentored two new SDRs" eighth in full-text and outside the semantic top
  eight.
- **Requirements with no evidence still return facts.** Semantic search
  always returns its nearest neighbors (19-20, the candidate cap), exact
  returns none. Retrieval cannot tell the model "nothing supports this";
  the existing claim validation and the ATS report's missing/uncertain
  states (ADR 0006) remain responsible for that.

Decision: keep the full verified profile whenever it fits the drafting
budget (`RETRIEVAL_FULL_CONTEXT_MAX_CHARS` default raised from 12,000 to
60,000, and auto never exceeds `MAX_CONTEXT_CHARS`). Use hybrid retrieval
when a profile would otherwise be cut by position, because a relevance-based
selection that keeps 86-100% of requirements' direct evidence is better than
dropping whatever comes last. `RETRIEVAL_MODE=hybrid` stays available for
measurement. No approximate vector index (exact search is 51-56 ms per
posting). No reranker: a cross-encoder would add a second model download
and CPU time, and since the default rarely retrieves, it is deferred until
large profiles are common or a draft-level evaluation shows misses that
matter.

## Not yet measured (needs an authorized budget)

Factual errors, human writing preference, and model cost per draft for full
context vs hybrid selection require live drafting calls. A proposed run:
the 8 postings x 2 modes x 2 profiles = 32 drafts with `gpt-5.6-luna` at low
effort, about $0.07 at the earlier measured $0.0021 per run, capped at $1.
Each draft would be checked by the existing claim validator (factual errors)
and compared side by side by the owner (preference). Until then, the
retrieval-level numbers above are the evidence, and they are on a small
fictional set labeled by the assistant.

## Consequences

- Most users get exactly the milestone 4 behavior (every confirmed fact),
  now with a recorded reason, and nothing is downloaded at runtime.
- Large experience banks get relevance-based selection instead of a
  positional cut, with provenance showing why each fact was chosen.
- Worker images must include the 90 MB model snapshot and CPU torch.
  `requirements.txt` is locked on Windows; torch on Linux pulls CUDA
  packages from PyPI that this lock does not contain, so milestone 8 must
  produce a Linux lock against PyTorch's CPU wheel index and verify the
  worker host's memory with the model loaded.
- The model loads lazily, so the first retrieval in a fresh worker process
  waits for it (19.6 s observed in the 2026-10-12 browser check, a 5-fact
  profile with `RETRIEVAL_MODE=hybrid`). It is not preloaded at worker start,
  because under the default most drafts never need it and it costs memory.
- Account deletion (deferred) must also remove `retrievals` (no cascade from
  runs); chunks cascade from evidence.
