// Status bar on top of a task chat: where the task stands and everything the
// user can do right now (GET .../actions, the same list the cards are built
// from). Cards scroll away and are posted only on a change; this stays.
import { useState } from 'react'
import { ChevronDown } from 'lucide-react'
import type { ServiceButton, TaskAction, TaskActions } from '../../api/client'
import { nextStep } from './Board'
import { StatusBadge } from './shared'

const TONE: Record<TaskAction['kind'], string> = {
  primary: 'border-amber-500/70 bg-amber-900/30 text-amber-100',
  secondary: 'border-slate-600 bg-slate-800 text-slate-200',
  danger: 'border-rose-700 bg-rose-950/40 text-rose-200',
}

export default function TaskStatusBar({ state, onAction }: {
  state: TaskActions
  onAction: (b: ServiceButton) => Promise<void>
}) {
  const [more, setMore] = useState(false)
  const [acting, setActing] = useState(false)
  const { task, actions } = state
  const step = nextStep(task)
  const primary = actions.filter((a) => a.kind === 'primary')
  const rest = actions.filter((a) => a.kind !== 'primary')
  const folded = rest.length > 2 || (primary.length > 0 && rest.length > 0)
  const shown = [...primary, ...(!folded || more ? rest : [])]
  const press = async (b: TaskAction) => {
    setActing(true)
    try { await onAction(b) } finally { setActing(false) }
  }
  return (
    <div className="border-b border-slate-800 bg-slate-900/70 px-3 py-2">
      <div className="flex items-center gap-2 text-xs">
        <StatusBadge task={task} />
        {step && <span className={`flex min-w-0 items-center gap-1 ${step.tone}`}>{step.icon}<span className="truncate">{step.text}</span></span>}
        {folded && (
          <button onClick={() => setMore((m) => !m)} aria-expanded={more}
            className="ml-auto flex shrink-0 items-center gap-0.5 text-slate-400 hover:text-slate-200">
            Weitere Aktionen <ChevronDown size={12} className={more ? 'rotate-180' : ''} />
          </button>
        )}
      </div>
      {task.user_summary && (
        // Open while the user has to decide; the key reopens it on a new state.
        <details key={task.status} open={task.status === 'plan_ready'} className="mt-2 text-xs">
          <summary className="cursor-pointer text-slate-400 hover:text-slate-200">Zusammenfassung</summary>
          <div className="mt-1 max-h-48 overflow-y-auto whitespace-pre-wrap break-words rounded border border-slate-700 bg-slate-950/50 p-2 text-slate-200">
            {task.user_summary}
          </div>
        </details>
      )}
      {shown.length > 0 && (
        <div className="mt-2 flex max-h-40 flex-wrap gap-1.5 overflow-y-auto">
          {shown.map((a) => (
            <button key={a.data} disabled={acting} onClick={() => press(a)} title={a.label}
              className={`max-w-full truncate rounded-lg border px-2.5 py-1.5 text-xs disabled:opacity-40 ${TONE[a.kind]}`}>
              {a.label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
