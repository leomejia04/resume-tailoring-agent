# ADR 0002: API conventions and frontend/backend connection

Status: accepted (milestone 2)

## Decisions

1. **Same-origin by default.** In development the browser loads the React app
   from Vite (`127.0.0.1:5173`), and Vite proxies `/api` to FastAPI
   (`127.0.0.1:8000`). The browser only ever talks to one origin, so CORS is
   not involved. Production should keep frontend and API on one origin
   (reverse proxy or path routing) for the same reason.
2. **CORS only when explicitly configured.** `CORS_ALLOWED_ORIGINS` lists exact
   origins; it is empty by default (no CORS middleware at all). Wildcards,
   paths, credentials in URLs, and non-HTTP schemes are rejected at startup;
   production additionally requires HTTPS and forbids localhost. Credentialed
   CORS is off until the cookie/CSRF design lands with auth (milestone 3).
3. **Versioned routes** under `/api/v1`. OpenAPI and Swagger UI live at
   `/api/openapi.json` and `/api/docs` in development only.
4. **One error envelope** for every failure:
   `{"error": {"code", "message", "request_id", "fields"}}`. Validation errors
   report field locations and messages but never the submitted values. The
   OpenAPI schema documents this envelope (FastAPI's default
   `HTTPValidationError` is replaced) so generated docs match real behavior.
5. **Pure ASGI middleware** (`app/api/middleware.py`):
   - `RequestContextMiddleware` (outermost): server-generated request ID
     (client-supplied IDs are ignored), `X-Request-ID` response header,
     security headers (nosniff, frame denial, no-referrer, no-store, a
     `default-src 'none'` CSP for JSON responses, HSTS in production), one log
     line per request (method, path, status, duration, request ID; never
     bodies or query strings), and a safe 500 for unhandled exceptions with
     details logged server-side only.
   - `BodySizeLimitMiddleware`: rejects declared oversized bodies immediately
     and counts streamed bytes for bodies without `Content-Length`. The limit
     error is raised as a Starlette `HTTPException` because FastAPI converts
     any other exception during body parsing into a generic 400.
6. **Uvicorn flags:** `--no-access-log` (our request log replaces it and
   uvicorn's would include query strings) and `--no-server-header`.
7. **Job input preview** (`POST /api/v1/jobs/preview`) validates a pasted
   posting with Pydantic (`extra="forbid"`, length limits, http/https links
   without credentials, URL-only descriptions rejected), normalizes text, and
   detects section headings with deterministic Python. It saves nothing,
   calls no AI provider, and does not fetch links. It is unauthenticated in
   milestone 2 only because it stores nothing and costs nothing; job routes
   move behind authentication in milestone 3.
8. **Frontend:** a single `fetch` wrapper (`src/api/client.ts`) maps every
   failure (error envelope, non-JSON error, network failure, timeout) to one
   `ApiError` with the request ID. Types in `src/api/types.ts` mirror the
   Pydantic schemas by hand for now; generating them from OpenAPI is an option
   once the API grows. React Router provides URLs per screen; no state library
   is used. ESLint forbids `dangerouslySetInnerHTML`, `innerHTML`/`outerHTML`
   assignment, and `insertAdjacentHTML`.

## Known limitations

- The frontend trusts the shape of successful responses from our own API
  (TypeScript types, no runtime schema validation).
- No rate limiting yet: no login, signup, or paid endpoints exist. It is
  required before those are added.
- The heading detector is heuristic (Title Case or trailing colon plus
  keywords); it can miss unusual headings. It only informs the user.
