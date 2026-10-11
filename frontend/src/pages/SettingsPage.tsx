import { useEffect, useId, useState, type FormEvent } from 'react'
import { getPreferences, rememberPreferences } from '../api/client'
import { messageOf } from '../api/errors'
import type { FormatPreferences } from '../api/types'
import { Alert } from '../components/Alert'

export function SettingsPage() {
  const [prefs, setPrefs] = useState<FormatPreferences | null>(null)
  const [lockVersion, setLockVersion] = useState(1)
  const [notice, setNotice] = useState<{ tone: 'error' | 'success'; message: string; requestId?: string | null } | null>(
    null,
  )
  const [busy, setBusy] = useState(false)
  const ids = { bullets: useId(), project: useId() }

  useEffect(() => {
    const controller = new AbortController()
    getPreferences(controller.signal)
      .then((result) => {
        setPrefs(result.preferences)
        setLockVersion(result.lock_version)
      })
      .catch((err) => {
        if (!controller.signal.aborted) setNotice({ tone: 'error', ...messageOf(err) })
      })
    return () => controller.abort()
  }, [])

  async function save(event: FormEvent) {
    event.preventDefault()
    if (!prefs || busy) return
    setBusy(true)
    setNotice(null)
    try {
      const result = await rememberPreferences(prefs, lockVersion)
      setLockVersion(result.lock_version)
      setNotice({ tone: 'success', message: 'Saved. New resumes will start with these settings.' })
    } catch (err) {
      setNotice({ tone: 'error', ...messageOf(err) })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-900">Settings</h1>
        <p className="mt-2 max-w-2xl text-slate-600">
          Formatting defaults are only stored when you save them here or choose “remember” while editing a resume.
        </p>
      </div>
      {notice && (
        <Alert tone={notice.tone} requestId={notice.requestId}>
          {notice.message}
        </Alert>
      )}
      {prefs === null ? (
        !notice && <p role="status">Loading…</p>
      ) : (
        <form onSubmit={save} className="space-y-4 rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
          <h2 className="text-lg font-semibold">Formatting defaults</h2>
          <div className="flex flex-wrap gap-4">
            <div>
              <label htmlFor={ids.bullets} className="block text-sm font-medium">
                Most bullets per job
              </label>
              <input
                id={ids.bullets}
                type="number"
                min={1}
                max={8}
                value={prefs.max_bullets_per_entry}
                onChange={(e) => setPrefs({ ...prefs, max_bullets_per_entry: Number(e.target.value) })}
                className="mt-1 w-20 rounded-md border border-slate-300 px-2 py-1"
              />
            </div>
            <div>
              <label htmlFor={ids.project} className="block text-sm font-medium">
                Most bullets per project
              </label>
              <input
                id={ids.project}
                type="number"
                min={1}
                max={6}
                value={prefs.project_bullets}
                onChange={(e) => setPrefs({ ...prefs, project_bullets: Number(e.target.value) })}
                className="mt-1 w-20 rounded-md border border-slate-300 px-2 py-1"
              />
            </div>
          </div>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={prefs.bold_project_technologies}
              onChange={(e) => setPrefs({ ...prefs, bold_project_technologies: e.target.checked })}
            />
            Bold project technologies
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={prefs.show_headline}
              onChange={(e) => setPrefs({ ...prefs, show_headline: e.target.checked })}
            />
            Show a headline on rebuilt layouts
          </label>
          <button type="submit" aria-disabled={busy} className="rounded-md bg-indigo-700 px-4 py-2 font-medium text-white aria-disabled:opacity-60">
            {busy ? 'Saving…' : 'Save defaults'}
          </button>
        </form>
      )}
      <section className="rounded-lg border border-slate-200 bg-white p-6 text-sm text-slate-700">
        <h2 className="text-lg font-semibold text-slate-900">Your data</h2>
        <p className="mt-2">Deleting your account and all of your files is not available yet; it is planned for a later step.</p>
      </section>
    </div>
  )
}
