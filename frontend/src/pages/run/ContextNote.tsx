import type { DraftContext } from '../../api/types'

const facts = (n: number) => `${n} confirmed fact${n === 1 ? '' : 's'}`

/** Says which of the user's confirmed facts the AI read for the draft (ADR 0010). */
export function ContextNote({ context }: { context: DraftContext | null | undefined }) {
  if (!context) return null
  const version = `version ${context.draft_version}`
  if (context.mode === 'full') {
    return (
      <p className="text-sm text-slate-600">
        When it wrote {version}, the AI read all {facts(context.facts_selected)}
        {context.facts_selected < context.facts_total ? ` it had room for (of ${context.facts_total})` : ''}.
      </p>
    )
  }
  return (
    <p className="text-sm text-slate-600">
      When it wrote {version}, the AI read {context.facts_selected} of your {facts(context.facts_total)}: the ones
      most relevant to this posting, plus your education and at least one fact from each job. Your experience bank
      is unchanged, and any fact can still be added below.
    </p>
  )
}
