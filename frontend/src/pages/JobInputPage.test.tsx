import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { jsonResponse, mockFetch } from '../test/http'
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

async function renderWithDescription(text = POSTING) {
  const user = userEvent.setup()
  render(<JobInputPage />)
  await user.click(screen.getByLabelText('Job description'))
  await user.paste(text)
  return user
}

describe('JobInputPage', () => {
  it('labels every field', () => {
    render(<JobInputPage />)
    for (const label of [/Job title/, /Company/, /Link to posting/, 'Job description']) {
      expect(screen.getByLabelText(label)).toBeInTheDocument()
    }
  })

  it('validates before sending and points to the problem', async () => {
    const fetchMock = mockFetch()
    const user = await renderWithDescription('too short')
    await user.click(screen.getByRole('button', { name: 'Check posting' }))

    expect(fetchMock).not.toHaveBeenCalled()
    const field = screen.getByLabelText('Job description')
    expect(field).toHaveAttribute('aria-invalid', 'true')
    expect(field).toHaveAccessibleDescription(/at least 40 characters/)
    expect(screen.getByRole('alert')).toHaveFocus()
  })

  it('shows what the server recognized', async () => {
    mockFetch(jsonResponse(200, PREVIEW))
    const user = await renderWithDescription()
    await user.type(screen.getByLabelText(/Company/), 'Fictional Retail Co.')
    await user.click(screen.getByRole('button', { name: 'Check posting' }))

    const heading = await screen.findByRole('heading', { name: 'What we found' })
    expect(heading).toHaveFocus()
    expect(screen.getByText('Required qualifications')).toBeInTheDocument()
    expect(screen.getByText('Responsibilities')).toBeInTheDocument()
    expect(screen.getByText('This looks shorter than a typical posting.')).toBeInTheDocument()
    expect(screen.getByText(/not saved/)).toBeInTheDocument()
  })

  it('disables the button while checking', async () => {
    let resolve: (response: Response) => void = () => {}
    mockFetch(new Promise<Response>((r) => (resolve = r)))
    const user = await renderWithDescription()
    await user.click(screen.getByRole('button', { name: 'Check posting' }))

    expect(screen.getByRole('button', { name: 'Checking…' })).toBeDisabled()
    resolve(jsonResponse(200, PREVIEW))
    expect(await screen.findByRole('button', { name: 'Check posting' })).toBeEnabled()
  })

  it('maps server field errors onto the matching input', async () => {
    mockFetch(
      jsonResponse(422, {
        error: {
          code: 'validation_error',
          message: 'Some fields are missing or invalid.',
          request_id: 'r1',
          fields: [
            {
              loc: ['body', 'source_url'],
              message: 'links must not contain a username or password',
              type: 'value_error',
            },
          ],
        },
      }),
    )
    const user = await renderWithDescription()
    await user.type(screen.getByLabelText(/Link to posting/), 'https://user:pw@example.com/job')
    await user.click(screen.getByRole('button', { name: 'Check posting' }))

    const link = screen.getByLabelText(/Link to posting/)
    expect(await screen.findByText('Links must not contain a username or password')).toBeInTheDocument()
    expect(link).toHaveAttribute('aria-invalid', 'true')
  })

  it('shows a safe message and reference for server errors', async () => {
    mockFetch(
      jsonResponse(500, {
        error: { code: 'internal_error', message: 'Something went wrong on our side.', request_id: 'ref-500', fields: [] },
      }),
    )
    const user = await renderWithDescription()
    await user.click(screen.getByRole('button', { name: 'Check posting' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Something went wrong on our side.')
    expect(alert).toHaveTextContent('ref-500')
  })

  it('explains when the server cannot be reached', async () => {
    mockFetch(new TypeError('Failed to fetch'))
    const user = await renderWithDescription()
    await user.click(screen.getByRole('button', { name: 'Check posting' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not reach the server')
  })
})
