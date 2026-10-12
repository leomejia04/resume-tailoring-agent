import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import {
  ApiError,
  approvalDownloadUrl,
  deleteApplication,
  listApplications,
  listApprovals,
  updateApplication,
} from '../api/client'
import { messageOf } from '../api/errors'
import type { ApplicationOut, ApplicationStatus, ApprovalOut } from '../api/types'
import { Alert } from '../components/Alert'
import { ApplicationForm } from './tracker/ApplicationForm'
import {
  STATUS_LABELS,
  STATUSES,
  formatDay,
  safeHref,
  toInput,
  todayIso,
  valuesOf,
  type FormValues,
} from './tracker/format'

type Notice = { tone: 'error' | 'success'; message: string; requestId?: string | null }
type Editing = { entry: ApplicationOut | null; initial: FormValues; key: number }

const BADGE: Record<ApplicationStatus, string> = {
  planned: 'bg-slate-100 text-slate-800',
  applied: 'bg-indigo-50 text-indigo-800',
  interviewing: 'bg-amber-50 text-amber-900',
  offer: 'bg-emerald-50 text-emerald-900',
  accepted: 'bg-emerald-100 text-emerald-900',
  rejected: 'bg-slate-100 text-slate-700',
  withdrawn: 'bg-slate-100 text-slate-700',
}

function newValues(approval?: ApprovalOut): FormValues {
  return {
    company: approval?.company ?? '',
    role: approval?.job_title ?? '',
    job_url: approval?.source_url ?? '',
    approval_id: approval?.id ?? '',
    applied_on: todayIso(),
    status: 'applied',
    notes: '',
  }
}

export function TrackerPage() {
  const [entries, setEntries] = useState<ApplicationOut[] | null>(null)
  const [approvals, setApprovals] = useState<ApprovalOut[]>([])
  const [loadError, setLoadError] = useState<{ message: string; requestId: string | null } | null>(null)
  const [notice, setNotice] = useState<Notice | null>(null)
  const [filter, setFilter] = useState<ApplicationStatus | ''>('')
  const [editing, setEditing] = useState<Editing | null>(null)
  const [confirming, setConfirming] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [params, setParams] = useSearchParams()
  const addButtonRef = useRef<HTMLButtonElement>(null)
  const focusAddButton = useRef(false)
  const keepButtonRef = useRef<HTMLButtonElement>(null)

  // "Track application" links from approved resumes open a prefilled form.
  const [prefillApproval] = useState(() => params.get('approval'))

  const reload = useCallback(async () => {
    try {
      setEntries(await listApplications())
      setLoadError(null)
    } catch (err) {
      setLoadError(messageOf(err))
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    listApplications(undefined, controller.signal)
      .then(setEntries)
      .catch((err) => {
        if (!controller.signal.aborted) setLoadError(messageOf(err))
      })
    listApprovals(controller.signal)
      .then((list) => {
        setApprovals(list)
        const approval = list.find((a) => a.id === prefillApproval)
        if (approval) setEditing({ entry: null, initial: newValues(approval), key: Date.now() })
      })
      .catch(() => undefined) // the form still works without resume links
    return () => controller.abort()
  }, [prefillApproval])

  useEffect(() => {
    if (confirming) keepButtonRef.current?.focus()
  }, [confirming])

  useEffect(() => {
    if (focusAddButton.current && !editing) {
      focusAddButton.current = false
      addButtonRef.current?.focus()
    }
  }, [editing, entries])

  function openNew() {
    setNotice(null)
    setEditing({ entry: null, initial: newValues(), key: Date.now() })
  }

  function openEdit(entry: ApplicationOut) {
    setNotice(null)
    setConfirming(null)
    setEditing({ entry, initial: valuesOf(entry), key: Date.now() })
  }

  // The link's parameter lives until its form closes, so a reload reopens it.
  function finishEditing() {
    focusAddButton.current = true
    setEditing(null)
    if (params.has('approval')) setParams({}, { replace: true })
  }

  function saved(created: boolean) {
    finishEditing()
    setNotice({ tone: 'success', message: created ? 'Application added.' : 'Changes saved.' })
    void reload()
  }

  async function failed(err: unknown) {
    setNotice({ tone: 'error', ...messageOf(err) })
    if (err instanceof ApiError && (err.code === 'conflict' || err.status === 404)) await reload()
  }

  async function changeStatus(entry: ApplicationOut, status: ApplicationStatus) {
    if (busyId) return
    setBusyId(entry.id)
    setNotice(null)
    try {
      const updated = await updateApplication(entry.id, toInput({ ...valuesOf(entry), status }), entry.lock_version)
      setEntries((current) => current?.map((e) => (e.id === updated.id ? updated : e)) ?? null)
      setNotice({ tone: 'success', message: `Status changed to ${STATUS_LABELS[status]}.` })
    } catch (err) {
      await failed(err)
    } finally {
      setBusyId(null)
    }
  }

  async function remove(entry: ApplicationOut) {
    if (busyId) return
    setBusyId(entry.id)
    setNotice(null)
    try {
      await deleteApplication(entry.id, entry.lock_version)
      setConfirming(null)
      setEntries((current) => current?.filter((e) => e.id !== entry.id) ?? null)
      setNotice({ tone: 'success', message: `${entry.role} at ${entry.company} was deleted.` })
      focusAddButton.current = true
    } catch (err) {
      setConfirming(null)
      await failed(err)
    } finally {
      setBusyId(null)
    }
  }

  const counts = new Map<ApplicationStatus, number>()
  for (const e of entries ?? []) counts.set(e.status, (counts.get(e.status) ?? 0) + 1)
  const shown = (entries ?? []).filter((e) => !filter || e.status === filter)

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-900">Application tracker</h1>
        <p className="mt-2 max-w-2xl text-slate-600">
          Keep a private record of where you applied and which approved resume you sent. Only you can see it, and
          nothing here is sent to employers or AI providers.
        </p>
      </div>
      <div className="flex flex-wrap items-end gap-4">
        {!editing && (
          <button
            type="button"
            ref={addButtonRef}
            onClick={openNew}
            className="rounded-md bg-indigo-700 px-4 py-2 font-medium text-white hover:bg-indigo-800"
          >
            Add application
          </button>
        )}
        {entries && entries.length > 0 && (
          <div>
            <label htmlFor="tracker-filter" className="block text-sm font-medium text-slate-800">
              Show
            </label>
            <select
              id="tracker-filter"
              value={filter}
              onChange={(e) => setFilter(e.target.value as ApplicationStatus | '')}
              className="mt-1 rounded-md border border-slate-300 bg-white px-3 py-2"
            >
              <option value="">All ({entries.length})</option>
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {STATUS_LABELS[s]} ({counts.get(s) ?? 0})
                </option>
              ))}
            </select>
          </div>
        )}
      </div>
      {notice && (
        <Alert tone={notice.tone} requestId={notice.requestId}>
          {notice.message}
        </Alert>
      )}
      {editing && (
        <ApplicationForm
          key={editing.key}
          entry={editing.entry}
          initial={editing.initial}
          approvals={approvals}
          onSaved={() => saved(editing.entry === null)}
          onCancel={finishEditing}
          onStale={() => void reload()}
        />
      )}
      <section aria-labelledby="applications-heading">
        <h2 id="applications-heading" className="sr-only">
          Your applications
        </h2>
        {loadError && (
          <Alert tone="error" requestId={loadError.requestId}>
            {loadError.message}
          </Alert>
        )}
        {entries === null && !loadError && <p role="status">Loading…</p>}
        {entries?.length === 0 && (
          <p className="text-slate-600">
            No applications yet. Add one when you apply, or use “Track application” on an approved resume.
          </p>
        )}
        {entries && entries.length > 0 && shown.length === 0 && (
          <p className="text-slate-600">No applications with this status.</p>
        )}
        {shown.length > 0 && (
          <ul className="divide-y divide-slate-200 rounded-md border border-slate-200 bg-white">
            {shown.map((entry) => {
              const href = safeHref(entry.job_url)
              const busy = busyId === entry.id
              return (
                <li key={entry.id} className="space-y-2 px-4 py-4" aria-busy={busy}>
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div>
                      <h3 className="font-semibold text-slate-900">
                        {entry.role} <span className="font-normal text-slate-600">at</span> {entry.company}
                      </h3>
                      <p className="text-sm text-slate-600">
                        <span className={`mr-2 rounded px-2 py-0.5 text-xs font-medium ${BADGE[entry.status]}`}>
                          {STATUS_LABELS[entry.status]}
                        </span>{' '}
                        {entry.applied_on ? `Applied on ${formatDay(entry.applied_on)}` : 'Not applied yet'}
                      </p>
                    </div>
                    <div>
                      <label htmlFor={`status-${entry.id}`} className="sr-only">
                        Status for {entry.role} at {entry.company}
                      </label>
                      <select
                        id={`status-${entry.id}`}
                        value={entry.status}
                        aria-disabled={busy}
                        onChange={(e) => void changeStatus(entry, e.target.value as ApplicationStatus)}
                        className="rounded-md border border-slate-300 bg-white px-2 py-1 text-sm"
                      >
                        {STATUSES.map((s) => (
                          <option key={s} value={s}>
                            {STATUS_LABELS[s]}
                          </option>
                        ))}
                      </select>
                    </div>
                  </div>
                  <ul className="space-y-1 text-sm">
                    {href && (
                      <li>
                        <a
                          href={href}
                          target="_blank"
                          rel="noopener noreferrer"
                          aria-label={`Job posting for ${entry.role} at ${entry.company} (opens in a new tab)`}
                          className="break-all text-indigo-700 underline"
                        >
                          Job posting
                        </a>
                      </li>
                    )}
                    {entry.resume && (
                      <li className="flex flex-wrap gap-x-3">
                        <span className="text-slate-700">
                          Resume sent: version {entry.resume.draft_version}
                        </span>
                        <a href={approvalDownloadUrl(entry.resume.approval_id)} className="break-all text-indigo-700 underline" download>
                          Download {entry.resume.filename}
                        </a>
                        <Link
                          to={`/runs/${entry.resume.run_id}`}
                          aria-label={`Open review for ${entry.role} at ${entry.company}`}
                          className="text-indigo-700 underline"
                        >
                          Open review
                        </Link>
                      </li>
                    )}
                  </ul>
                  {entry.notes && <p className="whitespace-pre-wrap break-words text-sm text-slate-700">{entry.notes}</p>}
                  {confirming === entry.id ? (
                    <div role="group" aria-label="Confirm delete" className="rounded-md border border-red-200 bg-red-50 p-3 text-sm">
                      <p className="font-medium text-red-900">
                        Delete this entry permanently? The approved resume itself is kept.
                      </p>
                      <div className="mt-2 flex flex-wrap gap-2">
                        <button
                          type="button"
                          onClick={() => void remove(entry)}
                          aria-disabled={busy}
                          className="rounded-md bg-red-700 px-3 py-1 font-medium text-white aria-disabled:opacity-60"
                        >
                          {busy ? 'Deleting…' : 'Delete permanently'}
                        </button>
                        <button
                          type="button"
                          ref={keepButtonRef}
                          onClick={() => setConfirming(null)}
                          className="rounded-md border border-slate-300 bg-white px-3 py-1 font-medium"
                        >
                          Keep
                        </button>
                      </div>
                    </div>
                  ) : (
                    <div className="flex flex-wrap gap-2 text-sm">
                      <button
                        type="button"
                        onClick={() => openEdit(entry)}
                        aria-label={`Edit ${entry.role} at ${entry.company}`}
                        className="rounded-md border border-slate-300 px-3 py-1 font-medium"
                      >
                        Edit
                      </button>
                      <button
                        type="button"
                        onClick={() => setConfirming(entry.id)}
                        aria-label={`Delete ${entry.role} at ${entry.company}`}
                        className="rounded-md border border-slate-300 px-3 py-1 font-medium text-red-800"
                      >
                        Delete
                      </button>
                    </div>
                  )}
                </li>
              )
            })}
          </ul>
        )}
      </section>
    </div>
  )
}
