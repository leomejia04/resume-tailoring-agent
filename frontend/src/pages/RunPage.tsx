import { useCallback, useEffect, useId, useRef, useState, type FormEvent } from 'react'
import { Link, useParams } from 'react-router'
import { answerQuestion, cancelRun, generateDraft, getAIStatus, getRun, listEvidence } from '../api/client'
import { messageOf } from '../api/errors'
import type { AIStatus, EvidenceItem, ExperienceType, Question, RunOut, RunStatus } from '../api/types'
import { AIBadge } from '../components/AIBadge'
import { Alert } from '../components/Alert'
import { DraftEditor } from './run/DraftEditor'
import { ExportPanel } from './run/ExportPanel'
import { ChatPanel, FormatPanel } from './run/RevisePanel'
import { VersionHistory } from './run/VersionHistory'

const STATUS_LABELS: Record<RunStatus, string> = {
  analyzing: 'Reading the posting',
  awaiting_confirmation: 'Questions for you',
  ready: 'Ready to draft',
  drafting: 'Drafting',
  validating: 'Checking the draft',
  awaiting_review: 'Draft ready for review',
  approved: 'Approved',
  failed: 'Something went wrong',
  cancelled: 'Cancelled',
}
const CATEGORY_LABELS = {
  required: 'Required',
  preferred: 'Preferred',
  responsibility: 'Responsibilities',
  terminology: 'Other terms',
} as const
const KIND_LABELS: Record<Exclude<ExperienceType, 'unclassified'>, string> = {
  professional: 'Professional (paid job)',
  internship: 'Internship',
  academic: 'Academic (coursework, degree)',
  personal: 'Personal project or activity',
  other: 'Other',
}
const DRAFTABLE: RunStatus[] = ['awaiting_confirmation', 'ready', 'awaiting_review', 'approved', 'failed']

function failureMessage(code: string | null): string {
  if (!code) return 'The last step failed. You can try again.'
  if (code.endsWith('budget_exceeded')) return 'Your daily AI spending limit was reached. Try again tomorrow.'
  if (code.endsWith('timeout')) return 'The AI took too long to answer. Try again.'
  if (code.endsWith('rate_limited')) return 'The AI service is busy. Wait a minute and try again.'
  return 'The AI step failed. You can try again; nothing was changed.'
}

interface EntryOption {
  evidenceId: string
  label: string
}

function QuestionCard({
  runId,
  question,
  entries,
  onAnswered,
}: {
  runId: string
  question: Question
  entries: EntryOption[]
  onAnswered: () => void
}) {
  const id = useId()
  const [answer, setAnswer] = useState<'yes' | 'no' | 'skip' | ''>('')
  const [kind, setKind] = useState<Exclude<ExperienceType, 'unclassified'>>('professional')
  const [entry, setEntry] = useState('')
  const [description, setDescription] = useState('')
  const [error, setError] = useState<{ message: string; requestId: string | null } | null>(null)
  const [pending, setPending] = useState(false)

  if (question.status !== 'open') {
    const label = { confirmed: 'Added to your facts', declined: 'Not used', dismissed: 'Skipped', open: '' }[
      question.status
    ]
    return (
      <li className="rounded-md border border-slate-200 bg-white p-4">
        <p className="font-medium">{question.term}</p>
        <p className="text-sm text-slate-600">{label}</p>
      </li>
    )
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (pending) return
    setError(null)
    if (!answer) {
      setError({ message: 'Choose an answer.', requestId: null })
      return
    }
    if (answer === 'yes' && !description.toLowerCase().includes(question.term.toLowerCase())) {
      setError({ message: `Describe what you did with ${question.term}, mentioning it by name.`, requestId: null })
      return
    }
    setPending(true)
    try {
      await answerQuestion(runId, question.id, {
        answer,
        ...(answer === 'yes'
          ? { experience_type: kind, description: description.trim(), ...(entry ? { entry_evidence_id: entry } : {}) }
          : {}),
      })
      onAnswered()
    } catch (err) {
      setError(messageOf(err))
    } finally {
      setPending(false)
    }
  }

  return (
    <li className="rounded-md border border-slate-200 bg-white p-4">
      <form onSubmit={submit} noValidate className="space-y-3">
        <fieldset>
          <legend className="font-medium text-slate-900">{question.question}</legend>
          <div className="mt-2 flex flex-wrap gap-4 text-sm">
            {(
              [
                ['yes', 'Yes, I have'],
                ['no', 'No'],
                ['skip', 'Skip for now'],
              ] as const
            ).map(([value, label]) => (
              <label key={value} className="flex items-center gap-2">
                <input
                  type="radio"
                  name={`${id}-answer`}
                  value={value}
                  checked={answer === value}
                  onChange={() => setAnswer(value)}
                />
                {label}
              </label>
            ))}
          </div>
        </fieldset>
        {answer === 'yes' && (
          <div className="space-y-3">
            <div className="grid gap-3 sm:grid-cols-2">
              <div>
                <label htmlFor={`${id}-kind`} className="block text-sm font-medium text-slate-800">
                  Kind of experience
                </label>
                <select
                  id={`${id}-kind`}
                  value={kind}
                  onChange={(e) => setKind(e.target.value as Exclude<ExperienceType, 'unclassified'>)}
                  className="mt-1 w-full rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
                >
                  {Object.entries(KIND_LABELS).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label htmlFor={`${id}-entry`} className="block text-sm font-medium text-slate-800">
                  Where <span className="font-normal text-slate-500">(optional)</span>
                </label>
                <select
                  id={`${id}-entry`}
                  value={entry}
                  onChange={(e) => setEntry(e.target.value)}
                  className="mt-1 w-full rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
                >
                  <option value="">Not tied to a specific job or project</option>
                  {entries.map((option) => (
                    <option key={option.evidenceId} value={option.evidenceId}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </div>
            </div>
            <div>
              <label htmlFor={`${id}-description`} className="block text-sm font-medium text-slate-800">
                What did you do with {question.term}?
              </label>
              <textarea
                id={`${id}-description`}
                rows={2}
                maxLength={500}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                aria-describedby={`${id}-hint`}
                className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-2"
              />
              <p id={`${id}-hint`} className="text-xs text-slate-500">
                Be specific and only describe what you actually did. This becomes a confirmed fact you can edit later.
              </p>
            </div>
          </div>
        )}
        {error && (
          <Alert tone="error" requestId={error.requestId}>
            {error.message}
          </Alert>
        )}
        <button
          type="submit"
          aria-disabled={pending}
          className="rounded-md bg-indigo-700 px-3 py-1.5 text-sm font-medium text-white aria-disabled:opacity-60"
        >
          {pending ? 'Saving…' : 'Save answer'}
        </button>
      </form>
    </li>
  )
}

export function RunPage() {
  const { runId = '' } = useParams()
  const [run, setRun] = useState<RunOut | null>(null)
  const [ai, setAI] = useState<AIStatus | null>(null)
  const [entries, setEntries] = useState<EntryOption[]>([])
  const [error, setError] = useState<{ message: string; requestId: string | null } | null>(null)
  const [busy, setBusy] = useState<'draft' | 'cancel' | null>(null)
  const draftHeadingRef = useRef<HTMLHeadingElement>(null)
  // Set when a new draft arrives; the effect below moves focus after React renders it.
  const focusDraftRef = useRef(false)

  useEffect(() => {
    if (focusDraftRef.current && run?.draft) {
      focusDraftRef.current = false
      draftHeadingRef.current?.focus()
    }
  }, [run])

  const load = useCallback(
    (signal?: AbortSignal) => {
      getRun(runId, signal)
        .then(setRun)
        .catch((err) => {
          if (!signal?.aborted) setError(messageOf(err))
        })
    },
    [runId],
  )

  useEffect(() => {
    const controller = new AbortController()
    load(controller.signal)
    getAIStatus(controller.signal)
      .then(setAI)
      .catch(() => undefined)
    listEvidence(undefined, controller.signal)
      .then((items: EvidenceItem[]) => {
        const seen = new Map<string, EntryOption>()
        for (const item of items) {
          if (item.verification_status === 'verified' && item.entry && !seen.has(item.entry)) {
            seen.set(item.entry, { evidenceId: item.id, label: [item.entry, item.date_text].filter(Boolean).join(' · ') })
          }
        }
        setEntries([...seen.values()])
      })
      .catch(() => undefined)
    return () => controller.abort()
  }, [load])

  async function draft() {
    if (busy) return
    setBusy('draft')
    setError(null)
    try {
      const updated = await generateDraft(runId)
      focusDraftRef.current = updated.draft !== null
      setRun(updated)
    } catch (err) {
      setError(messageOf(err))
      load()
    } finally {
      setBusy(null)
    }
  }

  async function cancel() {
    if (busy) return
    setBusy('cancel')
    try {
      setRun(await cancelRun(runId))
    } catch (err) {
      setError(messageOf(err))
    } finally {
      setBusy(null)
    }
  }

  if (!run) {
    return error ? (
      <Alert tone="error" requestId={error.requestId}>
        {error.message}
      </Alert>
    ) : (
      <p role="status">Loading…</p>
    )
  }

  const openQuestions = run.questions.filter((q) => q.status === 'open').length
  const canDraft = DRAFTABLE.includes(run.status) && run.analysis !== null
  return (
    <div className="space-y-8" aria-busy={busy !== null}>
      <div>
        <p className="text-sm text-slate-600">
          <Link to="/jobs/new" className="underline">
            Job postings
          </Link>
        </p>
        <h1 className="text-2xl font-semibold text-slate-900">
          {run.job.title ?? 'Job posting'}
          {run.job.company ? ` at ${run.job.company}` : ''}
        </h1>
        <p role="status" className="mt-1 text-slate-700">
          Status: <span className="font-medium">{STATUS_LABELS[run.status]}</span>
        </p>
      </div>
      <AIBadge status={ai} />
      {error && (
        <Alert tone="error" requestId={error.requestId}>
          {error.message}
        </Alert>
      )}
      {run.status === 'failed' && <Alert tone="error">{failureMessage(run.error_code)}</Alert>}

      {run.analysis && (
        <section aria-labelledby="requirements-heading">
          <h2 id="requirements-heading" className="mb-2 text-lg font-semibold">
            What the posting asks for
          </h2>
          <div className="grid gap-4 sm:grid-cols-2">
            {(Object.keys(CATEGORY_LABELS) as (keyof typeof CATEGORY_LABELS)[]).map((category) => {
              const items = run.analysis!.requirements.filter((r) => r.category === category)
              if (items.length === 0) return null
              return (
                <div key={category} className="rounded-md border border-slate-200 bg-white p-4">
                  <h3 className="font-medium">{CATEGORY_LABELS[category]}</h3>
                  <ul className="mt-1 list-disc space-y-1 pl-5 text-sm">
                    {items.map((r) => (
                      <li key={r.id}>
                        {r.text}
                        {r.mandatory && <span className="ml-1 text-xs text-red-700">(must have)</span>}
                      </li>
                    ))}
                  </ul>
                </div>
              )
            })}
          </div>
        </section>
      )}

      {run.questions.length > 0 && (
        <section aria-labelledby="questions-heading">
          <h2 id="questions-heading" className="text-lg font-semibold">
            Questions about your experience
          </h2>
          <p className="mb-3 text-sm text-slate-600">
            Your confirmed facts don't mention these. Answer only from real experience; tailored resumes never claim
            anything you haven't confirmed.
          </p>
          <ul className="space-y-3">
            {run.questions.map((q) => (
              <QuestionCard key={q.id} runId={run.id} question={q} entries={entries} onAnswered={() => load()} />
            ))}
          </ul>
        </section>
      )}

      {run.status !== 'cancelled' && (
        <div className="flex flex-wrap items-center gap-3">
          <button
            type="button"
            onClick={draft}
            aria-disabled={!canDraft || busy !== null}
            disabled={!canDraft}
            className="rounded-md bg-indigo-700 px-4 py-2 font-medium text-white aria-disabled:opacity-60"
          >
            {busy === 'draft' ? 'Drafting… this can take a minute' : run.draft ? 'Draft again' : 'Generate draft'}
          </button>
          {openQuestions > 0 && canDraft && (
            <p className="text-sm text-slate-600">{openQuestions} unanswered questions will be treated as gaps.</p>
          )}
          <button
            type="button"
            onClick={cancel}
            aria-disabled={busy !== null}
            className="rounded-md border border-slate-300 px-3 py-2 text-sm aria-disabled:opacity-60"
          >
            Cancel this run
          </button>
        </div>
      )}

      {run.draft && (
        <section aria-labelledby="draft-heading">
          <h2 id="draft-heading" ref={draftHeadingRef} tabIndex={-1} className="mb-2 text-lg font-semibold focus:outline-none">
            Draft (version {run.draft.version})
          </h2>
          <p className="mb-3 text-sm text-slate-600">
            {run.draft.model === 'demo-deterministic-v1' ? 'Produced by demo rules, not AI.' : `Written by ${run.draft.model}.`}{' '}
            AI cost for this run: ${Number(run.usage.cost_usd).toFixed(4)}.
          </p>
          <DraftEditor run={run} draft={run.draft} onRun={setRun} canRegenerate={ai?.provider !== 'disabled'} />
        </section>
      )}
      {run.draft && <ExportPanel run={run} onRun={setRun} />}
      {run.draft && <FormatPanel key={run.draft.version} run={run} onRun={setRun} />}
      {run.draft && <ChatPanel run={run} onRun={setRun} enabled={ai?.provider === 'openai'} />}
      {run.draft && <VersionHistory run={run} onRun={setRun} />}
    </div>
  )
}
