import { createContext, useContext } from 'react'
import type { SessionResponse, User } from '../api/types'

export type AuthState =
  | { status: 'loading' }
  | { status: 'signed_out'; reason?: 'account_deleted' }
  | { status: 'signed_in'; user: User }

export interface AuthValue {
  state: AuthState
  signIn: (email: string, password: string) => Promise<void>
  signUp: (email: string, password: string) => Promise<SessionResponse['status']>
  signOut: () => Promise<void>
  /** Record a session the server already created (e.g. after a password reset). */
  sessionStarted: (user: User) => void
  /** Delete the account (password re-check on the server); ends the session. */
  deleteAccount: (password: string) => Promise<void>
}

export const AuthContext = createContext<AuthValue | null>(null)

export function useAuth(): AuthValue {
  const value = useContext(AuthContext)
  if (!value) throw new Error('useAuth must be used inside <AuthProvider>')
  return value
}

/** Only allow redirects to paths inside this app (prevents open redirects). */
export function safeNext(next: string | null | undefined): string {
  if (!next || !next.startsWith('/') || next.startsWith('//') || next.startsWith('/\\')) return '/'
  return next
}
