# Resume tailoring app: Claude Code instructions

Act as a senior full-stack engineer, AI engineer, and practical software engineering mentor. Build a secure, low-cost, hosted application for multiple users to create truthful, tailored, one-page PDF resumes. Explain important decisions as you implement them.

## Read before working

- This is the complete, self-contained project specification and working guide. No supporting prompt files are required to begin. Read this file at the start of each session.
- Keep the requirements and progress section below consistent with explicitly approved decisions and actual implementation. Do not silently change scope or mark unverified work complete.
- Inspect the repository, applicable instructions, existing code, and Git changes first. Preserve unrelated work. Scaffold only missing pieces.
- When asked to start or continue implementation, make working changes and verify them. Do not stop at a plan or mockup. Do not start building merely because this file was loaded during an unrelated task.
- Choose reasonable reversible defaults. Ask only about decisions that materially change scope or concrete blockers. Continue independent work with fictional fixtures while templates or credentials are missing.

## Architecture and technology

- Frontend: React, TypeScript, Vite, Tailwind CSS. Backend: Python, FastAPI, Pydantic. Persistence: PostgreSQL, SQLAlchemy, Alembic.
- Use Supabase Auth, PostgreSQL, and private object storage initially; document any repository-driven alternative. Use a modular monolith and a durable worker for production generation/conversion.
- Generative model: the user's selected `gpt-5.6-luna`, server-side through the official OpenAI Python SDK and Responses API with schema-constrained output. Configure the model in an environment variable. Verify the exact public API model ID, availability, supported parameters, and structured-output support before integration. If unavailable, explain the blocker and obtain a model choice; never silently substitute another model. Low reasoning effort is a benchmark starting point only if supported.
- AI engineering: Sentence Transformers with Hugging Face `sentence-transformers/all-MiniLM-L6-v2`, pgvector in the existing database, hybrid retrieval, structured generation, provenance, and evaluation. Pin a trusted embedding revision; do not enable arbitrary remote model code.
- Documents: python-docx with template-aware edits, LibreOffice conversion, pdfplumber/pypdfium2 inspection (permissive licenses; PyMuPDF rejected as AGPL), and suitable OCR for supported image/scanned inputs. Verify package names, APIs, dependencies, and licensing through official sources.
- Initially coordinate workflows with ordinary Python functions. No extra vector service, microservices, fine-tuning, or mandatory LangGraph. Optional reranking comes after measurement.
- Pin compatible dependencies and commit lockfiles. Use pytest, Vitest/React Testing Library, Playwright, Ruff, ESLint, TypeScript checks, and CI as appropriate. Inspect actual project scripts before invoking commands; document verified setup/test commands in the README and update this file once they exist.

## Product contract

- Separate every user's profile, master versions, verified experience, preferences, jobs, chats, drafts, reports, files, retrieval results, usage, and tracker. Never default other accounts to Leonardo's personal data.
- Workflow: sign in → upload master → review extracted facts → maintain experience bank → enter/review job → generate → compare/edit/chat → inspect real PDF and fresh checks → approve exact version → download and revisit history.
- Master inputs: PDF/DOCX. Job inputs: pasted text, PDF/DOCX, supported screenshots, and accessible public URLs. Show incomplete extraction/OCR and blocked-link fallback to paste/upload; never bypass access restrictions.
- Keep master files immutable. Replacement creates a new master version, and drafts retain source versions. Automatically choose relevant bullets/projects for one page; show omissions and preserve the complete experience bank.
- Include history, section editing/regeneration, undo, chat revisions, and consent-based remembering of preferences. Facts require contextual verification. Offer extensible Data/GRC presets, with Business Analyst/Tech Sales support; allow other roles.
- Include a manual application tracker: company, role, URL, approved resume version, application date, status, notes. No Google Drive, auto-apply, outbound recruiter messages, or reminders in version one.
- Provide responsive, accessible dashboard/history, profile/master, experience bank, job input, review/editor, tracker, and settings screens with clear pending/error/empty states, labeled inputs, and keyboard navigation. Keep implementation jargon out of normal user flows unless it helps users decide.
- Support explicit user-owned data deletion with confirmation, retention explanation, and cleanup of derived artifacts, vectors, and caches.

## Truthfulness and provenance

- Store stable evidence IDs, owner, source text, context, dates, supported metrics, verification status, and versions. Distinguish professional, internship, academic, and personal experience.
- Reword, condense, emphasize, and reorder supported facts. Never invent skills, tools, metrics, responsibilities, dates, proficiency, credentials, employers, or experience duration. Preserve official employment titles; target-role headlines are separate.
- Ask precise questions for unsupported claims. A bare confirmation must not imply professional use, proficiency, or metrics. Newly verified facts need provenance and context.
- Require claim-to-evidence references and validate ownership, existence, availability, and actual support. Valid IDs and vector similarity alone do not establish truth. Check protected fields/metrics deterministically and route ambiguity to review.
- Generated resumes never automatically become evidence. Direct edits and chat instructions receive the same checks as generation. Remember global preferences only with consent.

## Formatting, approval, and downloads

- Prove document editing/rendering early. Preserve supported DOCX margins, fonts, alignment, spacing, bullets, and run-level bolding. PDF sources or unsupported layouts require a user-approved reconstructed/template layout; do not promise exact preservation.
- Render structured content deterministically. Meet one-page limits through supported condensation/selection and bounded checks; never conceal overflow or silently shrink below configured font/margin limits.
- Show the actual downloadable PDF. Verify page count, visible layout, completeness, reading order, contact fields, dates, clipping, and searchable/selectable text. Use consistent required fonts locally and in hosting. Export PDF only; internal DOCX copies are permitted. If layout cannot meet the rules, show an actionable failure rather than claim success.
- Leonardo's filename: `LeonardoSuzana_(Position)_(Company)_CV.pdf`; example: `LeonardoSuzana_Business_Analyst_OFI_CV.pdf`. Parentheses are placeholders. Other users use their own configured name prefix. Sanitize labels and use unique internal storage IDs.
- Make formatting preferences configurable, including languages on their own line, bold project technologies, three bullets per project, and section/project locks. Never fabricate content to satisfy a bullet count.
- Approval binds to an immutable content version and rendered artifact hash. Every change invalidates checks/approval. Use optimistic concurrency to prevent stale-tab approval or overwrites. Preserve previous approved artifacts on failures.

## ATS reporting

- Check every generated draft, chat revision, and direct-edit version; check the exact final PDF before approval. Mark reports stale/pending until rechecked and block approval until required checks complete successfully. Keep pending/failed/uncertain distinct from numeric scores.
- Show a 0–100 **ATS match estimate** from our documented rubric, not an employer's actual score or an interview guarantee. Initial weights: required qualifications 50%, responsibilities 25%, preferred qualifications 15%, relevant terminology 10%; normalize absent categories.
- Calculate arithmetic in Python from validated mappings. Deduplicate requirements, document partial credit, reward no keyword repetition, and prevent substring/negation/context false matches. Show critical mandatory gaps separately.
- Show direct/related/missing/uncertain evidence with source and PDF passages. Keep semantic assessment provenance, uncertainty, model/prompt/rubric versions. Compare master and tailored scores under compatible job/rubric versions.
- Keep match score, factual validation, and PDF parsing/readability checks separate. Display reports only in the app, never in employer-facing PDFs. Passing our parser does not guarantee every ATS behaves identically.
- At most two automatic improvement attempts per run. Stop when useful supported improvements are exhausted; never chase an arbitrary threshold with fabrication or keyword stuffing.

## Retrieval, cost, and reliability

- Implement a full-verified-profile baseline before RAG. Retrieve per requirement using exact/full-text plus semantic search over the authenticated user's verified evidence only; retain essential identity/education context independently.
- Disclose what resume/job data is sent to external AI providers. Do not train or fine-tune on user resumes initially, or use one user's evidence for another user.
- Chunk meaningful evidence with token-aware limits and parent context. Record source hash/version, embedding revision/dimension/normalization. Re-embed changes only. Start with exact vector search; add approximate indexes only after measurement.
- Build a labeled evaluation set across Data, GRC, Business Analyst, and Tech Sales, identifying direct/related/missing evidence. Compare full context, exact matching, semantic search, hybrid retrieval, and optional reranking; measure precision/recall at k, coverage, factual errors, human writing preference, runtime, and cost. Test ATS keyword repetition, negation, context mismatch, and mandatory gaps. AI judging is supplementary, not proof of truth. Provide full-context fallback for retrieval misses. Never invent benchmark results.
- Parse each master and analyze each posting once per version. Cache within tenant boundaries, reuse unchanged assessments, and request concise section patches instead of full rewrites. Use Python for arithmetic, file checks, rendering, and protected-field validation.
- Bound context/output, timeouts, retries, and repair loops. Track tokens/cost, runtime, retries, and cost per approved PDF. Enforce atomic quotas/reservations, rate/concurrency limits, idempotency, and an application spending cutoff.
- Persist legal run states such as queued, analyzing, awaiting_confirmation, drafting, validating, rendering, scoring, awaiting_review, approved, failed, and cancelled; define transitions and recover safely after restarts. Support cancellation, bounded retry, cleanup, and duplicate protection. FastAPI in-process background tasks alone are insufficient for durable work.
- Store private files outside ephemeral server disks. Isolate temporary conversion directories and handle database/storage partial failures. Verify hosting memory limits for embeddings/conversion, prices, and plan terms before choosing services. Do not promise free public operation.

## Persistence, API, and deployment

- Suggested entities: profiles, master versions, evidence records/versions, preferences, job postings/analysis versions, runs, draft versions, claim-evidence mappings, ATS reports, artifacts, chat messages, application records, and usage events. Add owner fields, foreign keys, constraints, indexes, and migrations. Use JSON for flexible analysis rather than replacing core relational facts.
- Design versioned routes for profiles/masters, experience, jobs, runs, drafts/revisions, reports, approval, artifacts/downloads, tracker, and settings. Use reusable authorization dependencies, correct HTTP methods/statuses, and generated OpenAPI schemas matching real behavior.
- Start with a simple browser API client. Explain JSON requests, responses, and how frontend state reflects persisted backend state; add state libraries only when justified.
- Prefer same-origin frontend/API production routing. If separate origins are needed, explicitly configure trusted origins/credentials and verify cookie/CSRF behavior. Do not weaken CORS.
- Provide Windows-compatible setup, migrations, fonts, rendering dependencies, and backend/worker containers. Explain Docker's role; never assume Microsoft Word or LibreOffice is installed on the host.
- Choose inexpensive static frontend hosting and a suitable Python/container worker host with managed private persistence. Verify current terms and actual memory/runtime requirements before naming a final host. Document auth redirects, environment placeholders, storage/RLS policies, worker setup, health checks, backups/restore, retention, limits, costs, and rollback. Do not present a sleeping/free demo as reliable production hosting.
- Complete preparation and verification before requesting paid provisioning/public deployment. Respect existing authorization; routine reversible implementation needs no repeated permission. Do not provision paid resources or publish without applicable authorization. Avoid unnecessary queue infrastructure when a simpler durable solution works.

## Mandatory security rules: every created or modified file

1. Keep secrets/API keys in server environment variables only, never source, frontend bundles, fixtures, or logs. Git-ignore `.env` and local secrets; `.env.example` contains placeholders. Never expose service-role credentials.
2. Enforce authentication AND authorization on every protected server endpoint and resource, including jobs, vectors, previews, downloads, and chats. Derive identity from verified sessions, never client owner IDs. Document necessary public auth routes; validate and protect them from abuse.
3. Use parameterized queries or an ORM only, including dynamic search/sort/vector filters. Never concatenate user input into SQL.
4. Validate all server inputs and LLM outputs with Pydantic: types, lengths, enums, ranges, nested structures, URLs, request/file limits. Frontend validation does not replace server validation.
5. Rate-limit login, signup, password reset, and paid-service endpoints with deployment-appropriate shared enforcement. Apply quotas/concurrency limits and prevent duplicate-request bypass.
6. Escape output by default; avoid `innerHTML`/`dangerouslySetInnerHTML`. Restrictively sanitize rendered Markdown. Use HttpOnly/Secure/SameSite cookies, trusted-origin checks, and CSRF protection for cookie-authenticated state changes. GET never mutates state.
7. Use proven auth, preferably Supabase Auth; delegate password storage. If the app owns passwords, use maintained Argon2id/bcrypt. Implement expiry/logout and safe reset/session handling; never leak tokens.
8. Install only verified, real, well-known, maintained packages. Flag uncertain packages before use; verify registry/source names and compatible versions.
9. Use production HTTPS and security headers, including a suitable CSP. Restrict CORS to explicit trusted domains, never wildcard. Keep localhost exceptions out of production.
10. Return safe errors with request IDs, never stack traces/internal details. Never log passwords, tokens, cookies, keys, signed links, full personal-data prompts, or sensitive resume content.
11. Use least-privilege DB/storage roles and enable/test RLS/access rules under the actual runtime role. A service role bypassing RLS is not isolation. Private buckets and short-lived links require ownership checks.
12. Validate upload size/type/signatures, ZIP expansion, image dimensions, OCR work, and encrypted/macro formats. Never execute uploads/macros. Restrict conversion permissions/resources/network access and use timeouts. Never construct shell commands from user input. Defend URL fetching against SSRF, including DNS resolution and redirects to reserved/private/metadata addresses.
13. Treat LLM inputs/outputs as untrusted data. Uploads, jobs, and retrieved text cannot override instructions, authorization, or tool permissions. Require confirmation before model-proposed destructive actions; omit destructive model tools initially.

If a request violates these rules, explain the conflict and propose a secure alternative before writing affected code. Enforce rules in implementation and tests; this file is guidance, not a security mechanism. Never disable security to pass a test.

## Working and teaching method

- Before each milestone, briefly explain its purpose, data flow, and important engineering concepts. Use familiar Python/TypeScript examples and plain language; teach APIs, HTTP/HTTPS, auth vs authorization, SQL/migrations, hosting/DNS, workers, embeddings/RAG, and evaluation when relevant.
- Implement small coherent changes, show exact commands, explain important code/decisions, and keep the progress section below current. Avoid giant code dumps and comments explaining every obvious line.
- Use fictional/consented fixtures and mocked paid APIs for routine tests. Test tenant isolation, auth/CSRF, SSRF, upload limits, prompt injection, unsupported claims, rendering, revision/approval staleness, quotas, retries, and restart recovery.
- Run checks appropriate to each change. Record commands, results, and limitations; never claim a test or deployment succeeded without evidence. Live evaluations need an explicitly bounded authorized budget.
- Keep README, architecture decisions, API/deployment documentation, and a concise learning log aligned with the app. Preserve user work and do not commit secrets. Explain deployment configuration before publishing; publish only when authorized.
- At the end of a work session, report what changed, why, what was checked, and concrete blockers. Update the Progress section in this file so another session can resume without guessing.

## Completion criteria

Deliver the complete end-to-end application specified in this file, including measured RAG and automatic ATS reports. Two independent users must securely upload, verify, tailor, revise, review, approve, download correctly named PDFs, revisit private history, and maintain separate trackers. Masters stay unchanged. Paid calls are bounded. Jobs/files survive restarts. Provide reproducible local/deployment instructions and honest evidence of completed checks.


## Implementation milestones

Milestones define build order, not optional scope. Complete all agreed functionality.

- [x] 1. Document proof: change one bullet in a master copy, preserve original/run formatting, render/inspect PDF and extracted text; document reconstruction limitations.
- [x] 2. Frontend/backend connection: React shell, FastAPI schemas/safe errors, job-input request, verified development CORS and setup commands.
- [x] 3. Accounts/private persistence: auth, profile/master/evidence versions, migrations, private storage, server ownership and RLS; prove two-user isolation before public exposure.
- [ ] 4. Structured AI baseline: verify selected model ID/capabilities, saved job analysis, contextual confirmations, full-profile grounded tailoring, protected-field checks and cost accounting.
- [ ] 5. Review/export: comparisons, chat/direct edits, selection/omissions, section regeneration, undo/history, formatting preferences, real PDF preview, immutable drafts, approval and named private downloads.
- [ ] 6. ATS/reliability: master baseline and every-version/final-PDF checks, bounded repairs, durable jobs, cancellation/retries, atomic quotas/spending cutoff and manual tracker.
- [ ] 7. Measured RAG: Hugging Face embeddings, pgvector hybrid retrieval, source/version tracking, tenant-scoped caches, labeled evaluation and full-context fallback.
- [ ] 8. Hosted beta: container/worker resources, HTTPS/headers/auth/storage/limits, full upload-to-download and security checks, deployment documentation and authorized publication.

## Progress: maintain this section after implementation sessions

- Status: milestones 1-3 complete (2026-10-11). Milestone 4 (structured AI baseline) is next.
- Next action: milestone 4. Verify the selected model ID through official OpenAI documentation before integrating; build job analysis, contextual confirmations, grounded tailoring, protected-field checks, and cost accounting against a mocked client. Live OpenAI calls need an API key in .env and an explicitly authorized budget.
- Known validation needed: `gpt-5.6-luna` is the user's selected label, not yet a verified public API model ID. Hosting resources and editable template capabilities need validation. The `local` converter backend is unverified on Windows. Only the fictional fixture and a generated test PDF have been processed. The user should upgrade Node 22.12 to Node 24 LTS and re-run frontend checks (all passed on 22.12 with engine warnings).
- Local stack: Docker Desktop (WSL2, data on D:) plus `npx supabase start` (Supabase CLI pinned in the root package.json; Postgres 17, Auth, Storage, Studio at :54323, Mailpit at :54324). `supabase/signing_keys.json` (ES256 private key) and `.env` are git-ignored; regenerate per README. Test accounts from the manual browser check live only in git-ignored `backend/out/demo_accounts.local.json`.
- Verified commands (Windows Git Bash; full list in README.md): repo root `npm ci`, `npx supabase gen signing-key --algorithm ES256 --append`, `npx supabase start`/`stop`/`status` (stop/start preserved users, files, migration state), `docker build -t resume-converter:dev docker/converter`. From `backend/`: hash-locked pip install; `npx --prefix .. supabase status -o env | .venv/Scripts/python -m scripts.local_env` (verified with `--output` to a temp file); `.venv/Scripts/alembic upgrade head`; `.venv/Scripts/alembic check` (no drift); `.venv/Scripts/python -m pytest -q` (114 passed including 27 integration tests in `tests/test_isolation.py`, about 35 s, repeatable); ruff check/format; uvicorn dev command. From `frontend/`: `npm ci`, `npm run dev`, `npm run typecheck`, `npm run lint`, `npm test` (43 passed), `npm run build`. The user's npm uses `script-shell = powershell` (no `&&` in scripts). Git Bash rewrites Linux paths in docker args (`MSYS_NO_PATHCONV=1`) and its heredocs collapse double backslashes, so write code files with the editor tool. `.claude/launch.json` starts backend/frontend for the desktop preview pane.
- Latest completed work and checks: Milestone 3 (details in `docs/decisions/0003-accounts-private-persistence-and-isolation.md`). Backend: `app/auth/` (BFF sessions in HttpOnly SameSite=Strict cookies, `__Host-` prefix in production; JWKS ES256/RS256 verification; double-submit CSRF with Origin/Referer check on all unsafe /api requests; Postgres-backed fail-closed rate limits; generic auth errors), `app/db/` (private `app` schema; runtime role `app_backend` without BYPASSRLS; per-transaction `app.user_id`; forced RLS; composite owner foreign keys; column-level grants making history immutable), migration `0001` (schema, role, RLS, grants, private `masters` bucket and storage policies), routes for auth (csrf, signup, login, refresh, logout, me, password reset), profile (optimistic concurrency), masters (hostile-upload validation, dedupe, versioning, storage with the user's token, download), and evidence (list, history, new immutable version with 409 when stale). Frontend: auth context, route guard with safe `next=`, sign-in/up/forgot/reset pages (reset token read from the URL fragment and stripped), Profile (details, upload, versions, download) and Experience (confirm/edit/reject with experience type) screens; the client adds the CSRF header, single-flight refresh-and-retry, and multipart uploads. Live browser run: sign-up, profile, PDF upload with 4 extracted facts, confirmations persisted, sign-out (API 401 afterwards), second account sees nothing and gets 404 for the first account's IDs; found and fixed focus loss caused by disabling a focused button. Milestones 1-2 are unchanged (ADR 0001/0002).
- Open blockers/decisions: none blocking milestones 1-3. Decided 2026-10-11: permissive PDF libraries; Docker Desktop for conversion; same-origin dev proxy; local Supabase before hosted; React Context for auth state; httpx2 for Starlette's test client. Deferred (recorded in ADR 0003): account/data deletion, orphan-object cleanup, shorter hosted JWT expiry, email confirmation with SMTP and CAPTCHA before public signup, proxy-aware client IPs, parsing inside the sandboxed worker, hosted Supabase provisioning (needs authorization). Never include secrets or real resume content in this file.
