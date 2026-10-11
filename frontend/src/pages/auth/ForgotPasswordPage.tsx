import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link } from 'react-router'
import { requestPasswordReset } from '../../api/client'
import { fieldErrors, messageOf } from '../../api/errors'
import { Alert } from '../../components/Alert'
import { SubmitButton } from '../../components/SubmitButton'
import { TextField } from '../../components/TextField'

interface Result {
  tone: 'error' | 'success'
  message: string
  requestId: string | null
}

export function ForgotPasswordPage() {
  const [email, setEmail] = useState('')
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [result, setResult] = useState<Result | null>(null)
  const [pending, setPending] = useState(false)
  const alertRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (result) alertRef.current?.focus()
  }, [result])

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    setResult(null)
    if (!email.trim()) {
      setErrors({ email: 'Enter your email address.' })
      return
    }
    setErrors({})
    setPending(true)
    try {
      const response = await requestPasswordReset(email)
      setResult({ tone: 'success', message: response.message, requestId: null })
    } catch (error) {
      setErrors(fieldErrors(error))
      setResult({ tone: 'error', ...messageOf(error) })
    } finally {
      setPending(false)
    }
  }

  return (
    <div className="space-y-5">
      <h1 className="text-2xl font-semibold text-slate-900">Reset your password</h1>
      {result && (
        <Alert ref={alertRef} tone={result.tone} requestId={result.requestId}>
          {result.message}
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
        <SubmitButton pending={pending} label="Send reset link" pendingLabel="Sending…" />
      </form>
      <Link to="/sign-in" className="text-sm text-indigo-700 underline">
        Back to sign in
      </Link>
    </div>
  )
}
