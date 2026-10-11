import type { AIStatus } from '../api/types'

/** Says plainly which engine produced results; demo output is never presented as AI. */
export function AIBadge({ status }: { status: AIStatus | null }) {
  if (!status) return null
  if (status.provider === 'demo') {
    return (
      <p className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900">
        Demo mode: results come from simple built-in rules, not an AI model. Nothing is sent to any AI provider.
      </p>
    )
  }
  if (status.provider === 'disabled') {
    return (
      <p className="rounded-md border border-slate-300 bg-slate-50 px-3 py-2 text-sm text-slate-700">
        AI tailoring is not set up on this server yet.
      </p>
    )
  }
  return (
    <details className="rounded-md border border-slate-300 bg-slate-50 px-3 py-2 text-sm text-slate-700">
      <summary className="cursor-pointer font-medium">What we send to the AI provider</summary>
      <p className="mt-2">{status.disclosure}</p>
      <p className="mt-2 text-slate-600">
        Model: {status.model}. Today you have used ${Number(status.spent_today_usd).toFixed(3)} of your $
        {Number(status.user_daily_budget_usd).toFixed(2)} daily limit.
      </p>
    </details>
  )
}
