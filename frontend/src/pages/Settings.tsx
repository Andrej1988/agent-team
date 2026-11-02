import { useEffect, useState } from 'react'
import { api, type ClaudeAuth } from '../api/client'
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
