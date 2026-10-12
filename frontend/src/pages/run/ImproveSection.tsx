import { useState } from 'react'
import { improveDraft } from '../../api/client'
import { messageOf } from '../../api/errors'
import type { ImprovementAttempt, RunOut } from '../../api/types'
import { Alert } from '../../components/Alert'
import { draftBusy, failure, isActive, jobOf, progress } from './jobs'

/** Why improvement stopped (the improve job's result.stopped; ADR 0008). */
const STOPPED: Record<string, string> = {
  no_gaps: 'Every requirement is already covered.',
  exhausted:
    'Nothing more to add: the remaining gaps are not covered by your confirmed facts. If one is true for you, confirm it on the Experience page first.',
  limit_reached: 'Both automatic attempts for this run have been used.',
  no_improvement: 'The last attempt would not have raised the estimate without losing other matches, so it was not saved.',
  new_repetition: 'The last attempt would have repeated terms too often, so it was not saved.',
  unsupported_changes: 'The last attempt suggested claims your confirmed facts do not support, so it was not saved.',
  too_long: 'The last attempt made a section too long, so it was not saved.',
  draft_changed: 'The draft changed while this was running, so nothing was saved.',
  not_checked: 'The draft could not be scored, so nothing was tried.',
}

const ATTEMPT_OUTCOMES: Record<string, string> = {
  no_improvement: 'not saved: it would not have raised the estimate',
  new_repetition: 'not saved: it repeated terms too often',
  unsupported_changes: 'not saved: it included unsupported claims',
  too_long: 'not saved: it made a section too long',
  draft_changed: 'not saved: the draft changed meanwhile',
  interrupted: 'interrupted',
  timeout: 'did not finish: the AI took too long',
  budget_exceeded: 'did not run: your daily AI spending limit was reached',
}

function describe(attempt: ImprovementAttempt): string {
  if (attempt.status === 'running') return 'in progress'
  if (attempt.status === 'saved') {
    const scores =
      attempt.score_before !== null && attempt.score_after !== null
        ? ` from ${attempt.score_before} to ${attempt.score_after}`
        : ''
    return `raised the estimate${scores} (version ${attempt.new_version})`
  }
  return ATTEMPT_OUTCOMES[attempt.outcome ?? ''] ?? (attempt.status === 'failed' ? 'did not finish' : 'not saved')
}

export function ImproveSection({
  run,
  onRun,
  enabled,
}: {
  run: RunOut
  onRun: (run: RunOut) => void
  enabled: boolean
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<{ message: string; requestId: string | null } | null>(null)
  const draft = run.draft
  if (!draft) return null

  const job = jobOf(run, 'improve')
  const active = isActive(job)
  const attempts = run.improvements ?? []
  const left = run.improvements_left ?? 0
  // A stop reason describes the version the job checked; after an edit it no longer applies.
  const stopped =
    job?.status === 'succeeded' && job.result?.version === draft.version ? String(job.result?.stopped ?? '') : ''
  const failed = failure(job, 'Automatic improvement')
  const requirementText = new Map((run.analysis?.requirements ?? []).map((r) => [r.id, r.text]))
  const canRetry = enabled && left > 0 && !active && !draftBusy(run) && stopped !== 'no_gaps'

  async function improve() {
    if (busy || !canRetry || !draft) return
    setBusy(true)
    setError(null)
    try {
      onRun(await improveDraft(run.id, draft.version))
    } catch (err) {
      setError(messageOf(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-2 rounded-md border border-slate-200 bg-white p-4 text-sm" aria-busy={active || busy}>
      <h3 className="font-medium text-slate-800">Automatic improvement</h3>
      <p className="text-slate-600">
        After each draft, up to two attempts per run look for requirements your confirmed facts cover but the draft
        does not show. A change is saved only if it raises the estimate without losing other matches or repeating
        terms. Unconfirmed skills are never added.
      </p>
      {active && (
        <p role="status" className="font-medium text-indigo-900">
          {progress(job, 'Improving the match with your confirmed facts…')}
        </p>
      )}
      {attempts.length > 0 && (
        <ul className="list-disc space-y-1 pl-5 text-slate-700">
          {attempts.map((a) => (
            <li key={a.attempt}>
              Attempt {a.attempt}: {describe(a)}
              {a.targets.length > 0 && (
                <span className="text-slate-600">
                  {' '}
                  · worked on {a.targets.map((id) => requirementText.get(id) ?? id).join('; ')}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
      {!active && stopped && STOPPED[stopped] && <p className="text-slate-700">{STOPPED[stopped]}</p>}
      {!active && failed && <Alert tone="error">{failed}</Alert>}
      {error && (
        <Alert tone="error" requestId={error.requestId}>
          {error.message}
        </Alert>
      )}
      {canRetry && (
        <div className="flex flex-wrap items-center gap-3">
          <button
            type="button"
            onClick={improve}
            aria-disabled={busy}
            className="rounded-md border border-slate-300 px-3 py-1.5 aria-disabled:opacity-60"
          >
            {busy ? 'Starting…' : job ? 'Try improving again' : 'Improve the match'}
          </button>
          <span className="text-xs text-slate-600">
            {left} of 2 attempts left for this run.
          </span>
        </div>
      )}
    </div>
  )
}
