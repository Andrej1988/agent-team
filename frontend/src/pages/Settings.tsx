import { useEffect, useState } from 'react'
import { api, type ClaudeAuth, type DeployStatus, type InboxChannel, type InboxMode } from '../api/client'
import { pillCls, PillRow } from '../components/nav/Pills'
import { BudgetSettings } from '../components/Budget'

function b64ToBytes(s: string): ArrayBuffer {
  const p = '='.repeat((4 - (s.length % 4)) % 4)
  const raw = atob((s + p).replace(/-/g, '+').replace(/_/g, '/'))
  return Uint8Array.from(raw, (c) => c.charCodeAt(0)).buffer as ArrayBuffer
}

const ACTIVE = new Set(['starting', 'awaiting_code', 'verifying'])

function ClaudeLogin() {
  const [auth, setAuth] = useState<ClaudeAuth | null>(null)
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const load = (fresh = false) => api.claudeAuth.status(fresh).then(setAuth, (e) => setErr(String(e)))
  useEffect(() => { load(true) }, [])
  const login = auth?.login
  useEffect(() => {
    if (!login || !ACTIVE.has(login.status)) return
    const id = window.setInterval(() => load(true), 2000)
    return () => window.clearInterval(id)
  }, [login?.status])
  const start = async () => {
    setBusy(true); setErr(null)
    try { await api.claudeAuth.start(); await load(true) } catch (e) { setErr(String(e)) } finally { setBusy(false) }
  }
  const submit = async () => {
    if (!login?.id) return
    setBusy(true); setErr(null)
    try { await api.claudeAuth.code(login.id, code.trim()); setCode(''); await load(true) } catch (e) { setErr(String(e)) } finally { setBusy(false) }
  }
  const btn = 'rounded-lg bg-sky-600 px-4 py-2 text-white hover:bg-sky-500 disabled:opacity-50'
  return (
    <section className="rounded-lg border border-slate-700 bg-slate-800/60 p-4">
      <h2 className="font-semibold text-slate-100">Claude-Anmeldung</h2>
      <p className="mt-1 text-sm text-slate-400">Gemeinsame Anmeldung aller Agenten (Chat-Kanäle, Task-Worker, Watchdog, Scouts).</p>
      {!auth ? <div className="mt-2 text-sm text-slate-400">Lade …</div> : (
        <div className="mt-2 text-sm">
          {auth.logged_in === true && <div className="text-emerald-300">✓ angemeldet{auth.email ? ` als ${auth.email}` : ''}</div>}
          {auth.logged_in === false && <div className="text-rose-300">✗ abgemeldet — alle Agenten stehen</div>}
          {auth.logged_in == null && <div className="text-amber-300">Status unbekannt ({auth.error ?? 'Chat-Dienst?'})</div>}
          {login && login.message && login.status !== 'idle' && <div className="mt-1 text-slate-300">{login.message}</div>}
          {login?.status === 'awaiting_code' && login.url && (
            <div className="mt-2 space-y-2">
              <a href={login.url} target="_blank" rel="noreferrer" className="inline-block rounded bg-slate-700 px-3 py-2 text-sky-300 underline">1. Bei Claude anmelden (neuer Tab)</a>
              <div className="flex gap-2">
                <input value={code} onChange={(e) => setCode(e.target.value)} placeholder="2. Bestätigungscode einfügen"
                  className="flex-1 rounded border border-slate-600 bg-slate-900 px-2 py-2" autoComplete="off" />
                <button disabled={busy || !code.trim()} onClick={submit} className={btn}>Senden</button>
              </div>
            </div>
          )}
          {(!login || !ACTIVE.has(login.status)) && (
            <div className="mt-3 flex gap-2">
              <button disabled={busy} onClick={start} className={btn}>{auth.logged_in ? 'Neu anmelden' : 'Anmelden'}</button>
              <button onClick={() => load(true)} className="rounded-lg border border-slate-600 px-4 py-2 text-slate-200">Status prüfen</button>
            </div>
          )}
          {login && ACTIVE.has(login.status) && (
            <button onClick={() => api.claudeAuth.cancel().then(() => load(true))} className="mt-2 text-xs text-slate-400 underline">Anmeldung abbrechen</button>
          )}
        </div>
      )}
      {err && <div className="mt-2 text-xs text-rose-300">{err}</div>}
    </section>
  )
}

// Deploy button: agents cannot redeploy their own services (they only reach
// Docker through a restricting proxy), so the user does it here. A host-side
// deployer builds exactly the commit shown and restarts web, workers and chat.
function DeploySection() {
  const [st, setSt] = useState<DeployStatus | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [confirm, setConfirm] = useState(false)
  const [showLog, setShowLog] = useState(false)
  const load = () => api.deploy.status().then((s) => { setSt(s); setErr(null) }, (e) => setErr(String(e)))
  useEffect(() => { load() }, [])
  const running = st?.job?.state === 'running' || !!st?.queued
  useEffect(() => {
    if (!running) return
    const id = window.setInterval(load, 3000)
    return () => window.clearInterval(id)
  }, [running])
  if (!st || !st.enabled) return null
  const p = st.pending
  const upToDate = !!p && p.deployed === p.head
  const busy = st.busy ?? []
  const start = async () => {
    if (!p) return
    setErr(null)
    try { await api.deploy.start(p.head); setConfirm(false); setShowLog(true); await load() } catch (e) { setErr(String(e)) }
  }
  const job = st.job
  const when = (t?: number) => (t ? new Date(t * 1000).toLocaleString('de-DE') : '')
  return (
    <section className="rounded-lg border border-slate-700 bg-slate-800/60 p-4">
      <h2 className="font-semibold text-slate-100">Agenten-Team deployen</h2>
      <p className="mt-1 text-sm text-slate-400">
        Baut den angezeigten Commit und startet {(st.services ?? []).join(', ') || 'die Dienste'} neu.
        Laufende Chat-Antworten und Task-Stufen werden dabei abgebrochen.
      </p>
      {st.error && <div className="mt-2 text-xs text-rose-300">Deployer: {st.error}</div>}
      {!p ? <div className="mt-2 text-sm text-amber-300">Noch kein Status vom Deployer.</div> : (
        <div className="mt-2 space-y-2 text-sm">
          <div className="text-slate-300">
            Live: <code className="text-slate-100">{p.deployed?.slice(0, 10) ?? '—'}</code>
            {' · '}{p.branch}: <code className="text-slate-100">{p.head.slice(0, 10)}</code> {p.head_subject}
          </div>
          {upToDate ? <div className="text-emerald-300">✓ Live-Stand ist aktuell</div> : (
            <>
              <div className="text-slate-300">{p.commits.length} neue Commit(s):</div>
              <ul className="max-h-48 overflow-auto rounded bg-slate-900/60 p-2 text-xs text-slate-300">
                {p.commits.map((c) => <li key={c.sha}><code>{c.sha}</code> {c.subject} <span className="text-slate-500">({c.author})</span></li>)}
              </ul>
              {p.files.length > 0 && (
                <details className="text-xs text-slate-400"><summary>Geänderte Dateien</summary>
                  <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap">{p.files.join('\n')}</pre></details>
              )}
            </>
          )}
          {busy.length > 0 && (
            <div className="rounded border border-amber-700 bg-amber-900/30 p-2 text-xs text-amber-200">
              Gerade aktiv (wird abgebrochen): {busy.map((b) => `${b.label}${b.kind === 'task' ? ' (Task-Stufe)' : ''}`).join(', ')}
            </div>
          )}
          {!running && !confirm && (
            <button onClick={() => setConfirm(true)} className="rounded-lg bg-sky-600 px-4 py-2 text-white hover:bg-sky-500">
              {upToDate ? 'Trotzdem neu deployen' : 'Deployen'}
            </button>
          )}
          {!running && confirm && (
            <div className="flex flex-wrap gap-2">
              <button onClick={start} className="rounded-lg bg-rose-600 px-4 py-2 text-white hover:bg-rose-500">
                Ja, {p.head.slice(0, 10)} jetzt deployen
              </button>
              <button onClick={() => setConfirm(false)} className="rounded-lg border border-slate-600 px-4 py-2 text-slate-200">Abbrechen</button>
            </div>
          )}
        </div>
      )}
      {st.queued && !job?.state && <div className="mt-2 text-sm text-sky-300">Auftrag wartet auf den Deployer …</div>}
      {job && (
        <div className="mt-3 text-sm">
          <div className={job.state === 'failed' ? 'text-rose-300' : job.state === 'done' ? 'text-emerald-300' : 'text-sky-300'}>
            {job.state === 'running' ? '⏳ Deploy läuft' : job.state === 'done' ? '✓ Zuletzt deployt' : '✗ Deploy fehlgeschlagen'}
            {' '}<code>{job.commit?.slice(0, 10)}</code> · {when(job.finished ?? job.started)} · {job.by}
          </div>
          <button onClick={() => setShowLog(!showLog)} className="text-xs text-slate-400 underline">{showLog ? 'Log ausblenden' : 'Log anzeigen'}</button>
          {showLog && <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-slate-900/60 p-2 text-xs text-slate-400">{(job.log ?? []).slice(-80).join('\n')}</pre>}
        </div>
      )}
      {err && <div className="mt-2 text-xs text-rose-300">{err}</div>}
    </section>
  )
}

const MODE_LABEL: Record<InboxMode, string> = { important: 'Wichtiges', all: 'Alles', none: 'Nichts' }

// Per chat channel: what of it lands in the inbox (and is pushed).
function InboxChannels() {
  const [chans, setChans] = useState<InboxChannel[] | null>(null)
  const [error, setError] = useState('')
  useEffect(() => { api.inboxChannels().then(setChans, (e) => setError(String(e))) }, [])
  const set = async (id: string, mode: InboxMode) => {
    try {
      await api.setInboxChannel(id, mode)
      setChans((cur) => (cur ?? []).map((c) => (c.id === id ? { ...c, mode } : c)))
    } catch (e) { setError(String(e)) }
  }
  const byProject = new Map<string, InboxChannel[]>()
  for (const c of chans ?? []) byProject.set(c.project_name, [...(byProject.get(c.project_name) ?? []), c])
  return (
    <section className="rounded-lg border border-slate-700 bg-slate-800/60 p-4">
      <h2 className="font-semibold text-slate-100">Inbox je Kanal</h2>
      <p className="mt-1 text-sm text-slate-400">
        Fragen und Freigaben kommen immer. „Wichtiges“: als wichtig markierte Meldungen, Karten mit Knöpfen und
        Antworten auf deine eigene Nachricht — Routine (Laufberichte, Scout-Vorschläge, Log-Fehler) bleibt im Chat.
        Wiederholungen derselben Meldung erscheinen einmal mit Zähler.
      </p>
      {error && <div className="mt-2 text-xs text-rose-300">{error}</div>}
      {chans === null ? <div className="mt-2 text-sm text-slate-500">Lade …</div> : (
        <div className="mt-3 space-y-3">
          {[...byProject.entries()].map(([proj, list]) => (
            <div key={proj}>
              <div className="mb-1 text-xs uppercase tracking-wide text-slate-500">{proj}</div>
              {list.map((c) => (
                <div key={c.id} className="flex items-center gap-2 py-1">
                  <span className="min-w-0 flex-1 truncate text-sm text-slate-200" title={c.id}>{c.label}</span>
                  <PillRow label={`Inbox ${c.label}`}>
                    {(['important', 'all', 'none'] as InboxMode[]).map((m) => (
                      <button key={m} className={pillCls(c.mode === m)} onClick={() => set(c.id, m)}>{MODE_LABEL[m]}</button>
                    ))}
                  </PillRow>
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

export default function Settings() {
  const [user, setUser] = useState('')
  const [log, setLog] = useState<string[]>([])
  const add = (m: string) => setLog((l) => [...l, m])

  useEffect(() => {
    api.me().then((m) => setUser(m.user), () => {})
    const standalone = window.matchMedia('(display-mode: standalone)').matches
      || (navigator as unknown as { standalone?: boolean }).standalone
    add(`Modus: ${standalone ? 'installierte App' : 'Browser-Tab'}`)
    add(`Push-API: ${'PushManager' in window ? 'ja' : 'nein'}, Berechtigung: ${window.Notification ? Notification.permission : '—'}`)
  }, [])

  const subscribe = async () => {
    try {
      const perm = await Notification.requestPermission()
      add(`Berechtigung: ${perm}`)
      if (perm !== 'granted') return
      const reg = await navigator.serviceWorker.ready
      const { key } = await api.push.key()
      const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(key) })
      const r = await api.push.subscribe(sub.toJSON())
      add(`Abonniert (${r.subscriptions} Gerät(e) gesamt)`)
    } catch (e) {
      add(`Fehler: ${e}`)
    }
  }
  const test = (delay: number) => async () => {
    try { add(JSON.stringify(await api.push.test(delay))) } catch (e) { add(`Fehler: ${e}`) }
  }

  const btn = 'w-full rounded-lg bg-sky-600 px-4 py-2.5 text-white hover:bg-sky-500 sm:w-auto'
  return (
    <div className="mx-auto max-w-xl space-y-4">
      <div className="text-sm text-slate-400">Angemeldet als {user || '…'}</div>
      <ClaudeLogin />
      <BudgetSettings />
      <DeploySection />
      <InboxChannels />
      <section className="rounded-lg border border-slate-700 bg-slate-800/60 p-4">
        <h2 className="font-semibold text-slate-100">Benachrichtigungen</h2>
        <p className="mt-1 text-sm text-slate-400">
          Push bei neuen Inbox-Einträgen (Rückfragen, Freigaben, Probleme). Pro Gerät einmal aktivieren —
          auf dem iPhone nur in der installierten App.
        </p>
        <div className="mt-3 flex flex-col gap-2 sm:flex-row">
          <button className={btn} onClick={subscribe}>Benachrichtigungen aktivieren</button>
          <button className={btn} onClick={test(0)}>Test sofort</button>
          <button className={btn} onClick={test(15)}>Test in 15 s</button>
        </div>
        <pre className="mt-3 whitespace-pre-wrap break-words text-xs text-slate-400">{log.join('\n')}</pre>
      </section>
    </div>
  )
}
