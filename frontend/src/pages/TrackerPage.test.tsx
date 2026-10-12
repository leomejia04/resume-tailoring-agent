import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router'
import { describe, expect, it } from 'vitest'
import type { ApplicationOut, ApprovalOut } from '../api/types'
import { errorResponse, jsonResponse, routeFetch } from '../test/http'
import { TrackerPage } from './TrackerPage'
import { todayIso } from './tracker/format'

const APPROVAL: ApprovalOut = {
  id: 'ap-1',
  run_id: 'run-1',
  job_title: 'Data Analyst',
  company: 'Fictional Retail Co.',
  source_url: 'https://jobs.example.com/fictional/1',
  filename: 'JordanExample_Data_Analyst_Fictional_Retail_Co_CV.pdf',
  draft_version: 3,
  artifact_sha256: 'a'.repeat(64),
  approved_at: '2026-10-09T12:00:00Z',
}

function entry(overrides: Partial<ApplicationOut> = {}): ApplicationOut {
  return {
    id: 'a1',
    company: 'Fictional Retail Co.',
    role: 'Data Analyst',
    job_url: 'https://jobs.example.com/fictional/1',
    applied_on: '2026-10-01',
    status: 'applied',
    notes: 'Recruiter screen\nnext week',
    resume: {
      approval_id: 'ap-1',
      run_id: 'run-1',
      filename: APPROVAL.filename,
      draft_version: 3,
      approved_at: APPROVAL.approved_at,
    },
    lock_version: 2,
    created_at: '2026-10-01T09:00:00Z',
    updated_at: '2026-10-02T09:00:00Z',
    ...overrides,
  }
}

const OTHER = entry({
  id: 'a2',
  company: 'Another Fictional Co.',
  role: 'GRC Analyst',
  job_url: null,
  applied_on: null,
  status: 'interviewing',
  notes: null,
  resume: null,
  lock_version: 1,
})

function Location() {
  const location = useLocation()
  return <p data-testid="location">{location.pathname + location.search}</p>
}

function renderPage(path = '/tracker') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="tracker" element={<TrackerPage />} />
      </Routes>
      <Location />
    </MemoryRouter>,
  )
}

const bodyOf = (fetchMock: ReturnType<typeof routeFetch>, method: string) =>
  JSON.parse(String(fetchMock.mock.calls.find(([, init]) => init?.method === method)![1]?.body))

describe('TrackerPage', () => {
  it('starts empty and adds an application with today as the date applied', async () => {
    const created = entry({ resume: null, job_url: null, notes: null, applied_on: todayIso(), lock_version: 1 })
    const fetchMock = routeFetch({
      'GET /applications': [() => jsonResponse(200, []), () => jsonResponse(200, [created])],
      'GET /approvals': () => jsonResponse(200, []),
      'POST /applications': () => jsonResponse(201, created),
    })
    renderPage()
    expect(await screen.findByText(/No applications yet/)).toBeInTheDocument()

    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'Add application' }))
    expect(screen.getByRole('heading', { name: 'Add an application' })).toHaveFocus()
    expect(screen.getByLabelText(/Date applied/)).toHaveValue(todayIso())
    await user.type(screen.getByLabelText('Company'), '  Fictional Retail Co. ')
    await user.type(screen.getByLabelText('Role'), 'Data Analyst')
    await user.click(screen.getByRole('button', { name: 'Save application' }))

    expect(await screen.findByText('Application added.')).toBeInTheDocument()
    expect(bodyOf(fetchMock, 'POST')).toEqual({
      company: 'Fictional Retail Co.',
      role: 'Data Analyst',
      job_url: null,
      approval_id: null,
      applied_on: todayIso(),
      status: 'applied',
      notes: null,
    })
    expect(await screen.findByRole('heading', { name: 'Data Analyst at Fictional Retail Co.' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Add an application' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add application' })).toHaveFocus()
  })

  it('checks required fields and links before sending, and shows server field errors', async () => {
    const fetchMock = routeFetch({
      'GET /applications': () => jsonResponse(200, []),
      'GET /approvals': () => jsonResponse(200, [APPROVAL]),
      'POST /applications': () =>
        errorResponse(422, 'validation_error', 'Some fields are missing or invalid.', [
          { loc: ['body', 'approval_id'], message: 'Choose one of your approved resumes.', type: 'approval_not_found' },
        ]),
    })
    renderPage()
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Add application' }))
    await user.type(screen.getByLabelText(/Link to the posting/), 'jobs.example.com/1')
    await user.click(screen.getByRole('button', { name: 'Save application' }))

    const summary = screen.getByRole('alert')
    expect(summary).toHaveFocus()
    expect(within(summary).getByRole('link', { name: 'Company: Enter the company name.' })).toBeInTheDocument()
    expect(within(summary).getByRole('link', { name: /Link to the posting: Enter a full web address/ })).toBeInTheDocument()
    expect(screen.getByLabelText('Company')).toHaveAttribute('aria-invalid', 'true')
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'POST')).toBe(false)

    await user.clear(screen.getByLabelText(/Link to the posting/))
    await user.type(screen.getByLabelText('Company'), 'Fictional Co.')
    await user.type(screen.getByLabelText('Role'), 'Analyst')
    await user.click(screen.getByRole('button', { name: 'Save application' }))
    expect(await screen.findByText('Choose one of your approved resumes.', { selector: 'p' })).toBeInTheDocument()
    expect(screen.getByLabelText(/Resume you sent/)).toHaveAttribute('aria-invalid', 'true')
  })

  it('opens a form prefilled from an approved resume link', async () => {
    const fetchMock = routeFetch({
      'GET /applications': () => jsonResponse(200, []),
      'GET /approvals': () => jsonResponse(200, [APPROVAL]),
      'POST /applications': () => jsonResponse(201, entry()),
    })
    renderPage('/tracker?approval=ap-1')
    expect(await screen.findByRole('heading', { name: 'Add an application' })).toBeInTheDocument()
    expect(screen.getByTestId('location')).toHaveTextContent('/tracker?approval=ap-1')
    expect(screen.getByLabelText('Company')).toHaveValue('Fictional Retail Co.')
    expect(screen.getByLabelText('Role')).toHaveValue('Data Analyst')
    expect(screen.getByLabelText(/Link to the posting/)).toHaveValue(APPROVAL.source_url)
    expect(screen.getByLabelText(/Resume you sent/)).toHaveValue('ap-1')

    expect(screen.queryByRole('button', { name: 'Add application' })).not.toBeInTheDocument()

    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'Save application' }))
    expect(await screen.findByText('Application added.')).toBeInTheDocument()
    expect(bodyOf(fetchMock, 'POST')).toMatchObject({ approval_id: 'ap-1', job_url: APPROVAL.source_url })
    expect(screen.getByTestId('location')).toHaveTextContent(/^\/tracker$/)
    // Loaded once: removing the link's parameter does not reload the page or reopen the form.
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/approvals'))).toHaveLength(1)
  })

  it('shows each entry with its posting, resume, notes, and only safe links', async () => {
    routeFetch({
      'GET /applications': () =>
        jsonResponse(200, [entry(), entry({ id: 'a3', role: 'Odd Link', job_url: 'javascript:alert(1)', resume: null })]),
      'GET /approvals': () => jsonResponse(200, []),
    })
    renderPage()
    const item = (await screen.findByRole('heading', { name: 'Data Analyst at Fictional Retail Co.' })).closest('li')!
    const scope = within(item)
    expect(scope.getByText(`Applied on ${new Date(2026, 9, 1).toLocaleDateString()}`)).toBeInTheDocument()
    const posting = scope.getByRole('link', { name: /Job posting/ })
    expect(posting).toHaveAttribute('href', 'https://jobs.example.com/fictional/1')
    expect(posting).toHaveAttribute('target', '_blank')
    expect(posting).toHaveAttribute('rel', 'noopener noreferrer')
    expect(scope.getByRole('link', { name: `Download ${APPROVAL.filename}` })).toHaveAttribute(
      'href',
      '/api/v1/approvals/ap-1/download',
    )
    expect(scope.getByRole('link', { name: /Open review/ })).toHaveAttribute('href', '/runs/run-1')
    expect(scope.getByText(/Recruiter screen/)).toHaveClass('whitespace-pre-wrap')

    const odd = within(screen.getByRole('heading', { name: 'Odd Link at Fictional Retail Co.' }).closest('li')!)
    expect(odd.queryByRole('link', { name: /Job posting/ })).not.toBeInTheDocument()
  })

  it('filters by status and changes a status with the version it loaded', async () => {
    const fetchMock = routeFetch({
      'GET /applications': () => jsonResponse(200, [entry(), OTHER]),
      'GET /approvals': () => jsonResponse(200, []),
      'PUT /applications/a1': () => jsonResponse(200, entry({ status: 'offer', lock_version: 3 })),
    })
    renderPage()
    const user = userEvent.setup()
    await screen.findByRole('heading', { name: 'GRC Analyst at Another Fictional Co.' })
    await user.selectOptions(screen.getByLabelText('Show'), 'interviewing')
    expect(screen.queryByRole('heading', { name: 'Data Analyst at Fictional Retail Co.' })).not.toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Interviewing (1)' })).toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText('Show'), '')

    await user.selectOptions(screen.getByLabelText('Status for Data Analyst at Fictional Retail Co.'), 'offer')
    expect(await screen.findByText('Status changed to Offer.')).toBeInTheDocument()
    expect(bodyOf(fetchMock, 'PUT')).toEqual({
      company: 'Fictional Retail Co.',
      role: 'Data Analyst',
      job_url: 'https://jobs.example.com/fictional/1',
      approval_id: 'ap-1',
      applied_on: '2026-10-01',
      status: 'offer',
      notes: 'Recruiter screen\nnext week',
      expected_version: 2,
    })
  })

  it('does not overwrite a newer version from another tab', async () => {
    const fetchMock = routeFetch({
      'GET /applications': [() => jsonResponse(200, [entry()]), () => jsonResponse(200, [entry({ lock_version: 3 })])],
      'GET /approvals': () => jsonResponse(200, [APPROVAL]),
      'PUT /applications/a1': () =>
        errorResponse(409, 'conflict', 'This application changed in another tab. Reload to see the latest.'),
    })
    renderPage()
    const user = userEvent.setup()
    await screen.findByRole('heading', { name: 'Data Analyst at Fictional Retail Co.' })
    await user.click(screen.getByRole('button', { name: /Edit Data Analyst/ }))
    expect(screen.getByLabelText(/Notes/)).toHaveValue('Recruiter screen\nnext week')
    await user.type(screen.getByLabelText(/Notes/), ' - offer call')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))

    expect(await screen.findByText(/changed in another tab; the list below shows the latest version\. Your changes were not saved/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save changes' })).not.toBeInTheDocument()
    expect(bodyOf(fetchMock, 'PUT').expected_version).toBe(2)
    expect(fetchMock.mock.calls.filter(([url, init]) => String(url).endsWith('/applications') && init?.method === 'GET')).toHaveLength(2)
    await user.click(screen.getByRole('button', { name: 'Close' }))
    expect(screen.getByRole('button', { name: 'Add application' })).toHaveFocus()
  })

  it('asks before deleting and sends the version it loaded', async () => {
    const fetchMock = routeFetch({
      'GET /applications': () => jsonResponse(200, [entry(), OTHER]),
      'GET /approvals': () => jsonResponse(200, []),
      'DELETE /applications/a1': () => new Response(null, { status: 204 }),
    })
    renderPage()
    const user = userEvent.setup()
    await screen.findByRole('heading', { name: 'Data Analyst at Fictional Retail Co.' })
    await user.click(screen.getByRole('button', { name: /Delete Data Analyst/ }))
    expect(screen.getByRole('button', { name: 'Keep' })).toHaveFocus()
    await user.click(screen.getByRole('button', { name: 'Keep' }))
    expect(screen.queryByRole('group', { name: 'Confirm delete' })).not.toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'DELETE')).toBe(false)

    await user.click(screen.getByRole('button', { name: /Delete Data Analyst/ }))
    await user.click(screen.getByRole('button', { name: 'Delete permanently' }))
    expect(await screen.findByText('Data Analyst at Fictional Retail Co. was deleted.')).toBeInTheDocument()
    const call = fetchMock.mock.calls.find(([, init]) => init?.method === 'DELETE')!
    expect(String(call[0])).toBe('/api/v1/applications/a1?expected_version=2')
    expect(screen.queryByRole('heading', { name: 'Data Analyst at Fictional Retail Co.' })).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'GRC Analyst at Another Fictional Co.' })).toBeInTheDocument()
  })

  it('shows a load failure with its reference', async () => {
    routeFetch({
      'GET /applications': () => errorResponse(503, 'service_unavailable', 'Storage is temporarily unavailable.'),
      'GET /approvals': () => jsonResponse(200, []),
    })
    renderPage()
    expect(await screen.findByText('Storage is temporarily unavailable.')).toBeInTheDocument()
    expect(screen.getByText('ref-503')).toBeInTheDocument()
  })
})
