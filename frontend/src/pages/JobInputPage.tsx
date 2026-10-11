import { useEffect, useId, useRef, useState, type FormEvent } from 'react'
import { ApiError, previewJobInput } from '../api/client'
import { JOB_LIMITS, type JobInputPreview, type JobInputRequest, type SectionKind } from '../api/types'

type FieldName = 'title' | 'company' | 'source_url' | 'description'
type FieldErrors = Partial<Record<FieldName, string>>
type FormValues = Record<FieldName, string>

const FIELD_ORDER: FieldName[] = ['title', 'company', 'source_url', 'description']
const FIELD_LABELS: Record<FieldName, string> = {
  title: 'Job title',
  company: 'Company',
  source_url: 'Link to posting',
  description: 'Job description',
}
const SECTION_LABELS: Record<SectionKind, string> = {
  required: 'Required qualifications',
  preferred: 'Preferred qualifications',
  responsibilities: 'Responsibilities',
  about_role: 'About the role',
  about_company: 'About the company',
  benefits: 'Benefits',
}
const EMPTY: FormValues = { title: '', company: '', source_url: '', description: '' }

function validate(values: FormValues): FieldErrors {
  const errors: FieldErrors = {}
  const description = values.description.trim()
  if (!description) {
    errors.description = 'Paste the job description.'
  } else if (description.length < JOB_LIMITS.descriptionMin) {
    errors.description = `Paste the full job description (at least ${JOB_LIMITS.descriptionMin} characters).`
  }
  if (values.source_url.trim()) {
    try {
      const url = new URL(values.source_url.trim())
      if (url.protocol !== 'https:' && url.protocol !== 'http:') throw new Error('scheme')
    } catch {
      errors.source_url = 'Enter a full web address starting with https://'
    }
  }
  return errors
}

function toRequest(values: FormValues): JobInputRequest {
  const optional = (value: string) => value.trim() || undefined
  return {
    title: optional(values.title),
    company: optional(values.company),
    source_url: optional(values.source_url),
    description: values.description,
  }
}

function fieldErrorsFrom(error: ApiError): FieldErrors {
  const errors: FieldErrors = {}
  for (const field of error.fields) {
    const name = field.loc[1]
    if (typeof name === 'string' && (FIELD_ORDER as string[]).includes(name)) {
      const message = field.message
      errors[name as FieldName] = message.charAt(0).toUpperCase() + message.slice(1)
    }
  }
  return errors
}

export function JobInputPage() {
  const formId = useId()
  const [values, setValues] = useState<FormValues>(EMPTY)
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({})
  const [formError, setFormError] = useState<{ message: string; requestId: string | null } | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [result, setResult] = useState<JobInputPreview | null>(null)
  const errorSummaryRef = useRef<HTMLDivElement>(null)
  const resultHeadingRef = useRef<HTMLHeadingElement>(null)
  const requestRef = useRef<AbortController | null>(null)

  useEffect(() => () => requestRef.current?.abort(), [])

  useEffect(() => {
    if (result) resultHeadingRef.current?.focus()
  }, [result])

  useEffect(() => {
    if (formError || Object.keys(fieldErrors).length > 0) errorSummaryRef.current?.focus()
  }, [formError, fieldErrors])

  const fieldId = (name: FieldName) => `${formId}-${name}`
  const errorId = (name: FieldName) => `${formId}-${name}-error`

  function update(name: FieldName, value: string) {
    setValues((current) => ({ ...current, [name]: value }))
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setFormError(null)
    setResult(null)
    const errors = validate(values)
    setFieldErrors(errors)
    if (Object.keys(errors).length > 0) return

    requestRef.current?.abort()
    const controller = new AbortController()
    requestRef.current = controller
    setSubmitting(true)
    try {
      setResult(await previewJobInput(toRequest(values), controller.signal))
    } catch (error) {
      if (controller.signal.aborted) return
      if (error instanceof ApiError) {
        const serverFieldErrors = fieldErrorsFrom(error)
        if (Object.keys(serverFieldErrors).length > 0) {
          setFieldErrors(serverFieldErrors)
        } else {
          setFormError({ message: error.message, requestId: error.requestId })
        }
      } else {
        setFormError({ message: 'Something unexpected happened. Please try again.', requestId: null })
      }
    } finally {
      if (requestRef.current === controller) setSubmitting(false)
    }
  }

  const errorEntries = FIELD_ORDER.filter((name) => fieldErrors[name])
  const inputClass = (name: FieldName) =>
    `mt-1 block w-full rounded-md border bg-white px-3 py-2 text-slate-900 shadow-sm ${
      fieldErrors[name] ? 'border-red-600' : 'border-slate-300'
    }`

  function renderInput(name: Exclude<FieldName, 'description'>, maxLength?: number, type = 'text') {
    return (
      <div>
        <label htmlFor={fieldId(name)} className="block text-sm font-medium text-slate-800">
          {FIELD_LABELS[name]} <span className="font-normal text-slate-500">(optional)</span>
        </label>
        <input
          id={fieldId(name)}
          name={name}
          type={type}
          value={values[name]}
          maxLength={maxLength}
          inputMode={type === 'url' ? 'url' : undefined}
          aria-invalid={fieldErrors[name] ? true : undefined}
          aria-describedby={fieldErrors[name] ? errorId(name) : undefined}
          onChange={(event) => update(name, event.target.value)}
          className={inputClass(name)}
        />
        {fieldErrors[name] && (
          <p id={errorId(name)} className="mt-1 text-sm text-red-700">
            {fieldErrors[name]}
          </p>
        )}
      </div>
    )
  }

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-semibold text-slate-900">Add a job posting</h1>
        <p className="mt-2 max-w-2xl text-slate-600">
          Paste the full posting. We will check what was included before you tailor a resume.
        </p>
      </div>

      {(formError || errorEntries.length > 0) && (
        <div
          ref={errorSummaryRef}
          tabIndex={-1}
          role="alert"
          className="rounded-md border border-red-300 bg-red-50 p-4 text-red-900"
        >
          {formError ? (
            <>
              <p className="font-medium">{formError.message}</p>
              {formError.requestId && (
                <p className="mt-1 text-sm">
                  If this keeps happening, share this reference: <code>{formError.requestId}</code>
                </p>
              )}
            </>
          ) : (
            <>
              <p className="font-medium">Please fix the following:</p>
              <ul className="mt-2 list-disc pl-5 text-sm">
                {errorEntries.map((name) => (
                  <li key={name}>
                    <a href={`#${fieldId(name)}`} className="underline">
                      {FIELD_LABELS[name]}: {fieldErrors[name]}
                    </a>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}

      <form noValidate onSubmit={handleSubmit} className="space-y-5 rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
        <div className="grid gap-5 sm:grid-cols-2">
          {renderInput('title', JOB_LIMITS.titleMax)}
          {renderInput('company', JOB_LIMITS.companyMax)}
        </div>
        {renderInput('source_url', undefined, 'url')}
        <div>
          <label htmlFor={fieldId('description')} className="block text-sm font-medium text-slate-800">
            {FIELD_LABELS.description}
          </label>
          <p id={`${formId}-description-hint`} className="text-sm text-slate-500">
            Include requirements and responsibilities. Opening links is not available yet.
          </p>
          <textarea
            id={fieldId('description')}
            name="description"
            required
            rows={14}
            value={values.description}
            maxLength={JOB_LIMITS.descriptionMax}
            aria-invalid={fieldErrors.description ? true : undefined}
            aria-describedby={[`${formId}-description-hint`, fieldErrors.description ? errorId('description') : '']
              .filter(Boolean)
              .join(' ')}
            onChange={(event) => update('description', event.target.value)}
            className={inputClass('description')}
          />
          {fieldErrors.description && (
            <p id={errorId('description')} className="mt-1 text-sm text-red-700">
              {fieldErrors.description}
            </p>
          )}
          <p className="mt-1 text-right text-xs text-slate-500">
            {values.description.length.toLocaleString()} / {JOB_LIMITS.descriptionMax.toLocaleString()} characters
          </p>
        </div>
        <div className="flex items-center gap-4">
          <button
            type="submit"
            disabled={submitting}
            className="rounded-md bg-indigo-700 px-4 py-2 font-medium text-white hover:bg-indigo-800 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {submitting ? 'Checking…' : 'Check posting'}
          </button>
          <p aria-live="polite" className="text-sm text-slate-600">
            {submitting ? 'Checking the posting…' : ''}
          </p>
        </div>
      </form>

      {result && (
        <section aria-labelledby={`${formId}-result`} className="rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
          <h2 id={`${formId}-result`} ref={resultHeadingRef} tabIndex={-1} className="text-xl font-semibold focus:outline-none">
            What we found
          </h2>
          <p className="mt-1 text-sm text-slate-600">This check is not saved. Saving postings comes with accounts.</p>
          <dl className="mt-4 grid grid-cols-2 gap-4 sm:max-w-sm">
            <div>
              <dt className="text-sm text-slate-500">Words</dt>
              <dd className="text-lg font-medium">{result.word_count.toLocaleString()}</dd>
            </div>
            <div>
              <dt className="text-sm text-slate-500">Characters</dt>
              <dd className="text-lg font-medium">{result.character_count.toLocaleString()}</dd>
            </div>
          </dl>
          <h3 className="mt-6 font-medium">Sections recognized</h3>
          {result.sections.length > 0 ? (
            <ul className="mt-2 space-y-1">
              {result.sections.map((section) => (
                <li key={`${section.kind}-${section.line}`}>
                  <span className="font-medium">{SECTION_LABELS[section.kind]}</span>{' '}
                  <span className="text-slate-600">(“{section.heading}”, line {section.line})</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-2 text-slate-600">No section headings were recognized.</p>
          )}
          {result.warnings.length > 0 && (
            <>
              <h3 className="mt-6 font-medium">Worth checking</h3>
              <ul className="mt-2 list-disc space-y-1 pl-5 text-amber-900">
                {result.warnings.map((warning) => (
                  <li key={warning.code}>{warning.message}</li>
                ))}
              </ul>
            </>
          )}
        </section>
      )}
    </div>
  )
}
