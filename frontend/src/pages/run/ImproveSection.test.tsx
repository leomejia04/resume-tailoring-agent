import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'
import type { ImprovementAttempt, RunJob, RunOut } from '../../api/types'
import { jsonResponse, routeFetch } from '../../test/http'
import { ImproveSection } from './ImproveSection'

const SAVED: ImprovementAttempt = {
  attempt: 1,
  status: 'saved',
  outcome: 'improved',
  base_version: 1,
  new_version: 2,
  targets: ['R1'],
  score_before: 52,
  score_after: 68,
  created_at: '',
  finished_at: '',
}

function improveJob(overrides: Partial<RunJob> = {}): RunJob {
  return {
    id: 'job-i',
    kind: 'improve',
    status: 'succeeded',
    attempts: 1,
    max_attempts: 3,
    error_code: null,
    result: { stopped: 'exhausted', saved_versions: [2], version: 2 },
    created_at: '',
    finished_at: '',
    ...overrides,
  }
}

function makeRun(overrides: Partial<RunOut> = {}): RunOut {
  return {
    id: 'run-1',
    status: 'awaiting_review',
    error_code: null,
    lock_version: 3,
    job: { id: 'job-1', title: 'Data Analyst', company: 'Fictional Retail', source_url: null, version: 1, created_at: '' },
    master_version_id: null,
    analysis: {
      role_title: 'Data Analyst',
      seniority: null,
      requirements: [{ id: 'R1', category: 'required', text: 'Experience with SQL and Python', terms: ['SQL'], mandatory: true }],
    },
    analysis_model: 'demo-deterministic-v1',
    questions: [],
    draft: {
      version: 2,
      content: { headline: 'Data Analyst', summary: null, entries: [], skills: [], omitted: [] },
      validation: { rule_version: 'draft-rules/v1', issues: [], removed: [], repaired: false, passed: true },
      created_by: 'improve',
      model: 'demo-deterministic-v1',
      prompt_version: 'improve/v1',
      created_at: '',
      sources: {},
    } as unknown as RunOut['draft'],
    jobs: [improveJob()],
    improvements: [SAVED],
    improvements_left: 1,
    usage: { calls: 3, cost_usd: '0', input_tokens: 0, output_tokens: 0 },
    created_at: '',
    updated_at: '',
    ...overrides,
  }
}

function Harness({ initial }: { initial: RunOut }) {
  const [run, setRun] = useState(initial)
  return <ImproveSection run={run} onRun={setRun} enabled />
}

describe('ImproveSection', () => {
  it('explains each attempt and why it stopped, and lets the user try again', async () => {
    const fetchMock = routeFetch({
      'POST /runs/run-1/draft/improve': () =>
        jsonResponse(202, makeRun({ jobs: [improveJob({ status: 'queued', attempts: 0, result: null })] })),
    })
    render(<Harness initial={makeRun()} />)
    expect(
      screen.getByText(/Attempt 1: raised the estimate from 52 to 68 \(version 2\)/),
    ).toHaveTextContent('worked on Experience with SQL and Python')
    expect(screen.getByText(/remaining gaps are not covered by your confirmed facts/)).toBeInTheDocument()
    expect(screen.getByText('1 of 2 attempts left for this run.')).toBeInTheDocument()

    await userEvent.setup().click(screen.getByRole('button', { name: 'Try improving again' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Waiting to start…')
    expect(screen.queryByRole('button', { name: 'Try improving again' })).not.toBeInTheDocument()
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST')!
    expect(JSON.parse(String(post[1]?.body))).toEqual({ expected_version: 2 })
  })

  it('offers no retry once both attempts are used, and shows rejected attempts plainly', () => {
    const rejected: ImprovementAttempt = {
      ...SAVED,
      attempt: 2,
      status: 'rejected',
      outcome: 'unsupported_changes',
      base_version: 2,
      new_version: null,
      score_after: null,
    }
    render(
      <Harness
        initial={makeRun({
          improvements: [SAVED, rejected],
          improvements_left: 0,
          jobs: [improveJob({ result: { stopped: 'unsupported_changes', version: 2 } })],
        })}
      />,
    )
    expect(screen.getByText(/Attempt 2: not saved: it included unsupported claims/)).toBeInTheDocument()
    expect(screen.getByText(/suggested claims your confirmed facts do not support/)).toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('drops a stop reason that was about an older version', () => {
    render(<Harness initial={makeRun({ jobs: [improveJob({ result: { stopped: 'exhausted', version: 1 } })] })} />)
    expect(screen.queryByText(/remaining gaps are not covered/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Try improving again' })).toBeInTheDocument()
  })

  it('shows a short failure sentence when the job failed', () => {
    render(
      <Harness
        initial={makeRun({
          improvements: [],
          improvements_left: 2,
          jobs: [improveJob({ status: 'failed', error_code: 'budget_exceeded', result: null })],
        })}
      />,
    )
    expect(screen.getByText(/daily AI spending limit was reached/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Try improving again' })).toBeInTheDocument()
  })
})
