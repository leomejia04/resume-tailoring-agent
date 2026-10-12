import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ContextNote } from './ContextNote'

describe('ContextNote', () => {
  it('says every fact was read for full context', () => {
    render(<ContextNote context={{ mode: 'full', reason: 'small_profile', facts_selected: 18, facts_total: 18, draft_version: 1 }} />)
    expect(screen.getByText(/the AI read all 18 confirmed facts\./)).toBeInTheDocument()
  })

  it('explains a relevance selection without jargon', () => {
    render(<ContextNote context={{ mode: 'hybrid', reason: 'relevance', facts_selected: 30, facts_total: 120, draft_version: 2 }} />)
    const note = screen.getByText(/read 30 of your 120 confirmed facts/)
    expect(note).toHaveTextContent('most relevant to this posting')
    expect(note).toHaveTextContent('Your experience bank is unchanged')
    expect(note.textContent).not.toMatch(/hybrid|retrieval|embedding/i)
  })

  it('renders nothing for older drafts without context', () => {
    const { container } = render(<ContextNote context={null} />)
    expect(container).toBeEmptyDOMElement()
  })
})
