import { useState } from 'react'
import { approvalDownloadUrl, approveDraft, artifactPdfUrl, artifactPreviewUrl, renderDraft } from '../../api/client'
import { messageOf } from '../../api/errors'
import type { RunOut } from '../../api/types'
import { Alert } from '../../components/Alert'

interface Props {
  run: RunOut
  onRun: (run: RunOut) => void
}

export function ExportPanel({ run, onRun }: Props) {
  const [busy, setBusy] = useState<'render' | 'approve' | null>(null)
  const [error, setError] = useState<{ message: string; requestId: string | null } | null>(null)
  const draft = run.draft
  if (!draft) return null
  const artifact = run.artifact ?? null
  const current = artifact !== null && artifact.draft_version === draft.version

  async function render() {
    if (busy) return
    setBusy('render')
    setError(null)
    try {
      onRun(await renderDraft(run.id))
    } catch (err) {
      setError(messageOf(err))
    } finally {
      setBusy(null)
    }
  }

  async function approve() {
    if (busy || !artifact) return
    setBusy('approve')
    setError(null)
    try {
      onRun(await approveDraft(run.id, draft!.version, artifact.id, artifact.sha256))
    } catch (err) {
      setError(messageOf(err))
    } finally {
      setBusy(null)
    }
  }

  return (
    <section aria-labelledby="export-heading" className="space-y-4" aria-busy={busy !== null}>
      <h2 id="export-heading" className="text-lg font-semibold">
        PDF
      </h2>
      {error && (
        <Alert tone="error" requestId={error.requestId}>
          {error.message}
        </Alert>
      )}
      {run.approval && (
        <Alert tone="success">
          Version {run.approval.draft_version} is approved.{' '}
          <a href={approvalDownloadUrl(run.approval.id)} className="break-all underline" download>
            Download {run.approval.filename}
          </a>
        </Alert>
      )}
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={render}
          aria-disabled={busy !== null}
          className="rounded-md bg-indigo-700 px-4 py-2 font-medium text-white aria-disabled:opacity-60"
        >
          {busy === 'render' ? 'Creating PDF… this can take a minute' : current ? 'Create PDF again' : 'Create PDF'}
        </button>
        {!current && artifact && <p className="text-sm text-slate-600">The draft changed since the last PDF.</p>}
      </div>

      {artifact && current && (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,18rem)]">
          <figure className="rounded-md border border-slate-200 bg-white p-2 shadow-sm">
            <img
              src={artifactPreviewUrl(artifact.id)}
              alt="Page 1 of the resume exactly as it will be downloaded"
              className="w-full"
            />
            <figcaption className="mt-2 text-sm">
              <a href={artifactPdfUrl(artifact.id)} target="_blank" rel="noopener noreferrer" className="text-indigo-700 underline">
                Open the PDF
              </a>{' '}
              <span className="break-all text-slate-500">({artifact.filename})</span>
            </figcaption>
          </figure>
          <div className="space-y-3 text-sm">
            {artifact.passed ? (
              <p className="font-medium text-emerald-800">
                Checks passed: one page, all text selectable, nothing cut off, every line in reading order.
              </p>
            ) : (
              <div>
                <p className="font-medium text-red-800">This PDF did not pass the checks, so it can't be approved:</p>
                <ul className="mt-1 list-disc pl-5 text-red-800">
                  {artifact.checks.page_count > 1 && <li>It is {artifact.checks.page_count} pages long.</li>}
                  {artifact.checks.clipped.length > 0 && <li>Some text runs off the page.</li>}
                  {artifact.checks.missing_phrases.map((p) => (
                    <li key={p}>Missing from the PDF: “{p}”</li>
                  ))}
                  {artifact.checks.out_of_order.map((p) => (
                    <li key={p}>Out of order: “{p}”</li>
                  ))}
                </ul>
              </div>
            )}
            {artifact.layout === 'reconstructed' && (
              <p className="text-amber-800">This uses a rebuilt layout rather than your original design. Review it carefully.</p>
            )}
            {artifact.checks.notes.map((note) => (
              <p key={note} className="text-slate-700">
                {note}
              </p>
            ))}
            {artifact.checks.dropped_to_fit.length > 0 && (
              <details>
                <summary className="cursor-pointer">Removed to fit one page ({artifact.checks.dropped_to_fit.length})</summary>
                <ul className="mt-1 list-disc pl-5 text-slate-600">
                  {artifact.checks.dropped_to_fit.map((t) => (
                    <li key={t}>{t}</li>
                  ))}
                </ul>
              </details>
            )}
            {!run.approval && artifact.passed && (
              <div className="space-y-2 border-t border-slate-200 pt-3">
                <p className="text-slate-700">
                  Approving locks this exact PDF. Any later change creates a new version that needs approval again.
                </p>
                <button
                  type="button"
                  onClick={approve}
                  aria-disabled={busy !== null}
                  className="rounded-md bg-emerald-700 px-4 py-2 font-medium text-white aria-disabled:opacity-60"
                >
                  {busy === 'approve' ? 'Approving…' : 'Approve this PDF'}
                </button>
              </div>
            )}
          </div>
        </div>
      )}
    </section>
  )
}
