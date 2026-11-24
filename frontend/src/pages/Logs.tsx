import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Link, Navigate, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { ChevronLeft, Layers, Radio, RefreshCw, Search } from 'lucide-react'
import { api, type SystemContainer, type SystemContainers } from '../api/client'
import { Pill, PillRow } from '../components/nav/Pills'

const TAILS = [200, 1000, 5000]
const LIVE_CAP = 5000
const COMBINED_TAIL = 15

function lineClass(line: string): string {
  const l = line.toLowerCase()
  if (/\b(error|critical|fatal|exception|traceback)\b/.test(l)) return 'text-red-400'
  if (/\bwarn(ing)?\b/.test(l)) return 'text-amber-300'
  if (/\bdebug\b/.test(l)) return 'text-slate-500'
  return 'text-slate-300'
}

const isProblem = (line: string) => lineClass(line) !== 'text-slate-300' && lineClass(line) !== 'text-slate-500'

function shortName(c: SystemContainer, group: string): string {
  if (c.service) return c.service
  return group !== 'all' && group !== 'other' ? c.name.replace(new RegExp(`^${group}[-_]`), '') : c.name
}

function Dot({ state }: { state: string | null }) {
  const cls = state === 'running' ? 'bg-emerald-400' : state === 'restarting' ? 'bg-amber-400' : 'bg-red-400'
  return <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${cls}`} title={state ?? ''} />
}

export default function Logs() {
  const { group = '', container = '' } = useParams()
  const [params] = useSearchParams()
  const combined = params.get('combined')
  const [data, setData] = useState<SystemContainers | null>(null)
  const [error, setError] = useState('')

  const load = useCallback(() => {
    api.system.containers().then((d) => { setData(d); setError('') }, (e) => setError(String(e.message || e)))
  }, [])
  useEffect(() => {
    load()
    const id = window.setInterval(load, 30_000)
    return () => window.clearInterval(id)
  }, [load])

  if (!group) return <Navigate to="/logs/all" replace />
  if (error && !data) return <div className="text-sm text-red-300">Container-Liste nicht abrufbar: {error}</div>
  if (!data) return <div className="text-sm text-slate-400">Lade …</div>

  const counts = new Map<string, number>()
  for (const c of data.containers) counts.set(c.group, (counts.get(c.group) ?? 0) + 1)
  const inGroup = group === 'all' ? data.containers : data.containers.filter((c) => c.group === group)

  return (
    <div className="space-y-3">
      <PillRow label="Container-Gruppe">
        <Pill to="/logs/all" active={group === 'all'}>Alle <span className="text-slate-500">{data.containers.length}</span></Pill>
        {data.groups.filter((g) => counts.get(g.id)).map((g) => (
          <Pill key={g.id} to={`/logs/${g.id}`} active={group === g.id}>
            {g.label} <span className="text-slate-500">{counts.get(g.id)}</span>
          </Pill>
        ))}
      </PillRow>
      <div className="flex gap-4">
        <ContainerList items={inGroup} group={group} selected={container} combined={combined}
                       className={container || combined ? 'hidden md:block' : 'w-full md:w-64'} />
        <div className={`min-w-0 flex-1 ${container || combined ? '' : 'hidden md:block'}`}>
          {container ? (
            <LogView key={container} name={container} group={group}
                     info={data.containers.find((c) => c.name === container)} />
          ) : combined ? (
            <Combined key={combined} group={group} label={combined}
                      items={inGroup.filter((c) => (c.compose_project ?? '') === combined)} />
          ) : (
            <p className="pt-2 text-sm text-slate-500">Container links wählen.</p>
          )}
        </div>
      </div>
    </div>
  )
}

function ContainerList({ items, group, selected, combined, className }: {
  items: SystemContainer[]; group: string; selected: string; combined: string | null; className: string
}) {
  const [q, setQ] = useState('')
  const shown = q ? items.filter((c) => c.name.toLowerCase().includes(q.toLowerCase())) : items
  const byProject = new Map<string, SystemContainer[]>()
  for (const c of shown) {
    const k = c.compose_project ?? ''
    byProject.set(k, [...(byProject.get(k) ?? []), c])
  }
  return (
    <aside className={`shrink-0 md:w-64 ${className}`}>
      <label className="mb-2 flex items-center gap-1.5 rounded border border-slate-700 bg-slate-900 px-2 py-1">
        <Search size={13} className="text-slate-500" />
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Container filtern"
               className="w-full bg-transparent text-sm text-slate-200 outline-none placeholder:text-slate-600" />
      </label>
      <div className="space-y-3 md:max-h-[calc(100vh-var(--header-h,56px)-8rem)] md:overflow-y-auto">
        {[...byProject.entries()].map(([proj, list]) => (
          <div key={proj || '-'}>
            <div className="mb-1 flex items-center justify-between px-2 text-[10px] font-semibold uppercase tracking-widest text-slate-500">
              <span className="truncate">{proj || 'ohne Compose-Projekt'}</span>
              {proj && list.length >= 2 && (
                <Link to={`/logs/${group}?combined=${encodeURIComponent(proj)}`}
                      className={`flex items-center gap-1 normal-case tracking-normal ${combined === proj ? 'text-sky-300' : 'hover:text-slate-300'}`}>
                  <Layers size={11} /> alle
                </Link>
              )}
            </div>
            {list.map((c) => (
              <Link key={c.name} to={`/logs/${group}/${encodeURIComponent(c.name)}`}
                    className={`flex items-center gap-2 rounded px-2 py-1 text-xs ${
                      selected === c.name ? 'bg-slate-700 text-white' : 'text-slate-400 hover:bg-slate-800 hover:text-slate-200'
                    }`}>
                <Dot state={c.state} />
                <span className="truncate" title={c.name}>{shortName(c, group)}</span>
              </Link>
            ))}
          </div>
        ))}
        {shown.length === 0 && <p className="px-2 text-xs text-slate-600">Keine Container.</p>}
      </div>
    </aside>
  )
}

function LogView({ name, group, info }: { name: string; group: string; info?: SystemContainer }) {
  const navigate = useNavigate()
  const [tailIdx, setTailIdx] = useState(0)
  const [lines, setLines] = useState<string[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [live, setLive] = useState(false)
  const [onlyProblems, setOnlyProblems] = useState(false)
  const [q, setQ] = useState('')
  const box = useRef<HTMLDivElement | null>(null)
  const stick = useRef(true)

  const fetchLines = useCallback(() => {
    setLoading(true)
    api.system.logs(name, TAILS[tailIdx]).then(
      (r) => { setLines(r.lines); setError('') },
      (e) => setError(String(e.message || e)),
    ).finally(() => setLoading(false))
  }, [name, tailIdx])
  useEffect(() => { fetchLines() }, [fetchLines])

  useEffect(() => {
    if (!live) return
    const es = new EventSource(api.system.logStreamUrl(name))
    es.addEventListener('line', (e) => {
      const line = JSON.parse((e as MessageEvent).data) as string
      setLines((prev) => (prev.length >= LIVE_CAP ? [...prev.slice(-LIVE_CAP + 1), line] : [...prev, line]))
    })
    es.addEventListener('error', () => { /* browser reconnects by itself */ })
    return () => es.close()
  }, [live, name])

  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return lines.filter((l) => (!onlyProblems || isProblem(l)) && (!needle || l.toLowerCase().includes(needle)))
  }, [lines, onlyProblems, q])

  useLayoutEffect(() => {
    const el = box.current
    if (el && stick.current) el.scrollTop = el.scrollHeight
  }, [shown])

  const btn = (on: boolean) =>
    `flex items-center gap-1 rounded border px-2 py-1 text-xs ${on ? 'border-sky-500 bg-sky-900/40 text-sky-200' : 'border-slate-700 text-slate-300 hover:bg-slate-800'}`

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <button onClick={() => navigate(`/logs/${group}`)} className="md:hidden rounded p-1 text-slate-400 hover:bg-slate-800" aria-label="Zur Liste">
          <ChevronLeft size={16} />
        </button>
        <span className="font-mono text-sm text-white">{name}</span>
        {info && <span className="text-xs text-slate-500">{info.status}</span>}
        <div className="ml-auto flex flex-wrap items-center gap-1.5">
          <label className="flex items-center gap-1 rounded border border-slate-700 px-2 py-1">
            <Search size={12} className="text-slate-500" />
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Suchen"
                   className="w-28 bg-transparent text-xs text-slate-200 outline-none placeholder:text-slate-600" />
          </label>
          <button className={btn(onlyProblems)} onClick={() => setOnlyProblems((v) => !v)}>Nur Warnungen/Fehler</button>
          <button className={btn(live)} onClick={() => setLive((v) => !v)} title="Neue Zeilen live anhängen">
            <Radio size={12} /> Live
          </button>
          <button className={btn(false)} onClick={fetchLines} disabled={loading} title="Neu laden">
            <RefreshCw size={12} className={loading ? 'animate-spin' : ''} />
          </button>
        </div>
      </div>
      {error && <div className="text-xs text-red-300">{error}</div>}
      <div
        ref={box}
        onScroll={(e) => {
          const el = e.currentTarget
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40
        }}
        className="h-[calc(100vh-var(--header-h,56px)-11rem)] min-h-[300px] overflow-auto rounded border border-slate-800 bg-slate-950 p-2 font-mono text-[11px] leading-[1.35]"
      >
        {tailIdx < TAILS.length - 1 && lines.length >= TAILS[tailIdx] && (
          <button onClick={() => { stick.current = false; setTailIdx((i) => i + 1) }}
                  className="mb-1 text-xs text-sky-400 hover:underline">
            ältere Zeilen laden (bis {TAILS[tailIdx + 1]})
          </button>
        )}
        {shown.map((l, i) => (
          <div key={i} className={`whitespace-pre-wrap break-all ${lineClass(l)}`}>{l}</div>
        ))}
        {!loading && shown.length === 0 && <div className="text-slate-600">{lines.length ? 'Keine passenden Zeilen.' : 'Keine Log-Zeilen.'}</div>}
      </div>
    </div>
  )
}

function Combined({ group, label, items }: { group: string; label: string; items: SystemContainer[] }) {
  const [logs, setLogs] = useState<Record<string, string[] | string>>({})
  const [loading, setLoading] = useState(false)
  const load = useCallback(() => {
    setLoading(true)
    Promise.allSettled(items.map((c) => api.system.logs(c.name, COMBINED_TAIL))).then((rs) => {
      const next: Record<string, string[] | string> = {}
      rs.forEach((r, i) => { next[items[i].name] = r.status === 'fulfilled' ? r.value.lines : String(r.reason) })
      setLogs(next)
      setLoading(false)
    })
  }, [items])
  useEffect(() => { load() }, [label]) // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <Link to={`/logs/${group}`} className="md:hidden rounded p-1 text-slate-400 hover:bg-slate-800" aria-label="Zur Liste">
          <ChevronLeft size={16} />
        </Link>
        <span className="text-sm font-semibold text-white">{label}</span>
        <span className="text-xs text-slate-500">je Container die letzten {COMBINED_TAIL} Zeilen</span>
        <button onClick={load} disabled={loading} className="ml-auto rounded border border-slate-700 px-2 py-1 text-xs text-slate-300 hover:bg-slate-800">
          <RefreshCw size={12} className={loading ? 'animate-spin' : ''} />
        </button>
      </div>
      {items.map((c) => {
        const v = logs[c.name]
        return (
          <div key={c.name} className="rounded border border-slate-800 bg-slate-950">
            <Link to={`/logs/${group}/${encodeURIComponent(c.name)}`}
                  className="flex items-center gap-2 border-b border-slate-800 px-2 py-1 text-xs text-slate-300 hover:text-white">
              <Dot state={c.state} /> {c.name} <span className="text-slate-600">{c.status}</span>
            </Link>
            <div className="p-2 font-mono text-[11px] leading-[1.35]">
              {v === undefined ? <span className="text-slate-600">…</span>
                : typeof v === 'string' ? <span className="text-red-300">{v}</span>
                : v.length === 0 ? <span className="text-slate-600">Keine Log-Zeilen.</span>
                : v.map((l, i) => <div key={i} className={`whitespace-pre-wrap break-all ${lineClass(l)}`}>{l}</div>)}
            </div>
          </div>
        )
      })}
    </div>
  )
}
