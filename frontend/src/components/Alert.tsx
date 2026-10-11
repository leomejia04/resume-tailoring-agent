import { forwardRef, type ReactNode } from 'react'

interface AlertProps {
  tone: 'error' | 'success' | 'info'
  children: ReactNode
  requestId?: string | null
}

const TONES = {
  error: 'border-red-300 bg-red-50 text-red-900',
  success: 'border-emerald-300 bg-emerald-50 text-emerald-900',
  info: 'border-slate-300 bg-slate-50 text-slate-800',
}

/** A focusable message box; errors are announced immediately. */
export const Alert = forwardRef<HTMLDivElement, AlertProps>(function Alert({ tone, children, requestId }, ref) {
  return (
    <div
      ref={ref}
      tabIndex={-1}
      role={tone === 'error' ? 'alert' : 'status'}
      className={`rounded-md border p-4 ${TONES[tone]}`}
    >
      <div className="font-medium">{children}</div>
      {requestId && (
        <p className="mt-1 text-sm">
          If this keeps happening, share this reference: <code>{requestId}</code>
        </p>
      )}
    </div>
  )
})
