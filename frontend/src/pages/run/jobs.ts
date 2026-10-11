import type { JobKind, RunJob, RunOut } from '../../api/types'

/** How often the run page re-reads the run while background work is active. */
export const POLL_MS = 1500

export function jobOf(run: RunOut, kind: JobKind): RunJob | undefined {
  return run.jobs?.find((job) => job.kind === kind)
}

export function isActive(job: RunJob | undefined): boolean {
  return job !== undefined && (job.status === 'queued' || job.status === 'running')
}

export function anyActive(run: RunOut): boolean {
  return (run.jobs ?? []).some(isActive)
}

/** Workflow jobs change the draft; edits wait for them to avoid version conflicts. */
export function draftBusy(run: RunOut): boolean {
  return (['draft', 'regenerate', 'chat'] as const).some((kind) => isActive(jobOf(run, kind)))
}

export function progress(job: RunJob | undefined, working: string): string {
  if (!job) return working
  if (job.status === 'queued' && job.attempts > 0) {
    return `Trying again (attempt ${job.attempts + 1} of ${job.max_attempts})…`
  }
  return job.status === 'queued' ? 'Waiting to start…' : working
}

const FAILURES: Record<string, string> = {
  budget_exceeded: 'Your daily AI spending limit was reached. Try again tomorrow.',
  conflict: 'The draft changed while this was running, so nothing was changed. Try again.',
  grant_expired: 'This waited too long to start. Try again.',
  master_changed: 'Your master resume changed. Try again.',
  lease_expired: 'This was interrupted. Try again.',
  timeout: 'The AI took too long to answer. Try again.',
  rate_limited: 'The AI service is busy. Wait a minute and try again.',
}

/** A short, actionable sentence for a failed job (never internal details). */
export function failure(job: RunJob | undefined, what: string): string | null {
  if (!job || job.status !== 'failed') return null
  const code = job.error_code ?? ''
  return FAILURES[code] ?? `${what} didn't work this time. Try again.`
}
