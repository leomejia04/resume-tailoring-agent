import { vi } from 'vitest'

export function jsonResponse(status: number, body: unknown, requestId = 'req-123'): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', 'X-Request-ID': requestId },
  })
}

export function errorResponse(status: number, code: string, message = 'Error', fields: unknown[] = []): Response {
  return jsonResponse(status, { error: { code, message, request_id: `ref-${status}`, fields } })
}

/** Replace global fetch with a mock that returns (or rejects with) each item in turn. */
export function mockFetch(...responses: (Response | Promise<Response> | Error)[]) {
  const fn = vi.fn<typeof fetch>()
  for (const response of responses) {
    if (response instanceof Error) fn.mockRejectedValueOnce(response)
    else fn.mockReturnValueOnce(Promise.resolve(response))
  }
  vi.stubGlobal('fetch', fn)
  return fn
}

type Handler = (init: RequestInit | undefined, url: string) => Response | Promise<Response> | Error

/**
 * Route requests by "METHOD /path" (path without /api/v1 and query string).
 * Handlers may be arrays to answer successive calls differently.
 */
export function routeFetch(routes: Record<string, Handler | Handler[]>) {
  const calls: Record<string, number> = {}
  const fn = vi.fn<typeof fetch>(async (input, init) => {
    const url = String(input)
    const path = url.replace(/^\/api\/v1/, '').split('?')[0]
    const key = `${init?.method ?? 'GET'} ${path}`
    const entry = routes[key]
    if (!entry) throw new Error(`Unexpected request: ${key}`)
    const index = calls[key] ?? 0
    calls[key] = index + 1
    const handler = Array.isArray(entry) ? entry[Math.min(index, entry.length - 1)]! : entry
    const result = await handler(init, url)
    if (result instanceof Error) throw result
    return result
  })
  vi.stubGlobal('fetch', fn)
  return fn
}

export const USER = { id: '11111111-1111-1111-1111-111111111111', email: 'jordan@example.com' }
