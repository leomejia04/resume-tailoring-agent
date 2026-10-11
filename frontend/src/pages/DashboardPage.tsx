import { useEffect, useState } from 'react'
import { Link } from 'react-router'
import { approvalDownloadUrl, listApprovals, listRuns } from '../api/client'
import { messageOf } from '../api/errors'
import type { ApprovalOut, RunStatus, RunSummary } from '../api/types'
import { Alert } from '../components/Alert'

const STATUS_LABELS: Record<RunStatus, string> = {
  analyzing: 'Reading the posting',
  awaiting_confirmation: 'Questions for you',
  ready: 'Ready to draft',
  drafting: 'Drafting',
  validating: 'Checking',
  awaiting_review: 'Draft ready',
  approved: 'Approved',
  failed: 'Needs attention',
  cancelled: 'Cancelled',
}

export function DashboardPage() {
  const [runs, setRuns] = useState<RunSummary[] | null>(null)
  const [approvals, setApprovals] = useState<ApprovalOut[]>([])
  const [error, setError] = useState<{ message: string; requestId: string | null } | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    listApprovals(controller.signal)
      .then(setApprovals)
      .catch(() => undefined)
    listRuns(controller.signal)
      .then(setRuns)
      .catch((err) => {
        if (!controller.signal.aborted) setError(messageOf(err))
      })
    return () => controller.abort()
  }, [])

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-900">Dashboard</h1>
        <p className="mt-2 max-w-2xl text-slate-600">
          Upload your master resume, confirm the facts it contains, then tailor it to a job posting.
        </p>
      </div>
      <div className="flex flex-wrap gap-3">
        <Link to="/profile" className="rounded-md bg-indigo-700 px-4 py-2 font-medium text-white hover:bg-indigo-800">
          Upload master resume
        </Link>
        <Link to="/experience" className="rounded-md border border-slate-300 px-4 py-2 font-medium">
          Review experience
        </Link>
        <Link to="/jobs/new" className="rounded-md border border-slate-300 px-4 py-2 font-medium">
          Tailor to a job posting
        </Link>
      </div>
      {approvals.length > 0 && (
        <section aria-labelledby="approved-heading">
          <h2 id="approved-heading" className="mb-2 text-lg font-semibold">
            Approved resumes
          </h2>
          <ul className="divide-y divide-slate-200 rounded-md border border-slate-200 bg-white">
            {approvals.map((a) => (
              <li key={a.id} className="flex flex-wrap items-center justify-between gap-2 px-4 py-3">
                <span>
                  <Link to={`/runs/${a.run_id}`} className="font-medium text-indigo-700 underline">
                    {a.job_title ?? 'Job posting'}
                    {a.company ? ` at ${a.company}` : ''}
                  </Link>
                  <span className="ml-2 text-sm text-slate-600">
                    version {a.draft_version} · {new Date(a.approved_at).toLocaleDateString()}
                  </span>
                </span>
                <a href={approvalDownloadUrl(a.id)} className="break-all text-sm text-indigo-700 underline" download>
                  Download {a.filename}
                </a>
              </li>
            ))}
          </ul>
        </section>
      )}
      <section aria-labelledby="history-heading">
        <h2 id="history-heading" className="mb-2 text-lg font-semibold">
          Recent tailoring
        </h2>
        {error && (
          <Alert tone="error" requestId={error.requestId}>
            {error.message}
          </Alert>
        )}
        {runs === null && !error && <p role="status">Loading…</p>}
        {runs?.length === 0 && <p className="text-slate-600">Nothing yet. Add a job posting to start.</p>}
        {runs && runs.length > 0 && (
          <ul className="divide-y divide-slate-200 rounded-md border border-slate-200 bg-white">
            {runs.map((run) => (
              <li key={run.id} className="flex flex-wrap items-center justify-between gap-2 px-4 py-3">
                <Link to={`/runs/${run.id}`} className="font-medium text-indigo-700 underline">
                  {run.job_title ?? 'Job posting'}
                  {run.company ? ` at ${run.company}` : ''}
                </Link>
                <span className="text-sm text-slate-600">
                  {STATUS_LABELS[run.status]} · {new Date(run.created_at).toLocaleDateString()}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}
