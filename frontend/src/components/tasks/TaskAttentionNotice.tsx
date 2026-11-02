import type { Task, TaskStage } from '../../api/client'
import { taskHelp, type TaskHelp } from './taskHelp'

export function AttentionCard({ help, taskId }: { help: TaskHelp; taskId?: number }) {
  const destinations = {
    plan: `task-pipeline-${taskId}`,
    answer: `task-answer-${taskId}`,
    stage: `task-stage-${taskId}-${help.stageSeq}`,
    login: `task-login-${taskId}`,
    run: `task-actions-${taskId}`,
  }
  const labels = { plan: 'Zum Plan und den Aktionen', answer: 'Zum Antwortfeld', stage: 'Zur betroffenen Stufe', login: 'Zur Anmeldung', run: 'Zu den Aktionen' }
  const focusTarget = () => {
    if (!help.target) return
    const element = document.getElementById(destinations[help.target])
    element?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    if (help.target === 'answer') element?.querySelector('textarea')?.focus({ preventScroll: true })
  }
  return (
    <div className="space-y-1.5 rounded border border-amber-800 bg-amber-950/30 p-3 text-xs text-amber-100" role="status">
      <div className="font-semibold">{help.title}</div>
      <p>{help.problem}</p>
      {help.context && <p className="text-slate-300">Aktueller Befund: {help.context}</p>}
      <p><span className="font-medium">Von dir benötigt: </span>{help.input}</p>
      <p><span className="font-medium">Nächster Schritt: </span>{help.next}</p>
      {help.target && taskId != null && (
        <button type="button" onClick={focusTarget} className="rounded border border-amber-700 px-2 py-1 text-amber-200 hover:bg-amber-900/30">
          {labels[help.target]}
        </button>
      )}
      {help.details && (
        <details className="text-slate-400">
          <summary className="cursor-pointer">Technische Details</summary>
          <pre className="mt-1 max-h-48 overflow-y-auto whitespace-pre-wrap break-words text-[11px]">{help.details}</pre>
        </details>
      )}
    </div>
  )
}

export default function TaskAttentionNotice({ task, stages }: { task: Task; stages: TaskStage[] }) {
  const help = taskHelp(task, stages)
  return help ? <AttentionCard help={help} taskId={task.id} /> : null
}
