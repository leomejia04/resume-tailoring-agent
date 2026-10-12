import { useEffect, useState } from 'react'
import { getVersion, listVersions, restoreVersion } from '../../api/client'
import { messageOf } from '../../api/errors'
import type { DraftContent, RunOut, VersionOut, VersionSummary } from '../../api/types'
import { Alert } from '../../components/Alert'

const ATS_LABELS = { pending: 'not checked', failed: 'check failed', stale: 'check out of date' } as const

function atsLabel(v: VersionSummary): string | null {
  if (!v.ats_status) return null
  if (v.ats_status === 'complete') return v.ats_score == null ? null : `ATS estimate ${v.ats_score}`
  return `ATS ${ATS_LABELS[v.ats_status]}`
}

const SOURCE_LABELS: Record<string, string> = {
  generation: 'Generated',
  repair: 'Generated and corrected',
  edit: 'Your edit',
  chat: 'Chat revision',
  improve: 'Automatic improvement',
}

function bulletTexts(content: DraftContent): Set<string> {
  return new Set(content.entries.flatMap((e) => e.bullets.map((b) => b.text)))
}

export function VersionHistory({ run, onRun }: { run: RunOut; onRun: (run: RunOut) => void }) {
  const current = run.draft
  const [versions, setVersions] = useState<VersionSummary[]>([])
  const [selected, setSelected] = useState<VersionOut | null>(null)
  const [error, setError] = useState<{ message: string; requestId: string | null } | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!current) return
    const controller = new AbortController()
    listVersions(run.id, controller.signal)
      .then(setVersions)
      .catch(() => undefined)
    return () => controller.abort()
  }, [run.id, current])

  if (!current) return null

  async function compare(version: number) {
    setError(null)
    try {
      setSelected(await getVersion(run.id, version))
    } catch (err) {
      setError(messageOf(err))
    }
  }

  async function restore(version: number) {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      onRun(await restoreVersion(run.id, version, current!.version))
      setSelected(null)
    } catch (err) {
      setError(messageOf(err))
    } finally {
      setBusy(false)
    }
  }

  const now = bulletTexts(current.content)
  const then = selected ? bulletTexts(selected.content) : new Set<string>()
  const added = [...now].filter((t) => !then.has(t))
  const removed = [...then].filter((t) => !now.has(t))

  return (
    <section aria-labelledby="history-heading" className="space-y-3">
      <h2 id="history-heading" className="text-lg font-semibold">
        Version history
      </h2>
      {error && (
        <Alert tone="error" requestId={error.requestId}>
          {error.message}
        </Alert>
      )}
      <ul className="divide-y divide-slate-200 rounded-md border border-slate-200 bg-white text-sm">
        {versions.map((v) => (
          <li key={v.version} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2">
            <span>
              Version {v.version} · {SOURCE_LABELS[v.created_by] ?? v.created_by} ·{' '}
              {new Date(v.created_at).toLocaleString()}
              {atsLabel(v) && <> · {atsLabel(v)}</>}
              {v.version === current.version && <span className="ml-1 font-medium">(current)</span>}
            </span>
            {v.version !== current.version && (
              <button type="button" onClick={() => compare(v.version)} className="text-indigo-700 underline">
                Compare with current
              </button>
            )}
          </li>
        ))}
      </ul>
      {selected && (
        <div className="rounded-md border border-slate-200 bg-white p-4 text-sm">
          <p className="font-medium">
            Version {selected.version} compared with version {current.version}
          </p>
          {added.length === 0 && removed.length === 0 && <p className="text-slate-600">The bullets are the same.</p>}
          {added.length > 0 && (
            <>
              <p className="mt-2 text-emerald-800">Only in the current version:</p>
              <ul className="list-disc pl-5">
                {added.map((t) => (
                  <li key={t}>{t}</li>
                ))}
              </ul>
            </>
          )}
          {removed.length > 0 && (
            <>
              <p className="mt-2 text-red-800">Only in version {selected.version}:</p>
              <ul className="list-disc pl-5">
                {removed.map((t) => (
                  <li key={t}>{t}</li>
                ))}
              </ul>
            </>
          )}
          <button
            type="button"
            aria-disabled={busy}
            onClick={() => restore(selected.version)}
            className="mt-3 rounded-md border border-slate-300 px-3 py-1.5 aria-disabled:opacity-60"
          >
            {busy ? 'Restoring…' : `Restore version ${selected.version} as a new version`}
          </button>
        </div>
      )}
    </section>
  )
}
