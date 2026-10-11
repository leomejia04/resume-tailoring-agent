import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'
import type { EvidenceItem, MasterVersion } from '../api/types'
import { errorResponse, jsonResponse, routeFetch } from '../test/http'
import { ExperiencePage } from './ExperiencePage'
import { ProfilePage } from './ProfilePage'

const MASTER: MasterVersion = {
  id: 'm1',
  version_number: 1,
  file_kind: 'docx',
  size_bytes: 38_000,
  sha256: 'a'.repeat(64),
  original_filename: 'Jordan Resume.docx',
  extraction_status: 'complete',
  extraction_notes: [],
  evidence_count: 2,
  created_at: '2026-10-11T08:00:00Z',
}

function evidence(overrides: Partial<EvidenceItem>): EvidenceItem {
  return {
    id: 'e1',
    master_version_id: 'm1',
    origin: 'master',
    position: 0,
    version: 1,
    text: 'Built Python and SQL scripts that reconciled weekly sales extracts.',
    source_text: 'Built Python and SQL scripts that reconciled weekly sales extracts.',
    section: 'experience',
    entry: 'Data Analyst Intern | Fictional Analytics Co.',
    date_text: 'Jun 2025 - Aug 2025',
    metrics: [],
    experience_type: 'unclassified',
    suggested_type: 'internship',
    verification_status: 'unverified',
    change_source: 'extracted',
    updated_at: '2026-10-11T08:00:00Z',
    ...overrides,
  }
}

const PROFILE = { email: 'jordan@example.com', display_name: null, filename_prefix: null, lock_version: 1 }

function renderPage(page: React.ReactNode) {
  return render(<MemoryRouter>{page}</MemoryRouter>)
}

describe('ProfilePage', () => {
  it('shows versions and saves details with the version it loaded', async () => {
    const fetchMock = routeFetch({
      'GET /profile': () => jsonResponse(200, PROFILE),
      'GET /masters': () => jsonResponse(200, [MASTER]),
      'PUT /profile': () => jsonResponse(200, { ...PROFILE, filename_prefix: 'JordanExample', lock_version: 2 }),
    })
    renderPage(<ProfilePage />)
    expect(await screen.findByText('Version 1: Jordan Resume.docx')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Download original/ })).toHaveAttribute('href', '/api/v1/masters/m1/file')

    const user = userEvent.setup()
    await user.type(screen.getByLabelText(/Download name prefix/), 'JordanExample')
    await user.click(screen.getByRole('button', { name: 'Save details' }))
    expect(await screen.findByText('Details saved.')).toBeInTheDocument()
    const put = fetchMock.mock.calls.find(([, init]) => init?.method === 'PUT')!
    expect(JSON.parse(String(put[1]?.body))).toEqual({
      expected_version: 1,
      display_name: null,
      filename_prefix: 'JordanExample',
    })
  })

  it('rejects unsupported files before uploading', async () => {
    const fetchMock = routeFetch({
      'GET /profile': () => jsonResponse(200, PROFILE),
      'GET /masters': () => jsonResponse(200, []),
    })
    renderPage(<ProfilePage />)
    expect(await screen.findByText(/No versions yet/)).toBeInTheDocument()
    const user = userEvent.setup({ applyAccept: false })
    await user.upload(screen.getByLabelText('Master resume file'), new File(['x'], 'notes.txt', { type: 'text/plain' }))
    expect(screen.getByRole('alert')).toHaveTextContent('Choose a .docx or .pdf file.')
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'POST')).toBe(false)
  })

  it('uploads a resume and reports what was found', async () => {
    routeFetch({
      'GET /profile': () => jsonResponse(200, PROFILE),
      'GET /masters': [() => jsonResponse(200, []), () => jsonResponse(200, [MASTER])],
      'POST /masters': () => jsonResponse(201, { master: { ...MASTER, evidence_count: 11 }, duplicate: false }),
    })
    renderPage(<ProfilePage />)
    const user = userEvent.setup()
    await user.upload(await screen.findByLabelText('Master resume file'), new File(['pk'], 'resume.docx'))
    await user.click(screen.getByRole('button', { name: 'Upload' }))
    expect(await screen.findByText(/Version 1 saved. We found 11 facts/)).toBeInTheDocument()
    expect(await screen.findByText('Version 1: Jordan Resume.docx')).toBeInTheDocument()
  })
})

describe('ExperiencePage', () => {
  it('groups facts by section and confirms with the chosen kind', async () => {
    const skills = evidence({
      id: 'e2',
      position: 1,
      section: 'skills',
      entry: null,
      date_text: null,
      text: 'Technical: SQL, Python',
      source_text: 'Technical: SQL, Python',
      suggested_type: 'unclassified',
    })
    const fetchMock = routeFetch({
      'GET /masters': () => jsonResponse(200, [MASTER]),
      'GET /evidence': () => jsonResponse(200, [evidence({}), skills]),
      'POST /evidence/e1/versions': () =>
        jsonResponse(201, evidence({ version: 2, experience_type: 'internship', verification_status: 'verified' })),
    })
    renderPage(<ExperiencePage />)
    expect(await screen.findByRole('heading', { name: 'Experience', level: 2 })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Skills', level: 2 })).toBeInTheDocument()
    expect(screen.getByText('2 facts: 0 confirmed, 2 need review, 0 rejected.')).toBeInTheDocument()

    const card = screen.getByText(/reconciled weekly sales extracts/).closest('li')!
    expect(within(card).getByLabelText('Kind of experience')).toHaveValue('internship')
    const confirm = within(card).getByRole('button', { name: 'Confirm' })
    await userEvent.setup().click(confirm)
    expect(await within(card).findByText('Confirmed')).toBeInTheDocument()
    // The card stays mounted, so keyboard focus is not lost after saving.
    expect(within(card).getByRole('button', { name: 'Update' })).toHaveFocus()

    const post = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST')!
    expect(JSON.parse(String(post[1]?.body))).toEqual({
      expected_version: 1,
      experience_type: 'internship',
      verification_status: 'verified',
    })
  })

  it('reloads when the item changed in another tab', async () => {
    const fetchMock = routeFetch({
      'GET /masters': () => jsonResponse(200, [MASTER]),
      'GET /evidence': () => jsonResponse(200, [evidence({})]),
      'POST /evidence/e1/versions': () =>
        errorResponse(409, 'conflict', 'This item changed in another tab. Reload to see the latest version.'),
    })
    renderPage(<ExperiencePage />)
    await userEvent.setup().click(await screen.findByRole('button', { name: 'Reject' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('changed in another tab')
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/api/v1/evidence')).length).toBe(2)
  })

  it('points new users to the upload step', async () => {
    routeFetch({ 'GET /masters': () => jsonResponse(200, []) })
    renderPage(<ExperiencePage />)
    expect(await screen.findByRole('link', { name: 'Upload your master resume' })).toHaveAttribute('href', '/profile')
  })
})
