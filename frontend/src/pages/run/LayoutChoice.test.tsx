import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { LayoutChoice } from './LayoutChoice'

describe('LayoutChoice', () => {
  it('defaults to the standard layout for drafts saved before the choice existed', () => {
    render(<LayoutChoice value={undefined} onChange={() => {}} />)
    expect(screen.getByRole('radio', { name: 'Standard layout (recommended)' })).toBeChecked()
  })

  it('reports a switch to the Word design and explains the PDF limit', async () => {
    const onChange = vi.fn()
    render(<LayoutChoice value="standard" onChange={onChange} />)
    const keep = screen.getByRole('radio', { name: 'Keep my Word file’s own design' })
    expect(keep).toHaveAccessibleDescription(/A PDF master always uses the standard layout/)
    await userEvent.click(keep)
    expect(onChange).toHaveBeenCalledWith('master')
  })
})
