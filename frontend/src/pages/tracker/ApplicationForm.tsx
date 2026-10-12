import { useEffect, useId, useRef, useState, type FormEvent } from 'react'
import { ApiError, createApplication, updateApplication } from '../../api/client'
import { fieldErrors as serverFieldErrors, messageOf } from '../../api/errors'
import type { ApplicationOut, ApplicationStatus, ApprovalOut } from '../../api/types'
import { Alert } from '../../components/Alert'
import {
  STATUS_LABELS,
  STATUSES,
  TRACKER_LIMITS,
  approvalLabel,
  safeHref,
  toInput,
  todayIso,
  type FormValues,
} from './format'

type Field = keyof FormValues
type Errors = Partial<Record<Field, string>>

const FIELD_LABELS: Record<Field, string> = {
  company: 'Company',
  role: 'Role',
  job_url: 'Link to the posting',
  approval_id: 'Resume you sent',
  applied_on: 'Date applied',
  status: 'Status',
  notes: 'Notes',
}
const FIELD_ORDER = Object.keys(FIELD_LABELS) as Field[]
// The page has already reloaded its list when these show.
const STALE_MESSAGES = {
  changed: 'This application was changed in another tab; the list below shows the latest version.',
  gone: 'This application was deleted in another tab.',
}

interface Props {
  /** The entry being edited, or null for a new one. */
  entry: ApplicationOut | null
  initial: FormValues
  approvals: ApprovalOut[]
  onSaved: (saved: ApplicationOut) => void
  onCancel: () => void
  /** The entry changed or disappeared elsewhere: the page reloads its list. */
  onStale: () => void
}

function validate(values: FormValues): Errors {
  const errors: Errors = {}
  if (!values.company.trim()) errors.company = 'Enter the company name.'
  if (!values.role.trim()) errors.role = 'Enter the role you applied for.'
  if (values.job_url.trim() && !safeHref(values.job_url.trim())) {
    errors.job_url = 'Enter a full web address starting with https://'
  }
  if (values.applied_on && values.applied_on > todayIso()) errors.applied_on = 'The date cannot be in the future.'
  return errors
}

export function ApplicationForm({ entry, initial, approvals, onSaved, onCancel, onStale }: Props) {
  const [values, setValues] = useState<FormValues>(initial)
  const [errors, setErrors] = useState<Errors>({})
  const [formError, setFormError] = useState<{ message: string; requestId: string | null } | null>(null)
  const [stale, setStale] = useState<'changed' | 'gone' | null>(null)
  const [busy, setBusy] = useState(false)
  const formId = useId()
  const headingRef = useRef<HTMLHeadingElement>(null)
  const summaryRef = useRef<HTMLDivElement>(null)
  const fieldId = (name: Field) => `${formId}-${name}`
  const errorId = (name: Field) => `${formId}-${name}-error`

  useEffect(() => {
    headingRef.current?.focus()
  }, [])

  useEffect(() => {
    if (formError || Object.keys(errors).length > 0) summaryRef.current?.focus()
  }, [formError, errors])

  function update<K extends Field>(name: K, value: FormValues[K]) {
    setValues((current) => ({ ...current, [name]: value }))
  }

  // Picking a resume fills empty fields from its job posting.
  function chooseApproval(id: string) {
    const approval = approvals.find((a) => a.id === id)
    setValues((current) => ({
      ...current,
      approval_id: id,
      company: current.company || approval?.company || '',
      role: current.role || approval?.job_title || '',
      job_url: current.job_url || approval?.source_url || '',
    }))
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (busy) return
    const found = validate(values)
    setErrors(found)
    setFormError(null)
    if (Object.keys(found).length > 0) return
    setBusy(true)
    try {
      const input = toInput(values)
      onSaved(entry ? await updateApplication(entry.id, input, entry.lock_version) : await createApplication(input))
    } catch (err) {
      const fromServer = serverFieldErrors(err) as Errors
      setErrors(Object.fromEntries(FIELD_ORDER.filter((n) => fromServer[n]).map((n) => [n, fromServer[n]])))
      setFormError(messageOf(err))
      if (err instanceof ApiError && (err.code === 'conflict' || err.status === 404)) {
        setStale(err.status === 404 ? 'gone' : 'changed')
        onStale()
      }
    } finally {
      setBusy(false)
    }
  }

  const inputClass = (name: Field) =>
    `mt-1 block w-full rounded-md border bg-white px-3 py-2 text-slate-900 shadow-sm ${
      errors[name] ? 'border-red-600' : 'border-slate-300'
    }`
  const described = (name: Field, hint = false) =>
    [hint ? `${fieldId(name)}-hint` : '', errors[name] ? errorId(name) : ''].filter(Boolean).join(' ') || undefined
  const errorText = (name: Field) =>
    errors[name] && (
      <p id={errorId(name)} className="mt-1 text-sm text-red-700">
        {errors[name]}
      </p>
    )
  const label = (name: Field, optional = false) => (
    <label htmlFor={fieldId(name)} className="block text-sm font-medium text-slate-800">
      {FIELD_LABELS[name]} {optional && <span className="font-normal text-slate-500">(optional)</span>}
    </label>
  )
  const textInput = (name: 'company' | 'role' | 'job_url', maxLength: number, optional = false) => (
    <div>
      {label(name, optional)}
      <input
        id={fieldId(name)}
        name={name}
        type={name === 'job_url' ? 'url' : 'text'}
        inputMode={name === 'job_url' ? 'url' : undefined}
        maxLength={maxLength}
        required={!optional}
        value={values[name]}
        aria-invalid={errors[name] ? true : undefined}
        aria-describedby={described(name)}
        onChange={(e) => update(name, e.target.value)}
        className={inputClass(name)}
      />
      {errorText(name)}
    </div>
  )

  // Keep the current link selectable even if the approvals list could not load.
  const linkedMissing = values.approval_id && !approvals.some((a) => a.id === values.approval_id)
  const errorEntries = FIELD_ORDER.filter((name) => errors[name])
  const title = entry ? `Edit ${entry.role} at ${entry.company}` : 'Add an application'

  return (
    <section aria-labelledby={`${formId}-heading`} className="rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
      <h2 id={`${formId}-heading`} ref={headingRef} tabIndex={-1} className="text-lg font-semibold">
        {title}
      </h2>
      {(formError || errorEntries.length > 0) && (
        <div className="mt-4">
          <Alert ref={summaryRef} tone="error" requestId={formError?.requestId}>
            {stale ? (
              `${STALE_MESSAGES[stale]} Your changes were not saved. Copy anything you need, then close this form.`
            ) : (
              (formError?.message ?? 'Please fix the highlighted fields.')
            )}
            {errorEntries.length > 0 && (
              <ul className="mt-2 list-disc pl-5 font-normal">
                {errorEntries.map((name) => (
                  <li key={name}>
                    <a href={`#${fieldId(name)}`} className="underline">
                      {FIELD_LABELS[name]}: {errors[name]}
                    </a>
                  </li>
                ))}
              </ul>
            )}
          </Alert>
        </div>
      )}
      <form noValidate onSubmit={submit} className="mt-4 space-y-4" aria-busy={busy}>
        <div className="grid gap-4 sm:grid-cols-2">
          {textInput('company', TRACKER_LIMITS.companyMax)}
          {textInput('role', TRACKER_LIMITS.roleMax)}
        </div>
        {textInput('job_url', TRACKER_LIMITS.urlMax, true)}
        <div>
          {label('approval_id', true)}
          <p id={`${fieldId('approval_id')}-hint`} className="text-sm text-slate-500">
            Link the approved resume you sent, so you can find that exact PDF later.
          </p>
          <select
            id={fieldId('approval_id')}
            value={values.approval_id}
            aria-invalid={errors.approval_id ? true : undefined}
            aria-describedby={described('approval_id', true)}
            onChange={(e) => chooseApproval(e.target.value)}
            className={inputClass('approval_id')}
          >
            <option value="">None</option>
            {linkedMissing && entry?.resume && (
              <option value={entry.resume.approval_id}>
                {entry.resume.filename} (version {entry.resume.draft_version})
              </option>
            )}
            {approvals.map((a) => (
              <option key={a.id} value={a.id}>
                {approvalLabel(a)}
              </option>
            ))}
          </select>
          {errorText('approval_id')}
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            {label('status')}
            <select
              id={fieldId('status')}
              value={values.status}
              onChange={(e) => update('status', e.target.value as ApplicationStatus)}
              className={inputClass('status')}
            >
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {STATUS_LABELS[s]}
                </option>
              ))}
            </select>
            {errorText('status')}
          </div>
          <div>
            {label('applied_on', true)}
            <p id={`${fieldId('applied_on')}-hint`} className="text-sm text-slate-500">
              Leave empty if you have not applied yet.
            </p>
            <input
              id={fieldId('applied_on')}
              type="date"
              max={todayIso()}
              value={values.applied_on}
              aria-invalid={errors.applied_on ? true : undefined}
              aria-describedby={described('applied_on', true)}
              onChange={(e) => update('applied_on', e.target.value)}
              className={inputClass('applied_on')}
            />
            {errorText('applied_on')}
          </div>
        </div>
        <div>
          {label('notes', true)}
          <textarea
            id={fieldId('notes')}
            rows={4}
            maxLength={TRACKER_LIMITS.notesMax}
            value={values.notes}
            aria-invalid={errors.notes ? true : undefined}
            aria-describedby={described('notes')}
            onChange={(e) => update('notes', e.target.value)}
            className={inputClass('notes')}
          />
          {errorText('notes')}
          <p className="mt-1 text-right text-xs text-slate-500">
            {values.notes.length.toLocaleString()} / {TRACKER_LIMITS.notesMax.toLocaleString()} characters
          </p>
        </div>
        <div className="flex flex-wrap gap-3">
          {!stale && (
            <button
              type="submit"
              aria-disabled={busy}
              className="rounded-md bg-indigo-700 px-4 py-2 font-medium text-white hover:bg-indigo-800 aria-disabled:opacity-60"
            >
              {busy ? 'Saving…' : entry ? 'Save changes' : 'Save application'}
            </button>
          )}
          <button type="button" onClick={onCancel} className="rounded-md border border-slate-300 px-4 py-2 font-medium">
            {stale ? 'Close' : 'Cancel'}
          </button>
        </div>
      </form>
    </section>
  )
}
