import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { Loader2, MessageSquare, Plus } from 'lucide-react'
import { api, type ChatChannel } from '../api/client'
import { pillCls, PillRow } from '../components/nav/Pills'

function when(ts: string | null): string {
  if (!ts) return ''
  const d = new Date(ts)
  const today = new Date()
  return d.toDateString() === today.toDateString()
    ? d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' })
    : d.toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit' })
}

const SCROLL_KEY = 'at.chatlist.scroll'
const PROJECT_KEY = 'at.chatlist.project'

export default function ChatList({ onUnread }: { onUnread?: (n: number) => void }) {
  const [channels, setChannels] = useState<ChatChannel[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [showArchived, setShowArchived] = useState(false)
  const [showNew, setShowNew] = useState(false)
  const [names, setNames] = useState<Record<string, string>>({})
  const [params, setParams] = useSearchParams()
  let stored = ''
  try { stored = localStorage.getItem(PROJECT_KEY) ?? '' } catch { /* private mode */ }
  const chosen = params.get('p') ?? stored
  const choose = (p: string) => {
    try { localStorage.setItem(PROJECT_KEY, p) } catch { /* private mode */ }
    setParams(p ? { p } : {}, { replace: true })
  }

  useEffect(() => {
    api.projects().then((ps) => setNames(Object.fromEntries(ps.map((p) => [p.id, p.name]))), () => {})
  }, [])

  useEffect(() => {
    const load = () => api.chat.channels().then((c) => {
      setChannels(c)
      onUnread?.(c.filter((x) => !x.archived).reduce((n, x) => n + x.unread, 0))
    }, (e) => setError(String(e)))
    load()
    const id = window.setInterval(load, 15_000)
    return () => window.clearInterval(id)
  }, [onUnread])

  // Back from a chat lands where the list was, not at the top: remember the
  // page scroll while the list is shown, restore it once the list has loaded.
  useEffect(() => {
    let frame = 0
    const save = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => sessionStorage.setItem(SCROLL_KEY, String(window.scrollY)))
    }
    window.addEventListener('scroll', save, { passive: true })
    return () => { cancelAnimationFrame(frame); window.removeEventListener('scroll', save) }
  }, [])
  const restored = useRef(false)
  useLayoutEffect(() => {
    if (restored.current || !channels) return
    restored.current = true
    const y = Number(sessionStorage.getItem(SCROLL_KEY) || 0)
    if (y > 0) window.scrollTo(0, y)
  }, [channels])

  if (error) return <div className="text-sm text-rose-300">{error}</div>
  if (!channels) return <div className="text-sm text-slate-400">Lade …</div>
  if (channels.length === 0) return <div className="text-sm text-slate-400">Keine Kanäle konfiguriert.</div>

  // Per project: the lead channel first, then the other channels, then the
  // task chats (newest activity first) under their own heading.
  const projectIds = [...new Set(channels.map((c) => c.project))]
  const project = projectIds.includes(chosen) ? chosen : ''
  const unreadOf = (p: string) => channels.filter((c) => !c.archived && (!p || c.project === p)).reduce((n, c) => n + c.unread, 0)
  const inProject = project ? channels.filter((c) => c.project === project) : channels
  const archivedCount = inProject.filter((c) => c.archived).length
  const byProject = new Map<string, ChatChannel[]>()
  for (const c of inProject.filter((x) => showArchived || !x.archived)) byProject.set(c.project, [...(byProject.get(c.project) ?? []), c])
  const sections: { key: string; title: string; list: ChatChannel[] }[] = []
  for (const [proj, list] of byProject) {
    const tasks = list.filter((c) => c.task_id != null)
    const checks = list.filter((c) => c.check_of)
      .sort((a, b) => (b.last_ts ?? '').localeCompare(a.last_ts ?? ''))
    const rest = list.filter((c) => c.task_id == null && !c.check_of).sort((a, b) => Number(!!b.lead) - Number(!!a.lead))
    const name = names[proj] ?? proj
    if (rest.length) sections.push({ key: proj, title: name, list: rest })
    if (tasks.length) sections.push({ key: `${proj}-tasks`, title: `${name} · Tasks`, list: tasks })
    if (checks.length) sections.push({ key: `${proj}-checks`, title: `${name} · Prüfungen`, list: checks })
  }
  const badge = (n: number) => n > 0 ? <span className="rounded-full bg-sky-600 px-1.5 text-[11px] font-semibold text-white">{n}</span> : null

  return (
    <div className="mx-auto max-w-2xl space-y-5">
      {projectIds.length > 1 && (
        <PillRow label="Projekt">
          <button className={pillCls(project === '')} onClick={() => choose('')}>Alle {badge(unreadOf(''))}</button>
          {projectIds.map((p) => (
            <button key={p} className={pillCls(project === p)} onClick={() => choose(p)}>{names[p] ?? p} {badge(unreadOf(p))}</button>
          ))}
        </PillRow>
      )}
      {showNew
        ? <NewChat initial={project} onCancel={() => setShowNew(false)} />
        : <button onClick={() => setShowNew(true)} className="flex w-full items-center justify-center gap-2 rounded-lg border border-sky-700 bg-sky-900/30 px-3 py-2.5 text-sm text-sky-100 hover:bg-sky-900/50">
            <Plus size={16} /> Neuer Chat für ein Repo
          </button>}
      {sections.map(({ key, title, list }) => (
        <section key={key}>
          <h2 className="mb-1.5 text-xs uppercase tracking-wide text-slate-500">{title}</h2>
          <div className="divide-y divide-slate-800 overflow-hidden rounded-lg border border-slate-700">
            {list.map((c) => (
              <Link key={c.id} to={`/chat/${c.id}`} className="flex items-center gap-3 bg-slate-800/50 px-3 py-2.5 hover:bg-slate-800">
                <MessageSquare size={18} className="shrink-0 text-slate-500" />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className={`font-medium ${c.archived ? 'text-slate-400' : 'text-slate-100'}`}>{c.label}</span>
                    {c.custom && <span className="rounded bg-sky-900/60 px-1.5 text-[11px] text-sky-200">{c.cwd.split('/').pop()}</span>}
                    {c.archived && <span className="rounded bg-slate-700 px-1.5 text-[11px] text-slate-300">archiviert</span>}
                    {(c.live?.busy || c.task?.working) && <Loader2 size={12} className="animate-spin text-sky-400" />}
                    {c.open_requests > 0 && <span className="rounded bg-amber-500/20 px-1.5 text-[11px] text-amber-200">wartet auf dich</span>}
                    <span className="ml-auto text-[11px] text-slate-500">{when(c.last_ts)}</span>
                  </div>
                  <div className="truncate text-xs text-slate-400">{c.preview ?? c.cwd}</div>
                </div>
                {c.unread > 0 && <span className="rounded-full bg-sky-600 px-1.5 text-[11px] font-semibold text-white">{c.unread}</span>}
              </Link>
            ))}
          </div>
        </section>
      ))}
      {archivedCount > 0 && (
        <button onClick={() => setShowArchived(!showArchived)} className="w-full rounded-lg border border-slate-700 px-3 py-2 text-xs text-slate-400 hover:text-slate-200">
          {showArchived ? 'Archivierte ausblenden' : `Archivierte anzeigen (${archivedCount})`}
        </button>
      )}
    </div>
  )
}


// "Neuer Chat": an own, long-lived chat in a project's root or one of its repos.
function NewChat({ onCancel, initial = '' }: { onCancel: () => void; initial?: string }) {
  const nav = useNavigate()
  const [opts, setOpts] = useState<{ project: string; name: string; root: string; repos: string[] }[] | null>(null)
  const [project, setProject] = useState('')
  const [repo, setRepo] = useState('')
  const [title, setTitle] = useState('')
  const [message, setMessage] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    api.chat.newOptions().then((o) => { setOpts(o); setProject((o.find((x) => x.project === initial) ?? o[0])?.project ?? '') }, (e) => setError(String(e)))
  }, [])
  const current = opts?.find((o) => o.project === project)
  const create = async () => {
    setBusy(true)
    try {
      const ch = await api.chat.createCustom({ project, repo, title: title.trim(), message: message.trim() || undefined })
      nav(`/chat/${ch.id}`)
    } catch (e) { setError(String(e)); setBusy(false) }
  }
  const input = 'w-full rounded border border-slate-600 bg-slate-900 px-2 py-1.5 text-sm text-slate-100'
  return (
    <div className="space-y-2 rounded-lg border border-sky-700 bg-slate-800/60 p-3 text-sm">
      <div className="font-medium text-slate-100">Neuer Chat</div>
      {error && <div className="text-xs text-rose-300">{error}</div>}
      {!opts ? <div className="text-xs text-slate-400">Lade …</div> : <>
        <label className="block text-xs text-slate-400">Projekt
          <select value={project} onChange={(e) => { setProject(e.target.value); setRepo('') }} className={input}>
            {opts.map((o) => <option key={o.project} value={o.project}>{o.name}</option>)}
          </select>
        </label>
        <label className="block text-xs text-slate-400">Ordner
          <select value={repo} onChange={(e) => setRepo(e.target.value)} className={input}>
            <option value="">{current?.root ?? 'Projekt'} (ganzes Projekt)</option>
            {current?.repos.map((r) => <option key={r} value={r}>{r}</option>)}
          </select>
        </label>
        <label className="block text-xs text-slate-400">Name
          <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="z. B. Swing-Bot umbauen" className={input} />
        </label>
        <label className="block text-xs text-slate-400">Erste Nachricht (optional)
          <textarea value={message} onChange={(e) => setMessage(e.target.value)} rows={3} placeholder="Worum geht es?" className={input} />
        </label>
        <div className="flex gap-2">
          <button onClick={create} disabled={busy || !title.trim() || !project}
            className="flex-1 rounded border border-sky-600 bg-sky-700 px-3 py-1.5 text-white disabled:opacity-50">Chat öffnen</button>
          <button onClick={onCancel} className="rounded border border-slate-600 px-3 py-1.5 text-slate-300">Abbrechen</button>
        </div>
      </>}
    </div>
  )
}
