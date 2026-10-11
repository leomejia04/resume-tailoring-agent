import { useCallback, useEffect, useRef, useState, type ChangeEvent, type FormEvent } from 'react'
import { Link } from 'react-router'
import { getProfile, listMasters, masterFileUrl, updateProfile, uploadMaster } from '../api/client'
import { fieldErrors, messageOf } from '../api/errors'
import { ACCOUNT_LIMITS, type MasterVersion, type Profile } from '../api/types'
import { Alert } from '../components/Alert'
import { TextField } from '../components/TextField'

type Notice = { tone: 'error' | 'success' | 'info'; message: string; requestId?: string | null }

const STATUS_LABELS: Record<MasterVersion['extraction_status'], string> = {
  complete: 'Read successfully',
  partial: 'Read with warnings',
  failed: 'Could not read text',
}

function formatSize(bytes: number) {
  return bytes < 1024 * 1024 ? `${Math.round(bytes / 1024)} KB` : `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function DetailsForm({ profile, onSaved }: { profile: Profile; onSaved: (profile: Profile) => void }) {
  const [displayName, setDisplayName] = useState(profile.display_name ?? '')
  const [prefix, setPrefix] = useState(profile.filename_prefix ?? '')
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [notice, setNotice] = useState<Notice | null>(null)
  const [pending, setPending] = useState(false)

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    setNotice(null)
    if (prefix && !/^[A-Za-z][A-Za-z0-9]*$/.test(prefix)) {
      setErrors({ filename_prefix: 'Use letters and digits only, starting with a letter.' })
      return
    }
    setErrors({})
    setPending(true)
    try {
      const saved = await updateProfile({
        expected_version: profile.lock_version,
        display_name: displayName.trim() || null,
        filename_prefix: prefix.trim() || null,
      })
      onSaved(saved)
      setNotice({ tone: 'success', message: 'Details saved.' })
    } catch (error) {
      setErrors(fieldErrors(error))
      setNotice({ tone: 'error', ...messageOf(error) })
    } finally {
      setPending(false)
    }
  }

  return (
    <form noValidate onSubmit={handleSubmit} className="space-y-4">
      {notice && (
        <Alert tone={notice.tone} requestId={notice.requestId}>
          {notice.message}
        </Alert>
      )}
      <div className="grid gap-4 sm:grid-cols-2">
        <TextField
          label="Your name"
          optional
          maxLength={ACCOUNT_LIMITS.displayNameMax}
          value={displayName}
          error={errors.display_name}
          onChange={(e) => setDisplayName(e.target.value)}
        />
        <TextField
          label="Download name prefix"
          optional
          hint="Used to name your PDFs, e.g. JordanExample_Data_Analyst_Company_CV.pdf"
          maxLength={ACCOUNT_LIMITS.filenamePrefixMax}
          value={prefix}
          error={errors.filename_prefix}
          onChange={(e) => setPrefix(e.target.value)}
        />
      </div>
      <button
        type="submit"
        disabled={pending}
        className="rounded-md bg-indigo-700 px-4 py-2 font-medium text-white hover:bg-indigo-800 disabled:opacity-60"
      >
        {pending ? 'Saving…' : 'Save details'}
      </button>
    </form>
  )
}

function UploadForm({ onUploaded }: { onUploaded: () => void }) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [file, setFile] = useState<File | null>(null)
  const [notice, setNotice] = useState<Notice | null>(null)
  const [pending, setPending] = useState(false)

  function choose(event: ChangeEvent<HTMLInputElement>) {
    setNotice(null)
    const chosen = event.target.files?.[0] ?? null
    if (chosen && !/\.(docx|pdf)$/i.test(chosen.name)) {
      setNotice({ tone: 'error', message: 'Choose a .docx or .pdf file.' })
      setFile(null)
      return
    }
    if (chosen && chosen.size > ACCOUNT_LIMITS.uploadMaxBytes) {
      setNotice({ tone: 'error', message: 'Files can be at most 5 MB.' })
      setFile(null)
      return
    }
    setFile(chosen)
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    if (!file) {
      setNotice({ tone: 'error', message: 'Choose a file to upload.' })
      return
    }
    setPending(true)
    setNotice({ tone: 'info', message: 'Uploading and reading your resume…' })
    try {
      const result = await uploadMaster(file)
      setNotice(
        result.duplicate
          ? { tone: 'info', message: `You already uploaded this exact file as version ${result.master.version_number}.` }
          : {
              tone: 'success',
              message: `Version ${result.master.version_number} saved. We found ${result.master.evidence_count} facts for you to review.`,
            },
      )
      setFile(null)
      if (inputRef.current) inputRef.current.value = ''
      onUploaded()
    } catch (error) {
      setNotice({ tone: 'error', ...messageOf(error) })
    } finally {
      setPending(false)
    }
  }

  return (
    <form noValidate onSubmit={handleSubmit} className="space-y-4">
      <div>
        <label htmlFor="master-file" className="block text-sm font-medium text-slate-800">
          Master resume file
        </label>
        <p id="master-file-hint" className="text-sm text-slate-500">
          A .docx or .pdf up to 5 MB. Your original file is never changed; each upload becomes a new version.
        </p>
        <input
          ref={inputRef}
          id="master-file"
          type="file"
          accept=".docx,.pdf,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
          aria-describedby="master-file-hint"
          onChange={choose}
          className="mt-2 block w-full text-sm file:mr-3 file:rounded-md file:border-0 file:bg-slate-100 file:px-3 file:py-2"
        />
      </div>
      {notice && (
        <Alert tone={notice.tone} requestId={notice.requestId}>
          {notice.message}
          {notice.tone === 'success' && (
            <>
              {' '}
              <Link to="/experience" className="underline">
                Review them now
              </Link>
            </>
          )}
        </Alert>
      )}
      <button
        type="submit"
        disabled={pending}
        className="rounded-md bg-indigo-700 px-4 py-2 font-medium text-white hover:bg-indigo-800 disabled:opacity-60"
      >
        {pending ? 'Uploading…' : 'Upload'}
      </button>
    </form>
  )
}

export function ProfilePage() {
  const [profile, setProfile] = useState<Profile | null>(null)
  const [masters, setMasters] = useState<MasterVersion[] | null>(null)
  const [loadError, setLoadError] = useState<Notice | null>(null)

  const loadMasters = useCallback((signal?: AbortSignal) => {
    listMasters(signal)
      .then(setMasters)
      .catch((error) => {
        if (!signal?.aborted) setLoadError({ tone: 'error', ...messageOf(error) })
      })
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    getProfile(controller.signal)
      .then(setProfile)
      .catch((error) => {
        if (!controller.signal.aborted) setLoadError({ tone: 'error', ...messageOf(error) })
      })
    loadMasters(controller.signal)
    return () => controller.abort()
  }, [loadMasters])

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-semibold text-slate-900">Profile</h1>
        <p className="mt-2 text-slate-600">Your details and master resume versions. Only you can see them.</p>
      </div>
      {loadError && (
        <Alert tone="error" requestId={loadError.requestId}>
          {loadError.message}
        </Alert>
      )}

      <section aria-labelledby="details-heading" className="rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
        <h2 id="details-heading" className="mb-4 text-lg font-semibold">
          Your details
        </h2>
        {profile ? (
          <DetailsForm profile={profile} onSaved={setProfile} />
        ) : (
          !loadError && <p role="status">Loading…</p>
        )}
      </section>

      <section aria-labelledby="master-heading" className="rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
        <h2 id="master-heading" className="mb-4 text-lg font-semibold">
          Master resume
        </h2>
        <UploadForm onUploaded={() => loadMasters()} />
        <h3 className="mt-8 mb-2 font-medium">Versions</h3>
        {masters === null ? (
          !loadError && <p role="status">Loading…</p>
        ) : masters.length === 0 ? (
          <p className="text-slate-600">No versions yet. Upload your resume to get started.</p>
        ) : (
          <ul className="divide-y divide-slate-200">
            {masters.map((master) => (
              <li key={master.id} className="flex flex-wrap items-start justify-between gap-2 py-3">
                <div>
                  <p className="font-medium">
                    Version {master.version_number}: {master.original_filename}
                  </p>
                  <p className="text-sm text-slate-600">
                    {new Date(master.created_at).toLocaleString()} · {formatSize(master.size_bytes)} ·{' '}
                    {STATUS_LABELS[master.extraction_status]} · {master.evidence_count} facts found
                  </p>
                  {master.extraction_notes.map((note) => (
                    <p key={note} className="text-sm text-amber-800">
                      {note}
                    </p>
                  ))}
                </div>
                <a href={masterFileUrl(master.id)} className="text-sm text-indigo-700 underline" download>
                  Download original<span className="sr-only"> of version {master.version_number}</span>
                </a>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}
