import type { ApplicationInput, ApplicationOut, ApplicationStatus, ApprovalOut } from '../../api/types'

export const STATUS_LABELS: Record<ApplicationStatus, string> = {
  planned: 'Planning to apply',
  applied: 'Applied',
  interviewing: 'Interviewing',
  offer: 'Offer',
  accepted: 'Accepted',
  rejected: 'Rejected',
  withdrawn: 'Withdrawn',
}

export const STATUSES = Object.keys(STATUS_LABELS) as ApplicationStatus[]

// Mirrors backend/app/schemas/tracker.py.
export const TRACKER_LIMITS = { companyMax: 200, roleMax: 200, urlMax: 2048, notesMax: 4000 }

/** Today's date in the user's own calendar, as YYYY-MM-DD (toISOString would use UTC). */
export function todayIso(now = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
}

/** Show a YYYY-MM-DD calendar date as-is; new Date('2026-10-01') would shift it through UTC. */
export function formatDay(iso: string): string {
  const [year, month, day] = iso.split('-').map(Number)
  return new Date(year!, month! - 1, day!).toLocaleDateString()
}

/** Only http(s) links become clickable (the server stores nothing else either). */
export function safeHref(url: string | null): string | null {
  if (!url) return null
  try {
    const parsed = new URL(url)
    return parsed.protocol === 'https:' || parsed.protocol === 'http:' ? parsed.href : null
  } catch {
    return null
  }
}

export function approvalLabel(approval: ApprovalOut): string {
  const job = `${approval.job_title ?? 'Job posting'}${approval.company ? ` at ${approval.company}` : ''}`
  return `${job}, version ${approval.draft_version} (approved ${new Date(approval.approved_at).toLocaleDateString()})`
}

/** Every form field as the user typed it; empty strings mean "none". */
export interface FormValues {
  company: string
  role: string
  job_url: string
  approval_id: string
  applied_on: string
  status: ApplicationStatus
  notes: string
}

export function valuesOf(entry: ApplicationOut): FormValues {
  return {
    company: entry.company,
    role: entry.role,
    job_url: entry.job_url ?? '',
    approval_id: entry.resume?.approval_id ?? '',
    applied_on: entry.applied_on ?? '',
    status: entry.status,
    notes: entry.notes ?? '',
  }
}

export function toInput(values: FormValues): ApplicationInput {
  const optional = (value: string) => (value.trim() ? value.trim() : null)
  return {
    company: values.company.trim(),
    role: values.role.trim(),
    job_url: optional(values.job_url),
    approval_id: values.approval_id || null,
    applied_on: values.applied_on || null,
    status: values.status,
    notes: optional(values.notes),
  }
}
