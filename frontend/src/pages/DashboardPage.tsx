import { Link } from 'react-router'

export function DashboardPage() {
  return (
    <div>
      <h1 className="text-2xl font-semibold text-slate-900">Dashboard</h1>
      <p className="mt-2 max-w-2xl text-slate-600">
        Start by uploading your master resume, then confirm the facts it contains. Tailored resumes and history
        arrive in the next steps.
      </p>
      <div className="mt-6 flex flex-wrap gap-3">
        <Link
          to="/profile"
          className="inline-block rounded-md bg-indigo-700 px-4 py-2 font-medium text-white hover:bg-indigo-800"
        >
          Upload master resume
        </Link>
        <Link to="/experience" className="inline-block rounded-md border border-slate-300 px-4 py-2 font-medium">
          Review experience
        </Link>
        <Link to="/jobs/new" className="inline-block rounded-md border border-slate-300 px-4 py-2 font-medium">
          Check a job posting
        </Link>
      </div>
    </div>
  )
}
