import { useId, useState } from 'react'
import { ApiError, editDraft, regenerateEntry } from '../../api/client'
import { messageOf } from '../../api/errors'
import type { DraftOut, EditOperation, RunOut } from '../../api/types'
import { Alert } from '../../components/Alert'

interface Props {
  run: RunOut
  draft: DraftOut
  onRun: (run: RunOut) => void
  canRegenerate: boolean
}

interface Problem {
  message: string
  requestId: string | null
  details: string[]
}

function BulletRow({
  text,
  sources,
  onSave,
  onRemove,
  onMove,
  canUp,
  canDown,
  busy,
}: {
  text: string
  sources: string[]
  onSave: (text: string) => Promise<boolean>
  onRemove: () => void
  onMove: (delta: number) => void
  canUp: boolean
  canDown: boolean
  busy: boolean
}) {
  const id = useId()
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState(text)

  if (editing) {
    return (
      <li className="space-y-2">
        <label htmlFor={id} className="sr-only">
          Bullet text
        </label>
        <textarea
          id={id}
          rows={3}
          maxLength={350}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          className="block w-full rounded-md border border-slate-300 px-3 py-2"
        />
        <div className="flex gap-2">
          <button
            type="button"
            aria-disabled={busy}
            onClick={async () => {
              if (!busy && (await onSave(value.trim()))) setEditing(false)
            }}
            className="rounded-md bg-indigo-700 px-3 py-1 text-sm text-white aria-disabled:opacity-60"
          >
            Save
          </button>
          <button
            type="button"
            onClick={() => {
              setEditing(false)
              setValue(text)
            }}
            className="rounded-md border border-slate-300 px-3 py-1 text-sm"
          >
            Cancel
          </button>
        </div>
      </li>
    )
  }
  return (
    <li>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <span className="min-w-0 flex-1">{text}</span>
        <span className="flex gap-1 text-xs">
          <button type="button" aria-disabled={busy} onClick={() => !busy && setEditing(true)} className="rounded border border-slate-300 px-2 py-0.5">
            Edit
          </button>
          <button
            type="button"
            aria-disabled={busy || !canUp}
            onClick={() => !busy && canUp && onMove(-1)}
            className="rounded border border-slate-300 px-2 py-0.5 aria-disabled:opacity-50"
          >
            <span aria-hidden="true">↑</span>
            <span className="sr-only">Move up</span>
          </button>
          <button
            type="button"
            aria-disabled={busy || !canDown}
            onClick={() => !busy && canDown && onMove(1)}
            className="rounded border border-slate-300 px-2 py-0.5 aria-disabled:opacity-50"
          >
            <span aria-hidden="true">↓</span>
            <span className="sr-only">Move down</span>
          </button>
          <button type="button" aria-disabled={busy} onClick={() => !busy && onRemove()} className="rounded border border-slate-300 px-2 py-0.5">
            Remove
          </button>
        </span>
      </div>
      <details className="text-sm text-slate-600">
        <summary className="cursor-pointer">Based on</summary>
        <ul className="mt-1 list-[circle] pl-5">
          {sources.map((s, i) => (
            <li key={i}>{s}</li>
          ))}
        </ul>
      </details>
    </li>
  )
}

export function DraftEditor({ run, draft, onRun, canRegenerate }: Props) {
  const [busy, setBusy] = useState(false)
  const [problem, setProblem] = useState<Problem | null>(null)
  const [headline, setHeadline] = useState<string | null>(null)
  const headlineId = useId()
  const { content, validation } = draft

  async function apply(operations: EditOperation[]): Promise<boolean> {
    if (busy) return false
    setBusy(true)
    setProblem(null)
    try {
      onRun(await editDraft(run.id, draft.version, operations))
      return true
    } catch (err) {
      setProblem({
        ...messageOf(err),
        details: err instanceof ApiError ? err.fields.map((f) => f.message) : [],
      })
      return false
    } finally {
      setBusy(false)
    }
  }

  async function regenerate(ref: string) {
    if (busy) return
    setBusy(true)
    setProblem(null)
    try {
      onRun(await regenerateEntry(run.id, ref, draft.version))
    } catch (err) {
      setProblem({ ...messageOf(err), details: [] })
    } finally {
      setBusy(false)
    }
  }

  const sourceText = (id: string) => draft.sources[id]?.text ?? 'A confirmed fact'
  const omitted = content.omitted.filter((o) => draft.sources[o.evidence_id])

  return (
    <div className="space-y-4" aria-busy={busy}>
      {problem && (
        <Alert tone="error" requestId={problem.requestId}>
          {problem.message}
          {problem.details.length > 0 && (
            <ul className="mt-1 list-disc pl-5 font-normal">
              {problem.details.map((d, i) => (
                <li key={i}>{d}</li>
              ))}
            </ul>
          )}
        </Alert>
      )}
      <div className="rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
        {headline === null ? (
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-xl font-semibold">{content.headline}</p>
            <button type="button" onClick={() => setHeadline(content.headline)} className="text-sm text-indigo-700 underline">
              Edit headline
            </button>
          </div>
        ) : (
          <form
            className="flex flex-wrap items-end gap-2"
            onSubmit={async (e) => {
              e.preventDefault()
              if (await apply([{ op: 'update_headline', text: headline.trim() }])) setHeadline(null)
            }}
          >
            <div>
              <label htmlFor={headlineId} className="block text-sm font-medium">
                Headline
              </label>
              <input
                id={headlineId}
                value={headline}
                maxLength={120}
                onChange={(e) => setHeadline(e.target.value)}
                className="mt-1 rounded-md border border-slate-300 px-3 py-1.5"
              />
            </div>
            <button type="submit" aria-disabled={busy} className="rounded-md bg-indigo-700 px-3 py-1.5 text-sm text-white aria-disabled:opacity-60">
              Save
            </button>
            <button type="button" onClick={() => setHeadline(null)} className="rounded-md border border-slate-300 px-3 py-1.5 text-sm">
              Cancel
            </button>
          </form>
        )}
        {content.summary && <p className="mt-2 text-slate-700">{content.summary}</p>}

        {content.entries.map((entry) => (
          <section key={entry.ref} className="mt-5" aria-label={entry.entry ?? 'Entry'}>
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <h3 className="font-semibold">{entry.entry}</h3>
              <span className="flex items-center gap-3">
                {entry.date_text && <span className="text-sm text-slate-600">{entry.date_text}</span>}
                {canRegenerate && (
                  <button
                    type="button"
                    aria-disabled={busy}
                    onClick={() => regenerate(entry.ref)}
                    className="text-sm text-indigo-700 underline aria-disabled:opacity-60"
                  >
                    Rewrite this section
                  </button>
                )}
              </span>
            </div>
            <ul className="mt-2 list-disc space-y-2 pl-5">
              {entry.bullets.map((bullet, index) => (
                <BulletRow
                  key={`${draft.version}-${entry.ref}-${index}`}
                  text={bullet.text}
                  sources={bullet.evidence_ids.map(sourceText)}
                  busy={busy}
                  canUp={index > 0}
                  canDown={index < entry.bullets.length - 1}
                  onSave={(text) => apply([{ op: 'update_bullet', entry_ref: entry.ref, index, text }])}
                  onRemove={() => apply([{ op: 'remove_bullet', entry_ref: entry.ref, index }])}
                  onMove={(delta) => apply([{ op: 'move_bullet', entry_ref: entry.ref, index, to: index + delta }])}
                />
              ))}
            </ul>
          </section>
        ))}
        {content.skills.length > 0 && (
          <section className="mt-5" aria-label="Skills">
            <h3 className="font-semibold">Skills</h3>
            {content.skills.map((skill, index) => (
              <p key={index} className="flex flex-wrap justify-between gap-2">
                <span>{skill.text}</span>
                <button type="button" aria-disabled={busy} onClick={() => apply([{ op: 'remove_skill', index }])} className="text-xs underline">
                  Remove
                </button>
              </p>
            ))}
          </section>
        )}
      </div>

      <div className="rounded-lg border border-slate-200 bg-white p-4 text-sm">
        <p className="font-medium">
          {validation.passed ? 'Every statement is backed by your confirmed facts.' : 'Some statements were removed.'}
        </p>
        {validation.repaired && <p className="text-slate-600">The first draft was corrected once automatically.</p>}
        {validation.removed.length > 0 && (
          <>
            <p className="mt-2 text-slate-700">Removed because they were not supported by your facts:</p>
            <ul className="list-disc pl-5 text-slate-600">
              {validation.removed.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </>
        )}
      </div>

      {omitted.length > 0 && (
        <details className="rounded-lg border border-slate-200 bg-white p-4 text-sm">
          <summary className="cursor-pointer font-medium">Facts left out ({omitted.length})</summary>
          <p className="mt-1 text-slate-600">Your experience bank keeps all of them. Add any back to this resume:</p>
          <ul className="mt-2 space-y-2">
            {omitted.map((o) => (
              <li key={o.evidence_id} className="flex flex-wrap items-start justify-between gap-2">
                <span className="min-w-0 flex-1">{sourceText(o.evidence_id)}</span>
                <button
                  type="button"
                  aria-disabled={busy}
                  onClick={() => apply([{ op: 'add_fact', evidence_id: o.evidence_id }])}
                  className="rounded border border-slate-300 px-2 py-0.5 text-xs"
                >
                  Add<span className="sr-only">: {sourceText(o.evidence_id)}</span>
                </button>
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}
