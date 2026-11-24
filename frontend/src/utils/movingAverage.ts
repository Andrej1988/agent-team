// Timestamps are milliseconds; gaps do not count as zero-valued samples.
export function movingAverage(
  data: Record<string, number>[],
  key: string,
  minutes: number,
): (number | undefined)[] {
  const windowMs = minutes * 60_000
  let start = 0
  let sum = 0
  let count = 0
  return data.map((point, index) => {
    if (Number.isFinite(point[key])) {
      sum += point[key]
      count++
    }
    while (start <= index && data[start].ts <= point.ts - windowMs) {
      if (Number.isFinite(data[start][key])) {
        sum -= data[start][key]
        count--
      }
      start++
    }
    // Preserve missing readings instead of drawing an average across a gap.
    return Number.isFinite(point[key]) && count > 0 ? sum / count : undefined
  })
}
