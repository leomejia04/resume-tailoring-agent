import { useEffect, useState } from 'react'
import { getHealth } from '../api/client'

type Status = 'checking' | 'online' | 'offline'

const LABELS: Record<Status, string> = {
  checking: 'Checking server…',
  online: 'Server connected',
  offline: 'Server unreachable',
}
const DOTS: Record<Status, string> = {
  checking: 'bg-slate-400',
  online: 'bg-emerald-500',
  offline: 'bg-red-500',
}

export function ApiStatus() {
  const [status, setStatus] = useState<Status>('checking')

  useEffect(() => {
    const controller = new AbortController()
    getHealth(controller.signal)
      .then(() => setStatus('online'))
      .catch(() => {
        if (!controller.signal.aborted) setStatus('offline')
      })
    return () => controller.abort()
  }, [])

  return (
    <p role="status" className="flex items-center gap-2 text-sm text-slate-600">
      <span aria-hidden="true" className={`inline-block size-2 rounded-full ${DOTS[status]}`} />
      {LABELS[status]}
    </p>
  )
}
