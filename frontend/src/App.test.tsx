import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'
import App from './App'
import { AuthProvider } from './auth/AuthProvider'
import { USER, errorResponse, jsonResponse, routeFetch } from './test/http'

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AuthProvider>
        <App />
      </AuthProvider>
    </MemoryRouter>,
  )
}

const signedIn = {
  'GET /auth/me': () => jsonResponse(200, USER),
  'GET /health': () => jsonResponse(200, { status: 'ok', version: '0.1.0' }),
}

describe('App shell', () => {
  it('shows the signed-in user, server status, skip link, and navigation', async () => {
    routeFetch(signedIn)
    renderAt('/')
    expect(await screen.findByText('Server connected')).toBeInTheDocument()
    expect(screen.getByText(USER.email)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Skip to main content' })).toHaveAttribute('href', '#main')

    const nav = screen.getByRole('navigation', { name: 'Main' })
    expect(within(nav).getByRole('link', { name: 'Dashboard' })).toHaveAttribute('aria-current', 'page')
    await userEvent.setup().click(within(nav).getByRole('link', { name: 'Job posting' }))
    expect(screen.getByRole('heading', { level: 1, name: 'Add a job posting' })).toBeInTheDocument()
  })

  it('sends signed-out visitors to sign in and remembers where they were going', async () => {
    routeFetch({ 'GET /auth/me': () => errorResponse(401, 'unauthenticated', 'Please sign in.') })
    renderAt('/experience')
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByRole('navigation', { name: 'Main' })).not.toBeInTheDocument()
  })

  it('renews an expired session silently on load', async () => {
    const fetchMock = routeFetch({
      'GET /auth/me': [() => errorResponse(401, 'session_expired'), () => jsonResponse(200, USER)],
      'POST /auth/refresh': () => jsonResponse(200, { status: 'signed_in', user: USER }),
      'GET /health': () => jsonResponse(200, { status: 'ok', version: '0.1.0' }),
    })
    renderAt('/')
    expect(await screen.findByText(USER.email)).toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith('/auth/refresh'))).toBe(true)
  })

  it('reports an unreachable server', async () => {
    routeFetch({ ...signedIn, 'GET /health': () => new TypeError('Failed to fetch') })
    renderAt('/')
    expect(await screen.findByText('Server unreachable')).toBeInTheDocument()
  })

  it('shows a not-found page for unknown paths', async () => {
    routeFetch(signedIn)
    renderAt('/nope')
    expect(await screen.findByRole('heading', { name: 'Page not found' })).toBeInTheDocument()
  })

  it('signs out', async () => {
    routeFetch({ ...signedIn, 'POST /auth/logout': () => new Response(null, { status: 204 }) })
    renderAt('/')
    await userEvent.setup().click(await screen.findByRole('button', { name: 'Sign out' }))
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
  })
})
