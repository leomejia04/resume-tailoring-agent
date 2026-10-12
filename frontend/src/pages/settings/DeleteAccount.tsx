import { useEffect, useId, useRef, useState, type FormEvent } from 'react'
import { messageOf } from '../../api/errors'
import { useAuth } from '../../auth/AuthContext'
import { Alert } from '../../components/Alert'

const CONFIRM_WORD = 'DELETE'

/** Account deletion: explains what happens, then needs the password and a typed word. */
export function DeleteAccount() {
  const { deleteAccount } = useAuth()
  const [open, setOpen] = useState(false)
  const [password, setPassword] = useState('')
  const [typed, setTyped] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<{ message: string; requestId: string | null } | null>(null)
  const ids = { password: useId(), typed: useId(), hint: useId() }
  const passwordRef = useRef<HTMLInputElement>(null)
  const alertRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (open) passwordRef.current?.focus()
  }, [open])
  useEffect(() => {
    if (error) alertRef.current?.focus()
  }, [error])

  const ready = password.length > 0 && typed === CONFIRM_WORD

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!ready || pending) return
    setPending(true)
    setError(null)
    try {
      await deleteAccount(password) // the route guard then shows the sign-in confirmation
    } catch (err) {
      setError(messageOf(err))
      setPending(false)
    }
  }

  return (
    <section aria-labelledby="delete-account-heading" className="rounded-lg border border-red-200 bg-white p-6 text-sm text-slate-700">
      <h2 id="delete-account-heading" className="text-lg font-semibold text-slate-900">
        Delete your account
      </h2>
      <p className="mt-2">This permanently deletes, right away:</p>
      <ul className="mt-1 list-disc space-y-0.5 pl-6">
        <li>your sign-in, profile, and settings</li>
        <li>every uploaded master resume and the facts taken from it</li>
        <li>job postings, drafts, chat messages, generated PDFs, and match reports</li>
        <li>approved resumes, your application tracker, and usage history</li>
      </ul>
      <p className="mt-2">
        It cannot be undone. PDFs you already downloaded stay on your devices. If database backups are enabled, copies in
        them expire within 7 days. Text already sent to OpenAI to write drafts is
        handled under OpenAI&apos;s API data policy: by default it is not used for training and is kept for up to 30 days
        to monitor abuse.
      </p>
      {!open ? (
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="mt-4 rounded-md border border-red-300 px-4 py-2 font-medium text-red-800 hover:bg-red-50"
        >
          Delete account…
        </button>
      ) : (
        <form onSubmit={submit} className="mt-4 space-y-3" aria-describedby={ids.hint}>
          {error && (
            <Alert ref={alertRef} tone="error" requestId={error.requestId}>
              {error.message}
            </Alert>
          )}
          <p id={ids.hint}>
            Enter your password and type <strong>{CONFIRM_WORD}</strong> to confirm. If some tasks are still running,
            wait for them to finish first.
          </p>
          <div>
            <label htmlFor={ids.password} className="block font-medium">
              Password
            </label>
            <input
              ref={passwordRef}
              id={ids.password}
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="mt-1 w-full max-w-sm rounded-md border border-slate-300 px-3 py-2"
            />
          </div>
          <div>
            <label htmlFor={ids.typed} className="block font-medium">
              Type {CONFIRM_WORD}
            </label>
            <input
              id={ids.typed}
              type="text"
              autoComplete="off"
              spellCheck={false}
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              className="mt-1 w-full max-w-sm rounded-md border border-slate-300 px-3 py-2"
            />
          </div>
          <div className="flex flex-wrap gap-3">
            <button
              type="submit"
              aria-disabled={!ready || pending}
              className="rounded-md bg-red-700 px-4 py-2 font-medium text-white aria-disabled:opacity-60"
            >
              {pending ? 'Deleting…' : 'Permanently delete my account'}
            </button>
            <button
              type="button"
              onClick={() => {
                setOpen(false)
                setPassword('')
                setTyped('')
                setError(null)
              }}
              className="rounded-md border border-slate-300 px-4 py-2 font-medium"
            >
              Cancel
            </button>
          </div>
        </form>
      )}
    </section>
  )
}
