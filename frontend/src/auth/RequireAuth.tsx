import { Navigate, Outlet, useLocation } from 'react-router'
import { useAuth } from './AuthContext'

export function RequireAuth() {
  const { state } = useAuth()
  const location = useLocation()
  if (state.status === 'loading') {
    return (
      <p role="status" className="p-8 text-slate-600">
        Loading…
      </p>
    )
  }
  if (state.status === 'signed_out') {
    if (state.reason === 'account_deleted') return <Navigate to="/sign-in?deleted=1" replace />
    const next = encodeURIComponent(location.pathname + location.search)
    return <Navigate to={`/sign-in?next=${next}`} replace />
  }
  return <Outlet />
}
