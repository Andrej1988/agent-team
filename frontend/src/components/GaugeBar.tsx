
interface Props {
  label: string
  value: number   // 0–100
  sub?: string
  warnAt?: number
  critAt?: number
}

export default function GaugeBar({ label, value, sub, warnAt = 70, critAt = 90 }: Props) {
  const colour =
    value >= critAt
      ? 'bg-red-500'
      : value >= warnAt
        ? 'bg-amber-500'
        : 'bg-blue-500'

  const textColour =
    value >= critAt ? 'text-red-400' : value >= warnAt ? 'text-amber-400' : 'text-slate-200'

  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between text-sm">
        <span className="text-slate-300">{label}</span>
        <span className={`font-bold ${textColour}`}>{value.toFixed(1)}%</span>
      </div>
      <div className="h-2 w-full overflow-hidden rounded-full bg-slate-700">
        <div
          className={`h-full rounded-full transition-all duration-500 ${colour}`}
          style={{ width: `${Math.min(value, 100)}%` }}
        />
      </div>
      {sub && <p className="text-xs text-slate-500">{sub}</p>}
    </div>
  )
}
