import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'
import type { AtsOverview, AtsReport, AtsResult, RunOut } from '../../api/types'
import { jsonResponse, routeFetch } from '../../test/http'
import { AtsPanel } from './AtsPanel'

const RESULT: AtsResult = {
  rubric_version: 'ats-rubric/v1',
  analysis_fingerprint: 'f'.repeat(64),
  score: 64,
  categories: [
    { category: 'required', requirements: 2, weight: 0.5556, score: 50 },
    { category: 'responsibility', requirements: 1, weight: 0.2778, score: 100 },
    { category: 'preferred', requirements: 1, weight: 0.1667, score: 50 },
  ],
  requirements: [
    {
      id: 'R1',
      merged_ids: [],
      category: 'required',
      text: 'Experience with SQL and Python',
      mandatory: true,
      scored: true,
      status: 'direct',
      credit: 1,
      matched_terms: ['SQL', 'Python'],
      missing_terms: [],
      excluded_terms: [],
      notes: [],
      matches: [
        {
          kind: 'direct',
          basis: 'exact_term',
          where: 'Data Analyst Intern · bullet 1 (PDF)',
          snippet: 'Built Python and SQL scripts',
          term: 'SQL',
        },
      ],
    },
    {
      id: 'R2',
      merged_ids: [],
      category: 'required',
      text: 'Tableau dashboards for store managers',
      mandatory: true,
      scored: true,
      status: 'missing',
      credit: 0,
      matched_terms: [],
      missing_terms: ['Tableau'],
      excluded_terms: [],
      notes: [],
      matches: [],
    },
    {
      id: 'R3',
      merged_ids: [],
      category: 'preferred',
      text: 'Snowflake or another cloud warehouse',
      mandatory: false,
      scored: true,
      status: 'related',
      credit: 0.5,
      matched_terms: [],
      missing_terms: ['Snowflake'],
      excluded_terms: [],
      notes: ['Asks for professional experience; found only in academic or personal work.'],
      matches: [],
    },
  ],
  mandatory_gaps: ['R2'],
  mandatory_partial: [],
  counts: { direct: 1, related: 1, missing: 1, uncertain: 0 },
  repeated_terms: [{ term: 'SQL', mentions: 5 }],
}

function report(kind: AtsReport['kind'], status: AtsReport['status'], score: number | null, extra: Partial<AtsReport> = {}): AtsReport {
  return {
    kind,
    status,
    score: status === 'complete' ? score : null,
    result: status === 'complete' ? { ...RESULT, score } : null,
    rubric_version: 'ats-rubric/v1',
    draft_version: kind === 'master_baseline' ? null : 2,
    artifact_id: kind === 'final_pdf' ? 'art-1' : null,
    master_version_id: kind === 'master_baseline' ? 'm-1' : null,
    checked_at: '2026-10-11T09:00:00Z',
    message: null,
    ...extra,
  }
}

function overview(parts: Partial<AtsOverview>): AtsOverview {
  return {
    rubric_version: 'ats-rubric/v1',
    baseline: report('master_baseline', 'complete', 50),
    draft: report('draft', 'complete', 60),
    final_pdf: null,
    draft_vs_baseline: { compatible: true, reason: null, baseline: 50, tailored: 60, delta: 10, improved: ['R3'], declined: [] },
    pdf_vs_baseline: null,
    ...parts,
  }
}

function makeRun(ats: AtsOverview): RunOut {
  return {
    id: 'run-1',
    status: 'awaiting_review',
    error_code: null,
    lock_version: 3,
    job: { id: 'job-1', title: 'Data Analyst', company: 'Fictional Retail', source_url: null, version: 1, created_at: '' },
    master_version_id: null,
    analysis: null,
    analysis_model: null,
    questions: [],
    draft: null,
    ats,
    usage: { calls: 1, cost_usd: '0', input_tokens: 0, output_tokens: 0 },
    created_at: '',
    updated_at: '',
  }
}

function Harness({ initial }: { initial: RunOut }) {
  const [run, setRun] = useState(initial)
  return <AtsPanel run={run} onRun={setRun} />
}

describe('AtsPanel', () => {
  it('shows the estimate with its disclaimer, the master comparison, and evidence per requirement', () => {
    const pdf = report('final_pdf', 'complete', 64)
    render(
      <AtsPanel
        run={makeRun(overview({ final_pdf: pdf, pdf_vs_baseline: { ...overview({}).draft_vs_baseline!, tailored: 64, delta: 14 } }))}
        onRun={() => undefined}
      />,
    )
    expect(screen.getByRole('heading', { name: 'ATS match estimate' })).toBeInTheDocument()
    expect(screen.getByText(/not any employer’s score and does not guarantee an interview/)).toBeInTheDocument()

    const pdfCard = screen.getByRole('heading', { name: 'Final PDF' }).parentElement!
    expect(within(pdfCard).getByText('64')).toBeInTheDocument()
    expect(within(pdfCard).getByText('+14 vs. your master resume')).toBeInTheDocument()
    expect(screen.getByText('Details below are for the final PDF.')).toBeInTheDocument()

    // Mandatory gaps are shown separately from the number, with a truthfulness reminder.
    const gaps = screen.getByText('Must-have requirements this resume does not show:').parentElement!
    expect(within(gaps).getByText('Tableau dashboards for store managers')).toBeInTheDocument()
    expect(within(gaps).getByText(/Never add it otherwise/)).toBeInTheDocument()

    expect(screen.getByRole('row', { name: /Required qualifications \(2\) 56% 50%/ })).toBeInTheDocument()
    expect(screen.getByText('Better covered: Snowflake or another cloud warehouse')).toBeInTheDocument()
    expect(screen.getByText('Shown directly (1)')).toBeInTheDocument()
    expect(screen.getByText('Data Analyst Intern · bullet 1 (PDF)')).toBeInTheDocument()
    expect(screen.getByText('(50% credit)')).toBeInTheDocument()
    expect(screen.getByText('Asks for professional experience; found only in academic or personal work.')).toBeInTheDocument()
    expect(screen.getByText(/SQL \(5\)\. Repeating a term does not raise the estimate\./)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Run the check' })).not.toBeInTheDocument()
  })

  it('never shows a number for pending or failed checks, and re-runs them on request', async () => {
    const fetchMock = routeFetch({
      'POST /runs/run-1/ats/check': () => jsonResponse(200, makeRun(overview({}))),
    })
    const initial = overview({
      baseline: report('master_baseline', 'failed', null, { message: 'Your file could not be read right now.' }),
      draft: report('draft', 'pending', null, { checked_at: null }),
      draft_vs_baseline: null,
    })
    render(<Harness initial={makeRun(initial)} />)
    expect(screen.getByText('Check failed')).toBeInTheDocument()
    expect(screen.getByText('Your file could not be read right now.')).toBeInTheDocument()
    expect(screen.getByText('Not checked yet')).toBeInTheDocument()
    expect(screen.queryByText('/ 100')).not.toBeInTheDocument()
    expect(screen.queryByText(/Details below/)).not.toBeInTheDocument()

    await userEvent.setup().click(screen.getByRole('button', { name: 'Run the check' }))
    expect(await screen.findByText('+10 vs. your master resume')).toBeInTheDocument()
    expect(screen.getByText('Details below are for draft version 2.')).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('asks for a new PDF, not a re-check, when the draft changed after the last PDF', () => {
    const stalePdf = report('final_pdf', 'stale', null, {
      artifact_id: null,
      draft_version: 1,
      rubric_version: null,
      message: 'The draft changed after the last PDF. Create the PDF again to check it.',
    })
    render(<AtsPanel run={makeRun(overview({ final_pdf: stalePdf }))} onRun={() => undefined} />)
    const pdfCard = screen.getByRole('heading', { name: 'Final PDF' }).parentElement!
    expect(within(pdfCard).getByText('Out of date')).toBeInTheDocument()
    expect(within(pdfCard).getByText(/Create the PDF again/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Run the check' })).not.toBeInTheDocument()
    expect(screen.getByText('Details below are for draft version 2.')).toBeInTheDocument()
  })
})
