import { useEffect, useMemo, useState } from 'react'
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { RefreshCw } from 'lucide-react'
import { api, type HostMetrics as HostMetricsType, type DriveHealth } from '../../system/api'
import GaugeBar from '../../components/GaugeBar'
import { movingAverage } from '../../utils/movingAverage'

// ─── Shared chart helpers ───────────────────────────────────────────────────

const TICK_STYLE = { fill: '#94a3b8', fontSize: 11 }
const HISTORY_COLOURS = ['#3b82f6', '#22c55e', '#f97316', '#a78bfa', '#fbbf24', '#ec4899', '#2dd4bf', '#f43f5e']

const fmtClock = (v: number) =>
  new Date(v).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })

function shortGpu(name: string) {
  return name.replace(/\s*\(GPU\s*\d+\)$/i, '')
}

function HistoryPending({ title }: { title: string }) {
  return (
    <div>
      <p className="mb-2 text-xs text-slate-500">{title}</p>
      <p className="text-xs text-slate-500">
        Collecting samples — chart appears once more than one minute of history is available.
      </p>
    </div>
  )
}

interface ChartLine {
  key: string
  label: string
  color: string
}

type ChartDatum = Record<string, number>
type AxisDomainEntry = number | string | ((value: number) => number)

interface HistoryChartProps {
  title: string
  rightHint?: string
  data: ChartDatum[]
  lines: ChartLine[]
  height?: number
  unit?: string
  yDomain?: [AxisDomainEntry, AxisDomainEntry]
  yTicks?: number[]
  yWidth?: number
  referenceLine?: { y: number; label?: string }
  showLegend?: boolean
  showMovingAverages?: boolean
  tooltipFormatter?: (value: number, name: string) => [string, string?]
}

function HistoryChart({
  title,
  rightHint,
  data,
  lines,
  height = 200,
  unit,
  yDomain,
  yTicks,
  yWidth = 40,
  referenceLine,
  showLegend = false,
  showMovingAverages = false,
  tooltipFormatter,
}: HistoryChartProps) {
  const chart = useMemo(() => {
    if (!showMovingAverages) return { data, lines }
    const averagedData = data.map((point) => ({ ...point }))
    const averagedLines = [...lines]
    for (const line of lines) {
      for (const [minutes, color] of [[5, '#22c55e'], [15, '#f97316']] as const) {
        const key = `${line.key}_avg${minutes}`
        movingAverage(data, line.key, minutes).forEach((value, index) => {
          if (value !== undefined) averagedData[index][key] = value
        })
        averagedLines.push({ key, label: `${line.label} · ${minutes} min avg`, color })
      }
    }
    return { data: averagedData, lines: averagedLines }
  }, [data, lines, showMovingAverages])

  return (
    <div>
      <div className="mb-2 flex items-baseline justify-between">
        <p className="text-xs text-slate-500">{title}</p>
        {rightHint && <span className="text-xs text-slate-600">{rightHint}</span>}
      </div>
      <ResponsiveContainer width="100%" height={height}>
        <LineChart data={chart.data} margin={{ top: 4, right: 12, bottom: 4, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
          <XAxis
            dataKey="ts"
            tick={TICK_STYLE}
            axisLine={{ stroke: '#334155' }}
            tickLine={false}
            tickFormatter={fmtClock}
            minTickGap={60}
          />
          <YAxis
            tick={TICK_STYLE}
            axisLine={false}
            tickLine={false}
            unit={unit}
            domain={yDomain}
            ticks={yTicks}
            width={yWidth}
          />
          <Tooltip
            contentStyle={{ background: '#1e293b', border: '1px solid #334155', borderRadius: 6 }}
            labelStyle={{ color: '#94a3b8', fontSize: 11 }}
            itemStyle={{ fontSize: 12 }}
            labelFormatter={(v: number) => new Date(v).toLocaleString()}
            formatter={tooltipFormatter as never}
          />
          {(showLegend || showMovingAverages) && <Legend wrapperStyle={{ fontSize: 12, color: '#94a3b8' }} />}
          {referenceLine && (
            <ReferenceLine
              y={referenceLine.y}
              stroke="#64748b"
              strokeDasharray="4 4"
              label={
                referenceLine.label
                  ? {
                      value: referenceLine.label,
                      position: 'insideTopRight',
                      fill: '#64748b',
                      fontSize: 10,
                    }
                  : undefined
              }
            />
          )}
          {chart.lines.map((s) => (
            <Line
              key={s.key}
              type="monotone"
              dataKey={s.key}
              name={s.label}
              stroke={s.color}
              strokeWidth={1.5}
              dot={false}
              connectNulls
              isAnimationActive={false}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}

function HardwareSection({
  title,
  children,
  rightSlot,
}: {
  title: string
  children: React.ReactNode
  rightSlot?: React.ReactNode
}) {
  return (
    <div className="rounded-lg border border-slate-700 bg-slate-800 p-4">
      <div className="mb-4 flex items-baseline justify-between">
        <h2 className="text-sm font-semibold text-slate-200">{title}</h2>
        {rightSlot}
      </div>
      <div className="space-y-4">{children}</div>
    </div>
  )
}

function Divider() {
  return <div className="border-t border-slate-700" />
}

// ─── Data hooks ─────────────────────────────────────────────────────────────

type LoadSample = { ts: number; cpu_pct: number; load1: number; load5: number; load15: number }

function useLoadHistory() {
  const [samples, setSamples] = useState<LoadSample[]>([])
  const [loaded, setLoaded] = useState(false)

  useEffect(() => {
    let cancelled = false
    const load = () =>
      api
        .loadHistory(24)
        .then((d) => {
          if (!cancelled) {
            setSamples(d.samples.map((s) => ({ ...s, ts: s.ts * 1000 })))
            setLoaded(true)
          }
        })
        .catch(() => {
          if (!cancelled) setLoaded(true)
        })
    load()
    const id = setInterval(load, 60_000)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [])

  return { samples, loaded }
}

type TempHistory = Record<string, { category: string; samples: { ts: number; temp: number }[] }>

function useTemperatureHistory() {
  const [history, setHistory] = useState<TempHistory>({})
  const [loaded, setLoaded] = useState(false)

  useEffect(() => {
    let cancelled = false
    const load = () =>
      api
        .temperatureHistory(24)
        .then((d) => {
          if (!cancelled) {
            setHistory(d)
            setLoaded(true)
          }
        })
        .catch(() => {
          if (!cancelled) setLoaded(true)
        })
    load()
    const id = setInterval(load, 60_000)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [])

  return { history, loaded }
}

type UsageHistory = Awaited<ReturnType<typeof api.usageHistory>>

function useUsageHistory() {
  const [data, setData] = useState<UsageHistory | null>(null)
  const [loaded, setLoaded] = useState(false)

  useEffect(() => {
    let cancelled = false
    const load = () =>
      api
        .usageHistory(24)
        .then((d) => {
          if (!cancelled) {
            setData(d)
            setLoaded(true)
          }
        })
        .catch(() => {
          if (!cancelled) setLoaded(true)
        })
    load()
    const id = setInterval(load, 60_000)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [])

  return { data, loaded }
}

// ─── Series helpers ─────────────────────────────────────────────────────────

type TempSeries = { key: string; label: string; samples: { ts: number; temp: number }[] }

function pickHottest(
  history: TempHistory,
  category: string,
): [string, { ts: number; temp: number }[]] | null {
  const candidates = Object.entries(history).filter(([, v]) => v.category === category)
  if (!candidates.length) return null
  const last = (s: { temp: number }[]) => (s.length ? s[s.length - 1].temp : -Infinity)
  let best = candidates[0]
  for (const c of candidates) {
    if (last(c[1].samples) > last(best[1].samples)) best = c
  }
  return [best[0], best[1].samples]
}

function mergeTempSeries(series: TempSeries[]): {
  merged: ChartDatum[]
  lines: ChartLine[]
} {
  const map = new Map<number, ChartDatum>()
  for (const s of series) {
    for (const pt of s.samples) {
      const slot = Math.round(pt.ts / 60) * 60 // bucket to nearest minute
      if (!map.has(slot)) map.set(slot, { ts: slot * 1000 })
      ;(map.get(slot) as ChartDatum)[s.key] = pt.temp
    }
  }
  return {
    merged: Array.from(map.values()).sort((a, b) => (a.ts as number) - (b.ts as number)),
    lines: series.map((s, i) => ({
      key: s.key,
      label: s.label,
      color: HISTORY_COLOURS[i % HISTORY_COLOURS.length],
    })),
  }
}

function mergeUsageSamples<T extends { ts: number }>(
  groups: { key: string; label: string; samples: T[] }[],
  metric: keyof T,
): { merged: ChartDatum[]; lines: ChartLine[] } {
  const map = new Map<number, ChartDatum>()
  for (const g of groups) {
    for (const pt of g.samples) {
      const slot = Math.round(pt.ts / 60) * 60
      if (!map.has(slot)) map.set(slot, { ts: slot * 1000 })
      const value = pt[metric] as unknown as number
      ;(map.get(slot) as ChartDatum)[g.key] = value
    }
  }
  return {
    merged: Array.from(map.values()).sort((a, b) => (a.ts as number) - (b.ts as number)),
    lines: groups.map((g, i) => ({
      key: g.key,
      label: g.label,
      color: HISTORY_COLOURS[i % HISTORY_COLOURS.length],
    })),
  }
}

// ─── Per-section history charts ─────────────────────────────────────────────

// CPU usage and load average as one number: up to 100 % it is the CPU usage
// (like Windows); load / cores above 100 % means processes queue for a core or
// wait on disk/swap — high load with idle CPU counts too.
function cpuPressure(cpuPct: number, load1: number, cores: number): number {
  return Math.max(cpuPct, cores > 0 ? (load1 / cores) * 100 : 0)
}

function CpuPressureHistoryChart({ cores }: { cores: number }) {
  const { samples, loaded } = useLoadHistory()
  const data = useMemo(
    () => samples.map((s) => ({ ts: s.ts, pressure: Math.round(cpuPressure(s.cpu_pct, s.load1, cores)) })),
    [samples, cores],
  )
  if (!loaded) return null
  if (data.length < 2) return <HistoryPending title="CPU Pressure — Last 24h" />
  return (
    <HistoryChart
      title="CPU Pressure — Last 24h"
      rightHint="max(CPU %, load ÷ cores) · above 100 % = queue"
      data={data as unknown as ChartDatum[]}
      lines={[{ key: 'pressure', label: 'Pressure', color: '#a855f7' }]}
      unit="%"
      yDomain={[0, (dataMax: number) => Math.max(100, Math.ceil(dataMax / 50) * 50)]}
      referenceLine={{ y: 100, label: 'all cores busy' }}
      tooltipFormatter={(v: number) => [`${v.toFixed(0)} %`, 'Pressure']}
    />
  )
}

function CpuUsageHistoryChart() {
  const { samples, loaded } = useLoadHistory()
  if (!loaded) return null
  if (samples.length < 2) return <HistoryPending title="CPU Usage — Last 24h" />
  const cur = samples[samples.length - 1].cpu_pct
  return (
    <HistoryChart
      title="CPU Usage — Last 24h"
      rightHint={`now ${cur.toFixed(0)}% · ${samples.length} samples`}
      data={samples as unknown as ChartDatum[]}
      lines={[{ key: 'cpu_pct', label: 'CPU %', color: '#3b82f6' }]}
      unit="%"
      yDomain={[0, 100]}
      yTicks={[0, 25, 50, 75, 100]}
      showMovingAverages
      tooltipFormatter={(v: number, name: string) => [`${v.toFixed(1)} %`, name]}
    />
  )
}

function LoadHistoryChart({ cores }: { cores: number }) {
  const { samples, loaded } = useLoadHistory()
  if (!loaded) return null
  if (samples.length < 2) return <HistoryPending title="Load Average — Last 24h" />
  return (
    <HistoryChart
      title="Load Average — Last 24h"
      rightHint={`run-queue length · ${cores}-core machine = 100% busy`}
      data={samples as unknown as ChartDatum[]}
      lines={[
        { key: 'load1', label: '1 min', color: '#3b82f6' },
        { key: 'load5', label: '5 min', color: '#22c55e' },
        { key: 'load15', label: '15 min', color: '#f97316' },
      ]}
      yDomain={[0, (dataMax: number) => Math.max(Math.ceil(dataMax), cores || 1)]}
      referenceLine={cores > 0 ? { y: cores, label: `${cores} cores` } : undefined}
      showLegend
      tooltipFormatter={(v: number) => [v.toFixed(2)]}
    />
  )
}

function TemperatureChartForCategory({
  title,
  series,
  loaded,
  showMovingAverages = false,
}: {
  title: string
  series: TempSeries[]
  loaded: boolean
  showMovingAverages?: boolean
}) {
  const { merged, lines } = useMemo(() => mergeTempSeries(series), [series])
  if (!loaded) return null
  if (merged.length < 2) return <HistoryPending title={title} />
  return (
    <HistoryChart
      title={title}
      rightHint={`${merged.length} samples`}
      data={merged}
      lines={lines}
      unit="°"
      yDomain={['dataMin - 2', 'dataMax + 2']}
      showLegend
      showMovingAverages={showMovingAverages}
      tooltipFormatter={(v: number) => [`${v.toFixed(1)} °C`]}
    />
  )
}

function RamUsageHistoryChart() {
  const { data, loaded } = useUsageHistory()
  const merged = useMemo(() => {
    if (!data) return [] as ChartDatum[]
    return data.memory.samples
      .map((s) => ({ ts: s.ts * 1000, percent: s.percent, used_gb: s.used_gb }))
      .sort((a, b) => a.ts - b.ts)
  }, [data])
  if (!loaded) return null
  if (merged.length < 2) return <HistoryPending title="RAM Usage — Last 24h" />
  const cur = merged[merged.length - 1].percent
  return (
    <HistoryChart
      title="RAM Usage — Last 24h"
      rightHint={`now ${cur.toFixed(0)}% · ${merged.length} samples`}
      data={merged}
      lines={[{ key: 'percent', label: 'RAM %', color: '#3b82f6' }]}
      unit="%"
      yDomain={[0, 100]}
      yTicks={[0, 25, 50, 75, 100]}
      tooltipFormatter={(v: number) => [`${v.toFixed(1)} %`, 'RAM']}
    />
  )
}

function GpuUsageHistoryChart({ metric, title }: { metric: 'util_pct' | 'mem_pct'; title: string }) {
  const { data, loaded } = useUsageHistory()
  const { merged, lines } = useMemo(() => {
    if (!data) return { merged: [] as ChartDatum[], lines: [] as ChartLine[] }
    const groups = data.gpus.map((g) => ({
      key: `gpu${g.index}`,
      label: `${shortGpu(g.name)} (GPU ${g.index})`,
      samples: g.samples.map((s) => ({ ts: s.ts * 1000, util_pct: s.util_pct, mem_pct: s.mem_pct })),
    }))
    return mergeUsageSamples(groups, metric)
  }, [data, metric])
  if (!loaded) return null
  if (merged.length < 2) return <HistoryPending title={title} />
  return (
    <HistoryChart
      title={title}
      rightHint={`${merged.length} samples`}
      data={merged}
      lines={lines}
      unit="%"
      yDomain={[0, 100]}
      yTicks={[0, 25, 50, 75, 100]}
      showLegend
      tooltipFormatter={(v: number) => [`${v.toFixed(1)} %`]}
    />
  )
}

function DiskUsageHistoryChart() {
  const { data, loaded } = useUsageHistory()
  const { merged, lines } = useMemo(() => {
    if (!data) return { merged: [] as ChartDatum[], lines: [] as ChartLine[] }
    const groups = data.disks.map((d) => ({
      key: `disk:${d.mountpoint}`,
      label: d.label ?? d.mountpoint,
      samples: d.samples.map((s) => ({ ts: s.ts * 1000, percent: s.percent, used_gb: s.used_gb })),
    }))
    return mergeUsageSamples(groups, 'percent')
  }, [data])
  if (!loaded) return null
  if (merged.length < 2) return <HistoryPending title="Disk Usage — Last 24h" />
  return (
    <HistoryChart
      title="Disk Usage — Last 24h"
      rightHint={`${merged.length} samples`}
      data={merged}
      lines={lines}
      unit="%"
      yDomain={[0, 100]}
      yTicks={[0, 25, 50, 75, 100]}
      showLegend
      tooltipFormatter={(v: number) => [`${v.toFixed(1)} %`]}
    />
  )
}

// ─── Misc helpers ───────────────────────────────────────────────────────────

function tempColour(t: number, category?: string): string {
  if (category === 'Storage') {
    if (t >= 60) return 'text-red-400'
    if (t >= 50) return 'text-amber-400'
    return 'text-green-400'
  }
  if (t >= 90) return 'text-red-400'
  if (t >= 70) return 'text-amber-400'
  return 'text-green-400'
}

function TempChips({
  items,
  empty,
}: {
  items: { name: string; temp: number; category?: string }[]
  empty?: string
}) {
  if (!items.length) return empty ? <p className="text-xs text-slate-500">{empty}</p> : null
  return (
    <div className="flex flex-wrap gap-2">
      {items.map((t) => (
        <div
          key={t.name}
          className="flex items-center gap-2 rounded-md border border-slate-700 bg-slate-900 px-3 py-1.5"
        >
          <span className="text-xs text-slate-400">{t.name}</span>
          <span className={`text-sm font-bold ${tempColour(t.temp, t.category)}`}>
            {t.temp.toFixed(1)}°C
          </span>
        </div>
      ))}
    </div>
  )
}

function healthBadge(level: string) {
  const map: Record<string, string> = {
    good: 'bg-green-900 text-green-300 border-green-700',
    warning: 'bg-amber-900 text-amber-300 border-amber-700',
    error: 'bg-red-900 text-red-300 border-red-700',
    unknown: 'bg-slate-700 text-slate-400 border-slate-600',
  }
  return map[level] ?? map.unknown
}

function healthLabel(level: string) {
  return { good: 'Good', warning: 'Warning', error: 'Critical', unknown: 'Unknown' }[level] ?? level
}

function SmartCards({ drives }: { drives: DriveHealth | null }) {
  if (!drives) return <p className="text-sm text-slate-500">Loading drive health…</p>
  if (!drives.available) {
    return <p className="text-sm text-slate-500">smartctl unavailable: {drives.error}</p>
  }
  return (
    <div>
      <div className="mb-2 flex items-baseline justify-between">
        <p className="text-[11px] font-semibold uppercase tracking-widest text-slate-500">
          SMART Status
        </p>
        {drives.last_run && (
          <span className="text-xs text-slate-500">Last scan: {drives.last_run} (1h cache)</span>
        )}
      </div>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {drives.drives.map((drive) => (
          <div
            key={drive.device}
            className="space-y-2 rounded-md border border-slate-700 bg-slate-900 p-3"
          >
            <div className="flex items-start justify-between gap-2">
              <div>
                {drive.nickname && (
                  <p className="text-sm font-semibold text-slate-200">{drive.nickname}</p>
                )}
                <p className="font-mono text-xs text-slate-500">{drive.device}</p>
                {drive.model && <p className="text-xs text-slate-400">{drive.model}</p>}
              </div>
              <span
                className={`shrink-0 rounded border px-2 py-0.5 text-[11px] font-semibold ${healthBadge(
                  drive.health_level,
                )}`}
              >
                {healthLabel(drive.health_level)}
              </span>
            </div>
            <p className="text-xs italic text-slate-400">{drive.estimated_health}</p>
            <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
              {drive.temperature_c != null && (
                <>
                  <span className="text-slate-500">Temperature</span>
                  <span
                    className={`font-medium ${
                      drive.temperature_c >= 55 ? 'text-amber-400' : 'text-slate-200'
                    }`}
                  >
                    {drive.temperature_c}°C
                  </span>
                </>
              )}
              {drive.power_on_hours != null && (
                <>
                  <span className="text-slate-500">Power-On Hours</span>
                  <span className="text-slate-200">{drive.power_on_hours.toLocaleString()} h</span>
                </>
              )}
              {drive.age_years != null && (
                <>
                  <span className="text-slate-500">Age</span>
                  <span className="text-slate-200">{drive.age_years} yr</span>
                </>
              )}
              {drive.raw_read_errors != null && (
                <>
                  <span className="text-slate-500">Raw Read Errors</span>
                  <span
                    className={`font-medium ${
                      drive.raw_read_errors > 0 ? 'text-amber-400' : 'text-slate-200'
                    }`}
                  >
                    {drive.raw_read_errors.toLocaleString()}
                  </span>
                </>
              )}
              {drive.reallocated_sectors != null && (
                <>
                  <span className="text-slate-500">Reallocated Sectors</span>
                  <span
                    className={`font-medium ${
                      drive.reallocated_sectors > 0 ? 'text-amber-400' : 'text-slate-200'
                    }`}
                  >
                    {drive.reallocated_sectors}
                  </span>
                </>
              )}
              {drive.pending_sectors != null && (
                <>
                  <span className="text-slate-500">Pending Sectors</span>
                  <span
                    className={`font-medium ${
                      (drive.pending_sectors ?? 0) > 0 ? 'text-red-400' : 'text-slate-200'
                    }`}
                  >
                    {drive.pending_sectors}
                  </span>
                </>
              )}
              {drive.uncorrectable_sectors != null && (
                <>
                  <span className="text-slate-500">Uncorrectable</span>
                  <span
                    className={`font-medium ${
                      (drive.uncorrectable_sectors ?? 0) > 0 ? 'text-red-400' : 'text-slate-200'
                    }`}
                  >
                    {drive.uncorrectable_sectors}
                  </span>
                </>
              )}
              {drive.reported_uncorrectable != null && (
                <>
                  <span className="text-slate-500">Reported Uncorrect.</span>
                  <span
                    className={`font-medium ${
                      (drive.reported_uncorrectable ?? 0) > 0 ? 'text-red-400' : 'text-slate-200'
                    }`}
                  >
                    {drive.reported_uncorrectable}
                  </span>
                </>
              )}
              {drive.spin_retry_count != null && (
                <>
                  <span className="text-slate-500">Spin Retry Count</span>
                  <span
                    className={`font-medium ${
                      (drive.spin_retry_count ?? 0) > 0 ? 'text-amber-400' : 'text-slate-200'
                    }`}
                  >
                    {drive.spin_retry_count}
                  </span>
                </>
              )}
              {drive.available_spare_pct != null && (
                <>
                  <span className="text-slate-500">Available Spare</span>
                  <span
                    className={`font-medium ${
                      (drive.available_spare_pct ?? 100) < 10 ? 'text-red-400' : 'text-slate-200'
                    }`}
                  >
                    {drive.available_spare_pct}%
                  </span>
                </>
              )}
              {drive.percentage_used != null && (
                <>
                  <span className="text-slate-500">Drive Wear</span>
                  <span
                    className={`font-medium ${
                      (drive.percentage_used ?? 0) >= 70 ? 'text-amber-400' : 'text-slate-200'
                    }`}
                  >
                    {drive.percentage_used}%
                  </span>
                </>
              )}
              {drive.media_errors != null && (
                <>
                  <span className="text-slate-500">Media Errors</span>
                  <span
                    className={`font-medium ${
                      (drive.media_errors ?? 0) > 0 ? 'text-red-400' : 'text-slate-200'
                    }`}
                  >
                    {drive.media_errors}
                  </span>
                </>
              )}
              {drive.error_log_entries != null && (
                <>
                  <span className="text-slate-500">Error Log Entries</span>
                  <span
                    className={`font-medium ${
                      (drive.error_log_entries ?? 0) > 0 ? 'text-amber-400' : 'text-slate-200'
                    }`}
                  >
                    {drive.error_log_entries}
                  </span>
                </>
              )}
              {drive.power_cycles != null && (
                <>
                  <span className="text-slate-500">Power Cycles</span>
                  <span className="text-slate-200">{drive.power_cycles.toLocaleString()}</span>
                </>
              )}
              {drive.unsafe_shutdowns != null && (
                <>
                  <span className="text-slate-500">Unsafe Shutdowns</span>
                  <span
                    className={`font-medium ${
                      (drive.unsafe_shutdowns ?? 0) > 5 ? 'text-amber-400' : 'text-slate-200'
                    }`}
                  >
                    {drive.unsafe_shutdowns}
                  </span>
                </>
              )}
              {drive.data_read != null && (
                <>
                  <span className="text-slate-500">Data Read</span>
                  <span className="text-slate-200">{drive.data_read}</span>
                </>
              )}
              {drive.data_written != null && (
                <>
                  <span className="text-slate-500">Data Written</span>
                  <span className="text-slate-200">{drive.data_written}</span>
                </>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

// ─── Page ───────────────────────────────────────────────────────────────────

export default function HostMetrics() {
  const [data, setData] = useState<HostMetricsType | null>(null)
  const [drives, setDrives] = useState<DriveHealth | null>(null)
  const [autoRefresh, setAutoRefresh] = useState(true)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const { history: tempHistory, loaded: tempLoaded } = useTemperatureHistory()

  const load = () => {
    api.host().then((d) => {
      setData(d)
      setLastUpdated(new Date())
    })
  }

  useEffect(() => {
    load()
    // Drive health is checked once — data is cached 1h server-side
    api.drives().then(setDrives).catch(() => {})
    if (!autoRefresh) return
    const id = setInterval(load, 5_000)
    return () => clearInterval(id)
  }, [autoRefresh])

  // Group current temperature readings by category for the chips.
  const tempsByCategory = useMemo(() => {
    const groups: Record<string, HostMetricsType['temperatures']> = {
      CPU: [],
      GPU: [],
      Storage: [],
      Motherboard: [],
      System: [],
    }
    if (data) {
      for (const t of data.temperatures) {
        const cat = t.category || 'System'
        if (!groups[cat]) groups[cat] = []
        groups[cat].push(t)
      }
    }
    return groups
  }, [data])

  // Series builders for per-category temp 24h charts.
  const cpuTempSeries = useMemo<TempSeries[]>(() => {
    const hottest = pickHottest(tempHistory, 'CPU')
    return hottest ? [{ key: 'cpu', label: hottest[0], samples: hottest[1] }] : []
  }, [tempHistory])

  const gpuTempSeries = useMemo<TempSeries[]>(() => {
    return Object.entries(tempHistory)
      .filter(([, v]) => v.category === 'GPU')
      .map(([name, v]) => ({ key: `gpu:${name}`, label: shortGpu(name), samples: v.samples }))
  }, [tempHistory])

  const storageTempSeries = useMemo<TempSeries[]>(() => {
    return Object.entries(tempHistory)
      .filter(([, v]) => v.category === 'Storage')
      .map(([name, v]) => ({ key: `st:${name}`, label: name, samples: v.samples }))
  }, [tempHistory])

  const motherboardTempSeries = useMemo<TempSeries[]>(() => {
    return Object.entries(tempHistory)
      .filter(([, v]) => v.category === 'Motherboard' || v.category === 'System')
      .map(([name, v]) => ({ key: `mb:${name}`, label: name, samples: v.samples }))
  }, [tempHistory])

  return (
    <div className="space-y-4 p-3 sm:space-y-6 sm:p-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold text-white">Host Metrics</h1>
        <div className="flex items-center gap-3">
          {lastUpdated && (
            <span className="text-xs text-slate-500">
              Updated {lastUpdated.toLocaleTimeString()}
            </span>
          )}
          <button
            onClick={() => setAutoRefresh((v) => !v)}
            className={`flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs transition-colors ${
              autoRefresh
                ? 'bg-blue-600 text-white'
                : 'bg-slate-700 text-slate-400 hover:bg-slate-600'
            }`}
          >
            <RefreshCw size={12} className={autoRefresh ? 'animate-spin' : ''} />
            {autoRefresh ? 'Auto (5s)' : 'Paused'}
          </button>
        </div>
      </div>

      {!data ? (
        <div className="text-slate-500">Loading…</div>
      ) : (
        <>
          {/* ─── CPU ─────────────────────────────────────────────────── */}
          <HardwareSection title="CPU">
            <GaugeBar
              label={`Overall (${data.cpu.count_logical} logical / ${data.cpu.count_physical} physical cores)`}
              value={data.cpu.overall}
              sub={`Load avg: ${data.cpu.load_avg.join(' / ')}`}
            />
            <GaugeBar
              label="Pressure (CPU usage + load average)"
              value={cpuPressure(data.cpu.overall, data.cpu.load_avg[0], data.cpu.count_logical)}
              sub={`CPU ${data.cpu.overall.toFixed(0)} % · load ${data.cpu.load_avg[0].toFixed(1)} on ${data.cpu.count_logical} cores`
                + (data.cpu.load_avg[0] > data.cpu.count_logical
                  ? ` = ${(data.cpu.load_avg[0] / data.cpu.count_logical).toFixed(1)}× oversubscribed, processes are queueing`
                  : ' — no queue')}
            />
            {tempsByCategory.CPU.length > 0 && <TempChips items={tempsByCategory.CPU} />}
            <Divider />
            <CpuPressureHistoryChart cores={data.cpu.count_logical} />
            <CpuUsageHistoryChart />
            <LoadHistoryChart cores={data.cpu.count_logical} />
            <TemperatureChartForCategory
              title="CPU Temperature — Last 24h"
              series={cpuTempSeries}
              loaded={tempLoaded}
              showMovingAverages
            />
          </HardwareSection>

          {/* ─── Memory ──────────────────────────────────────────────── */}
          <HardwareSection title="Memory">
            <GaugeBar
              label="RAM Usage"
              value={data.memory.percent}
              sub={`Used: ${data.memory.used_gb.toFixed(1)} GB · Available: ${data.memory.available_gb.toFixed(1)} GB · Cached: ${data.memory.cached_gb.toFixed(1)} GB · Total: ${data.memory.total_gb.toFixed(1)} GB`}
            />
            <Divider />
            <RamUsageHistoryChart />
          </HardwareSection>

          {/* ─── GPU ─────────────────────────────────────────────────── */}
          {data.gpus.length > 0 && (
            <HardwareSection title="GPU">
              <div className="space-y-4">
                {data.gpus.map((g) => (
                  <div key={g.index} className="space-y-2">
                    <GaugeBar
                      label={`${g.name} (GPU ${g.index}) — Utilization`}
                      value={g.util_pct}
                      sub={`VRAM: ${(g.mem_used_mb / 1024).toFixed(2)} / ${(g.mem_total_mb / 1024).toFixed(2)} GiB · ${g.mem_pct.toFixed(0)}%`}
                    />
                    <GaugeBar
                      label={`${g.name} (GPU ${g.index}) — VRAM`}
                      value={g.mem_pct}
                    />
                  </div>
                ))}
              </div>
              {tempsByCategory.GPU.length > 0 && <TempChips items={tempsByCategory.GPU} />}
              <Divider />
              <GpuUsageHistoryChart metric="util_pct" title="GPU Utilization — Last 24h" />
              <GpuUsageHistoryChart metric="mem_pct" title="GPU VRAM Usage — Last 24h" />
              <TemperatureChartForCategory
                title="GPU Temperature — Last 24h"
                series={gpuTempSeries}
                loaded={tempLoaded}
              />
            </HardwareSection>
          )}

          {/* ─── Storage ─────────────────────────────────────────────── */}
          <HardwareSection title="Storage">
            {data.disks.length > 0 ? (
              <div className="space-y-3">
                {data.disks.map((d) => (
                  <GaugeBar
                    key={d.mountpoint}
                    label={`${d.label ?? d.mountpoint} (${d.mountpoint}, ${d.fstype})`}
                    value={d.percent}
                    sub={`Used: ${d.used_gb.toFixed(1)} GB / ${d.total_gb.toFixed(1)} GB · Free: ${d.free_gb.toFixed(1)} GB`}
                  />
                ))}
              </div>
            ) : (
              <p className="text-sm text-slate-500">No disk information available</p>
            )}
            {tempsByCategory.Storage.length > 0 && (
              <TempChips items={tempsByCategory.Storage} />
            )}
            <Divider />
            <DiskUsageHistoryChart />
            <TemperatureChartForCategory
              title="Drive Temperature — Last 24h"
              series={storageTempSeries}
              loaded={tempLoaded}
            />
            <Divider />
            <SmartCards drives={drives} />
          </HardwareSection>

          {/* ─── Motherboard ─────────────────────────────────────────── */}
          {(tempsByCategory.Motherboard.length > 0 || tempsByCategory.System.length > 0) && (
            <HardwareSection title="Motherboard">
              <TempChips
                items={[...tempsByCategory.Motherboard, ...tempsByCategory.System]}
                empty="No motherboard sensors"
              />
              {motherboardTempSeries.length > 0 && (
                <>
                  <Divider />
                  <TemperatureChartForCategory
                    title="Motherboard Temperature — Last 24h"
                    series={motherboardTempSeries}
                    loaded={tempLoaded}
                  />
                </>
              )}
            </HardwareSection>
          )}

          {/* ─── Network ─────────────────────────────────────────────── */}
          <HardwareSection title="Network I/O (cumulative)">
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              {[
                { label: 'Sent', value: `${data.network.bytes_sent_mb.toFixed(1)} MB` },
                { label: 'Received', value: `${data.network.bytes_recv_mb.toFixed(1)} MB` },
                { label: 'Pkts Sent', value: data.network.packets_sent.toLocaleString() },
                { label: 'Pkts Recv', value: data.network.packets_recv.toLocaleString() },
              ].map(({ label, value }) => (
                <div key={label} className="rounded-md border border-slate-700 bg-slate-900 p-3">
                  <p className="text-xs text-slate-500">{label}</p>
                  <p className="mt-1 text-sm font-semibold text-slate-200">{value}</p>
                </div>
              ))}
            </div>
          </HardwareSection>
        </>
      )}
    </div>
  )
}
