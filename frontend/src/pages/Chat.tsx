import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  Archive, ArrowLeft, Check, ChevronDown, ChevronRight, CircleHelp, Copy, ImagePlus, Search, Loader2, Quote, RotateCcw, Send,
  Settings2, ShieldAlert, Square, Trash2, TriangleAlert, Wrench, X,
} from 'lucide-react'
import { api, type CatalogModel, type ChatChannel, type ChatEvent, type ChatQuestion, type ServiceButton, type ServiceChannel, type Task } from '../api/client'

// ---- helpers -----------------------------------------------------------------

function hhmm(ts: string): string {
  const d = new Date(ts)
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' })
}

function fileToDataUrl(f: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader()
    r.onload = () => resolve(String(r.result))
    r.onerror = () => reject(r.error)
    r.readAsDataURL(f)
  })
}

type Block =
  | { kind: 'event'; ev: ChatEvent; tools: ChatEvent[] }
  | { kind: 'tools'; events: ChatEvent[] }

// Work in progress: tool calls, their results and background-task notes.
const ACTIVITY = new Set<ChatEvent['type']>(['tool_use', 'tool_result', 'bg'])

// All activity between two messages folds into one block that hangs on the
// message above it and is deleted with it. Only activity before the first
// message (start of the log) stands alone.
function toBlocks(events: ChatEvent[]): Block[] {
  const out: Block[] = []
  for (const ev of events) {
    if (ev.type === 'busy' || ev.type === 'turn_end' || ev.type === 'resolved') continue
    if (ACTIVITY.has(ev.type)) {
      const last = out[out.length - 1]
      if (last && last.kind === 'event') last.tools.push(ev)
      else if (last && last.kind === 'tools') last.events.push(ev)
      else out.push({ kind: 'tools', events: [ev] })
    } else {
      out.push({ kind: 'event', ev, tools: [] })
    }
  }
  return out
}

// ---- pieces ------------------------------------------------------------------

function ToolsBlock({ events, onDelete }: { events: ChatEvent[]; onDelete: (seqs: number[]) => void }) {
  const [open, setOpen] = useState(false)
  const calls = events.filter((e) => e.type === 'tool_use')
  const bg = events.filter((e) => e.type === 'bg').length
  const errors = events.filter((e) => e.type === 'tool_result' && e.error).length
  return (
    <div className="my-1 text-xs text-slate-400">
      <button onClick={() => setOpen(!open)} className="flex min-w-0 max-w-full items-center gap-1 py-1 hover:text-slate-200">
        {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        <Wrench size={12} />
        <span className="shrink-0">
          {calls.length > 0 && `${calls.length} Werkzeugaufruf${calls.length === 1 ? '' : 'e'}`}
          {calls.length > 0 && bg > 0 && ' · '}
          {bg > 0 && `${bg} Hintergrund`}
          {calls.length === 0 && bg === 0 && 'Ergebnisse'}
        </span>
        {calls.length > 0 && <span className="min-w-0 truncate text-slate-500">· {calls[calls.length - 1].text}</span>}
        {errors > 0 && <span className="shrink-0 text-rose-400">· {errors} Fehler</span>}
      </button>
      {open && (
        <>
          <div className="mt-1 space-y-0.5 border-l border-slate-700 pl-2 font-mono text-[11px]">
            {events.map((e) => (
              <div key={e.seq} className={`whitespace-pre-wrap break-all ${e.type === 'tool_use' ? 'text-sky-300' : e.type === 'bg' ? 'italic text-slate-500' : e.error ? 'text-rose-300' : 'text-slate-500'}`}>
                {e.type === 'tool_use' ? `→ ${e.text}` : e.type === 'bg' ? e.text : `← ${e.text || '(leer)'}`}
              </div>
            ))}
          </div>
          <button onClick={() => onDelete(events.map((e) => e.seq))} className="mt-1 inline-flex items-center gap-1.5 rounded border border-slate-700 px-2.5 py-1.5 text-xs text-slate-400 hover:border-rose-500/60 hover:text-rose-300">
            <Trash2 size={13} /> Nur diese Werkzeugaufrufe löschen
          </button>
        </>
      )}
    </div>
  )
}

function PermissionCard({ ev, open, channel, onDelete }: { ev: ChatEvent; open: boolean; channel: string; onDelete: () => void }) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const decide = async (d: 'once' | 'always' | 'deny') => {
    setBusy(true)
    try { await api.chat.permission(channel, ev.req_id!, d) } catch (e) { setErr(String(e)) } finally { setBusy(false) }
  }
  return (
    <div className={`my-2 rounded-lg border p-3 ${open ? 'border-sky-500/60 bg-sky-500/10' : 'border-slate-700 bg-slate-800/40'}`}>
      <div className="flex items-center gap-1.5 text-sm font-medium text-sky-200"><ShieldAlert size={15} /> Freigabe</div>
      <div className="mt-1 text-sm text-slate-200">{ev.title}</div>
      {ev.detail && <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded bg-slate-900/70 p-2 text-[11px] text-slate-300">{ev.detail}</pre>}
      {ev.blocked_path && <div className="mt-1 text-xs text-amber-300">Pfad außerhalb: {ev.blocked_path}</div>}
      {ev.rules && ev.rules.length > 0 && <div className="mt-1 text-xs text-slate-400">„Immer“ erlaubt: {ev.rules.join(', ')}</div>}
      {open ? (
        <div className="mt-2 flex flex-wrap gap-2">
          <button disabled={busy} onClick={() => decide('once')} className="rounded bg-emerald-600 px-3 py-1.5 text-sm text-white disabled:opacity-50">Einmal erlauben</button>
          {ev.can_always && <button disabled={busy} onClick={() => decide('always')} className="rounded bg-emerald-800 px-3 py-1.5 text-sm text-white disabled:opacity-50">Immer erlauben</button>}
          <button disabled={busy} onClick={() => decide('deny')} className="rounded bg-rose-700 px-3 py-1.5 text-sm text-white disabled:opacity-50">Ablehnen</button>
        </div>
      ) : (
        // Only resolved requests can be deleted (the server refuses open ones).
        <div className="mt-1 flex items-center justify-between text-xs text-slate-400">
          <span>— {ev.outcome ?? 'erledigt'}</span>
          <DeleteButton onClick={onDelete} />
        </div>
      )}
      {err && <div className="mt-1 text-xs text-rose-300">{err}</div>}
    </div>
  )
}

function QuestionCard({ ev, open, channel, onDelete }: { ev: ChatEvent; open: boolean; channel: string; onDelete: () => void }) {
  const qs: ChatQuestion[] = ev.questions ?? []
  const [sel, setSel] = useState<number[][]>(() => qs.map(() => []))
  const [texts, setTexts] = useState<string[]>(() => qs.map(() => ''))
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const toggle = (qi: number, oi: number, multi: boolean) =>
    setSel((s) => s.map((x, i) => (i !== qi ? x : multi ? (x.includes(oi) ? x.filter((y) => y !== oi) : [...x, oi]) : [oi])))
  const ready = qs.every((_, i) => sel[i].length > 0 || texts[i].trim())
  const submit = async () => {
    setBusy(true)
    try {
      await api.chat.question(channel, ev.req_id!, qs.map((_, i) => ({ selected: sel[i], text: texts[i].trim() || undefined })))
    } catch (e) { setErr(String(e)) } finally { setBusy(false) }
  }
  return (
    <div className={`my-2 rounded-lg border p-3 ${open ? 'border-amber-500/60 bg-amber-500/10' : 'border-slate-700 bg-slate-800/40'}`}>
      <div className="flex items-center gap-1.5 text-sm font-medium text-amber-200"><CircleHelp size={15} /> Frage</div>
      {qs.map((q, qi) => (
        <div key={qi} className="mt-2">
          {q.header && <div className="text-[11px] uppercase tracking-wide text-slate-400">{q.header}</div>}
          <div className="text-sm text-slate-100">{q.question}</div>
          <div className="mt-1.5 space-y-1.5">
            {q.options.map((o, oi) => {
              const on = sel[qi]?.includes(oi)
              return (
                <button key={oi} disabled={!open} onClick={() => toggle(qi, oi, q.multi)}
                  className={`block w-full rounded border px-2.5 py-1.5 text-left text-sm ${on ? 'border-amber-400 bg-amber-400/20 text-white' : 'border-slate-600 text-slate-200'} disabled:opacity-60`}>
                  <span className="font-medium">{o.label}</span>
                  {o.description && <span className="block text-xs text-slate-400">{o.description}</span>}
                </button>
              )
            })}
          </div>
          {open && (
            <input value={texts[qi]} onChange={(e) => setTexts((t) => t.map((x, i) => (i === qi ? e.target.value : x)))}
              placeholder="Eigene Antwort (optional)" className="mt-1.5 w-full rounded border border-slate-600 bg-slate-900 px-2 py-1.5 text-sm" />
          )}
        </div>
      ))}
      {open ? (
        <button disabled={!ready || busy} onClick={submit} className="mt-2 rounded bg-amber-600 px-3 py-1.5 text-sm text-white disabled:opacity-50">Antworten</button>
      ) : (
        // Only resolved requests can be deleted (the server refuses open ones).
        <div className="mt-1 flex items-center justify-between text-xs text-slate-400">
          <span>— {ev.outcome ?? 'erledigt'}</span>
          <DeleteButton onClick={onDelete} />
        </div>
      )}
      {err && <div className="mt-1 text-xs text-rose-300">{err}</div>}
    </div>
  )
}

// Padded so the tap target is finger-sized; the negative margin keeps the layout tight.
const INPUT_MAX_PX = 200

// Button names for the regular duties (scout ids in team.yaml).
const DUTY_LABEL: Record<string, string> = {
  research: 'Research', reliability: 'Reliability', 'order-ids': 'Order-IDs', tests: 'Tests',
  'data-quality': 'Datenqualität', logging: 'Logging', deps: 'Abhängigkeiten', conventions: 'Konventionen',
  backtest: 'Backtest-Audit', watchdog: 'Auswertung', 'product-owner': 'Produkt-Durchsicht',
  'qa-tester': 'App-Test', legal: 'Rechtsprüfung', review: 'Review',
}

// A lead card's task no longer waits for a decision (started from elsewhere,
// finished, closed, parked, deleted — e.g. in the chat with the lead): what
// happened to it, instead of buttons that 409.
function taskMovedOn(t: Task): string | null {
  if (t.status === 'done') return `#${t.id} schon erledigt`
  if (t.status === 'closed') return `#${t.id} geschlossen`
  if (t.status === 'backlog') return `#${t.id} im Backlog`
  if (t.mode === 'pipeline') return `#${t.id} läuft bereits`
  return null
}
// The task a lead card is about: proposal (start:<id>) or tidy card
// (park/close/done/delete/keep:<id>, merge:<id>:<into>).
const CARD_TASK = /^(?:start|park|close|done|delete|keep|merge):(\d+)/

// Clipboard API needs a secure context; the textarea trick covers the rest.
async function copyText(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    document.execCommand('copy')
    ta.remove()
  }
}

function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState(false)
  const copy = async () => {
    await copyText(text)
    setDone(true)
    window.setTimeout(() => setDone(false), 1500)
  }
  return (
    <button onClick={copy} className="flex items-center gap-1 rounded border border-slate-600 px-2 py-1 text-xs text-slate-300 hover:border-sky-500/60 hover:text-sky-200">
      {done ? <><Check size={12} /> Kopiert</> : <><Copy size={12} /> Kopieren</>}
    </button>
  )
}

function DeleteButton({ onClick, className = '' }: { onClick: () => void; className?: string }) {
  return (
    <button onClick={onClick} className={`-m-2 p-2 opacity-70 hover:text-rose-300 hover:opacity-100 ${className}`} aria-label="Löschen" title="Löschen">
      <Trash2 size={14} />
    </button>
  )
}

function EventView({ ev, open, channel, onQuote, onDelete, onButton, pressed, done, onCheck }: {
  ev: ChatEvent; open: boolean; channel: string; onQuote: (t: string) => void; onDelete: (seqs: number[]) => void
  onButton?: (ev: ChatEvent, b: ServiceButton) => void; pressed?: string; done?: string
  onCheck?: (ev: ChatEvent) => void
}) {
  const del = () => onDelete([ev.seq])
  switch (ev.type) {
    case 'command':
      // My input to the program behind a service channel (typed or a button).
      return (
        <div className="my-2 flex justify-end">
          <div className="max-w-[85%] rounded-2xl rounded-br-sm bg-slate-700 px-3 py-2 text-sm text-white">
            <div className="whitespace-pre-wrap break-words">{ev.label ? `▸ ${ev.label}` : ev.text}</div>
            <div className="mt-0.5 flex items-center justify-end gap-2 text-[10px] text-slate-300/60">
              <DeleteButton onClick={del} />
              {hhmm(ev.ts)}
            </div>
          </div>
        </div>
      )
    case 'user':
      return (
        <div className="my-2 flex justify-end">
          <div className="max-w-[85%] rounded-2xl rounded-br-sm bg-sky-700 px-3 py-2 text-sm text-white">
            {ev.quote && <div className="mb-1 line-clamp-3 border-l-2 border-sky-300/60 pl-2 text-xs text-sky-100/80">{ev.quote}</div>}
            <div className="whitespace-pre-wrap break-words">{ev.text}</div>
            {ev.images && ev.images.length > 0 && <div className="mt-1 text-xs text-sky-100/80">📎 {ev.images.length} Bild{ev.images.length > 1 ? 'er' : ''}</div>}
            <div className="mt-0.5 flex items-center justify-end gap-2 text-[10px] text-sky-100/60">
              <DeleteButton onClick={del} />
              {hhmm(ev.ts)}
            </div>
          </div>
        </div>
      )
    case 'assistant':
      return (
        <div className="group my-2 flex">
          <div className="max-w-[92%] rounded-2xl rounded-bl-sm bg-slate-800 px-3 py-2 text-sm text-slate-100">
            {/* Task chat: a stage agent's own message names its role. */}
            {ev.author && <div className="mb-0.5 text-[11px] font-medium text-sky-300">{ev.author}</div>}
            <div className="whitespace-pre-wrap break-words">{ev.text}</div>
            <div className="mt-0.5 flex items-center justify-end gap-2 text-[10px] text-slate-500">
              <button onClick={() => onQuote(ev.text ?? '')} className="opacity-60 hover:opacity-100" aria-label="Zitieren"><Quote size={11} /></button>
              <DeleteButton onClick={del} />
              {hhmm(ev.ts)}
            </div>
          </div>
        </div>
      )
    case 'alert': {
      // Pipeline progress / lead cards are status, not warnings.
      const calm = ev.source === 'pipeline' || ev.source === 'lead'
      return (
        <div className={`my-2 rounded-lg border p-3 text-sm ${calm ? 'border-slate-600 bg-slate-800/60' : 'border-amber-500/50 bg-amber-500/10'}`}>
          <div className={`flex min-w-0 flex-wrap items-center gap-1.5 font-medium ${calm ? 'text-slate-100' : 'text-amber-200'}`}>
            {!calm && <TriangleAlert size={15} />} <span className="min-w-0 break-words">{ev.title || 'Meldung'}</span>
            {ev.source && <span className="text-xs font-normal text-amber-200/60">· {ev.source}</span>}
            <span className="ml-auto text-[10px] font-normal text-slate-400">{hhmm(ev.ts)}</span>
          </div>
          <div className="mt-1 whitespace-pre-wrap break-words text-slate-200">{ev.text}</div>
          {done !== undefined && (
            <div className="mt-2 rounded border border-emerald-600/50 bg-emerald-900/20 px-2 py-1 text-xs text-emerald-200">
              ✓ Erledigt{done ? `: ${done}` : ''}
            </div>
          )}
          {ev.buttons && ev.buttons.length > 0 && done === undefined && (
            // One tap per message: after a press the row shows what was chosen.
            <div className="mt-2 space-y-1.5">
              {ev.buttons.map((row, ri) => (
                <div key={ri} className="flex flex-wrap gap-1.5">
                  {row.map((b) => (
                    <button key={b.data} disabled={!!pressed || !onButton} onClick={() => onButton?.(ev, b)}
                      className={`min-h-[40px] flex-1 rounded-lg border px-3 py-2 text-sm ${pressed === b.data
                        ? 'border-sky-400 bg-sky-600 text-white' : 'border-amber-500/60 bg-slate-900 text-amber-100 disabled:opacity-40'}`}>
                      {b.label}
                    </button>
                  ))}
                </div>
              ))}
            </div>
          )}
          <div className="mt-2 flex flex-wrap gap-2">
            {(onButton === undefined || !ev.buttons?.length) && <button onClick={() => onQuote(`${ev.title ? ev.title + '\n' : ''}${ev.text ?? ''}`)} className="flex items-center gap-1 rounded border border-amber-500/50 px-2 py-1 text-xs text-amber-100">
              <Quote size={12} /> Darauf antworten
            </button>}
            {!calm && onCheck && (
              // An agent examines it in a new chat and recommends fix or delete.
              <button onClick={() => onCheck(ev)} className="flex items-center gap-1 rounded border border-sky-600/60 px-2 py-1 text-xs text-sky-200 hover:bg-sky-900/30">
                <Search size={12} /> Prüfen
              </button>
            )}
            <CopyButton text={[ev.title, ev.source ? `Quelle: ${ev.source} · ${hhmm(ev.ts)}` : hhmm(ev.ts), ev.text].filter(Boolean).join('\n')} />
            <button onClick={del} className="flex items-center gap-1 rounded border border-slate-600 px-2 py-1 text-xs text-slate-300 hover:border-rose-500/60 hover:text-rose-200">
              {/* On a card with buttons "Löschen" would read like an answer; this only hides the message. */}
              <Trash2 size={12} /> {ev.buttons?.length ? 'Karte ausblenden' : 'Löschen'}
            </button>
          </div>
        </div>
      )
    }
    case 'permission':
      return <PermissionCard ev={ev} open={open} channel={channel} onDelete={del} />
    case 'question':
      return <QuestionCard ev={ev} open={open} channel={channel} onDelete={del} />
    case 'bg':
      return (
        <div className="my-1 flex items-center gap-2 text-xs italic text-slate-500">
          <span className="min-w-0 flex-1 break-words">{ev.text}</span>
          <DeleteButton onClick={del} />
        </div>
      )
    case 'error':
      return (
        <div className="my-2 flex items-start gap-2 rounded border border-rose-500/50 bg-rose-500/10 px-3 py-2 text-sm text-rose-200">
          <div className="flex-1 whitespace-pre-wrap break-words">
            {ev.text}
            {ev.auth && <Link to="/settings" className="mt-1 block text-sky-300 underline">→ Claude-Anmeldung</Link>}
          </div>
          <DeleteButton onClick={del} className="mt-0.5" />
        </div>
      )
    case 'system':
      return (
        <div className="my-2 flex items-center justify-center gap-3 text-center text-xs text-slate-500">
          <span className="min-w-0 break-words">{ev.text}</span>
          <DeleteButton onClick={del} />
        </div>
      )
    default:
      return null
  }
}

function SettingsPanel({ channel, info, alertCount, onClose, onChanged, onDeleteAlerts, onClear }: {
  channel: string; info: ChatChannel; alertCount: number; onClose: () => void; onChanged: () => void; onDeleteAlerts: () => void; onClear: () => void
}) {
  const live = info.live
  const [backend, setBackend] = useState(live?.backend ?? 'claude')
  // Only the channel's own override; empty = the channel default (placeholder).
  const [model, setModel] = useState(live?.model_override ?? '')
  const [perm, setPerm] = useState(live?.perm ?? 'bypassPermissions')
  const [msg, setMsg] = useState<string | null>(null)
  const [catalog, setCatalog] = useState<Record<string, CatalogModel[]> | null>(null)
  useEffect(() => {
    api.chat.models().then(
      (c) => setCatalog({ claude: c.claude?.models ?? [], codex: c.codex?.models ?? [] }),
      () => setCatalog({}),
    )
  }, [])
  const channelDefault = backend === 'codex' ? live?.defaults.codex_model : live?.defaults.claude_model
  // Claude: store the alias (opus/sonnet/…) so the channel follows new versions.
  const options = (catalog?.[backend] ?? []).filter((m) => !m.disabled)
    .map((m) => ({ value: m.aliases?.[0] ?? m.value, label: `${m.label} — ${m.description}` }))
  const save = async () => {
    try {
      if (backend !== live?.backend) await api.chat.settings(channel, { backend })
      await api.chat.settings(channel, { model, perm })
      setMsg('Gespeichert — gilt ab der nächsten Nachricht.')
      onChanged()
    } catch (e) { setMsg(String(e)) }
  }
  const reset = async () => {
    if (!window.confirm('Sitzung zurücksetzen? Die nächste Nachricht startet ohne bisherigen Kontext.')) return
    try { await api.chat.reset(channel); setMsg('Sitzung zurückgesetzt.'); onChanged() } catch (e) { setMsg(String(e)) }
  }
  const field = 'w-full rounded border border-slate-600 bg-slate-900 px-2 py-1.5 text-sm'
  return (
    <div className="border-b border-slate-800 bg-slate-900 p-3 text-sm">
      <div className="mb-2 flex items-center justify-between">
        <span className="font-medium">Einstellungen</span>
        <button onClick={onClose} aria-label="Schließen"><X size={16} /></button>
      </div>
      {live ? (
        <div className="grid gap-2 sm:grid-cols-3">
          <label>Backend
            <select value={backend} onChange={(e) => { setBackend(e.target.value as 'claude' | 'codex'); setModel('') }} className={field}>
              <option value="claude">Claude</option><option value="codex">Codex</option>
            </select>
          </label>
          <label>Modell
            {catalog === null ? (
              <select disabled className={field}><option>Lade Modelle …</option></select>
            ) : options.length === 0 ? (
              <input value={model} onChange={(e) => setModel(e.target.value)}
                placeholder={channelDefault ?? 'CLI-Default'} className={field} />
            ) : (
              <select value={model} onChange={(e) => setModel(e.target.value)} className={field}>
                <option value="">Kanal-Standard ({channelDefault ?? 'CLI-Default'})</option>
                {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                {model && !options.some((o) => o.value === model) && <option value={model}>{model}</option>}
              </select>
            )}
          </label>
          <label>Rechte
            <select value={perm} onChange={(e) => setPerm(e.target.value)} className={field}>
              <option value="acceptEdits">acceptEdits — Änderungen ja, Riskantes fragt</option>
              <option value="default">default — alles fragt</option>
              <option value="bypassPermissions">bypass — nichts fragt</option>
            </select>
          </label>
        </div>
      ) : <div className="text-slate-400">Chat-Dienst nicht erreichbar.</div>}
      <div className="mt-2 text-xs text-slate-400">
        cwd {info.cwd} · aktiv: {live?.active_model ?? '—'} · Sitzung: {live?.session ? live.session.slice(0, 8) : 'neu'}
      </div>
      <div className="mt-2 flex flex-wrap gap-2">
        <button onClick={save} className="rounded bg-sky-600 px-3 py-1.5 text-white">Speichern</button>
        <button onClick={reset} className="flex items-center gap-1 rounded border border-slate-600 px-3 py-1.5"><RotateCcw size={14} /> Sitzung zurücksetzen</button>
        <button onClick={onClear} className="flex items-center gap-1 rounded border border-slate-600 px-3 py-1.5 hover:border-rose-500/60">
          <Trash2 size={14} /> Verlauf leeren
        </button>
        {alertCount > 0 && (
          <button onClick={onDeleteAlerts} className="flex items-center gap-1 rounded border border-slate-600 px-3 py-1.5 hover:border-rose-500/60">
            <Trash2 size={14} /> Alle {alertCount} Meldungen löschen
          </button>
        )}
      </div>
      {msg && <div className="mt-2 text-xs text-slate-300">{msg}</div>}
    </div>
  )
}

// ---- page --------------------------------------------------------------------

export default function Chat() {
  const { channel = '' } = useParams()
  const [info, setInfo] = useState<ChatChannel | null>(null)
  const [events, setEvents] = useState<ChatEvent[]>([])
  const [error, setError] = useState<string | null>(null)
  const [text, setText] = useState('')
  const [quote, setQuote] = useState<string | null>(null)
  const [images, setImages] = useState<{ name: string; data: string }[]>([])
  const [sending, setSending] = useState(false)
  const [showSettings, setShowSettings] = useState(false)
  const bottomRef = useRef<HTMLDivElement | null>(null)
  const scrollerRef = useRef<HTMLDivElement | null>(null)
  const followRef = useRef(true)
  const fileRef = useRef<HTMLInputElement | null>(null)
  const inputRef = useRef<HTMLTextAreaElement | null>(null)
  // The input grows with its text; only a very long message scrolls inside it.
  useLayoutEffect(() => {
    const el = inputRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight + 2, INPUT_MAX_PX)}px`
    el.style.overflowY = el.scrollHeight + 2 > INPUT_MAX_PX ? 'auto' : 'hidden'
  }, [text])
  // Seqs deleted in this view; ignored if the live stream delivers them again.
  const goneRef = useRef<Set<number>>(new Set())

  const loadInfo = useCallback(() => api.chat.info(channel).then(setInfo, (e) => setError(String(e))), [channel])

  // History, then live tail.
  useEffect(() => {
    let es: EventSource | null = null
    let cancelled = false
    setEvents([])
    setError(null)
    loadInfo()
    api.chat.events(channel, 0, 600).then((evs) => {
      if (cancelled) return
      setEvents(evs)
      const last = evs.length ? evs[evs.length - 1].seq : 0
      es = new EventSource(api.chat.streamUrl(channel, last))
      es.onmessage = (m) => {
        const ev = JSON.parse(m.data) as ChatEvent
        if (goneRef.current.has(ev.seq)) return
        setEvents((prev) => (prev.length && prev[prev.length - 1].seq >= ev.seq ? prev : [...prev, ev]))
        // Pipeline alerts (stage started/finished) change the task's status.
        if (ev.type === 'busy' || ev.type === 'system' || (ev.type === 'alert' && ev.source === 'pipeline')) loadInfo()
      }
    }, (e) => setError(String(e)))
    return () => { cancelled = true; es?.close() }
  }, [channel, loadInfo])

  // Task chat: the worker's progress only shows in the task status — poll it.
  const isTaskChat = info?.task_id != null
  useEffect(() => {
    if (!isTaskChat) return
    const id = window.setInterval(loadInfo, 15_000)
    return () => window.clearInterval(id)
  }, [isTaskChat, loadInfo])

  // Read marker: while the page is visible, everything shown counts as read
  // (also tells the inbox not to push replies for this channel).
  const lastSeq = events.length ? events[events.length - 1].seq : 0
  useEffect(() => {
    const mark = () => { if (document.visibilityState === 'visible') api.chat.read(channel, lastSeq).catch(() => {}) }
    mark()
    const id = window.setInterval(mark, 30_000)
    document.addEventListener('visibilitychange', mark)
    return () => { window.clearInterval(id); document.removeEventListener('visibilitychange', mark) }
  }, [channel, lastSeq])

  // Scroll only the message list (scrollIntoView would also scroll the window).
  useLayoutEffect(() => {
    const el = scrollerRef.current
    if (followRef.current && el) el.scrollTop = el.scrollHeight
  }, [events])

  const openReqs = useMemo(() => {
    const open = new Set<string>()
    const outcome: Record<string, string> = {}
    for (const e of events) {
      if (e.type === 'permission' || e.type === 'question') open.add(e.req_id!)
      if (e.type === 'resolved') { open.delete(e.req_id!); outcome[e.req_id!] = e.outcome ?? '' }
    }
    return { open, outcome }
  }, [events])
  const busy = useMemo(() => {
    for (let i = events.length - 1; i >= 0; i--) if (events[i].type === 'busy') return !!events[i].busy
    return info?.live?.busy ?? false
  }, [events, info])
  const blocks = useMemo(() => toBlocks(events), [events])
  // Service channel: input goes to the program behind it, not to an agent.
  const [service, setService] = useState<ServiceChannel | null>(null)
  useEffect(() => {
    setService(null)
    api.service.channels().then((l) => setService(l.find((s) => s.id === channel) ?? null)).catch(() => {})
  }, [channel])
  // Which card buttons were used: the tap (command) or, in lead/task chats,
  // the card's own "resolved" marker that survives deleting the tap bubble.
  const [pressed, cardDone] = useMemo(() => {
    const m: Record<number, string> = {}
    const d: Record<number, string> = {}
    for (const e of events) {
      if (e.type === 'command' && e.button_seq) m[e.button_seq] = e.text ?? ''
      const c = e.type === 'resolved' ? /^card:(\d+)$/.exec(e.req_id ?? '') : null
      if (c) { m[Number(c[1])] = e.data ?? ''; d[Number(c[1])] = e.outcome ?? '' }
    }
    return [m, d]
  }, [events])
  // Open lead cards whose task moved on (or is gone) mark themselves done.
  const [staleCards, setStaleCards] = useState<Record<number, string>>({})
  const startCards = useMemo(() => events.flatMap((e) => {
    if (e.type !== 'alert' || cardDone[e.seq] !== undefined || pressed[e.seq]) return []
    const id = e.buttons?.flat().map((x) => CARD_TASK.exec(x.data)?.[1]).find(Boolean)
    return id ? [`${e.seq}:${id}`] : []
  }).slice(-30).join(','), [events, cardDone, pressed])
  useEffect(() => {
    if (!info?.lead || !info.project || !startCards) return
    let off = false
    const project = info.project
    Promise.all(startCards.split(',').map(async (c) => {
      const [seq, id] = c.split(':').map(Number)
      try {
        return [seq, taskMovedOn(await api.lead.get(project, id))] as const
      } catch (e) {
        return [seq, String(e).includes('404') ? `#${id} gelöscht` : null] as const
      }
    })).then((rows) => {
      if (off) return
      const out: Record<number, string> = {}
      for (const [seq, moved] of rows) if (moved) out[seq] = moved
      setStaleCards(out)
    })
    return () => { off = true }
  }, [info?.lead, info?.project, startCards])
  const [leadRequested, setLeadRequested] = useState(false)
  useEffect(() => { setLeadRequested(false) }, [channel])
  // The lead's summary ("Lead: n zur Freigabe …", source lead:<id>) ends the wait.
  const leadSummaries = useMemo(() => events.filter((e) => e.type === 'alert' && (e.source ?? '').startsWith('lead:')).length, [events])
  useEffect(() => { setLeadRequested(false) }, [leadSummaries])
  const runLead = async () => {
    if (!info?.project) return
    try {
      const lead = (await api.chat.scouts(info.project)).find((x) => x.lead)
      if (!lead) { setError('Für dieses Projekt ist kein Lead-Lauf eingerichtet.'); return }
      await api.chat.runScout(info.project, lead.id)
      setLeadRequested(true)
    } catch (e) { setError(String(e)) }
  }
  // Role chats (channel id = <project>-<role>, User 2026-11-01): one button per
  // regular duty of that role (its scouts in team.yaml) to run it by hand. The
  // watchdog chat runs its own evaluation; the lead chat also offers duties of
  // the lead's role (e.g. a product owner's review).
  const [duties, setDuties] = useState<string[]>([])
  const [requested, setRequested] = useState<Record<string, boolean>>({})
  useEffect(() => {
    setDuties([])
    setRequested({})
    if (!info?.project || info.task_id != null || !info.id.startsWith(`${info.project}-`)) return
    const suffix = info.id.slice(info.project.length + 1)
    api.chat.scouts(info.project).then((list) => {
      const leadRole = list.find((x) => x.lead)?.role
      setDuties(list.filter((x) => !x.lead && !x.report
        && (x.role === suffix || x.id === suffix || (info.lead && x.role === leadRole))).map((x) => x.id))
    }, () => {})
  }, [info?.id, info?.project, info?.lead, info?.task_id])
  // A duty's next report (source scout:<id>) ends its wait.
  const dutyReports = useMemo(() => events.filter((e) => e.type === 'alert' && (e.source ?? '').startsWith('scout:'))
    .map((e) => (e.source ?? '').slice(6)).join(','), [events])
  useEffect(() => { setRequested({}) }, [dutyReports])
  const runDuty = async (sid: string) => {
    if (!info?.project) return
    try {
      await api.chat.runScout(info.project, sid)
      setRequested((r) => ({ ...r, [sid]: true }))
    } catch (e) { setError(String(e)) }
  }
  const toggleArchive = async (on: boolean) => {
    try {
      await api.chat.archive(channel, on)
      setInfo((i) => (i ? { ...i, archived: on } : i))
    } catch (e) { setError(String(e)) }
  }
  const navigate = useNavigate()
  const checkAlert = async (ev: ChatEvent) => {
    try {
      const r = await api.chat.check(channel, ev.seq)
      navigate(`/chat/${r.channel}`)
    } catch (e) { setError(String(e)) }
  }
  const dropCheckedAlert = async () => {
    const m = /^(.+):(\d+)$/.exec(info?.check_of ?? '')
    if (!m || !window.confirm('Original-Meldung löschen und diesen Prüf-Chat archivieren?')) return
    try {
      await api.chat.delete(m[1], { seqs: [Number(m[2])] })
      await toggleArchive(true)
      navigate('/chat')
    } catch (e) { setError(String(e)) }
  }
  const pressButton = async (ev: ChatEvent, b: ServiceButton) => {
    try {
      // Lead channel card: run the action (start = my approval) on the
      // channel's project first; the command event only records the tap.
      if (/^commit:\d+$/.test(b.data)) {
        // The task chat's lead commits the task's files (TASK_CHAT_RULES).
        await api.service.command(channel, { text: b.data, label: b.label, button_seq: ev.seq })
        await api.chat.message(channel, { text: `Bitte committe jetzt die Änderungen von Task #${b.data.slice(7)} (nur die Dateien dieses Tasks, kein Push).` })
        followRef.current = true
        return
      }
      if (/^deploy:\d+$/.test(b.data)) {
        // The task chat's lead deploys what the merged changes need (TASK_CHAT_RULES);
        // the message carries the rules too, for a chat process started before them.
        const id = b.data.slice(7)
        await api.service.command(channel, { text: b.data, label: b.label, button_seq: ev.seq })
        await api.chat.message(channel, { text: `Bitte deploye jetzt, was Task #${id} braucht: Prüfe, welche Dienste/Images/Skripte `
          + 'die übernommenen Änderungen betreffen, und baue bzw. starte nur diese neu, über den Deploy-Weg des Repos. '
          + 'Ist nichts zu deployen (z. B. ein Skript, das live gelesen wird), sag das. Kein Push. '
          + 'Berichte danach, was du neu gebaut/gestartet hast und wie du geprüft hast, dass es läuft.' })
        followRef.current = true
        return
      }
      if (b.data.startsWith('archive:')) {
        // "Abschließen": a task that stopped (blocked/failed …) is finished too —
        // only archiving left it in the inbox (user 2026-10-30).
        let label = b.label
        const id = Number(b.data.slice(8))
        if (info?.project && id) {
          const t = await api.lead.get(info.project, id)
          if (t.status !== 'done' && t.status !== 'closed') {
            await api.lead.done(info.project, id)
            label = `${b.label} → #${id} erledigt`
          }
        }
        await toggleArchive(true)
        await api.service.command(channel, { text: b.data, label: label.slice(0, 64), button_seq: ev.seq })
        return
      }
      // The recorded tap says what happened, not just what was tapped.
      let done = ''
      const op = /^opt:(\d+):(\d+):(\d+)$/.exec(b.data)
      if (!service && op && info?.project) {
        await api.lead.option(info.project, Number(op[1]), Number(op[2]), Number(op[3]))
        done = 'beantwortet'
      }
      const mg = /^merge:(\d+):(\d+)$/.exec(b.data)
      if (!service && mg && info?.project) {
        await api.lead.merge(info.project, Number(mg[1]), Number(mg[2]))
        done = `#${mg[1]} geschlossen, in #${mg[2]} übernommen`
      }
      // The lead's "Watchdog-Meldungen löschen" card: the alerts it listed go.
      // Decided either way, the card has done its job and goes too (user 2026-10-30).
      const wd = /^wdel:([a-z0-9-]+)$/.exec(b.data)
      if (!service && wd && info?.project) {
        await api.lead.dismissWatchdog(info.project, wd[1])
        await deleteSeqs([ev.seq])
        return
      }
      if (/^wkeep:[a-z0-9-]+$/.test(b.data)) {
        await deleteSeqs([ev.seq])
        return
      }
      if (/^keep:\d+$/.test(b.data)) done = 'bleibt'
      // keep:<id> only records the decision (no action).
      const rm = /^delete:(\d+)$/.exec(b.data)
      if (!service && rm && info?.project) {
        // Final — the one card action that asks first.
        if (!window.confirm(`Task #${rm[1]} endgültig löschen? Das lässt sich nicht rückgängig machen.`)) return
        await api.lead.remove(info.project, Number(rm[1]))
        done = `#${rm[1]} gelöscht`
      }
      // Done card of a task with its own branch: the worker merges it.
      const ap = /^apply:(\d+)$/.exec(b.data)
      if (!service && ap && info?.project) {
        await api.lead.apply(info.project, Number(ap[1]))
        done = 'wird übernommen, Meldung folgt'
      }
      // Stopped / failed task card (mirror.stuck_card): retry the stage or re-plan.
      const rt = /^retry:(\d+):(\d+)$/.exec(b.data)
      if (!service && rt && info?.project) {
        await api.lead.retry(info.project, Number(rt[1]), Number(rt[2]))
        done = `Stufe ${rt[2]} läuft erneut`
      }
      const rp = /^replan:(\d+)$/.exec(b.data)
      if (!service && rp && info?.project) {
        await api.lead.start(info.project, Number(rp[1]))   // POST /plan re-plans a blocked task
        done = 'Planer läuft'
      }
      const m = /^(start|park|close|discuss|next|done):(\d+)$/.exec(b.data)
      if (!service && m?.[1] === 'discuss') {
        // Talk it through with the lead first: the card becomes the quote of
        // my next message; nothing is recorded, the card stays open.
        setQuote(`${ev.title ? ev.title + '\n' : ''}${ev.text ?? ''}`)
        setText((t) => t || `Lass uns #${m[2]} besprechen: `)
        setTimeout(() => inputRef.current?.focus(), 0)
        return
      }
      if (!service && m && info?.project) {
        if (m[1] === 'start') {
          const moved = taskMovedOn(await api.lead.get(info.project, Number(m[2])))
          if (moved) done = moved          // stale card: record what happened, start nothing
          else {
            await api.lead.start(info.project, Number(m[2]))
            done = `gestartet, siehe Chat #${m[2]}`
          }
        } else if (m[1] === 'done') {
          await api.lead.done(info.project, Number(m[2]))
          done = `#${m[2]} als erledigt abgehakt`
        } else if (m[1] === 'close') {
          await api.lead.close(info.project, Number(m[2]))
          done = `#${m[2]} geschlossen`
        } else if (m[1] === 'next') {
          await api.lead.next(info.project, Number(m[2]))
          done = 'Stufe startet'
        } else {
          await api.lead.park(info.project, Number(m[2]))
          done = `#${m[2]} im Backlog`
        }
      }
      const label = done ? `${b.label} → ${done}` : b.label
      await api.service.command(channel, { text: b.data, label: label.slice(0, 64), button_seq: ev.seq })
      followRef.current = true
    } catch (e) { setError(String(e)) }
  }
  const alertCount = useMemo(() => events.filter((e) => e.type === 'alert').length, [events])

  // Deleting hides events (server keeps the transcript); drop them locally too.
  const deleteSeqs = async (seqs: number[]) => {
    try {
      const r = await api.chat.delete(channel, { seqs })
      const gone = new Set(r.deleted)
      gone.forEach((s) => goneRef.current.add(s))
      setEvents((prev) => prev.filter((e) => !gone.has(e.seq)))
    } catch (e) { setError(String(e)) }
  }
  const deleteAllAlerts = async () => {
    if (!window.confirm(`Alle ${alertCount} Meldungen in diesem Kanal löschen?`)) return
    try {
      const r = await api.chat.delete(channel, { all_alerts: true })
      const gone = new Set(r.deleted)
      gone.forEach((s) => goneRef.current.add(s))
      setEvents((prev) => prev.filter((e) => !gone.has(e.seq)))
    } catch (e) { setError(String(e)) }
  }

  // Hides everything except open approvals/questions; the session context stays.
  const clearHistory = async () => {
    if (!window.confirm('Ganzen Verlauf dieses Kanals löschen? Offene Freigaben/Fragen bleiben, der Sitzungskontext auch.')) return
    try {
      const r = await api.chat.delete(channel, { all: true })
      const gone = new Set(r.deleted)
      gone.forEach((s) => goneRef.current.add(s))
      setEvents((prev) => prev.filter((e) => !gone.has(e.seq)))
    } catch (e) { setError(String(e)) }
  }

  const sendMsg = async () => {
    if (!text.trim() && images.length === 0) return
    setSending(true)
    try {
      if (service) await api.service.command(channel, { text: text.trim() })
      else await api.chat.message(channel, { text, quote, images })
      setText(''); setQuote(null); setImages([])
      followRef.current = true
    } catch (e) {
      setError(String(e))
    } finally {
      setSending(false)
    }
  }
  const addImages = async (files: FileList | File[] | null) => {
    const list = Array.from(files ?? []).filter((f) => f.type.startsWith('image/'))
    if (!list.length) return
    const add = await Promise.all(list.map(async (f, i) => ({
      name: f.name && f.name !== 'image.png' ? f.name : `screenshot-${Date.now()}-${i}.png`,
      data: await fileToDataUrl(f),
    })))
    setImages((x) => [...x, ...add])
  }
  // Screenshots straight from the clipboard (Ctrl+V / long-press paste).
  const onPaste = (e: React.ClipboardEvent) => {
    const files = Array.from(e.clipboardData.items)
      .filter((it) => it.kind === 'file' && it.type.startsWith('image/'))
      .map((it) => it.getAsFile())
      .filter((f): f is File => f !== null)
    if (files.length) {
      e.preventDefault()
      addImages(files)
    }
  }
  const [dragging, setDragging] = useState(false)

  return (
    // Fills the viewport below the header; main's bottom safe-area padding stays below the input bar.
    <div className={`-mx-4 -my-4 flex h-[calc(100dvh-var(--header-h,49px)-env(safe-area-inset-bottom))] flex-col ${dragging ? 'ring-2 ring-inset ring-sky-500' : ''}`}
      onDragOver={(e) => { if (e.dataTransfer.types.includes('Files')) { e.preventDefault(); setDragging(true) } }}
      onDragLeave={(e) => { if (e.currentTarget === e.target) setDragging(false) }}
      onDrop={(e) => { e.preventDefault(); setDragging(false); addImages(e.dataTransfer.files) }}>
      <div className="flex items-center gap-2 border-b border-slate-800 px-3 py-2">
        <Link to="/chat" className="text-slate-400 hover:text-white" aria-label="Kanäle"><ArrowLeft size={18} /></Link>
        <div className="min-w-0">
          <div className="truncate font-medium">{info?.label ?? channel}</div>
          <div className="truncate text-[11px] text-slate-500">
            {info?.project} · {service ? 'Dienst-Kanal' : info?.live ? `${info.live.backend} ${info.live.active_model ?? info.live.model ?? ''}` : 'Dienst nicht erreichbar'}
          </div>
        </div>
        <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
          {busy && (
            <button onClick={() => api.chat.stop(channel).catch((e) => setError(String(e)))} className="flex items-center gap-1 rounded border border-rose-500/60 px-2 py-1 text-xs text-rose-200">
              <Square size={12} /> Stopp
            </button>
          )}
          {info?.check_of && (
            // Verdict "löschen": drop the original alert and file this chat away.
            <button onClick={dropCheckedAlert}
              className="flex items-center gap-1 rounded border border-slate-600 px-2 py-1 text-xs text-slate-300 hover:border-rose-500/60 hover:text-rose-200">
              <Trash2 size={12} /> Meldung löschen
            </button>
          )}
          {info?.lead && info.task_id == null && (
            // Run the lead now instead of waiting for the daily run.
            <button onClick={runLead} disabled={leadRequested}
              className="flex items-center gap-1 rounded border border-sky-700 px-2 py-1 text-xs text-sky-200 hover:text-white disabled:opacity-60">
              <RotateCcw size={12} /> {leadRequested ? 'Lead läuft gleich …' : 'Lead jetzt'}
            </button>
          )}
          {duties.map((sid) => (
            <button key={sid} onClick={() => runDuty(sid)} disabled={!!requested[sid]} title={`Pflicht „${sid}“ jetzt laufen lassen`}
              className="flex items-center gap-1 whitespace-nowrap rounded border border-sky-700 px-2 py-1 text-xs text-sky-200 hover:text-white disabled:opacity-60">
              <RotateCcw size={12} /> {requested[sid] ? `${DUTY_LABEL[sid] ?? sid} läuft gleich …` : `${DUTY_LABEL[sid] ?? sid} jetzt`}
            </button>
          ))}
          {(info?.task_id != null || info?.check_of || info?.custom) && (
            // Finished task / check chat: hide it from the list, keep the transcript.
            <button onClick={() => toggleArchive(!info.archived)}
              className="flex items-center gap-1 rounded border border-slate-600 px-2 py-1 text-xs text-slate-300 hover:text-white">
              <Archive size={12} /> {info.archived ? 'Zurückholen' : 'Archivieren'}
            </button>
          )}
          <button onClick={() => setShowSettings(!showSettings)} aria-label="Einstellungen" className="text-slate-400 hover:text-white"><Settings2 size={18} /></button>
        </div>
      </div>
      {showSettings && info && (
        <SettingsPanel channel={channel} info={info} alertCount={alertCount} onClose={() => setShowSettings(false)}
          onChanged={loadInfo} onDeleteAlerts={deleteAllAlerts} onClear={clearHistory} />
      )}

      <div ref={scrollerRef} className="flex-1 overflow-y-auto overflow-x-hidden px-3 py-2"
        onScroll={(e) => { const el = e.currentTarget; followRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80 }}>
        {error && <div className="my-2 text-xs text-rose-300">{error}</div>}
        {blocks.length === 0 && !error && <div className="mt-8 text-center text-sm text-slate-500">Noch keine Nachrichten in diesem Kanal.</div>}
        {blocks.map((b) => b.kind === 'tools'
          ? <ToolsBlock key={`t${b.events[0].seq}`} events={b.events} onDelete={deleteSeqs} />
          : (
            <div key={b.ev.seq}>
              {/* Deleting the message also deletes the tool calls hanging on it. */}
              <EventView channel={channel} onQuote={setQuote}
                onButton={service || info?.lead ? pressButton : undefined} pressed={pressed[b.ev.seq]} done={cardDone[b.ev.seq] ?? staleCards[b.ev.seq]}
                onCheck={service || info?.check_of ? undefined : checkAlert}
                onDelete={(seqs) => deleteSeqs([...seqs, ...b.tools.map((t) => t.seq)])}
                ev={{ ...b.ev, outcome: b.ev.req_id ? openReqs.outcome[b.ev.req_id] : undefined }}
                open={!!b.ev.req_id && openReqs.open.has(b.ev.req_id)} />
              {b.tools.length > 0 && <ToolsBlock events={b.tools} onDelete={deleteSeqs} />}
            </div>
          ))}
        {busy && <div className="my-2 flex items-center gap-1.5 text-xs text-slate-400"><Loader2 size={13} className="animate-spin" /> arbeitet …</div>}
        {!busy && info?.task?.working && (
          <div className="my-2 flex items-center gap-1.5 text-xs text-slate-400">
            <Loader2 size={13} className="animate-spin" /> {info.task.status === 'running' ? 'Stufe läuft …' : info.task.status === 'planning' ? 'plant …' : 'wird ausgearbeitet …'}
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      <div className="border-t border-slate-800 bg-slate-900 px-3 pb-2 pt-2">
        {quote && (
          <div className="mb-1.5 flex items-start gap-2 rounded border-l-2 border-sky-400 bg-slate-800 px-2 py-1 text-xs text-slate-300">
            <div className="line-clamp-2 flex-1 whitespace-pre-wrap">{quote}</div>
            <button onClick={() => setQuote(null)} aria-label="Zitat entfernen"><X size={13} /></button>
          </div>
        )}
        {images.length > 0 && (
          <div className="mb-1.5 flex flex-wrap gap-1.5">
            {images.map((im, i) => (
              <div key={i} className="relative">
                <img src={im.data} alt={im.name} className="h-12 w-12 rounded object-cover" />
                <button onClick={() => setImages((x) => x.filter((_, j) => j !== i))} className="absolute -right-1 -top-1 rounded-full bg-slate-700 p-0.5" aria-label="Bild entfernen"><X size={10} /></button>
              </div>
            ))}
          </div>
        )}
        {service && service.menu.length > 0 && (
          // The program's command menu: a tap puts the command in the input.
          <div className="mb-1.5 flex gap-1.5 overflow-x-auto pb-0.5">
            {service.menu.map(([cmd, desc]) => (
              <button key={cmd} title={desc} onClick={() => setText(`/${cmd} `)}
                className="shrink-0 rounded-full border border-slate-600 px-2.5 py-1 text-xs text-slate-300">/{cmd}</button>
            ))}
          </div>
        )}
        <div className="flex items-end gap-2">
          {!service && <button onClick={() => fileRef.current?.click()} className="pb-2 text-slate-400 hover:text-white" aria-label="Bild anhängen"><ImagePlus size={20} /></button>}
          <input ref={fileRef} type="file" accept="image/*" multiple hidden onChange={(e) => { addImages(e.target.files); e.target.value = '' }} />
          <textarea ref={inputRef} value={text} onChange={(e) => setText(e.target.value)} rows={1} onPaste={onPaste}
            // Enter is a new line; Ctrl/Cmd+Enter or the button sends (as in the task editor).
            onKeyDown={(e) => { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter' && !e.nativeEvent.isComposing) { e.preventDefault(); sendMsg() } }}
            enterKeyHint="enter"
            // Short enough for one line on a phone: a wrapped placeholder made the field scroll.
            placeholder={service ? 'Befehl, z. B. /status …' : busy ? 'Ergänzung …' : 'Nachricht …'}
            className="min-h-[40px] min-w-0 flex-1 resize-none overflow-hidden rounded-lg border border-slate-600 bg-slate-800 px-3 py-2 text-sm" />
          <button onClick={sendMsg} disabled={sending || (!text.trim() && images.length === 0)}
            className="mb-0.5 rounded-lg bg-sky-600 p-2.5 text-white disabled:opacity-40" aria-label="Senden">
            {sending ? <Loader2 size={18} className="animate-spin" /> : <Send size={18} />}
          </button>
        </div>
      </div>
    </div>
  )
}
