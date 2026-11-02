import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Gauge, Pause, Play } from 'lucide-react'
import { api, type BudgetConfig, type BudgetInfo } from '../api/client'

function when(ts: number | null): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  return d.toLocaleString('de-DE', { weekday: 'short', hour: '2-digit', minute: '2-digit' })
}

function ago(s: number | null): string {
  if (s == null) return 'nie'
  if (s < 90) return 'gerade'
  if (s < 5400) return `vor ${Math.round(s / 60)} min`
  return `vor ${Math.round(s / 3600)} h`
}

// Week bar: used (fill) vs. even-spend line (marker).
function WeekBar({ used, target }: { used: number | null; target: number | null }) {
  return (
    <div className="relative h-2.5 w-full overflow-hidden rounded bg-slate-700">
      <div className={`h-full ${used != null && target != null && used > target ? 'bg-amber-500' : 'bg-sky-500'}`}
        style={{ width: `${Math.min(used ?? 0, 100)}%` }} />
      {target != null && <div className="absolute top-0 h-full w-0.5 bg-white/80" style={{ left: `${Math.min(target, 100)}%` }} title="Soll-Linie" />}
    </div>
  )
}

export function BudgetStrip() {
  const [b, setB] = useState<BudgetInfo | null>(null)
  useEffect(() => {
    const load = () => api.budget.get().then(setB, () => {})
    load()
    const id = window.setInterval(load, 60_000)
    return () => window.clearInterval(id)
  }, [])
  if (!b) return null
  const d = b.decision
  return (
    <Link to="/settings" className="mb-3 block rounded-lg border border-slate-700 bg-slate-800/50 px-3 py-2 text-xs text-slate-300 hover:border-slate-500">
      <div className="mb-1 flex items-center gap-2">
        <Gauge size={13} />
        <span>Woche {d.week_used ?? '—'} % <span className="text-slate-500">/ Soll {d.week_target ?? '—'} %</span></span>
        <span className="text-slate-500">· 5 h {d.five_hour_used ?? '—'} %</span>
        <span className={`ml-auto rounded px-1.5 ${d.allowed ? 'bg-emerald-500/20 text-emerald-200' : 'bg-amber-500/20 text-amber-200'}`}>
          {d.allowed ? 'Teams frei' : 'Teams gebremst'}
        </span>
      </div>
      <WeekBar used={d.week_used} target={d.week_target} />
      {!d.allowed && <div className="mt-1 text-slate-400">{d.reason}</div>}
    </Link>
  )
}

export function BudgetSettings() {
  const [b, setB] = useState<BudgetInfo | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => { api.budget.get().then(setB, (e) => setErr(String(e))) }, [])
  const save = async (c: Partial<BudgetConfig>) => {
    try { setB(await api.budget.config(c)) } catch (e) { setErr(String(e)) }
  }
  const probe = async () => {
    setBusy(true)
    try { setB(await api.budget.probe()) } catch (e) { setErr(String(e)) } finally { setBusy(false) }
  }
  if (!b) return <section className="rounded-lg border border-slate-700 bg-slate-800/60 p-4 text-sm text-slate-400">Budget: lade … {err}</section>
  const d = b.decision
  const c = b.config
  const num = (k: keyof BudgetConfig, label: string, min: number, max: number, unit: string) => (
    <label className="block text-sm">
      <span className="text-slate-300">{label}: <b>{String(c[k])}{unit}</b></span>
      <input type="range" min={min} max={max} value={Number(c[k])} className="w-full"
        onChange={(e) => setB({ ...b, config: { ...c, [k]: Number(e.target.value) } })}
        onMouseUp={(e) => save({ [k]: Number((e.target as HTMLInputElement).value) })}
        onTouchEnd={(e) => save({ [k]: Number((e.target as HTMLInputElement).value) })} />
    </label>
  )
  return (
    <section id="budget" className="rounded-lg border border-slate-700 bg-slate-800/60 p-4">
      <h2 className="font-semibold text-slate-100">Budget (Claude-Abo)</h2>
      <p className="mt-1 text-sm text-slate-400">
        Autonome Arbeit (Scouts, Autopilot-Stufen) läuft nur, solange der Wochenverbrauch unter der Soll-Linie
        liegt. Die startet mit einem Vorsprung, der bis zum Reset auf null schrumpft: am Wochenanfang großzügig,
        dann Tag für Tag strenger. Was du selbst startest, wird nie gebremst.
      </p>
      <div className="mt-3 space-y-1 text-sm">
        <div className="flex justify-between"><span>Woche</span><span>{d.week_used ?? '—'} % · Soll {d.week_target ?? '—'} % · Reset {when(d.week_resets_at)}</span></div>
        <WeekBar used={d.week_used} target={d.week_target} />
        <div className="flex justify-between pt-1"><span>5-h-Fenster</span><span>{d.five_hour_used ?? '—'} % · Reset {when(d.five_hour_resets_at)}</span></div>
        <div className="flex justify-between text-slate-400"><span>Messung</span><span>{ago(d.sample_age_s)} ({b.latest?.source ?? '—'})</span></div>
        <div className={`mt-1 rounded px-2 py-1 ${d.allowed ? 'bg-emerald-500/10 text-emerald-200' : 'bg-amber-500/10 text-amber-200'}`}>
          {d.allowed ? 'Teams dürfen arbeiten' : 'Teams gebremst'} — {d.reason}{d.reserve_released ? ' (Reserve freigegeben)' : ''}
        </div>
      </div>
      <div className="mt-4 space-y-3">
        {num('early_lead_pct', 'Vorsprung am Wochenanfang', 0, 50, ' %')}
        {num('reserve_pct', 'Reserve für dich', 0, 50, ' %')}
        {num('release_hours', 'Reserve frei vor dem Reset', 0, 48, ' h')}
        {num('five_hour_max', 'Bremsen ab 5-h-Auslastung', 30, 100, ' %')}
        {num('user_active_minutes', 'Pause nach eigener Chat-Nachricht', 0, 60, ' min')}
      </div>
      <div className="mt-3 flex flex-wrap gap-2">
        <button onClick={() => save({ paused: !c.paused })}
          className={`flex items-center gap-1 rounded-lg px-4 py-2 text-white ${c.paused ? 'bg-emerald-600' : 'bg-amber-700'}`}>
          {c.paused ? <><Play size={15} /> Teams wieder freigeben</> : <><Pause size={15} /> Alle Teams pausieren</>}
        </button>
        <button disabled={busy} onClick={probe} className="rounded-lg border border-slate-600 px-4 py-2 text-slate-200 disabled:opacity-50">
          {busy ? 'Messe …' : 'Jetzt messen'}
        </button>
      </div>
      {err && <div className="mt-2 text-xs text-rose-300">{err}</div>}
    </section>
  )
}
