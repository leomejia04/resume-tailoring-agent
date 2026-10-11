import { useState } from 'react'
import { NavLink, Outlet, useNavigate } from 'react-router'
import { useAuth } from '../auth/AuthContext'
import { ApiStatus } from './ApiStatus'

const NAV_ITEMS = [
  { to: '/', label: 'Dashboard', end: true },
  { to: '/jobs/new', label: 'Job posting' },
  { to: '/experience', label: 'Experience' },
  { to: '/profile', label: 'Profile' },
  { to: '/tracker', label: 'Tracker' },
  { to: '/settings', label: 'Settings' },
]

function navClass({ isActive }: { isActive: boolean }) {
  const base = 'rounded-md px-3 py-2 text-sm font-medium'
  return isActive
    ? `${base} bg-indigo-50 text-indigo-800`
    : `${base} text-slate-700 hover:bg-slate-100 hover:text-slate-900`
}

function UserMenu() {
  const { state, signOut } = useAuth()
  const navigate = useNavigate()
  const [pending, setPending] = useState(false)
  if (state.status !== 'signed_in') return null

  async function handleSignOut() {
    setPending(true)
    try {
      await signOut()
    } finally {
      navigate('/sign-in', { replace: true })
    }
  }

  return (
    <div className="flex items-center gap-3 text-sm">
      <span className="text-slate-600">{state.user.email}</span>
      <button
        type="button"
        onClick={handleSignOut}
        disabled={pending}
        className="rounded-md border border-slate-300 px-3 py-1.5 text-slate-800 hover:bg-slate-100 disabled:opacity-60"
      >
        {pending ? 'Signing out…' : 'Sign out'}
      </button>
    </div>
  )
}

export function Layout() {
  return (
    <div className="min-h-screen">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-10 focus:rounded-md focus:bg-white focus:px-4 focus:py-2 focus:shadow"
      >
        Skip to main content
      </a>
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-5xl flex-wrap items-center justify-between gap-3 px-4 py-3">
          <p className="text-lg font-semibold text-slate-900">Resume Tailor</p>
          <div className="flex flex-wrap items-center gap-4">
            <ApiStatus />
            <UserMenu />
          </div>
        </div>
        <nav aria-label="Main" className="mx-auto max-w-5xl px-4 pb-2">
          <ul className="flex flex-wrap gap-1">
            {NAV_ITEMS.map((item) => (
              <li key={item.to}>
                <NavLink to={item.to} end={item.end} className={navClass}>
                  {item.label}
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>
      </header>
      <main id="main" tabIndex={-1} className="mx-auto max-w-5xl px-4 py-8 focus:outline-none">
        <Outlet />
      </main>
    </div>
  )
}
