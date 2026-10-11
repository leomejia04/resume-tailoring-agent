import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { describe, expect, it } from 'vitest'
import type { RunJob, RunOut } from '../api/types'
import { errorResponse, jsonResponse, routeFetch } from '../test/http'
import { RunPage } from './RunPage'

const AI = {
  provider: 'demo',
  model: 'demo-deterministic-v1',
  reasoning_effort: null,
  disclosure: 'We send the posting text and confirmed facts.',
  user_daily_budget_usd: '0.50',
  spent_today_usd: '0',
}

const EVIDENCE = [
  {
    id: 'ev-intern',
    master_version_id: 'm1',
    origin: 'master',
    position: 0,
    version: 2,
    text: 'Built SQL scripts.',
    source_text: 'Built SQL scripts.',
    section: 'experience',
    entry: 'Data Analyst Intern | Fictional Co.',
    date_text: 'Jun 2025 - Aug 2025',
    metrics: [],
    experience_type: 'internship',
    suggested_type: 'internship',
    verification_status: 'verified',
    change_source: 'extracted',
    updated_at: '2026-10-11T08:00:00Z',
  },
]

function makeRun(overrides: Partial<RunOut> = {}): RunOut {
  return {
    id: 'run-1',
    status: 'awaiting_confirmation',
    error_code: null,
    lock_version: 2,
    job: { id: 'job-1', title: 'Data Analyst', company: 'Fictional Retail', source_url: null, version: 1, created_at: '' },
    master_version_id: null,
    analysis: {
      role_title: 'Data Analyst',
      seniority: null,
      requirements: [
        { id: 'R1', category: 'required', text: 'SQL and Python', terms: ['SQL', 'Python'], mandatory: true },
        { id: 'R2', category: 'required', text: 'Tableau dashboards', terms: ['Tableau'], mandatory: true },
      ],
    },
    analysis_model: 'demo-deterministic-v1',
    questions: [
      {
        id: 'q1',
        requirement_id: 'R2',
        requirement_text: 'Tableau dashboards',
        term: 'Tableau',
        question: 'The posting asks for Tableau. Have you used Tableau? If so, where, and what did you do with it?',
        status: 'open',
        evidence_id: null,
      },
    ],
    draft: null,
    usage: { calls: 1, cost_usd: '0', input_tokens: 0, output_tokens: 0 },
    created_at: '',
    updated_at: '',
    ...overrides,
  }
}

const DRAFT = {
  version: 1,
  content: {
    headline: 'Data Analyst',
    summary: null,
    entries: [
      {
        ref: 'J1',
        section: 'experience',
        entry: 'Data Analyst Intern | Fictional Co.',
        date_text: 'Jun 2025 - Aug 2025',
        experience_type: 'internship',
        bullets: [{ text: 'Automated reporting with SQL.', evidence_ids: ['ev-intern'], requirement_ids: ['R1'] }],
      },
    ],
    skills: [],
    omitted: [{ evidence_id: 'x', reason: 'not_selected' }],
  },
  validation: {
    rule_version: 'draft-rules/v1',
    issues: [],
    removed: ['J1 bullet 2: Built Tableau dashboards that cut costs by 35%'],
    repaired: true,
    passed: false,
  },
  created_by: 'repair',
  model: 'demo-deterministic-v1',
  prompt_version: 'tailor/v1',
  created_at: '',
  sources: { 'ev-intern': { text: 'Built SQL scripts.', entry: 'Data Analyst Intern | Fictional Co.', experience_type: 'internship' } },
}

function renderRun(routes: Parameters<typeof routeFetch>[0]) {
  const fetchMock = routeFetch({
    'GET /ai/status': () => jsonResponse(200, AI),
    'GET /evidence': () => jsonResponse(200, EVIDENCE),
    ...routes,
  })
  render(
    <MemoryRouter initialEntries={['/runs/run-1']}>
      <Routes>
        <Route path="/runs/:runId" element={<RunPage />} />
      </Routes>
    </MemoryRouter>,
  )
  return fetchMock
}

describe('RunPage', () => {
  it('shows requirements, the open question, and that demo mode is not AI', async () => {
    renderRun({ 'GET /runs/run-1': () => jsonResponse(200, makeRun()) })
    expect(await screen.findByRole('heading', { name: 'Data Analyst at Fictional Retail' })).toBeInTheDocument()
    expect(screen.getByText('Questions for you')).toBeInTheDocument()
    expect(screen.getByText('Tableau dashboards')).toBeInTheDocument()
    expect(screen.getAllByText('(must have)')).toHaveLength(2)
    expect(screen.getByText(/not an AI model/)).toBeInTheDocument()
  })

  it('requires a real description before a yes becomes a fact', async () => {
    const fetchMock = renderRun({
      'GET /runs/run-1': [
        () => jsonResponse(200, makeRun()),
        () => jsonResponse(200, makeRun({ status: 'ready', questions: [{ ...makeRun().questions[0]!, status: 'confirmed' }] })),
      ],
      'POST /runs/run-1/questions/q1': () => jsonResponse(200, { ...makeRun().questions[0], status: 'confirmed' }),
    })
    const user = userEvent.setup()
    const question = (await screen.findByRole('group', { name: /Have you used Tableau/ })).closest('li')!
    await user.click(within(question).getByLabelText('Yes, I have'))
    await user.selectOptions(within(question).getByLabelText('Kind of experience'), 'internship')
    await user.selectOptions(within(question).getByLabelText(/Where/), 'ev-intern')
    await user.type(within(question).getByLabelText(/What did you do/), 'Made some charts once.')
    await user.click(within(question).getByRole('button', { name: 'Save answer' }))
    expect(within(question).getByRole('alert')).toHaveTextContent('mentioning it by name')
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'POST')).toBe(false)

    await user.clear(within(question).getByLabelText(/What did you do/))
    await user.type(within(question).getByLabelText(/What did you do/), 'Built two Tableau dashboards for sales.')
    await user.click(within(question).getByRole('button', { name: 'Save answer' }))
    expect(await screen.findByText('Added to your facts')).toBeInTheDocument()
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST')!
    expect(JSON.parse(String(post[1]?.body))).toEqual({
      answer: 'yes',
      experience_type: 'internship',
      entry_evidence_id: 'ev-intern',
      description: 'Built two Tableau dashboards for sales.',
    })
  })

  it('queues a draft, polls while it runs, then shows sources and what was removed', async () => {
    const job: RunJob = {
      id: 'job-d',
      kind: 'draft',
      status: 'queued',
      attempts: 0,
      max_attempts: 3,
      error_code: null,
      result: null,
      created_at: '',
      finished_at: null,
    }
    const fetchMock = renderRun({
      'GET /runs/run-1': [
        () => jsonResponse(200, makeRun({ status: 'ready', questions: [] })),
        () => jsonResponse(200, makeRun({ status: 'drafting', questions: [], jobs: [{ ...job, status: 'running', attempts: 1 }] })),
        () =>
          jsonResponse(
            200,
            makeRun({
              status: 'awaiting_review',
              questions: [],
              draft: DRAFT as RunOut['draft'],
              jobs: [{ ...job, status: 'succeeded', attempts: 1 }],
            }),
          ),
      ],
      'POST /runs/run-1/draft': () => jsonResponse(202, makeRun({ status: 'ready', questions: [], jobs: [job] })),
    })
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Generate draft' }))
    expect(await screen.findByRole('button', { name: 'Waiting to start…' })).toHaveAttribute('aria-disabled', 'true')
    expect(await screen.findByRole('button', { name: /Drafting…/ }, { timeout: 4000 })).toBeInTheDocument()
    const heading = await screen.findByRole('heading', { name: 'Draft (version 1)' }, { timeout: 4000 })
    await screen.findByText('Automated reporting with SQL.')
    expect(heading).toHaveFocus() // focus follows the finished job, not the request
    await new Promise((resolve) => setTimeout(resolve, 1700))
    const reads = fetchMock.mock.calls.filter(([url, init]) => String(url).endsWith('/runs/run-1') && (init?.method ?? 'GET') === 'GET')
    expect(reads).toHaveLength(3) // polling stopped once nothing was active
    expect(screen.getByText('Built SQL scripts.')).toBeInTheDocument() // the cited source fact
    expect(screen.getByText('Some statements were removed.')).toBeInTheDocument()
    expect(screen.getByText(/cut costs by 35%/)).toBeInTheDocument()
    expect(screen.getByText(/Produced by demo rules, not AI/)).toBeInTheDocument()
  }, 15_000)

  it('explains failures and spending limits plainly', async () => {
    renderRun({
      'GET /runs/run-1': () => jsonResponse(200, makeRun({ status: 'failed', error_code: 'draft_timeout', questions: [] })),
      'POST /runs/run-1/draft': () => errorResponse(429, 'budget_exceeded', 'This would exceed your daily AI spending limit.'),
    })
    expect(await screen.findByText(/took too long/)).toBeInTheDocument()
    await userEvent.setup().click(screen.getByRole('button', { name: 'Generate draft' }))
    expect(await screen.findByText(/daily AI spending limit/)).toBeInTheDocument()
  })

  it('shows not found for runs that are not yours', async () => {
    renderRun({ 'GET /runs/run-1': () => errorResponse(404, 'not_found', 'That tailoring run was not found.') })
    expect(await screen.findByRole('alert')).toHaveTextContent('not found')
  })
})
