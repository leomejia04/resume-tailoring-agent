import { useState } from 'react'
import { runAtsCheck } from '../../api/client'
import { messageOf } from '../../api/errors'
import type { AtsComparison, AtsReport, AtsRequirement, AtsResult, RequirementStatus, RunOut } from '../../api/types'
import { Alert } from '../../components/Alert'
import { ImproveSection } from './ImproveSection'
import { failure, isActive, jobOf, progress } from './jobs'

const CATEGORY_LABELS = {
  required: 'Required qualifications',
  responsibility: 'Responsibilities',
  preferred: 'Preferred qualifications',
  terminology: 'Relevant terms',
} as const

const GROUPS: { status: RequirementStatus; title: string; hint: string }[] = [
  { status: 'direct', title: 'Shown directly', hint: 'The resume uses the posting’s own terms.' },
  { status: 'related', title: 'Related', hint: 'Similar wording, or found only in academic or personal work. Half credit.' },
  { status: 'missing', title: 'Missing', hint: 'None of the terms appear. Add them only if they are true for you.' },
  { status: 'uncertain', title: 'Needs your review', hint: 'Could not be checked automatically. Not counted as a match.' },
]

const NOT_SCORED: Record<Exclude<AtsReport['status'], 'complete'>, string> = {
  pending: 'Not checked yet',
  failed: 'Check failed',
  stale: 'Out of date',
}

// A PDF made before the latest draft change needs a new PDF, not a re-check.
function needsCheck(report: AtsReport | null | undefined): boolean {
  return report != null && report.status !== 'complete' && !(report.kind === 'final_pdf' && report.artifact_id === null)
}

function Delta({ comparison }: { comparison: AtsComparison | null }) {
  if (!comparison || comparison.delta === null) return null
  if (!comparison.compatible) return <p className="text-xs text-slate-600">{comparison.reason}</p>
  const { delta } = comparison
  const text = delta === 0 ? 'Same as your master resume' : `${delta > 0 ? '+' : '−'}${Math.abs(delta)} vs. your master resume`
  return <p className={`text-sm ${delta > 0 ? 'text-emerald-800' : delta < 0 ? 'text-red-800' : 'text-slate-600'}`}>{text}</p>
}

function ScoreCard({
  label,
  report,
  comparison,
  empty,
  working,
}: {
  label: string
  report: AtsReport | null | undefined
  comparison?: AtsComparison | null
  empty: string
  /** Set while a job is producing this report: shown instead of "not checked". */
  working?: string
}) {
  return (
    <div className="rounded-md border border-slate-200 bg-white p-4">
      <h3 className="text-sm font-medium text-slate-700">{label}</h3>
      {!report ? (
        <p className="mt-1 text-sm text-slate-600">{empty}</p>
      ) : report.status === 'complete' ? (
        report.score === null ? (
          <p className="mt-1 text-sm text-slate-600">No requirements could be scored.</p>
        ) : (
          <>
            <p className="mt-1">
              <span className="text-3xl font-semibold text-slate-900">{report.score}</span>
              <span className="text-slate-600"> / 100</span>
            </p>
            <Delta comparison={comparison ?? null} />
          </>
        )
      ) : working ? (
        <p className="mt-1 font-medium text-slate-800">{working}</p>
      ) : (
        <>
          <p className="mt-1 font-medium text-slate-800">{NOT_SCORED[report.status]}</p>
          {report.message && <p className="text-sm text-slate-600">{report.message}</p>}
        </>
      )}
    </div>
  )
}

function RequirementItem({ item }: { item: AtsRequirement }) {
  return (
    <li className="rounded-md border border-slate-200 bg-white p-3">
      <p>
        {item.text}
        {item.mandatory && <span className="ml-1 text-xs text-red-700">(must have)</span>}
        {item.scored && item.credit > 0 && item.credit < 1 && (
          <span className="ml-1 text-xs text-slate-600">({Math.round(item.credit * 100)}% credit)</span>
        )}
        {!item.scored && <span className="ml-1 text-xs text-slate-600">(not scored)</span>}
      </p>
      {(item.matched_terms.length > 0 || item.missing_terms.length > 0) && (
        <p className="mt-1 text-xs text-slate-600">
          {item.matched_terms.length > 0 && <>Found: {item.matched_terms.join(', ')}. </>}
          {item.missing_terms.length > 0 && <>Not found: {item.missing_terms.join(', ')}.</>}
        </p>
      )}
      {item.notes.map((note) => (
        <p key={note} className="mt-1 text-xs text-slate-700">
          {note}
        </p>
      ))}
      {item.matches.length > 0 && (
        <ul className="mt-2 space-y-1 border-l-2 border-slate-200 pl-3 text-xs">
          {item.matches.map((m, i) => (
            <li key={i}>
              <span className="font-medium text-slate-700">{m.where}</span>
              {m.kind === 'negated' && <span className="text-amber-800"> (negative mention)</span>}
              <span className="text-slate-600">: “{m.snippet}”</span>
            </li>
          ))}
        </ul>
      )}
    </li>
  )
}

function Details({ result, source, comparison }: { result: AtsResult; source: string; comparison: AtsComparison | null }) {
  const byId = new Map(result.requirements.map((r) => [r.id, r]))
  const gaps = result.mandatory_gaps.map((id) => byId.get(id)).filter((r): r is AtsRequirement => r !== undefined)
  const changes = comparison?.compatible
    ? [
        ...comparison.improved.map((id) => ({ id, up: true })),
        ...comparison.declined.map((id) => ({ id, up: false })),
      ].filter((c) => byId.has(c.id))
    : []
  return (
    <div className="space-y-4">
      <p className="text-sm text-slate-600">Details below are for {source}.</p>
      {gaps.length > 0 && (
        <div className="rounded-md border border-red-300 bg-red-50 p-4 text-sm text-red-900">
          <p className="font-medium">Must-have requirements this resume does not show:</p>
          <ul className="mt-1 list-disc pl-5">
            {gaps.map((r) => (
              <li key={r.id}>{r.text}</li>
            ))}
          </ul>
          <p className="mt-2">
            If you have this experience, confirm it on the Experience page so it can be used. Never add it otherwise.
          </p>
        </div>
      )}
      {result.categories.length > 0 && (
        <table className="w-full text-left text-sm">
          <caption className="sr-only">Score by category</caption>
          <thead>
            <tr className="text-slate-600">
              <th scope="col" className="py-1 font-medium">
                Category
              </th>
              <th scope="col" className="py-1 pl-3 text-right font-medium">
                Weight
              </th>
              <th scope="col" className="py-1 pl-3 text-right font-medium">
                Covered
              </th>
            </tr>
          </thead>
          <tbody>
            {result.categories.map((c) => (
              <tr key={c.category} className="border-t border-slate-200">
                <th scope="row" className="py-1 font-normal">
                  {CATEGORY_LABELS[c.category]} ({c.requirements})
                </th>
                <td className="py-1 pl-3 text-right">{Math.round(c.weight * 100)}%</td>
                <td className="py-1 pl-3 text-right">{Math.round(c.score)}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {changes.length > 0 && (
        <div className="text-sm">
          <p className="font-medium">Compared with your master resume</p>
          <ul className="list-disc pl-5">
            {changes.map((c) => (
              <li key={c.id} className={c.up ? 'text-emerald-800' : 'text-red-800'}>
                {c.up ? 'Better covered: ' : 'Less covered: '}
                {byId.get(c.id)!.text}
              </li>
            ))}
          </ul>
        </div>
      )}
      {GROUPS.map(({ status, title, hint }) => {
        const items = result.requirements.filter((r) => r.status === status)
        if (items.length === 0) return null
        return (
          <details key={status} open={status === 'missing' || status === 'uncertain'}>
            <summary className="cursor-pointer font-medium">
              {title} ({items.length})
            </summary>
            <p className="mt-1 text-xs text-slate-600">{hint}</p>
            <ul className="mt-2 space-y-2 text-sm">
              {items.map((item) => (
                <RequirementItem key={item.id} item={item} />
              ))}
            </ul>
          </details>
        )
      })}
      {result.repeated_terms.length > 0 && (
        <p className="text-sm text-slate-700">
          Mentioned many times: {result.repeated_terms.map((t) => `${t.term} (${t.mentions})`).join(', ')}. Repeating a
          term does not raise the estimate.
        </p>
      )}
    </div>
  )
}

export function AtsPanel({
  run,
  onRun,
  canImprove = true,
}: {
  run: RunOut
  onRun: (run: RunOut) => void
  /** False when no AI provider is configured (the server would refuse). */
  canImprove?: boolean
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<{ message: string; requestId: string | null } | null>(null)
  const ats = run.ats
  if (!ats) return null

  const pdfCurrent = ats.final_pdf?.status === 'complete' && ats.final_pdf.artifact_id !== null
  const shown = pdfCurrent ? ats.final_pdf : ats.draft?.status === 'complete' ? ats.draft : ats.baseline
  const shownComparison = shown === ats.final_pdf ? ats.pdf_vs_baseline : shown === ats.draft ? ats.draft_vs_baseline : null
  const source =
    shown === ats.final_pdf
      ? 'the final PDF'
      : shown === ats.draft
        ? `draft version ${ats.draft?.draft_version}`
        : 'your master resume'
  const checkJob = jobOf(run, 'ats_check')
  const checking = isActive(checkJob)
  // The analysis job scores the master right after reading the posting.
  const scoringBaseline = isActive(jobOf(run, 'analyze'))
  const recheck = !scoringBaseline && [ats.baseline, ats.draft, ats.final_pdf].some(needsCheck)
  const working = checking ? progress(checkJob, 'Checking…') : undefined
  const checkFailed = failure(checkJob, 'The check')

  async function check() {
    if (busy || checking) return
    setBusy(true)
    setError(null)
    try {
      onRun(await runAtsCheck(run.id))
    } catch (err) {
      setError(messageOf(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section aria-labelledby="ats-heading" className="space-y-4" aria-busy={busy}>
      <div>
        <h2 id="ats-heading" className="text-lg font-semibold">
          ATS match estimate
        </h2>
        <p className="text-sm text-slate-600">
          An estimate from our published rubric of how well the resume covers this posting’s requirements (required
          50%, responsibilities 25%, preferred 15%, other terms 10%). It is not any employer’s score and does not
          guarantee an interview; applicant tracking systems differ.
        </p>
      </div>
      {error && (
        <Alert tone="error" requestId={error.requestId}>
          {error.message}
        </Alert>
      )}
      <div className="grid gap-3 sm:grid-cols-3">
        <ScoreCard
          label="Your master resume"
          report={ats.baseline}
          empty="Upload a master resume to compare."
          working={scoringBaseline ? 'Checking…' : working}
        />
        <ScoreCard
          label={ats.draft ? `This draft (version ${ats.draft.draft_version})` : 'This draft'}
          report={ats.draft}
          comparison={ats.draft_vs_baseline}
          empty="Generate a draft to check it."
          working={working}
        />
        <ScoreCard
          label="Final PDF"
          report={ats.final_pdf}
          comparison={ats.pdf_vs_baseline}
          empty="Create the PDF to check exactly what you will send."
          working={working}
        />
      </div>
      {checkFailed && !checking && <Alert tone="error">{checkFailed}</Alert>}
      {(recheck || checking) && (
        <button
          type="button"
          onClick={check}
          aria-disabled={busy || checking}
          className="rounded-md border border-slate-300 px-3 py-1.5 text-sm aria-disabled:opacity-60"
        >
          {busy || checking ? progress(checkJob, 'Checking…') : 'Run the check'}
        </button>
      )}
      <ImproveSection run={run} onRun={onRun} enabled={canImprove} />
      {shown?.status === 'complete' && shown.result && (
        <Details result={shown.result} source={source} comparison={shownComparison} />
      )}
    </section>
  )
}
