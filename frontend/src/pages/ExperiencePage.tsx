import { useCallback, useEffect, useId, useState } from 'react'
import { Link } from 'react-router'
import { ApiError, addEvidenceVersion, listEvidence, listMasters } from '../api/client'
import { messageOf } from '../api/errors'
import { ACCOUNT_LIMITS, type EvidenceItem, type ExperienceType, type MasterVersion } from '../api/types'
import { Alert } from '../components/Alert'

const TYPE_LABELS: Record<Exclude<ExperienceType, 'unclassified'>, string> = {
  professional: 'Professional (paid job)',
  internship: 'Internship',
  academic: 'Academic (coursework, degree)',
  personal: 'Personal project or activity',
  other: 'Other (skills, languages, certifications)',
}
const SECTION_LABELS: Record<string, string> = {
  summary: 'Summary',
  experience: 'Experience',
  education: 'Education',
  projects: 'Projects',
  skills: 'Skills',
  certifications: 'Certifications',
  leadership: 'Leadership and activities',
  awards: 'Awards',
}
const STATUS_STYLES = {
  unverified: { label: 'Needs review', className: 'bg-amber-100 text-amber-900' },
  verified: { label: 'Confirmed', className: 'bg-emerald-100 text-emerald-900' },
  rejected: { label: 'Rejected', className: 'bg-slate-200 text-slate-700' },
}

function initialType(item: EvidenceItem): Exclude<ExperienceType, 'unclassified'> {
  if (item.experience_type !== 'unclassified') return item.experience_type
  return item.suggested_type === 'unclassified' ? 'other' : item.suggested_type
}

interface ItemProps {
  item: EvidenceItem
  onChanged: (item: EvidenceItem) => void
  onStale: () => void
}

function EvidenceCard({ item, onChanged, onStale }: ItemProps) {
  const id = useId()
  // A choice made for an older version resets to the saved value once a newer version arrives.
  const [choice, setChoice] = useState({ version: item.version, kind: initialType(item) })
  const kind = choice.version === item.version ? choice.kind : initialType(item)
  const [editing, setEditing] = useState(false)
  const [text, setText] = useState(item.text)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<{ message: string; requestId: string | null } | null>(null)

  async function save(status: 'verified' | 'rejected' | 'unverified', newText?: string) {
    if (pending) return
    setError(null)
    if (newText !== undefined && !newText.trim()) {
      setError({ message: 'The fact cannot be empty.', requestId: null })
      return
    }
    setPending(true)
    try {
      const updated = await addEvidenceVersion(item.id, {
        expected_version: item.version,
        experience_type: kind,
        verification_status: status,
        ...(newText !== undefined && newText.trim() !== item.text ? { text: newText.trim() } : {}),
      })
      setEditing(false)
      onChanged(updated)
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) onStale()
      setError(messageOf(err))
    } finally {
      setPending(false)
    }
  }

  const status = STATUS_STYLES[item.verification_status]
  const context = [item.entry !== item.text ? item.entry : null, item.date_text].filter(Boolean).join(' · ')
  return (
    <li className="rounded-md border border-slate-200 bg-white p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          {editing ? (
            <>
              <label htmlFor={`${id}-text`} className="block text-sm font-medium text-slate-800">
                Wording
              </label>
              <textarea
                id={`${id}-text`}
                rows={3}
                maxLength={ACCOUNT_LIMITS.evidenceTextMax}
                value={text}
                onChange={(e) => setText(e.target.value)}
                className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-2"
              />
              <p className="mt-1 text-xs text-slate-500">
                Only describe what you actually did. The original wording stays on record.
              </p>
            </>
          ) : (
            <p className="text-slate-900">{item.text}</p>
          )}
          {context && <p className="mt-1 text-sm text-slate-600">{context}</p>}
          {item.change_source === 'user' && !editing && (
            <details className="mt-1 text-sm text-slate-600">
              <summary className="cursor-pointer">Edited by you, show original</summary>
              <p className="mt-1">{item.source_text}</p>
            </details>
          )}
        </div>
        <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${status.className}`}>{status.label}</span>
      </div>

      <div className="mt-3 flex flex-wrap items-end gap-3">
        <div>
          <label htmlFor={`${id}-kind`} className="block text-xs font-medium text-slate-700">
            Kind of experience
          </label>
          <select
            id={`${id}-kind`}
            value={kind}
            onChange={(e) =>
              setChoice({ version: item.version, kind: e.target.value as Exclude<ExperienceType, 'unclassified'> })
            }
            className="mt-1 rounded-md border border-slate-300 bg-white px-2 py-1 text-sm"
          >
            {Object.entries(TYPE_LABELS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </div>
        {editing ? (
          <>
            <button
              type="button"
              aria-disabled={pending}
              onClick={() => save('verified', text)}
              className="rounded-md bg-indigo-700 px-3 py-1.5 text-sm font-medium text-white aria-disabled:opacity-60"
            >
              Save and confirm
            </button>
            <button
              type="button"
              onClick={() => {
                setEditing(false)
                setText(item.text)
              }}
              className="rounded-md border border-slate-300 px-3 py-1.5 text-sm"
            >
              Cancel
            </button>
          </>
        ) : (
          <>
            <button
              type="button"
              aria-disabled={pending}
              onClick={() => save('verified')}
              className="rounded-md bg-indigo-700 px-3 py-1.5 text-sm font-medium text-white aria-disabled:opacity-60"
            >
              {item.verification_status === 'verified' ? 'Update' : 'Confirm'}
            </button>
            <button
              type="button"
              aria-disabled={pending}
              onClick={() => {
                if (pending) return
                setText(item.text)
                setEditing(true)
              }}
              className="rounded-md border border-slate-300 px-3 py-1.5 text-sm"
            >
              Edit
            </button>
            {item.verification_status !== 'rejected' && (
              <button
                type="button"
                aria-disabled={pending}
                onClick={() => save('rejected')}
                className="rounded-md border border-slate-300 px-3 py-1.5 text-sm"
              >
                Reject
              </button>
            )}
          </>
        )}
      </div>
      {error && (
        <div className="mt-3">
          <Alert tone="error" requestId={error.requestId}>
            {error.message}
          </Alert>
        </div>
      )}
    </li>
  )
}

export function ExperiencePage() {
  const [masters, setMasters] = useState<MasterVersion[] | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [items, setItems] = useState<EvidenceItem[] | null>(null)
  const [loadError, setLoadError] = useState<{ message: string; requestId: string | null } | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    listMasters(controller.signal)
      .then((list) => {
        setMasters(list)
        setSelected(list[0]?.id ?? null)
      })
      .catch((error) => {
        if (!controller.signal.aborted) setLoadError(messageOf(error))
      })
    return () => controller.abort()
  }, [])

  const loadItems = useCallback((masterId: string, signal?: AbortSignal) => {
    listEvidence(masterId, signal)
      .then(setItems)
      .catch((error) => {
        if (!signal?.aborted) setLoadError(messageOf(error))
      })
  }, [])

  useEffect(() => {
    if (!selected) return
    const controller = new AbortController()
    loadItems(selected, controller.signal)
    return () => controller.abort()
  }, [selected, loadItems])

  const replace = (updated: EvidenceItem) =>
    setItems((current) => current?.map((item) => (item.id === updated.id ? updated : item)) ?? null)

  const sections: [string, EvidenceItem[]][] = []
  for (const item of items ?? []) {
    const key = item.section ?? 'other'
    const group = sections.find(([name]) => name === key)
    if (group) group[1].push(item)
    else sections.push([key, [item]])
  }
  const counts = { verified: 0, rejected: 0, unverified: 0 }
  for (const item of items ?? []) counts[item.verification_status] += 1

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-900">Experience</h1>
        <p className="mt-2 max-w-2xl text-slate-600">
          Facts found in your resume. Only confirmed facts will be used in tailored resumes. Choose the right kind of
          experience so an internship is never presented as a full-time job.
        </p>
      </div>
      {loadError && (
        <Alert tone="error" requestId={loadError.requestId}>
          {loadError.message}
        </Alert>
      )}
      {masters !== null && masters.length === 0 && (
        <p className="text-slate-600">
          Nothing to review yet.{' '}
          <Link to="/profile" className="text-indigo-700 underline">
            Upload your master resume
          </Link>{' '}
          first.
        </p>
      )}
      {masters !== null && masters.length > 1 && selected && (
        <div>
          <label htmlFor="master-select" className="block text-sm font-medium text-slate-800">
            Resume version
          </label>
          <select
            id="master-select"
            value={selected}
            onChange={(e) => setSelected(e.target.value)}
            className="mt-1 rounded-md border border-slate-300 bg-white px-3 py-2"
          >
            {masters.map((m) => (
              <option key={m.id} value={m.id}>
                Version {m.version_number}: {m.original_filename}
              </option>
            ))}
          </select>
        </div>
      )}
      {selected && items === null && !loadError && <p role="status">Loading…</p>}
      {items !== null && (
        <>
          <p role="status" className="text-sm text-slate-700">
            {items.length} facts: {counts.verified} confirmed, {counts.unverified} need review, {counts.rejected}{' '}
            rejected.
          </p>
          {sections.map(([section, group]) => (
            <section key={section} aria-labelledby={`section-${section}`}>
              <h2 id={`section-${section}`} className="mb-2 text-lg font-semibold">
                {SECTION_LABELS[section] ?? 'Other'}
              </h2>
              <ul className="space-y-3">
                {group.map((item) => (
                  <EvidenceCard
                    key={item.id}
                    item={item}
                    onChanged={replace}
                    onStale={() => selected && loadItems(selected)}
                  />
                ))}
              </ul>
            </section>
          ))}
        </>
      )}
    </div>
  )
}
