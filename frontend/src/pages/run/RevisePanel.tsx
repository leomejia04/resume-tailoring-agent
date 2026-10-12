import { useEffect, useId, useState, type FormEvent } from 'react'
import { chatRevision, editDraft, getPreferences, listChat, rememberPreferences } from '../../api/client'
import { messageOf } from '../../api/errors'
import type { ChatMessage, FormatPreferences, RunOut } from '../../api/types'
import { Alert } from '../../components/Alert'
import { isActive, jobOf, progress } from './jobs'
import { LayoutChoice } from './LayoutChoice'

const DEFAULT_FORMAT: FormatPreferences = {
  layout: 'standard',
  max_bullets_per_entry: 4,
  project_bullets: 3,
  bold_project_technologies: true,
  languages_on_own_line: true,
  show_headline: false,
  locked_entries: [],
}

export function ChatPanel({ run, onRun, enabled }: { run: RunOut; onRun: (run: RunOut) => void; enabled: boolean }) {
  const id = useId()
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<{ message: string; requestId: string | null } | null>(null)
  const chatJob = jobOf(run, 'chat')
  const revising = isActive(chatJob)

  // The reply is saved by the background job: reload when a chat job starts or finishes.
  useEffect(() => {
    const controller = new AbortController()
    listChat(run.id, controller.signal)
      .then(setMessages)
      .catch(() => undefined)
    return () => controller.abort()
  }, [run.id, chatJob?.id, chatJob?.status])

  if (!run.draft) return null

  async function send(event: FormEvent) {
    event.preventDefault()
    if (busy || revising || text.trim().length < 2) return
    setBusy(true)
    setError(null)
    const message = text.trim()
    try {
      onRun(await chatRevision(run.id, message, run.draft!.version))
      setText('')
    } catch (err) {
      setError(messageOf(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section aria-labelledby="chat-heading" className="space-y-3">
      <h2 id="chat-heading" className="text-lg font-semibold">
        Ask for changes
      </h2>
      <p className="text-sm text-slate-600">
        Requests go through the same fact checks as everything else: anything your facts don't support is left out.
      </p>
      {messages.length > 0 && (
        <ol className="space-y-2 text-sm" aria-label="Revision conversation">
          {messages.map((m, i) => (
            <li key={i} className={m.role === 'user' ? 'text-slate-900' : 'rounded-md bg-slate-100 p-2 text-slate-800'}>
              <span className="font-medium">{m.role === 'user' ? 'You: ' : 'Reply: '}</span>
              {m.content}
              {m.draft_version && <span className="text-slate-500"> (version {m.draft_version})</span>}
            </li>
          ))}
        </ol>
      )}
      {error && (
        <Alert tone="error" requestId={error.requestId}>
          {error.message}
        </Alert>
      )}
      <form onSubmit={send} className="space-y-2">
        <label htmlFor={id} className="block text-sm font-medium">
          What should change?
        </label>
        <textarea
          id={id}
          rows={2}
          maxLength={500}
          value={text}
          disabled={!enabled}
          onChange={(e) => setText(e.target.value)}
          placeholder={enabled ? 'For example: put the SQL work first and keep each bullet shorter.' : 'Needs the AI provider.'}
          className="block w-full rounded-md border border-slate-300 px-3 py-2 disabled:bg-slate-100"
        />
        <button
          type="submit"
          aria-disabled={busy || revising || !enabled}
          disabled={!enabled}
          className="rounded-md bg-indigo-700 px-3 py-1.5 text-sm font-medium text-white aria-disabled:opacity-60"
        >
          {busy || revising ? progress(chatJob, 'Revising… this can take a minute') : 'Send'}
        </button>
      </form>
    </section>
  )
}

export function FormatPanel({ run, onRun }: { run: RunOut; onRun: (run: RunOut) => void }) {
  const draft = run.draft
  const initial = draft?.content.format ?? DEFAULT_FORMAT
  const [format, setFormat] = useState<FormatPreferences>(initial)
  const [remember, setRemember] = useState(false)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<{ tone: 'error' | 'success'; message: string; requestId?: string | null } | null>(
    null,
  )
  const ids = { bullets: useId(), project: useId() }
  if (!draft) return null
  const entries = draft.content.entries.map((e) => e.entry).filter((e): e is string => Boolean(e))

  async function apply(event: FormEvent) {
    event.preventDefault()
    if (busy) return
    setBusy(true)
    setNotice(null)
    try {
      const updated = await editDraft(run.id, draft!.version, [{ op: 'update_format', format }])
      if (remember) {
        const current = await getPreferences()
        await rememberPreferences(format, current.lock_version)
      }
      onRun(updated)
      setNotice({ tone: 'success', message: remember ? 'Applied and remembered for future resumes.' : 'Applied to this resume.' })
    } catch (err) {
      setNotice({ tone: 'error', ...messageOf(err) })
    } finally {
      setBusy(false)
    }
  }

  return (
    <section aria-labelledby="format-heading">
      <details className="rounded-lg border border-slate-200 bg-white p-4">
        <summary id="format-heading" className="cursor-pointer text-lg font-semibold">
          Formatting
        </summary>
        <form onSubmit={apply} className="mt-3 space-y-3 text-sm">
          <LayoutChoice value={format.layout} onChange={(layout) => setFormat({ ...format, layout })} />
          <div className="flex flex-wrap gap-4">
            <div>
              <label htmlFor={ids.bullets} className="block font-medium">
                Most bullets per job
              </label>
              <input
                id={ids.bullets}
                type="number"
                min={1}
                max={8}
                value={format.max_bullets_per_entry}
                onChange={(e) => setFormat({ ...format, max_bullets_per_entry: Number(e.target.value) })}
                className="mt-1 w-20 rounded-md border border-slate-300 px-2 py-1"
              />
            </div>
            <div>
              <label htmlFor={ids.project} className="block font-medium">
                Most bullets per project
              </label>
              <input
                id={ids.project}
                type="number"
                min={1}
                max={6}
                value={format.project_bullets}
                onChange={(e) => setFormat({ ...format, project_bullets: Number(e.target.value) })}
                className="mt-1 w-20 rounded-md border border-slate-300 px-2 py-1"
              />
            </div>
          </div>
          <p className="text-slate-600">Limits only remove bullets; nothing is ever invented to fill a count.</p>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={format.bold_project_technologies}
              onChange={(e) => setFormat({ ...format, bold_project_technologies: e.target.checked })}
            />
            Bold project technologies
          </label>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={format.show_headline}
              onChange={(e) => setFormat({ ...format, show_headline: e.target.checked })}
            />
            Show the headline on the PDF (standard layout only)
          </label>
          {entries.length > 0 && (
            <fieldset>
              <legend className="font-medium">Keep exactly as in my master resume</legend>
              {entries.map((entry) => (
                <label key={entry} className="mt-1 flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={format.locked_entries.includes(entry)}
                    onChange={(e) =>
                      setFormat({
                        ...format,
                        locked_entries: e.target.checked
                          ? [...format.locked_entries, entry]
                          : format.locked_entries.filter((x) => x !== entry),
                      })
                    }
                  />
                  {entry}
                </label>
              ))}
            </fieldset>
          )}
          <label className="flex items-center gap-2 border-t border-slate-200 pt-3">
            <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} />
            Remember these settings for future resumes
          </label>
          {notice && (
            <Alert tone={notice.tone} requestId={notice.requestId}>
              {notice.message}
            </Alert>
          )}
          <button type="submit" aria-disabled={busy} className="rounded-md bg-indigo-700 px-3 py-1.5 font-medium text-white aria-disabled:opacity-60">
            {busy ? 'Applying…' : 'Apply'}
          </button>
        </form>
      </details>
    </section>
  )
}
