import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'
import type { ArtifactOut, RunOut } from '../../api/types'
import { errorResponse, jsonResponse, routeFetch } from '../../test/http'
import { DraftEditor } from './DraftEditor'
import { ExportPanel } from './ExportPanel'
import { FormatPanel } from './RevisePanel'
import { VersionHistory } from './VersionHistory'

const FORMAT = {
  max_bullets_per_entry: 4,
  project_bullets: 3,
  bold_project_technologies: true,
  languages_on_own_line: true,
  show_headline: false,
  locked_entries: [],
}

function makeRun(overrides: Partial<RunOut> = {}, version = 1): RunOut {
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
    draft: {
      version,
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
            bullets: [
              { text: 'Built SQL scripts.', evidence_ids: ['e1'], requirement_ids: [] },
              { text: 'Documented 40 report fields.', evidence_ids: ['e2'], requirement_ids: [] },
            ],
          },
        ],
        skills: [],
        omitted: [{ evidence_id: 'e3', reason: 'not_selected' }],
        format: FORMAT,
      },
      validation: { rule_version: 'v1', issues: [], removed: [], repaired: false, passed: true },
      created_by: 'generation',
      model: 'demo-deterministic-v1',
      prompt_version: 'tailor/v1',
      created_at: '',
      sources: {
        e1: { text: 'Built SQL scripts.', entry: null, experience_type: 'internship' },
        e2: { text: 'Documented 40 report fields.', entry: null, experience_type: 'internship' },
        e3: { text: 'Resolved access requests.', entry: null, experience_type: 'professional' },
      },
    },
    artifact: null,
    approval: null,
    usage: { calls: 1, cost_usd: '0', input_tokens: 0, output_tokens: 0 },
    created_at: '',
    updated_at: '',
    ...overrides,
  }
}

const ARTIFACT: ArtifactOut = {
  id: 'art-1',
  draft_version: 1,
  sha256: 'a'.repeat(64),
  page_count: 1,
  layout: 'template',
  passed: true,
  checks: {
    page_count: 1,
    missing_phrases: [],
    out_of_order: [],
    clipped: [],
    dropped_to_fit: ['Wrote step-by-step guides.'],
    capped_by_preference: [],
    notes: [],
    searchable: true,
  },
  filename: 'JordanExample_Data_Analyst_Fictional_Retail_CV.pdf',
  created_at: '',
}

function Harness({ initial, children }: { initial: RunOut; children: (run: RunOut, set: (r: RunOut) => void) => React.ReactNode }) {
  const [run, setRun] = useState(initial)
  return <>{children(run, setRun)}</>
}

describe('ExportPanel', () => {
  it('creates the PDF, shows the real page, approves the exact bytes, and offers the named download', async () => {
    const fetchMock = routeFetch({
      'POST /runs/run-1/render': () => jsonResponse(201, makeRun({ artifact: ARTIFACT })),
      'POST /runs/run-1/approve': () =>
        jsonResponse(
          200,
          makeRun({
            status: 'approved',
            artifact: ARTIFACT,
            approval: { id: 'ap-1', draft_version: 1, artifact_id: 'art-1', filename: ARTIFACT.filename, approved_at: '' },
          }),
        ),
    })
    render(<Harness initial={makeRun()}>{(run, set) => <ExportPanel run={run} onRun={set} />}</Harness>)
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'Create PDF' }))

    const image = await screen.findByRole('img', { name: /exactly as it will be downloaded/ })
    expect(image).toHaveAttribute('src', '/api/v1/artifacts/art-1/preview.png')
    expect(screen.getByRole('link', { name: 'Open the PDF' })).toHaveAttribute('href', '/api/v1/artifacts/art-1/pdf')
    expect(screen.getByText(/Checks passed/)).toBeInTheDocument()
    expect(screen.getByText('Removed to fit one page (1)')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Approve this PDF' }))
    const download = await screen.findByRole('link', { name: `Download ${ARTIFACT.filename}` })
    expect(download).toHaveAttribute('href', '/api/v1/approvals/ap-1/download')
    const approveCall = fetchMock.mock.calls.find(([url]) => String(url).endsWith('/approve'))!
    expect(JSON.parse(String(approveCall[1]?.body))).toEqual({
      draft_version: 1,
      artifact_id: 'art-1',
      artifact_sha256: ARTIFACT.sha256,
    })
  })

  it('refuses approval for a PDF that failed its checks', () => {
    const failed = { ...ARTIFACT, passed: false, checks: { ...ARTIFACT.checks, page_count: 2 } }
    render(<ExportPanel run={makeRun({ artifact: failed })} onRun={() => undefined} />)
    expect(screen.getByText(/did not pass the checks/)).toBeInTheDocument()
    expect(screen.getByText('It is 2 pages long.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Approve this PDF' })).not.toBeInTheDocument()
  })

  it('notices when the PDF is older than the draft', () => {
    render(<ExportPanel run={makeRun({ artifact: ARTIFACT }, 2)} onRun={() => undefined} />)
    expect(screen.getByText('The draft changed since the last PDF.')).toBeInTheDocument()
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
  })
})

describe('DraftEditor', () => {
  it('shows why an unsupported edit was rejected', async () => {
    routeFetch({
      'POST /runs/run-1/draft/edits': () =>
        errorResponse(422, 'unsupported_edit', 'Some changes are not supported by your confirmed facts.', [
          { loc: ['body', 'operations.1'], message: "States '40%', which the cited facts do not contain.", type: 'unsupported_number' },
        ]),
    })
    const run = makeRun()
    render(<DraftEditor run={run} draft={run.draft!} onRun={() => undefined} canRegenerate={false} />)
    const user = userEvent.setup()
    const item = screen.getByText('Built SQL scripts.', { selector: 'span' }).closest('li')!
    await user.click(within(item).getByRole('button', { name: 'Edit' }))
    await user.clear(within(item).getByLabelText('Bullet text'))
    await user.type(within(item).getByLabelText('Bullet text'), 'Built SQL scripts that cut costs 40%.')
    await user.click(within(item).getByRole('button', { name: 'Save' }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('not supported by your confirmed facts')
    expect(alert).toHaveTextContent("States '40%'")
    expect(within(item).getByLabelText('Bullet text')).toBeInTheDocument() // still editing
  })

  it('adds a left-out fact and reorders bullets with single operations', async () => {
    const fetchMock = routeFetch({ 'POST /runs/run-1/draft/edits': () => jsonResponse(200, makeRun({}, 2)) })
    const run = makeRun()
    render(<DraftEditor run={run} draft={run.draft!} onRun={() => undefined} canRegenerate={false} />)
    const user = userEvent.setup()
    await user.click(screen.getByText('Facts left out (1)'))
    await user.click(screen.getByRole('button', { name: 'Add: Resolved access requests.' }))
    await user.click(screen.getAllByRole('button', { name: 'Move down' })[0]!)
    const bodies = fetchMock.mock.calls.map(([, init]) => JSON.parse(String(init?.body)))
    expect(bodies[0]).toEqual({ expected_version: 1, operations: [{ op: 'add_fact', evidence_id: 'e3' }] })
    expect(bodies[1].operations).toEqual([{ op: 'move_bullet', entry_ref: 'J1', index: 0, to: 1 }])
  })
})

describe('VersionHistory', () => {
  it('compares an older version and restores it as a new version', async () => {
    const older = makeRun({}, 1).draft!
    const olderContent = { ...older.content, entries: [{ ...older.content.entries[0]!, bullets: [older.content.entries[0]!.bullets[0]!] }] }
    const fetchMock = routeFetch({
      'GET /runs/run-1/draft/versions': () =>
        jsonResponse(200, [
          { version: 2, created_by: 'edit', created_at: '2026-10-11T09:00:00Z', passed: true, model: null },
          { version: 1, created_by: 'generation', created_at: '2026-10-11T08:00:00Z', passed: true, model: 'demo' },
        ]),
      'GET /runs/run-1/draft/versions/1': () =>
        jsonResponse(200, { version: 1, created_by: 'generation', created_at: '', passed: true, model: 'demo', content: olderContent, validation: older.validation }),
      'POST /runs/run-1/draft/restore': () => jsonResponse(200, makeRun({}, 3)),
    })
    render(<VersionHistory run={makeRun({}, 2)} onRun={() => undefined} />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Compare with current' }))
    expect(await screen.findByText('Only in the current version:')).toBeInTheDocument()
    expect(screen.getByText('Documented 40 report fields.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Restore version 1 as a new version' }))
    const restore = fetchMock.mock.calls.find(([url]) => String(url).endsWith('/restore'))!
    expect(JSON.parse(String(restore[1]?.body))).toEqual({ version: 1, expected_version: 2 })
  })
})

describe('FormatPanel', () => {
  it('remembers preferences only when asked', async () => {
    const fetchMock = routeFetch({
      'POST /runs/run-1/draft/edits': () => jsonResponse(200, makeRun({}, 2)),
      'GET /profile/preferences': () => jsonResponse(200, { preferences: FORMAT, lock_version: 4 }),
      'PUT /profile/preferences': () => jsonResponse(200, { preferences: { ...FORMAT, project_bullets: 2 }, lock_version: 5 }),
    })
    render(<FormatPanel run={makeRun()} onRun={() => undefined} />)
    const user = userEvent.setup()
    await user.click(screen.getByText('Formatting'))
    await user.clear(screen.getByLabelText('Most bullets per project'))
    await user.type(screen.getByLabelText('Most bullets per project'), '2')
    await user.click(screen.getByRole('button', { name: 'Apply' }))
    expect(await screen.findByText('Applied to this resume.')).toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'PUT')).toBe(false)

    await user.click(screen.getByLabelText('Remember these settings for future resumes'))
    await user.click(screen.getByRole('button', { name: 'Apply' }))
    expect(await screen.findByText('Applied and remembered for future resumes.')).toBeInTheDocument()
    const put = fetchMock.mock.calls.find(([, init]) => init?.method === 'PUT')!
    expect(JSON.parse(String(put[1]?.body))).toEqual({ preferences: { ...FORMAT, project_bullets: 2 }, expected_version: 4 })
  })
})
