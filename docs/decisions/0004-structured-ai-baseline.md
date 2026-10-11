# ADR 0004: Structured AI baseline (grounded tailoring)

Status: accepted (milestone 4; live smoke test passed 2026-10-11)

## Model verification (2026-10-11)
The selected model `gpt-5.6-luna` is listed on OpenAI's official model page
(developers.openai.com/api/docs/models/gpt-5.6-luna): Responses API and
Structured Outputs supported; reasoning effort none/low/medium/high/xhigh/max
(default medium); 1,050,000-token context, 128,000 output tokens; $0.20 input,
$0.02 cached input, $1.20 output per 1M tokens (2x/1.5x above 272K input;
cache writes 1.25x input). A help-center article about a limited preview could
not be fetched (403); account access can only be confirmed with the owner's
API key. OpenAI's data-controls page states API data is not used for training
by default and abuse-monitoring logs are kept up to 30 days.

Official SDK: `openai==3.28.0` (Apache-2.0), `client.responses.parse(...,
text_format=<Pydantic model>)`, requests sent with `store=False`, low
reasoning effort (configurable), a hashed `safety_identifier`, bounded
`max_output_tokens`, SDK timeout and at most 2 retries. No temperature is sent.

## Decisions
1. **Providers.** `LLM_PROVIDER` unset means OpenAI when `OPENAI_API_KEY`
   exists, otherwise AI is disabled (routes answer 503 `ai_unavailable`). A
   `demo` provider is a deterministic rules-based stand-in for development and
   tests; the UI labels it "not an AI model" and production settings reject it.
   Another model is never substituted silently.
2. **Data minimization.** The model receives the posting text and the user's
   *verified* facts (with their entry lines, dates, and experience type) under
   short aliases (`E1`, `J1`). No name, email, phone, files, unverified or
   rejected facts, or internal UUIDs. The disclosure is shown before tailoring.
3. **Prompt injection.** Postings are passed as JSON data with instructions to
   ignore embedded instructions, outputs are schema-constrained and
   re-validated, and the deterministic validator removes any unsupported claim
   regardless of what the model was told.
4. **Analysis caching.** One analysis per posting version, model, and prompt
   version (`job_analyses` unique constraint), stored per user (no cross-tenant
   sharing). Terms are kept only if they literally appear in the posting;
   requirements are deduplicated, bounded (40), and renumbered server-side.
5. **Contextual confirmations.** Required/preferred requirements whose terms no
   verified fact mentions produce at most 8 precise questions. "Yes" requires an
   experience type and a 15-500 character description that names the term,
   optionally tied to an existing entry; it creates a verified fact with
   `origin = confirmation` and provenance on the question. "No" is remembered
   and not asked again; it is never claimed.
6. **Grounded drafting and validation** (`app/tailoring/validate.py`, rules
   `draft-rules/v1`). Each bullet must cite facts from the same entry; numbers,
   years, requirement terms, and name-like tokens (acronyms, mixed case,
   capitalized mid-sentence) must appear in the cited facts; skills must cite
   unattached facts; the summary is checked against all verified facts; every
   headline word must come from the posting's role title or the facts. Titles,
   employers, and dates are rendered from the master and never model-written.
   One repair call receives the exact problems; anything still failing is
   removed and listed. Omitted facts are reported; the experience bank is never
   changed by drafting.
7. **Runs and transactions.** Explicit states (analyzing, awaiting_confirmation,
   ready, drafting, validating, awaiting_review, failed, cancelled) with a
   transition table and lock_version-conditional updates. No database
   transaction is held during a model call. Model calls run inside the HTTP
   request for this baseline (up to ~3 minutes); milestone 6 moves the same
   functions into a durable worker.
8. **Cost accounting and limits.** Every call (including failed ones) records
   tokens, cached/cache-write/reasoning tokens, Decimal cost, latency, model,
   prompt version, and outcome in `usage_events`. Before each call the worst-case
   cost is reserved atomically against per-user (default $0.50/day) and
   application (default $5.00/day) limits with conditional UPDATEs, then
   settled to the actual cost. Paid routes are also rate-limited per user.

## Verified
`tests/test_tailoring_rules.py` (19), `tests/test_tailoring_flow.py` (10,
integration), `tests/test_openai_client.py` (10, official SDK with the network
call replaced): fabricated numbers/tools/years/headlines/citations removed,
prompt-injection output neutralized, one repair then success, failure and
timeout accounting, spending cutoff, concurrent reservations (exactly 2 of 5
succeed against the cap), data sent to the model excludes identity, rejected
facts, and UUIDs, RLS on all new tables, request shape (`store=False`, hashed
safety identifier, low effort, structured output). A manual browser run in
demo mode covered posting -> questions -> confirmation -> draft -> redraft.

### Live smoke test (2026-10-11, owner-authorized $1 cap)
- Free checks first: `GET /v1/models/gpt-5.6-luna` returned the model for the
  owner's key. OpenAI's model page confirms $0.20 / $0.02 cached / $1.20 per
  1M tokens (cache writes 1.25x input; >272K-token prompts surcharged), the
  Responses API, Structured Outputs, and reasoning effort `low`; the configured
  prices already matched.
- One run on the fictional fixture resume and the fictional posting (which
  contains a prompt-injection line), through the real routes with the app cap
  lowered to $1.00: job analysis (4 sensible requirements, two precise
  questions about Tableau and Snowflake, injection ignored), draft (validation
  passed with no issues and no repair; every bullet cites one fact; no
  Kubernetes claim), one chat revision ("lead with the SQL work"; it reordered
  and tightened without new facts and explained it kept Power BI rather than
  Tableau), then a one-page PDF that passed the layout and ATS checks.
- Recorded usage: analysis 464 in / 129 out tokens, draft 1,166 / 700 (355
  reasoning), chat 1,555 / 282 (119 reasoning); total $0.0021, latencies
  4-8 s. Recorded cost uses our configured prices; the owner should compare it
  with OpenAI's usage dashboard.
- Local note: Norton Antivirus re-signs HTTPS on the development machine, so
  Python's bundled certificates reject api.openai.com. `USE_OS_TRUST_STORE=true`
  makes the OpenAI client verify against the Windows certificate store
  (`truststore`, MIT, the library pip bundles); verification is never off.

## Limitations
- The live check covered one fictional Data Analyst posting. Prompt quality
  across Data, GRC, Business Analyst, and Tech Sales postings is not yet
  measured; that belongs to the labeled evaluation set (milestone 7).
- Requirement coverage is literal term matching; synonyms and semantic
  coverage arrive with retrieval/ATS work (milestones 6-7).
- The name-like-token rule can reject harmless capitalization; the repair or
  removal step handles it, favoring truthfulness over recall.
- A crash during a model call can leave a run in analyzing/drafting and a
  reservation unsettled; stuck-run recovery and reservation expiry belong to the
  milestone 6 worker.
