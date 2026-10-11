import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router'
import { confirmPasswordReset } from '../../api/client'
import { fieldErrors, messageOf } from '../../api/errors'
import { ACCOUNT_LIMITS } from '../../api/types'
import { useAuth } from '../../auth/AuthContext'
import { Alert } from '../../components/Alert'
import { SubmitButton } from '../../components/SubmitButton'
import { TextField } from '../../components/TextField'

/** Read the one-time token from the URL fragment, then remove it from the address bar. */
function takeTokenFromHash(): string | null {
  const params = new URLSearchParams(window.location.hash.slice(1))
  const token = params.get('type') === 'recovery' ? params.get('token_hash') : null
  if (window.location.hash) {
    window.history.replaceState(null, '', window.location.pathname + window.location.search)
  }
  return token && /^[A-Za-z0-9_-]{10,200}$/.test(token) ? token : null
}

export function ResetPasswordPage() {
  const { sessionStarted } = useAuth()
  const navigate = useNavigate()
  const [token] = useState(takeTokenFromHash)
  const [password, setPassword] = useState('')
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [formError, setFormError] = useState<{ message: string; requestId: string | null } | null>(null)
  const [pending, setPending] = useState(false)
  const alertRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (formError) alertRef.current?.focus()
  }, [formError])

  if (!token) {
    return (
      <div className="space-y-4">
        <h1 className="text-2xl font-semibold text-slate-900">Choose a new password</h1>
        <Alert tone="error">This reset link is incomplete or has already been used.</Alert>
        <Link to="/forgot-password" className="text-sm text-indigo-700 underline">
          Request a new link
        </Link>
      </div>
    )
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    setFormError(null)
    if (password.length < ACCOUNT_LIMITS.passwordMin) {
      setErrors({ password: `Use at least ${ACCOUNT_LIMITS.passwordMin} characters.` })
      return
    }
    setErrors({})
    setPending(true)
    try {
      const session = await confirmPasswordReset(token as string, password)
      if (session.user) sessionStarted(session.user)
      navigate('/', { replace: true })
    } catch (error) {
      setErrors(fieldErrors(error))
      setFormError(messageOf(error))
    } finally {
      setPending(false)
    }
  }

  return (
    <div className="space-y-5">
      <h1 className="text-2xl font-semibold text-slate-900">Choose a new password</h1>
      {formError && (
        <Alert ref={alertRef} tone="error" requestId={formError.requestId}>
          {formError.message}
        </Alert>
      )}
      <form noValidate onSubmit={handleSubmit} className="space-y-4">
        <TextField
          label="New password"
          type="password"
          autoComplete="new-password"
          hint={`At least ${ACCOUNT_LIMITS.passwordMin} characters.`}
          maxLength={ACCOUNT_LIMITS.passwordMax}
          value={password}
          error={errors.password}
          onChange={(e) => setPassword(e.target.value)}
        />
        <SubmitButton pending={pending} label="Save new password" pendingLabel="Saving…" />
      </form>
    </div>
  )
}
