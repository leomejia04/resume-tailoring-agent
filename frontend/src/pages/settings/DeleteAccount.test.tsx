import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'
import App from '../../App'
import { AuthProvider } from '../../auth/AuthProvider'
import { USER, errorResponse, jsonResponse, routeFetch } from '../../test/http'

const PREFS = {
  preferences: {
    layout: 'standard',
    max_bullets_per_entry: 4,
    project_bullets: 3,
    bold_project_technologies: true,
    show_headline: false,
  },
  lock_version: 1,
  remembered: false,
}

const signedIn = {
  'GET /auth/me': () => jsonResponse(200, USER),
  'GET /health': () => jsonResponse(200, { status: 'ok', version: '0.1.0' }),
  'GET /profile/preferences': () => jsonResponse(200, PREFS),
  'GET /auth/csrf': () => jsonResponse(200, { csrf_token: 'c'.repeat(43) }),
}

function renderSettings() {
  render(
    <MemoryRouter initialEntries={['/settings']}>
      <AuthProvider>
        <App />
      </AuthProvider>
    </MemoryRouter>,
  )
  return userEvent.setup()
}

describe('Delete account', () => {
  it('explains retention and needs the password and the typed word', async () => {
    const fetchMock = routeFetch({
      ...signedIn,
      'POST /account/deletion': () => errorResponse(403, 'password_incorrect', 'Your password is incorrect.'),
    })
    const user = renderSettings()
    await user.click(await screen.findByRole('button', { name: 'Delete account…' }))
    expect(screen.getByText(/expire within 7 days/)).toBeInTheDocument()
    expect(screen.getByLabelText('Password')).toHaveFocus()

    const submit = screen.getByRole('button', { name: 'Permanently delete my account' })
    await user.type(screen.getByLabelText('Password'), 'wrong password')
    await user.type(screen.getByLabelText('Type DELETE'), 'delete')
    await user.click(submit)
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith('/account/deletion'))).toBe(false)

    await user.clear(screen.getByLabelText('Type DELETE'))
    await user.type(screen.getByLabelText('Type DELETE'), 'DELETE')
    await user.click(submit)
    expect(await screen.findByRole('alert')).toHaveTextContent('Your password is incorrect.')
    expect(screen.getByRole('heading', { level: 1, name: 'Settings' })).toBeInTheDocument()
  })

  it('deletes, ends the session, and confirms on the sign-in page', async () => {
    const fetchMock = routeFetch({
      ...signedIn,
      'POST /account/deletion': () => new Response(null, { status: 204 }),
    })
    const user = renderSettings()
    await user.click(await screen.findByRole('button', { name: 'Delete account…' }))
    await user.type(screen.getByLabelText('Password'), 'correct horse battery')
    await user.type(screen.getByLabelText('Type DELETE'), 'DELETE')
    await user.click(screen.getByRole('button', { name: 'Permanently delete my account' }))

    expect(await screen.findByText('Your account and all of its data were deleted.')).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 1, name: 'Sign in' })).toBeInTheDocument()
    const call = fetchMock.mock.calls.find(([url]) => String(url).endsWith('/account/deletion'))!
    expect(JSON.parse(String(call[1]?.body))).toEqual({ password: 'correct horse battery', confirmation: 'DELETE' })
  })
})
