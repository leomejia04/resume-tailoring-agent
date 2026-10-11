import { describe, expect, it, vi } from 'vitest'
import { jsonResponse, mockFetch } from '../test/http'
import { ApiError, apiRequest, previewJobInput } from './client'

describe('apiRequest', () => {
  it('sends JSON to the versioned API path on the same origin', async () => {
    const fetchMock = mockFetch(jsonResponse(200, { saved: false }))
    await previewJobInput({ description: 'x'.repeat(50) })
    const [url, init] = fetchMock.mock.calls[0]!
    expect(url).toBe('/api/v1/jobs/preview')
    expect(init?.method).toBe('POST')
    expect(init?.credentials).toBe('same-origin')
    expect(new Headers(init?.headers).get('Content-Type')).toBe('application/json')
    expect(JSON.parse(String(init?.body))).toEqual({ description: 'x'.repeat(50) })
  })

  it('turns the error envelope into an ApiError with the request ID', async () => {
    mockFetch(
      jsonResponse(422, {
        error: {
          code: 'validation_error',
          message: 'Some fields are missing or invalid.',
          request_id: 'abc',
          fields: [{ loc: ['body', 'description'], message: 'Field required', type: 'missing' }],
        },
      }),
    )
    const error = await apiRequest('/jobs/preview', { method: 'POST', body: {} }).catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ status: 422, code: 'validation_error', requestId: 'abc' })
    expect((error as ApiError).fields).toHaveLength(1)
  })

  it('handles non-JSON error responses', async () => {
    mockFetch(new Response('Bad Gateway', { status: 502, headers: { 'X-Request-ID': 'gw' } }))
    await expect(apiRequest('/health')).rejects.toMatchObject({ status: 502, code: 'http_error', requestId: 'gw' })
  })

  it('reports network failures without leaking internals', async () => {
    mockFetch(new TypeError('Failed to fetch'))
    await expect(apiRequest('/health')).rejects.toMatchObject({ status: 0, code: 'network_error' })
  })

  it('times out slow requests', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        (_url: RequestInfo | URL, init?: RequestInit) =>
          new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
          }),
      ),
    )
    await expect(apiRequest('/health', { timeoutMs: 10 })).rejects.toMatchObject({ code: 'timeout' })
  })
})
