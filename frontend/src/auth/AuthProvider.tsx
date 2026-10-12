import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import * as api from '../api/client'
import type { User } from '../api/types'
import { AuthContext, type AuthState, type AuthValue } from './AuthContext'

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: 'loading' })

  useEffect(() => {
    const controller = new AbortController()
    api
      .getMe(controller.signal)
      .then((user) => setState({ status: 'signed_in', user }))
      .catch(() => {
        if (!controller.signal.aborted) setState({ status: 'signed_out' })
      })
    const unsubscribe = api.onSessionEnded(() => setState({ status: 'signed_out' }))
    return () => {
      controller.abort()
      unsubscribe()
    }
  }, [])

  const sessionStarted = useCallback((user: User) => setState({ status: 'signed_in', user }), [])

  const signIn = useCallback(async (email: string, password: string) => {
    const session = await api.signIn(email, password)
    if (session.user) setState({ status: 'signed_in', user: session.user })
  }, [])

  const signUp = useCallback(async (email: string, password: string) => {
    const session = await api.signUp(email, password)
    if (session.status === 'signed_in' && session.user) setState({ status: 'signed_in', user: session.user })
    return session.status
  }, [])

  const signOut = useCallback(async () => {
    try {
      await api.signOut()
    } finally {
      setState({ status: 'signed_out' })
    }
  }, [])

  const deleteAccount = useCallback(async (password: string) => {
    await api.deleteAccount(password)
    setState({ status: 'signed_out', reason: 'account_deleted' })
  }, [])

  const value = useMemo<AuthValue>(
    () => ({ state, signIn, signUp, signOut, sessionStarted, deleteAccount }),
    [state, signIn, signUp, signOut, sessionStarted, deleteAccount],
  )
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}
