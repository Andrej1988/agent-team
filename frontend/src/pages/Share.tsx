import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { Bot, Check, ClipboardPaste, Lightbulb } from 'lucide-react'
import { api, type ProjectInfo } from '../api/client'

// PWA share target (manifest): "Teilen → Team" opens /share?url=…&text=…&title=…
// Opened from the nav ("Idee") without parameters, the link is pasted instead.
// One tap saves it as an idea in a project; the lead looks at it in its next run.
const LAST = 'at.share.project'

function sharedLink(params: URLSearchParams): string {
  return [params.get('url'), params.get('text'), params.get('title')].join(' ').match(/https?:\/\/\S+/)?.[0] ?? ''
}

export default function Share({ projects }: { projects: ProjectInfo[] }) {
  const [params] = useSearchParams()
  const shared = useMemo(() => sharedLink(params), [params])
  const [typed, setTyped] = useState('')
  const link = shared || typed.match(/https?:\/\/\S+/)?.[0] || ''
  // Shared text without the link itself (X/Reddit put the post text there).
  const text = useMemo(() => [params.get('title'), params.get('text')]
    .map((s) => (s ?? '').replace(shared, '').trim()).filter(Boolean).join('\n'), [params, shared])
  const options = projects.filter((p) => p.tasks)
  const [project, setProjectId] = useState('')
  const [title, setTitle] = useState('')
  const [note, setNote] = useState('')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState<{ id: number; project: string } | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (project || options.length === 0) return
    let last = ''
    try { last = localStorage.getItem(LAST) ?? '' } catch { /* private mode */ }
    setProjectId(options.some((p) => p.id === last) ? last : options[0].id)
  }, [options, project])
  useEffect(() => {
    const first = text.split('\n')[0]
    let host = ''
    try { host = link ? new URL(link).hostname.replace(/^www\./, '') : '' } catch { /* not a URL */ }
    setTitle((first ? `Untersuchen: ${first}` : host ? `Untersuchen: Link von ${host}` : '').slice(0, 200))
  }, [text, link])

  const navigate = useNavigate()
  // The lead reads the link itself and files a worked-out idea; its chat shows the result.
  const askAgent = async () => {
    if (!project || !link) return
    setSaving(true)
    setError(null)
    try {
      const r = await api.chat.idea({ project, link, text, note: note.trim() })
      try { localStorage.setItem(LAST, project) } catch { /* private mode */ }
      navigate(`/chat/${r.channel}`)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setSaving(false)
    }
  }

  const paste = async () => {
    try { setTyped((await navigator.clipboard.readText()).trim()) } catch { setError('Zwischenablage nicht lesbar — Link lang drücken und einfügen.') }
  }

  const save = async () => {
    if (!project || !title.trim()) return
    setSaving(true)
    setError(null)
    try {
      const details = [note.trim(), link && `Link: ${link}`, text && `Geteilter Text:\n${text}`,
        link && '(Per Teilen gespeichert — Inhalt des Links noch nicht angesehen.)'].filter(Boolean).join('\n\n')
      const t = await api.lead.create(project, { title: title.trim(), details, prio: 2, repos: [] })
      try { localStorage.setItem(LAST, project) } catch { /* private mode */ }
      setSaved({ id: t.id, project })
      setTyped('')
      setNote('')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  if (saved) {
    return (
      <div className="mx-auto max-w-lg space-y-3 rounded-lg border border-green-800 bg-green-900/20 p-4 text-sm text-slate-200">
        <div className="flex items-center gap-2 font-semibold text-green-300"><Check size={16} /> Als Idee #{saved.id} gespeichert</div>
        <p className="text-slate-400">Der Lead schaut sie sich beim nächsten Lauf an. Gestartet wird erst auf deinen Klick.</p>
        <div className="flex gap-2">
          <Link to={`/p/${saved.project}/tasks?task=${saved.id}`} className="rounded-md bg-slate-700 px-3 py-1.5 text-xs text-white">Task öffnen</Link>
          <button onClick={() => setSaved(null)} className="rounded-md border border-slate-600 px-3 py-1.5 text-xs text-slate-300">Noch eine</button>
          <Link to="/inbox" className="rounded-md border border-slate-600 px-3 py-1.5 text-xs text-slate-300">Fertig</Link>
        </div>
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-lg space-y-3 rounded-lg border border-slate-700 bg-slate-800 p-4 text-sm">
      <div className="flex items-center gap-2 font-semibold text-white"><Lightbulb size={16} /> Als Idee speichern</div>
      {shared ? (
        <a href={shared} target="_blank" rel="noreferrer" className="block break-all text-xs text-sky-400">{shared}</a>
      ) : (
        <div className="flex gap-1">
          <input value={typed} onChange={(e) => setTyped(e.target.value)} placeholder="Link einfügen" inputMode="url"
            className="min-w-0 flex-1 rounded-md border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-white" />
          <button onClick={paste} title="Aus der Zwischenablage einfügen" aria-label="Einfügen"
            className="flex items-center gap-1 rounded-md border border-slate-600 px-2.5 text-xs text-slate-300">
            <ClipboardPaste size={14} /> Einfügen
          </button>
        </div>
      )}
      {text && <p className="whitespace-pre-wrap break-words text-xs text-slate-400">{text.slice(0, 600)}</p>}
      <div className="flex flex-wrap gap-1">
        {options.map((p) => (
          <button key={p.id} onClick={() => setProjectId(p.id)}
            className={`rounded-md border px-3 py-1.5 text-xs ${project === p.id ? 'border-blue-500 bg-blue-600 text-white' : 'border-slate-600 text-slate-300'}`}>
            {p.name}
          </button>
        ))}
      </div>
      <input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={200} placeholder="Titel"
        className="w-full rounded-md border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-white" />
      <textarea value={note} onChange={(e) => setNote(e.target.value)} rows={3} placeholder="Notiz (optional): was soll untersucht werden?"
        className="w-full rounded-md border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-white" />
      {error && <p className="text-xs text-red-400">{error}</p>}
      {/* Always visible (hidden until a link was there, nobody found it); greyed out without a link. */}
      <button onClick={askAgent} disabled={saving || !project || !link}
        className="flex w-full items-center justify-center gap-1.5 rounded-md bg-violet-600 px-3 py-2 text-sm font-semibold text-white disabled:opacity-40">
        <Bot size={15} /> {link ? 'Mit Agent lesen & Idee vorschlagen' : 'Mit Agent lesen — erst Link einfügen'}
      </button>
      <button onClick={save} disabled={saving || !project || !title.trim()}
        className={`w-full rounded-md px-3 py-2 text-sm font-semibold disabled:opacity-50 ${link ? 'border border-slate-600 text-slate-200' : 'bg-blue-600 text-white'}`}>
        {saving ? 'Speichere …' : link ? 'Nur so speichern (ohne Lesen)' : 'Speichern'}
      </button>
      <p className="text-[11px] text-slate-500">„Mit Agent lesen“: Der Lead des Projekts öffnet den Link, fasst ihn zusammen und legt eine ausgearbeitete Idee an — du siehst es in seinem Chat. Gestartet wird nichts.</p>
    </div>
  )
}
