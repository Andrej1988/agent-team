// Kanban board of the Tasks page, left to right as a task flows: Todo
// → Zur Freigabe → In Arbeit → Wartet auf dich →
// Erledigt (docs/TASKS-V2.md). No drag & drop on purpose — transitions are gated
// (start only via approval, agents can't), so each card offers the one
// action that fits its state instead.
import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { AlertTriangle, Check, CircleHelp, Loader2, MessageSquare, Pause, Rocket, XCircle } from 'lucide-react'
import type { BoardTask, TaskPrio } from '../../api/client'
import { shortRepo, taskPhase } from './shared'

export type Lane = 'idee' | 'freigabe' | 'arbeit' | 'wartet' | 'erledigt'

export const LANES: { id: Lane; label: string; short: string; hint: string }[] = [
  { id: 'idee', label: 'Todo', short: 'Todo', hint: 'Offen — der Lead wählt daraus aus und schlägt zur Freigabe vor. Für später: Tab Backlog' },
  { id: 'freigabe', label: 'Zur Freigabe', short: 'Freigabe', hint: 'Lead empfiehlt den Start — besprechen und freigeben' },
  { id: 'arbeit', label: 'In Arbeit', short: 'Arbeit', hint: 'Agenten arbeiten, nichts zu tun' },
  { id: 'wartet', label: 'Wartet auf dich', short: 'Wartet', hint: 'Frage, Freigabe oder Fehler — du bist dran' },
  { id: 'erledigt', label: 'Erledigt', short: 'Erledigt', hint: 'Fertig in den letzten 7 Tagen' },
]

export function laneOf(t: BoardTask): Lane {
  const p = taskPhase(t)
  if (p === 'ausarbeitung' || p === 'geparkt' || p === 'geschlossen') return 'idee'
  return p as Lane
}

const PRIO_BAR: Record<TaskPrio, string> = { 1: 'border-l-red-500', 2: 'border-l-slate-600', 3: 'border-l-slate-800' }
const FOLDED = 8 // Todo / Erledigt show this many until "+ n weitere"

type Step = { icon: JSX.Element; text: string; tone: string }

export function nextStep(t: BoardTask): Step | null {
  const cur = t.board?.stages?.current
  const total = t.board?.stages?.total
  const at = cur && total ? ` · Stufe ${cur.index}/${total}` : ''
  switch (t.status) {
    case 'awaiting_input':
      return { icon: <CircleHelp size={12} />, text: `Frage offen${at}`, tone: 'text-violet-300' }
    case 'blocked':
      return { icon: <Pause size={12} />, text: `Gestoppt${at}`, tone: 'text-amber-300' }
    case 'failed':
      return { icon: <XCircle size={12} />, text: `Fehler${at}`, tone: 'text-red-300' }
    case 'limit_reached':
      return { icon: <AlertTriangle size={12} />, text: 'Limit erreicht', tone: 'text-amber-300' }
    case 'plan_ready':
      if (t.paused_reason) return { icon: <Pause size={12} />, text: `Pausiert: ${t.paused_reason}`, tone: 'text-slate-400' }
      if (cur?.confirm) return { icon: <Rocket size={12} />, text: `Freigabe: ${cur.label}`, tone: 'text-violet-300' }
      return { icon: <CircleHelp size={12} />, text: cur ? `Nächste Stufe bestätigen: ${cur.label}` : 'Plan bestätigen', tone: 'text-violet-300' }
    case 'planning':
      return { icon: <Loader2 size={12} className="animate-spin" />, text: 'Planer legt die Stufen fest', tone: 'text-blue-300' }
    case 'running':
      return { icon: <Loader2 size={12} className="animate-spin" />, text: cur ? `${cur.label}${at}` : 'läuft', tone: 'text-blue-300' }
    case 'refining':
      return { icon: <Loader2 size={12} className="animate-spin" />, text: 'wird ausgearbeitet', tone: 'text-blue-300' }
    case 'done':
      return t.board?.pending_apply
        ? { icon: <AlertTriangle size={12} />, text: 'nicht übernommen', tone: 'text-amber-300' }
        : { icon: <Check size={12} />, text: 'erledigt', tone: 'text-green-400' }
    default:
      return null
  }
}

function ago(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime()
  if (!Number.isFinite(ms)) return ''
  const m = Math.round(ms / 60000)
  if (m < 60) return `${Math.max(m, 1)} min`
  const h = Math.round(m / 60)
  if (h < 48) return `${h} h`
  return `${Math.round(h / 24)} T`
}

function Card({ t, selected, onSelect, action }: {
  t: BoardTask; selected: boolean; onSelect: () => void
  action?: { label: string; to: string; icon?: JSX.Element } | null
}) {
  const step = nextStep(t)
  const st = t.board?.stages
  const lane = laneOf(t)
  const note = lane === 'freigabe' && t.lead_note ? t.lead_note : null
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onSelect}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelect() } }}
      className={`cursor-pointer rounded-md border border-l-4 border-slate-700 bg-slate-900/60 px-2.5 py-2 text-left hover:border-slate-500 ${PRIO_BAR[t.prio]} ${
        selected ? 'ring-1 ring-blue-500' : ''
      }`}
    >
      <div className="flex items-start gap-1.5">
        <span className="shrink-0 pt-px font-mono text-[11px] text-slate-500">#{t.id}</span>
        <span title={t.title} className={`line-clamp-3 min-w-0 flex-1 break-words text-sm leading-snug ${t.prio === 1 ? 'font-medium text-slate-100' : 'text-slate-200'}`}>{t.title}</span>
      </div>
      {note && <div className="mt-1 line-clamp-2 text-[11px] text-slate-400">{note}</div>}
      {step && (
        <div className={`mt-1 flex items-center gap-1 text-[11px] ${step.tone}`}>
          {step.icon}
          <span className="truncate">{step.text}</span>
        </div>
      )}
      {lane === 'arbeit' && st && st.total > 0 && (
        <div className="mt-1.5 h-1 overflow-hidden rounded bg-slate-700" title={`${st.done} von ${st.total} Stufen fertig`}>
          <div className="h-full bg-blue-500" style={{ width: `${Math.round((st.done / st.total) * 100)}%` }} />
        </div>
      )}
      <div className="mt-1 flex items-center gap-2 text-[10px] text-slate-500">
        {t.repos.length > 0 && <span className="min-w-0 truncate">{t.repos.map(shortRepo).join(' · ')}</span>}
        <span className="ml-auto shrink-0" title={`zuletzt geändert ${t.updated_at}`}>{ago(t.updated_at)}</span>
      </div>
      {action && (
        <Link
          to={action.to}
          onClick={(e) => e.stopPropagation()}
          className="mt-1.5 inline-flex items-center gap-1 rounded border border-slate-600 bg-slate-800 px-2 py-0.5 text-[11px] text-slate-200 hover:border-blue-500 hover:text-white"
        >
          {action.icon ?? <MessageSquare size={11} />} {action.label}
        </Link>
      )}
    </div>
  )
}

export default function Board({ tasks, selectedId, onSelect, chatFor, leadChannel, onAllDone, searching = false }: {
  tasks: BoardTask[]
  searching?: boolean                         // a search filter is active: nothing folded
  onAllDone: () => void                       // "alle Erledigten" → the full list
  selectedId: number | null
  onSelect: (t: BoardTask) => void
  chatFor: (t: BoardTask) => string | null    // task chat channel id, if the task has one
  leadChannel: string | null
}) {
  const byLane = useMemo(() => {
    const m: Record<Lane, BoardTask[]> = { wartet: [], freigabe: [], arbeit: [], erledigt: [], idee: [] }
    for (const t of tasks) if (t.status !== 'backlog') m[laneOf(t)].push(t) // Backlog has its own tab
    m.erledigt.sort((a, b) => b.updated_at.localeCompare(a.updated_at))
    return m
  }, [tasks])
  const count = (id: Lane) => byLane[id].length
  // Phones open where something needs me, else where things move.
  const firstBusy: Lane = (['wartet', 'freigabe', 'arbeit'] as Lane[]).find((id) => count(id) > 0) ?? 'idee'
  const [lane, setLane] = useState<Lane | null>(null) // phones: one lane at a time
  // While searching, jump to a lane with hits if the chosen one has none.
  const firstHit = LANES.find((l) => count(l.id) > 0)?.id ?? 'idee'
  const shown = searching ? (lane && count(lane) > 0 ? lane : firstHit) : (lane ?? firstBusy)
  const [open, setOpen] = useState<Partial<Record<Lane, boolean>>>({})

  const actionFor = (t: BoardTask) => {
    const l = laneOf(t)
    const chat = chatFor(t)
    if (l === 'wartet') return chat ? { label: t.status === 'awaiting_input' || t.status === 'plan_ready' ? 'Im Chat antworten' : 'Im Chat ansehen', to: `/chat/${chat}` } : null
    if (l === 'freigabe') return leadChannel ? { label: 'Mit Lead besprechen', to: `/chat/${leadChannel}?draft=${encodeURIComponent(`Lass uns #${t.id} besprechen: `)}` } : null
    if (l === 'arbeit') return chat ? { label: 'Verlauf', to: `/chat/${chat}` } : null
    return null
  }

  const column = (id: Lane) => {
    let list = byLane[id]
    let more = 0
    const foldable = id === 'idee' || id === 'erledigt'
    if (foldable && !searching && !open[id] && list.length > FOLDED) {
      more = list.length - FOLDED
      list = list.slice(0, FOLDED)
    }
    return (
      <div className="flex flex-col gap-2">
        {list.map((t) => (
          <Card key={t.id} t={t} selected={selectedId === t.id} onSelect={() => onSelect(t)} action={actionFor(t)} />
        ))}
        {more > 0 && (
          <button onClick={() => setOpen((o) => ({ ...o, [id]: true }))} className="rounded border border-dashed border-slate-700 py-1.5 text-xs text-slate-400 hover:text-slate-200">
            + {more} weitere
          </button>
        )}
        {foldable && open[id] && byLane[id].length > FOLDED && (
          <button onClick={() => setOpen((o) => ({ ...o, [id]: false }))} className="py-1 text-xs text-slate-500 hover:text-slate-300">weniger zeigen</button>
        )}
        {byLane[id].length === 0 && <div className="py-3 text-center text-xs text-slate-600">—</div>}
        {id === 'erledigt' && (
          <button onClick={onAllDone} className="py-1 text-xs text-slate-500 hover:text-slate-300">alle Erledigten →</button>
        )}
      </div>
    )
  }

  const header = (l: (typeof LANES)[number]) => (
    <div className="mb-2 flex items-baseline gap-2" title={l.hint}>
      <span className={`text-xs font-semibold uppercase tracking-wider ${l.id === 'wartet' && byLane.wartet.length ? 'text-violet-300' : 'text-slate-400'}`}>{l.label}</span>
      <span className="text-xs text-slate-500">{byLane[l.id].length}</span>
    </div>
  )

  return (
    <>
      {/* Phones: lane chips + one column */}
      <div className="lg:hidden">
        <div className="mb-3 flex gap-1.5 overflow-x-auto pb-1">
          {LANES.map((l) => (
            <button
              key={l.id}
              onClick={() => setLane(l.id)}
              className={`flex shrink-0 items-center gap-1 rounded-full border px-2.5 py-1 text-xs ${
                shown === l.id ? 'border-blue-500 bg-blue-900/40 text-white' : 'border-slate-700 text-slate-400'
              }`}
            >
              {l.short}
              <span className={`rounded-full px-1.5 text-[10px] ${l.id === 'wartet' && byLane.wartet.length ? 'bg-violet-600 text-white' : 'bg-slate-700 text-slate-300'}`}>
                {byLane[l.id].length}
              </span>
            </button>
          ))}
        </div>
        <div className="mb-2 text-[11px] text-slate-500">{LANES.find((l) => l.id === shown)?.hint}</div>
        {column(shown)}
      </div>

      {/* Desktop: all columns */}
      <div className="hidden min-h-0 flex-1 grid-cols-5 gap-3 lg:grid">
        {LANES.map((l) => (
          <div
            key={l.id}
            className={`flex min-h-0 flex-col rounded-lg border p-2.5 ${
              l.id === 'wartet' && byLane.wartet.length ? 'border-violet-800 bg-violet-950/20' : 'border-slate-700 bg-slate-800'
            }`}
          >
            {header(l)}
            <div className="min-h-0 flex-1 overflow-y-auto pr-0.5">{column(l.id)}</div>
          </div>
        ))}
      </div>
    </>
  )
}
