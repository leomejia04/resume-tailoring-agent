import { describe, expect, it, vi } from 'vitest'
import { safeNext } from '../auth/AuthContext'
import { TEST_CSRF } from '../test/setup'
import { USER, errorResponse, jsonResponse, routeFetch } from '../test/http'
import { apiRequest, listMasters, onSessionEnded, uploadMaster } from './client'

describe('CSRF and sessions', () => {
  it('echoes the CSRF cookie on state-changing requests only', async () => {
    const fetchMock = routeFetch({
      'POST /jobs/preview': () => jsonResponse(200, {}),
      'GET /masters': () => jsonResponse(200, []),
    })
    await apiRequest('/jobs/preview', { method: 'POST', body: { description: 'x' } })
    await listMasters()
    const [, post] = fetchMock.mock.calls[0]!
    const [, get] = fetchMock.mock.calls[1]!
    expect(new Headers(post?.headers).get('X-CSRF-Token')).toBe(TEST_CSRF)
    expect(new Headers(get?.headers).has('X-CSRF-Token')).toBe(false)
  })

  it('fetches a CSRF token first when the cookie is missing', async () => {
    document.cookie = 'rt_csrf=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT'
    const fetchMock = routeFetch({
      'GET /auth/csrf': () => jsonResponse(200, { csrf_token: 'fresh-token-fresh-token-fresh-token-123' }),
      'POST /auth/logout': () => new Response(null, { status: 204 }),
    })
    await apiRequest('/auth/logout', { method: 'POST' })
    const [, logout] = fetchMock.mock.calls[1]!
    expect(new Headers(logout?.headers).get('X-CSRF-Token')).toBe('fresh-token-fresh-token-fresh-token-123')
  })

  it('refreshes an expired session once and retries', async () => {
    const fetchMock = routeFetch({
      'GET /masters': [() => errorResponse(401, 'session_expired'), () => jsonResponse(200, [])],
      'POST /auth/refresh': () => jsonResponse(200, { status: 'signed_in', user: USER }),
    })
    await expect(listMasters()).resolves.toEqual([])
    expect(fetchMock.mock.calls.map(([url, init]) => `${init?.method ?? 'GET'} ${url}`)).toEqual([
      'GET /api/v1/masters',
      'POST /api/v1/auth/refresh',
      'GET /api/v1/masters',
    ])
  })

  it('announces a dead session when refresh fails', async () => {
    routeFetch({
      'GET /masters': () => errorResponse(401, 'session_expired'),
      'POST /auth/refresh': () => errorResponse(401, 'session_expired'),
    })
    const ended = vi.fn()
    const unsubscribe = onSessionEnded(ended)
    await expect(listMasters()).rejects.toMatchObject({ status: 401 })
    expect(ended).toHaveBeenCalledOnce()
    unsubscribe()
  })

  it('sends uploads as multipart form data', async () => {
    const fetchMock = routeFetch({
      'POST /masters': () => jsonResponse(201, { master: {}, duplicate: false }),
    })
    await uploadMaster(new File(['data'], 'resume.docx'))
    const [, init] = fetchMock.mock.calls[0]!
    expect(init?.body).toBeInstanceOf(FormData)
    expect(new Headers(init?.headers).has('Content-Type')).toBe(false) // browser adds the boundary
  })
})

describe('safeNext', () => {
  it.each([
    ['/experience', '/experience'],
    ['/jobs/new?x=1', '/jobs/new?x=1'],
    ['https://evil.example', '/'],
    ['//evil.example', '/'],
    ['/\\evil.example', '/'],
    [null, '/'],
  ])('%s -> %s', (input, expected) => {
    expect(safeNext(input)).toBe(expected)
  })
})
