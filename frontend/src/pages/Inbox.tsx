import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { AlertTriangle, CheckCircle2, CircleHelp, Eye, MessageSquare, PlayCircle, Trash2, TriangleAlert } from 'lucide-react'
import { api, type InboxItem } from '../api/client'
import { BudgetStrip } from '../components/Budget'

const KIND_STYLE: Record<string, { icon: JSX.Element; cls: string; label: string }> = {
  question: { icon: <CircleHelp size={16} />, cls: 'border-amber-500/50 bg-amber-500/10 text-amber-200', label: 'Rückfrage' },
  approval: { icon: <PlayCircle size={16} />, cls: 'border-sky-500/50 bg-sky-500/10 text-sky-200', label: 'Freigabe' },
  problem: { icon: <AlertTriangle size={16} />, cls: 'border-rose-500/50 bg-rose-500/10 text-rose-200', label: 'Problem' },
  review: { icon: <Eye size={16} />, cls: 'border-emerald-500/50 bg-emerald-500/10 text-emerald-200', label: 'Prüfen' },
  alert: { icon: <TriangleAlert size={16} />, cls: 'border-orange-500/50 bg-orange-500/10 text-orange-200', label: 'Meldung' },
  reply: { icon: <MessageSquare size={16} />, cls: 'border-slate-500/50 bg-slate-500/10 text-slate-200', label: 'Antwort' },
}

function age(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime()
  if (!Number.isFinite(ms)) return ''
  const m = Math.round(ms / 60_000)
  if (m < 60) return `vor ${m} min`
  const h = Math.round(m / 60)
  if (h < 48) return `vor ${h} h`
  return `vor ${Math.round(h / 24)} T`
}

export default function Inbox({ onCount }: { onCount?: (n: number) => void }) {
  const [items, setItems] = useState<InboxItem[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const load = () =>
      api.inbox().then(
        (i) => { setItems(i); setError(null); onCount?.(i.length) },
        (e) => setError(String(e)),
      )
    load()
    const id = window.setInterval(load, 15_000)
    return () => window.clearInterval(id)
  }, [onCount])

  // Alerts can be deleted right here (hides them in the channel too).
  const remove = async (e: React.MouseEvent, i: InboxItem) => {
    e.preventDefault()
    e.stopPropagation()
    if (!i.channel || i.seq == null) return
    try {
      await api.chat.delete(i.channel, { seqs: [i.seq] })
      setItems((cur) => {
        const next = (cur ?? []).filter((x) => x.key !== i.key)
        onCount?.(next.length)
        return next
      })
    } catch (err) { setError(String(err)) }
  }

  if (error) return <div className="text-sm text-rose-300">Inbox nicht ladbar: {error}</div>
  if (items === null) return <div className="text-sm text-slate-400">Lade …</div>
  if (items.length === 0) {
    return (
      <div className="mx-auto max-w-2xl">
      <BudgetStrip />
      <div className="mt-10 flex flex-col items-center gap-2 text-slate-400">
        <CheckCircle2 size={32} className="text-emerald-400" />
        <div>Nichts wartet auf dich.</div>
      </div>
      </div>
    )
  }
  return (
    <div className="mx-auto max-w-2xl space-y-3">
      <BudgetStrip />
      {items.map((i) => {
        const k = KIND_STYLE[i.kind] ?? KIND_STYLE.review
        return (
          <Link
            key={i.key}
            to={i.link}
            className="block rounded-lg border border-slate-700 bg-slate-800/60 p-3 hover:border-slate-500"
          >
            <div className="flex items-center gap-2 text-xs">
              <span className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 ${k.cls}`}>
                {k.icon} {k.label}
              </span>
              <span className="text-slate-400">{i.project_name}</span>
              <span className="ml-auto text-slate-500">{age(i.updated_at)}</span>
              {i.kind === 'alert' && i.seq != null && (
                <button onClick={(e) => remove(e, i)} className="text-slate-500 hover:text-rose-300" aria-label="Meldung löschen" title="Meldung löschen">
                  <Trash2 size={14} />
                </button>
              )}
            </div>
            <div className="mt-1.5 font-medium text-slate-100">
              {i.task_id != null && <span className="text-slate-500">#{i.task_id} </span>}{i.title}
            </div>
            <div className="mt-0.5 text-sm text-slate-300">{i.reason}</div>
            {i.detail && <div className="mt-1 line-clamp-3 text-xs text-slate-400 whitespace-pre-wrap">{i.detail}</div>}
          </Link>
        )
      })}
    </div>
  )
}
