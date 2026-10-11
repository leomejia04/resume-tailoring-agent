import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { describe, expect, it } from 'vitest'
import { errorResponse, jsonResponse, routeFetch } from '../test/http'
import { JobInputPage } from './JobInputPage'

const POSTING = 'Requirements:\n- SQL and Excel\nResponsibilities:\n- Build weekly reports for the team'

const PREVIEW = {
  title: null,
  company: 'Fictional Retail Co.',
  source_url: null,
  character_count: POSTING.length,
  word_count: 14,
  sections: [
    { kind: 'required', heading: 'Requirements', line: 1 },
    { kind: 'responsibilities', heading: 'Responsibilities', line: 3 },
  ],
  warnings: [{ code: 'short_description', message: 'This looks shorter than a typical posting.' }],
  saved: false,
}

const AI_DEMO = {
  provider: 'demo',
  model: 'demo-deterministic-v1',
  reasoning_effort: null,
  disclosure: 'We send the posting and confirmed facts to OpenAI.',
  user_daily_budget_usd: '0.50',
  spent_today_usd: '0',
}

type Routes = Parameters<typeof routeFetch>[0]

function setup(routes: Routes = {}) {
  return routeFetch({ 'GET /ai/status': () => jsonResponse(200, AI_DEMO), ...routes })
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/jobs/new']}>
      <Routes>
        <Route path="/jobs/new" element={<JobInputPage />} />
        <Route path="/runs/:runId" element={<h1>Run page</h1>} />
      </Routes>
    </MemoryRouter>,
  )
}

async function renderWithDescription(text = POSTING) {
  const user = userEvent.setup()
  renderPage()
  await user.click(screen.getByLabelText('Job description'))
  await user.paste(text)
  return user
}

describe('JobInputPage', () => {
  it('labels every field', () => {
    setup()
    renderPage()
    for (const label of [/Job title/, /Company/, /Link to posting/, 'Job description']) {
      expect(screen.getByLabelText(label)).toBeInTheDocument()
    }
  })

  it('validates before sending and points to the problem', async () => {
    const fetchMock = setup()
    const user = await renderWithDescription('too short')
    await user.click(screen.getByRole('button', { name: 'Check posting' }))

    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/jobs/preview'))).toBe(false)
    const field = screen.getByLabelText('Job description')
    expect(field).toHaveAttribute('aria-invalid', 'true')
    expect(field).toHaveAccessibleDescription(/at least 40 characters/)
    expect(screen.getByRole('alert')).toHaveFocus()
  })

  it('shows what the server recognized', async () => {
    setup({ 'POST /jobs/preview': () => jsonResponse(200, PREVIEW) })
    const user = await renderWithDescription()
    await user.type(screen.getByLabelText(/Company/), 'Fictional Retail Co.')
    await user.click(screen.getByRole('button', { name: 'Check posting' }))

    const heading = await screen.findByRole('heading', { name: 'What we found' })
    expect(heading).toHaveFocus()
    expect(screen.getByText('Required qualifications')).toBeInTheDocument()
    expect(screen.getByText('Responsibilities')).toBeInTheDocument()
    expect(screen.getByText('This looks shorter than a typical posting.')).toBeInTheDocument()
    expect(screen.getByText(/Demo mode/)).toBeInTheDocument()
  })

  it('disables the button while checking', async () => {
    let resolve: (response: Response) => void = () => {}
    setup({ 'POST /jobs/preview': () => new Promise<Response>((r) => (resolve = r)) })
    const user = await renderWithDescription()
    await user.click(screen.getByRole('button', { name: 'Check posting' }))

    expect(screen.getByRole('button', { name: 'Checking…' })).toBeDisabled()
    resolve(jsonResponse(200, PREVIEW))
    expect(await screen.findByRole('button', { name: 'Check posting' })).toBeEnabled()
  })

  it('maps server field errors onto the matching input', async () => {
    setup({
      'POST /jobs/preview': () =>
        errorResponse(422, 'validation_error', 'Some fields are missing or invalid.', [
          { loc: ['body', 'source_url'], message: 'links must not contain a username or password', type: 'value_error' },
        ]),
    })
    const user = await renderWithDescription()
    await user.type(screen.getByLabelText(/Link to posting/), 'https://user:pw@example.com/job')
    await user.click(screen.getByRole('button', { name: 'Check posting' }))

    expect(await screen.findByText('Links must not contain a username or password')).toBeInTheDocument()
    expect(screen.getByLabelText(/Link to posting/)).toHaveAttribute('aria-invalid', 'true')
  })

  it('shows a safe message and reference for server errors', async () => {
    setup({ 'POST /jobs/preview': () => errorResponse(500, 'internal_error', 'Something went wrong on our side.') })
    const user = await renderWithDescription()
    await user.click(screen.getByRole('button', { name: 'Check posting' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Something went wrong on our side.')
    expect(alert).toHaveTextContent('ref-500')
  })

  it('explains when the server cannot be reached', async () => {
    setup({ 'POST /jobs/preview': () => new TypeError('Failed to fetch') })
    const user = await renderWithDescription()
    await user.click(screen.getByRole('button', { name: 'Check posting' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not reach the server')
  })

  it('saves the posting and opens the tailoring run', async () => {
    const fetchMock = setup({
      'POST /jobs/preview': () => jsonResponse(200, PREVIEW),
      'POST /jobs': () => jsonResponse(201, { id: 'job-1' }),
      'POST /runs': () => jsonResponse(201, { id: 'run-1' }),
    })
    const user = await renderWithDescription()
    await user.click(screen.getByRole('button', { name: 'Check posting' }))
    await user.click(await screen.findByRole('button', { name: 'Save and start tailoring' }))

    expect(await screen.findByRole('heading', { name: 'Run page' })).toBeInTheDocument()
    const runCall = fetchMock.mock.calls.find(([url, init]) => String(url).endsWith('/runs') && init?.method === 'POST')!
    expect(JSON.parse(String(runCall[1]?.body))).toEqual({ job_id: 'job-1' })
  })

  it('does not offer tailoring when AI is not set up', async () => {
    setup({
      'GET /ai/status': () => jsonResponse(200, { ...AI_DEMO, provider: 'disabled', model: null }),
      'POST /jobs/preview': () => jsonResponse(200, PREVIEW),
    })
    const user = await renderWithDescription()
    await user.click(screen.getByRole('button', { name: 'Check posting' }))
    expect(await screen.findByText(/not set up on this server/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save and start tailoring' })).toBeDisabled()
  })
})
