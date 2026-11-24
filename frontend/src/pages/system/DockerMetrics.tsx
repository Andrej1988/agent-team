import { useEffect, useState } from 'react'
import {
  Bar,
  BarChart,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { RefreshCw } from 'lucide-react'
import { api, type ContainerInfo, type DockerDiskUsage } from '../../system/api'

const TICK_STYLE = { fill: '#94a3b8', fontSize: 11 }

function StatusBadge({ status }: { status: string }) {
  const styles =
    status === 'running'
      ? 'bg-green-900/50 text-green-400 border-green-800'
      : status === 'exited'
        ? 'bg-red-900/50 text-red-400 border-red-800'
        : 'bg-slate-700 text-slate-400 border-slate-600'
  return (
    <span className={`rounded border px-1.5 py-0.5 text-xs ${styles}`}>{status}</span>
  )
}

function formatUptime(isoString: string): string {
  if (!isoString || isoString === '0001-01-01T00:00:00Z') return '—'
  const start = new Date(isoString)
  const now = new Date()
  const secs = Math.floor((now.getTime() - start.getTime()) / 1000)
  if (secs < 0) return '—'
  const h = Math.floor(secs / 3600)
  const m = Math.floor((secs % 3600) / 60)
  if (h >= 24) return `${Math.floor(h / 24)}d ${h % 24}h`
  if (h > 0) return `${h}h ${m}m`
  return `${m}m`
}

function mbToHuman(mb: number): string {
  if (mb >= 1024) return `${(mb / 1024).toFixed(1)} GB`
  return `${mb.toFixed(0)} MB`
}

export default function DockerMetrics() {
  const [containers, setContainers] = useState<ContainerInfo[]>([])
  const [disk, setDisk] = useState<DockerDiskUsage | null>(null)
  const [loading, setLoading] = useState(true)
  const [autoRefresh, setAutoRefresh] = useState(true)

  const load = () => {
    Promise.allSettled([api.docker.containers(), api.docker.disk()]).then(([c, d]) => {
      if (c.status === 'fulfilled') setContainers(c.value)
      if (d.status === 'fulfilled') setDisk(d.value)
      setLoading(false)
    })
  }

  useEffect(() => {
    load()
    if (!autoRefresh) return
    const id = setInterval(load, 15_000)
    return () => clearInterval(id)
  }, [autoRefresh])

  const running = containers.filter((c) => c.status === 'running').length
  const exited = containers.filter((c) => c.status !== 'running').length

  // Top 5 by memory
  const topByMem = [...containers]
    .filter((c) => c.status === 'running')
    .sort((a, b) => b.mem_mb - a.mem_mb)
    .slice(0, 5)

  const diskCategories = disk
    ? [
        { name: 'Images', ...disk.images },
        { name: 'Containers', ...disk.containers },
        { name: 'Volumes', ...disk.volumes },
        { name: 'Build Cache', ...disk.build_cache },
      ]
    : []

  return (
    <div className="space-y-4 p-3 sm:space-y-6 sm:p-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold text-white">Docker Metrics</h1>
        <button
          onClick={() => setAutoRefresh((v) => !v)}
          className={`flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs transition-colors ${
            autoRefresh
              ? 'bg-blue-600 text-white'
              : 'bg-slate-700 text-slate-400 hover:bg-slate-600'
          }`}
        >
          <RefreshCw size={12} className={autoRefresh ? 'animate-spin' : ''} />
          {autoRefresh ? 'Auto (15s)' : 'Paused'}
        </button>
      </div>

      {loading ? (
        <div className="text-slate-500">Loading…</div>
      ) : (
        <>
          {/* Summary row */}
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
            {[
              { label: 'Total Containers', value: containers.length },
              { label: 'Running', value: running, colour: 'text-green-400' },
              { label: 'Stopped / Exited', value: exited, colour: exited > 0 ? 'text-red-400' : 'text-slate-400' },
              {
                label: 'Total Docker Space',
                value: disk ? mbToHuman(disk.total_mb) : '—',
                colour: 'text-slate-200',
              },
            ].map(({ label, value, colour }) => (
              <div key={label} className="rounded-lg border border-slate-700 bg-slate-800 p-4">
                <p className="text-xs text-slate-500">{label}</p>
                <p className={`mt-1 text-2xl font-bold ${colour ?? 'text-white'}`}>{value}</p>
              </div>
            ))}
          </div>

          {/* Container table */}
          <div className="rounded-lg border border-slate-700 bg-slate-800 p-4">
            <h2 className="mb-3 text-sm font-semibold text-slate-200">Containers</h2>
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-slate-700 text-slate-500">
                    <th className="py-2 pr-3 text-left">Name</th>
                    <th className="py-2 pr-3 text-left">Status</th>
                    <th className="py-2 pr-3 text-right">CPU%</th>
                    <th className="py-2 pr-3 text-right">RAM</th>
                    <th className="py-2 pr-3 text-right">RAM%</th>
                    <th className="py-2 pr-3 text-right">Uptime</th>
                    <th className="py-2 text-left">Image</th>
                  </tr>
                </thead>
                <tbody>
                  {containers.length === 0 && (
                    <tr>
                      <td colSpan={7} className="py-4 text-center text-slate-600">
                        No containers found
                      </td>
                    </tr>
                  )}
                  {containers.map((c) => (
                    <tr key={c.id} className="border-b border-slate-700/40 hover:bg-slate-700/20">
                      <td className="py-2 pr-3 font-medium text-slate-200">{c.name}</td>
                      <td className="py-2 pr-3">
                        <StatusBadge status={c.status} />
                      </td>
                      <td className="py-2 pr-3 text-right text-slate-300">
                        {c.cpu_pct.toFixed(1)}
                      </td>
                      <td className="py-2 pr-3 text-right text-slate-300">
                        {mbToHuman(c.mem_mb)}
                      </td>
                      <td
                        className={`py-2 pr-3 text-right ${
                          c.mem_pct >= 90
                            ? 'text-red-400'
                            : c.mem_pct >= 70
                              ? 'text-amber-400'
                              : 'text-slate-300'
                        }`}
                      >
                        {c.mem_pct.toFixed(1)}%
                      </td>
                      <td className="py-2 pr-3 text-right text-slate-400">
                        {formatUptime(c.started_at)}
                      </td>
                      <td className="max-w-xs truncate py-2 text-slate-500">{c.image}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {/* Disk usage breakdown */}
          {disk && (
            <div className="grid gap-4 lg:grid-cols-2">
              {/* Category cards */}
              <div className="rounded-lg border border-slate-700 bg-slate-800 p-4">
                <h2 className="mb-3 text-sm font-semibold text-slate-200">
                  Disk Usage Breakdown
                  <span className="ml-2 text-xs font-normal text-slate-500">
                    Total: {mbToHuman(disk.total_mb)}
                  </span>
                </h2>
                <div className="grid grid-cols-2 gap-3">
                  {diskCategories.map(({ name, count, size_mb }) => (
                    <div
                      key={name}
                      className="rounded-md border border-slate-700 bg-slate-900 p-3"
                    >
                      <p className="text-xs text-slate-500">{name}</p>
                      <p className="mt-1 text-base font-bold text-slate-200">
                        {mbToHuman(size_mb)}
                      </p>
                      <p className="text-xs text-slate-600">{count} items</p>
                    </div>
                  ))}
                </div>
              </div>

              {/* Top 5 by memory chart */}
              {topByMem.length > 0 && (
                <div className="rounded-lg border border-slate-700 bg-slate-800 p-4">
                  <h2 className="mb-3 text-sm font-semibold text-slate-200">
                    Top Containers by Memory
                  </h2>
                  <ResponsiveContainer width="100%" height={170}>
                    <BarChart
                      layout="vertical"
                      data={topByMem.map((c) => ({ name: c.name, mem: c.mem_mb }))}
                      margin={{ top: 0, right: 16, bottom: 0, left: 0 }}
                    >
                      <XAxis
                        type="number"
                        tick={TICK_STYLE}
                        axisLine={false}
                        tickLine={false}
                        unit=" MB"
                      />
                      <YAxis
                        type="category"
                        dataKey="name"
                        tick={TICK_STYLE}
                        axisLine={false}
                        tickLine={false}
                        width={130}
                      />
                      <Tooltip
                        contentStyle={{
                          background: '#1e293b',
                          border: '1px solid #334155',
                          borderRadius: 6,
                        }}
                        formatter={(v: number) => [mbToHuman(v), 'RAM']}
                      />
                      <Bar dataKey="mem" radius={[0, 4, 4, 0]}>
                        {topByMem.map((_, i) => (
                          <Cell key={i} fill="#3b82f6" fillOpacity={0.8 - i * 0.1} />
                        ))}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  )
}
