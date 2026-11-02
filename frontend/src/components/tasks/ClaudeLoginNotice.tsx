import { useEffect, useState } from 'react'
import { api, type ClaudeLoginState, type Task } from '../../api/client'

const ACTIVE = new Set(['starting', 'awaiting_code', 'verifying'])
const BUTTON = 'rounded border border-amber-700 px-3 py-1.5 text-xs text-amber-200 hover:bg-amber-900/30 disabled:opacity-50'

export default function ClaudeLoginNotice({ task, onRequired }: { task: Task; onRequired?: (required: boolean) => void }) {
  const [required, setRequired] = useState(false)
  const [login, setLogin] = useState<ClaudeLoginState>({ status: 'idle' })
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [submitted, setSubmitted] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    setRequired(false)
    onRequired?.(false)
    setLogin({ status: 'idle' })
    setCode('')
    setError('')
    setSubmitted(false)
    if (task.status === 'failed') {
      api.tasks.authError(task.id).then(async (diagnosis) => {
        if (cancelled) return
        setRequired(diagnosis.required)
        onRequired?.(diagnosis.required)
        if (diagnosis.required) {
          const state = await api.tasks.claudeLoginStatus()
          if (!cancelled && ACTIVE.has(state.status)) setLogin(state)
        }
      }).catch(() => {
        // The task error remains visible even if diagnosis is unavailable.
      })
    }
    return () => { cancelled = true }
  }, [task.id, task.status, task.updated_at, onRequired])

  useEffect(() => {
    if (!ACTIVE.has(login.status)) return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        const state = await api.tasks.claudeLoginStatus()
        if (cancelled) return
        setLogin(state)
        setError('')
        if (!ACTIVE.has(state.status)) {
          setCode('')
          setSubmitted(false)
          return
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e))
      }
      if (!cancelled) timer = setTimeout(poll, 1500)
    }
    timer = setTimeout(poll, 1000)
    return () => { cancelled = true; clearTimeout(timer) }
  }, [login.id, login.status])

  async function start() {
    setBusy(true); setError(''); setCode(''); setSubmitted(false)
    try { setLogin(await api.tasks.startClaudeLogin()) }
    catch (e) { setError(e instanceof Error ? e.message : String(e)) }
    finally { setBusy(false) }
  }

  async function submit() {
    if (!login.id) return
    setBusy(true); setError('')
    const confirmation = code.trim()
    setCode('')
    try {
      await api.tasks.submitClaudeLoginCode(login.id, confirmation)
      setSubmitted(true)
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
    finally { setBusy(false) }
  }

  async function cancel() {
    if (!login.id) return
    setBusy(true); setError(''); setCode('')
    try { await api.tasks.cancelClaudeLogin(login.id) }
    catch (e) { setError(e instanceof Error ? e.message : String(e)) }
    finally { setBusy(false) }
  }

  if (!required) return null
  return (
    <div id={`task-login-${task.id}`} className="mt-2 space-y-2 rounded border border-amber-800 bg-amber-950/30 p-3 text-xs text-amber-100" role="status">
      <div className="font-semibold">Claude-Anmeldung abgelaufen oder ungültig</div>
      <p>Der letzte Lauf konnte sich bei Claude nicht anmelden. Bitte erneut anmelden und anschließend {task.mode === 'pipeline' ? 'die fehlgeschlagene Stufe mit „Retry“ wiederholen' : 'den fehlgeschlagenen Lauf erneut starten'}.</p>
      {login.message && <p>{login.message}</p>}
      {login.status === 'success' ? (
        <p className="text-green-300">Anmeldung erfolgreich. Du kannst den Lauf jetzt wiederholen.</p>
      ) : ACTIVE.has(login.status) ? (
        <div className="space-y-2">
          {login.url && (
            <a href={login.url} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer" className={`${BUTTON} inline-block`}>Bei Claude im Browser anmelden ↗</a>
          )}
          {login.status === 'awaiting_code' && !submitted && (
            <form onSubmit={(event) => { event.preventDefault(); void submit() }} className="flex flex-wrap gap-2">
              <input type="password" value={code} onChange={(event) => setCode(event.target.value)}
                autoComplete="off" spellCheck={false} maxLength={2048} aria-label="Bestätigungscode aus der Claude-Anmeldung"
                placeholder="Bestätigungscode aus dem Browser" className="min-w-64 rounded border border-slate-700 bg-slate-950 px-2 py-1.5 text-slate-100" />
              <button type="submit" disabled={busy || !code.trim()} className={BUTTON}>Anmeldung abschließen</button>
            </form>
          )}
          {submitted && <p>Bestätigungscode wird verarbeitet …</p>}
          <button onClick={() => void cancel()} disabled={busy} className={BUTTON}>Abbrechen</button>
        </div>
      ) : (
        <button onClick={() => void start()} disabled={busy} className={BUTTON}>{busy ? 'Anmeldung startet …' : 'Claude erneut anmelden'}</button>
      )}
      {error && <p className="text-red-300" role="alert">{error}</p>}
      <details className="text-slate-400">
        <summary className="cursor-pointer">Alternativ im Terminal anmelden</summary>
        <pre className="mt-1 whitespace-pre-wrap break-words">claude auth login --claudeai   # im Worker-Container (docker exec -it …) bzw. lokal im Terminal</pre>
      </details>
    </div>
  )
}
