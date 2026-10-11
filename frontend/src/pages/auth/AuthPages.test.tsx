import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it } from 'vitest'
import App from '../../App'
import { AuthProvider } from '../../auth/AuthProvider'
import { USER, errorResponse, jsonResponse, routeFetch } from '../../test/http'

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AuthProvider>
        <App />
      </AuthProvider>
    </MemoryRouter>,
  )
}

const signedOut = { 'GET /auth/me': () => errorResponse(401, 'unauthenticated') }
const health = {
  'GET /health': () => jsonResponse(200, { status: 'ok', version: '0.1.0' }),
  'GET /runs': () => jsonResponse(200, []),
  'GET /approvals': () => jsonResponse(200, []),
}

describe('Sign in', () => {
  it('signs in and returns to the page the user wanted', async () => {
    const fetchMock = routeFetch({
      ...signedOut,
      ...health,
      'POST /auth/login': () => jsonResponse(200, { status: 'signed_in', user: USER }),
      'GET /masters': () => jsonResponse(200, []),
    })
    renderAt('/sign-in?next=%2Fexperience')
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Email'), USER.email)
    await user.type(screen.getByLabelText('Password'), 'correct horse battery')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))

    expect(await screen.findByRole('heading', { level: 1, name: 'Experience' })).toBeInTheDocument()
    const login = fetchMock.mock.calls.find(([url]) => String(url).endsWith('/auth/login'))!
    expect(JSON.parse(String(login[1]?.body))).toEqual({ email: USER.email, password: 'correct horse battery' })
  })

  it('never redirects outside the app after sign-in', async () => {
    routeFetch({
      ...signedOut,
      ...health,
      'POST /auth/login': () => jsonResponse(200, { status: 'signed_in', user: USER }),
    })
    renderAt('/sign-in?next=https%3A%2F%2Fevil.example')
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Email'), USER.email)
    await user.type(screen.getByLabelText('Password'), 'correct horse battery')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))
    expect(await screen.findByRole('heading', { level: 1, name: 'Dashboard' })).toBeInTheDocument()
  })

  it('shows a generic message for wrong credentials', async () => {
    routeFetch({
      ...signedOut,
      'POST /auth/login': () => errorResponse(401, 'invalid_credentials', 'Email or password is incorrect.'),
    })
    renderAt('/sign-in')
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Email'), USER.email)
    await user.type(screen.getByLabelText('Password'), 'wrong password here')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Email or password is incorrect.')
    expect(alert).toHaveFocus()
  })
})

describe('Sign up', () => {
  it('checks password length before contacting the server', async () => {
    const fetchMock = routeFetch(signedOut)
    renderAt('/sign-up')
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Email'), USER.email)
    await user.type(screen.getByLabelText('Password'), 'short')
    await user.click(screen.getByRole('button', { name: 'Create account' }))
    expect(screen.getByLabelText('Password')).toHaveAccessibleDescription(/Use at least 12 characters/)
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/auth/signup'))).toBe(false)
  })

  it('shows server field errors next to the field', async () => {
    routeFetch({
      ...signedOut,
      'POST /auth/signup': () =>
        errorResponse(422, 'validation_error', 'Choose a stronger password.', [
          { loc: ['body', 'password'], message: 'Password is too common.', type: 'weak_password' },
        ]),
    })
    renderAt('/sign-up')
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Email'), USER.email)
    await user.type(screen.getByLabelText('Password'), 'password1234')
    await user.click(screen.getByRole('button', { name: 'Create account' }))
    expect(await screen.findByText('Password is too common.')).toBeInTheDocument()
  })
})

describe('Password reset', () => {
  afterEach(() => window.history.replaceState(null, '', '/'))

  it('takes the token from the link fragment, hides it, and submits it', async () => {
    window.history.replaceState(null, '', '/reset-password#token_hash=abcdef0123456789&type=recovery')
    const fetchMock = routeFetch({
      ...signedOut,
      ...health,
      'POST /auth/password-reset/confirm': () => jsonResponse(200, { status: 'signed_in', user: USER }),
    })
    renderAt('/reset-password')
    expect(window.location.hash).toBe('')
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('New password'), 'a brand new passphrase')
    await user.click(screen.getByRole('button', { name: 'Save new password' }))
    expect(await screen.findByRole('heading', { level: 1, name: 'Dashboard' })).toBeInTheDocument()
    const call = fetchMock.mock.calls.find(([url]) => String(url).endsWith('/password-reset/confirm'))!
    expect(JSON.parse(String(call[1]?.body))).toEqual({
      token_hash: 'abcdef0123456789',
      password: 'a brand new passphrase',
    })
  })

  it('explains an incomplete link', async () => {
    routeFetch(signedOut)
    renderAt('/reset-password')
    expect(await screen.findByText(/incomplete or has already been used/)).toBeInTheDocument()
  })

  it('gives the same answer whether or not the account exists', async () => {
    routeFetch({
      ...signedOut,
      'POST /auth/password-reset': () =>
        jsonResponse(202, { status: 'accepted', message: 'If an account exists for that email, a reset link is on its way.' }),
    })
    renderAt('/forgot-password')
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Email'), 'someone@example.com')
    await user.click(screen.getByRole('button', { name: 'Send reset link' }))
    expect(await screen.findByRole('status')).toHaveTextContent('If an account exists')
  })
})
