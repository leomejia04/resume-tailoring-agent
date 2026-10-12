import { useId } from 'react'
import type { FormatPreferences } from '../../api/types'

type Layout = FormatPreferences['layout']

const OPTIONS: { value: Layout; label: string; hint: string }[] = [
  {
    value: 'standard',
    label: 'Standard layout (recommended)',
    hint: 'One clean, single-column format that applicant tracking systems read reliably.',
  },
  {
    value: 'master',
    label: 'Keep my Word file’s own design',
    hint: 'Only for .docx masters. A PDF master always uses the standard layout.',
  },
]

/** Standard layout (ADR 0011) or the Word master's own design. */
export function LayoutChoice({ value, onChange }: { value: Layout | undefined; onChange: (layout: Layout) => void }) {
  const id = useId()
  const current = value ?? 'standard'
  return (
    <fieldset className="text-sm">
      <legend className="font-medium">Layout</legend>
      {OPTIONS.map((option) => (
        <div key={option.value} className="mt-1">
          <label className="flex items-center gap-2">
            <input
              type="radio"
              name={id}
              value={option.value}
              checked={current === option.value}
              onChange={() => onChange(option.value)}
              aria-describedby={`${id}-${option.value}`}
            />
            {option.label}
          </label>
          <p id={`${id}-${option.value}`} className="ml-6 text-slate-600">
            {option.hint}
          </p>
        </div>
      ))}
    </fieldset>
  )
}
