import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, Navigate, useNavigate } from 'react-router'
import { fieldErrors, messageOf } from '../../api/errors'
import { ACCOUNT_LIMITS } from '../../api/types'
import { useAuth } from '../../auth/AuthContext'
import { Alert } from '../../components/Alert'
import { SubmitButton } from '../../components/SubmitButton'
import { TextField } from '../../components/TextField'

export function SignUpPage() {
  const { state, signUp } = useAuth()
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [formError, setFormError] = useState<{ message: string; requestId: string | null } | null>(null)
  const [confirmationSent, setConfirmationSent] = useState(false)
  const [pending, setPending] = useState(false)
  const alertRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (formError || confirmationSent) alertRef.current?.focus()
  }, [formError, confirmationSent])

  if (state.status === 'signed_in' && !pending) return <Navigate to="/profile" replace />

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    setFormError(null)
    const local: Record<string, string> = {}
    if (!email.trim()) local.email = 'Enter your email address.'
    if (password.length < ACCOUNT_LIMITS.passwordMin) {
      local.password = `Use at least ${ACCOUNT_LIMITS.passwordMin} characters.`
    }
    setErrors(local)
    if (Object.keys(local).length) return
    setPending(true)
    try {
      const status = await signUp(email, password)
      if (status === 'confirmation_required') setConfirmationSent(true)
      else navigate('/profile', { replace: true })
    } catch (error) {
      setErrors(fieldErrors(error))
      setFormError(messageOf(error))
    } finally {
      setPending(false)
    }
  }

  if (confirmationSent) {
    return (
      <Alert ref={alertRef} tone="success">
        Check your email to confirm your account, then sign in.
      </Alert>
    )
  }

  return (
    <div className="space-y-5">
      <h1 className="text-2xl font-semibold text-slate-900">Create an account</h1>
      {formError && (
        <Alert ref={alertRef} tone="error" requestId={formError.requestId}>
          {formError.message}
        </Alert>
      )}
      <form noValidate onSubmit={handleSubmit} className="space-y-4">
        <TextField
          label="Email"
          type="email"
          autoComplete="email"
          value={email}
          error={errors.email}
          onChange={(e) => setEmail(e.target.value)}
        />
        <TextField
          label="Password"
          type="password"
          autoComplete="new-password"
          hint={`At least ${ACCOUNT_LIMITS.passwordMin} characters. A memorable phrase works well.`}
          maxLength={ACCOUNT_LIMITS.passwordMax}
          value={password}
          error={errors.password}
          onChange={(e) => setPassword(e.target.value)}
        />
        <SubmitButton pending={pending} label="Create account" pendingLabel="Creating account…" />
      </form>
      <p className="text-sm">
        Already have an account?{' '}
        <Link to="/sign-in" className="text-indigo-700 underline">
          Sign in
        </Link>
      </p>
    </div>
  )
}
