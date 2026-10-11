import { Outlet } from 'react-router'

export function PublicLayout() {
  return (
    <div className="min-h-screen px-4 py-12">
      <main id="main" className="mx-auto max-w-md">
        <p className="mb-6 text-center text-lg font-semibold text-slate-900">Resume Tailor</p>
        <div className="rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
          <Outlet />
        </div>
      </main>
    </div>
  )
}
