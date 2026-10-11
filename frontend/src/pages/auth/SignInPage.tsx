import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, Navigate, useNavigate, useSearchParams } from 'react-router'
import { fieldErrors, messageOf } from '../../api/errors'
import { safeNext, useAuth } from '../../auth/AuthContext'
import { Alert } from '../../components/Alert'
import { SubmitButton } from '../../components/SubmitButton'
import { TextField } from '../../components/TextField'

export function SignInPage() {
  const { state, signIn } = useAuth()
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const next = safeNext(params.get('next'))
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [formError, setFormError] = useState<{ message: string; requestId: string | null } | null>(null)
  const [pending, setPending] = useState(false)
  const alertRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (formError) alertRef.current?.focus()
  }, [formError])

  if (state.status === 'signed_in' && !pending) return <Navigate to={next} replace />

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    setFormError(null)
    const local: Record<string, string> = {}
    if (!email.trim()) local.email = 'Enter your email address.'
    if (!password) local.password = 'Enter your password.'
    setErrors(local)
    if (Object.keys(local).length) return
    setPending(true)
    try {
      await signIn(email, password)
      navigate(next, { replace: true })
    } catch (error) {
      setErrors(fieldErrors(error))
      setFormError(messageOf(error))
    } finally {
      setPending(false)
    }
  }

  return (
    <div className="space-y-5">
      <h1 className="text-2xl font-semibold text-slate-900">Sign in</h1>
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
          autoComplete="current-password"
          value={password}
          error={errors.password}
          onChange={(e) => setPassword(e.target.value)}
        />
        <SubmitButton pending={pending} label="Sign in" pendingLabel="Signing in…" />
      </form>
      <div className="flex justify-between text-sm">
        <Link to="/forgot-password" className="text-indigo-700 underline">
          Forgot password?
        </Link>
        <Link to="/sign-up" className="text-indigo-700 underline">
          Create an account
        </Link>
      </div>
    </div>
  )
}
