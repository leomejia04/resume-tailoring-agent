import { Link } from 'react-router'

export function NotFoundPage() {
  return (
    <div>
      <h1 className="text-2xl font-semibold text-slate-900">Page not found</h1>
      <p className="mt-2 text-slate-600">That page does not exist.</p>
      <Link to="/" className="mt-4 inline-block text-indigo-700 underline">
        Go to the dashboard
      </Link>
    </div>
  )
}
