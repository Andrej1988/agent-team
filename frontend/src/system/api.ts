// Types and calls for the System section (host metrics via the collector,
// Docker metrics via the read-only Docker proxy) — /api/system/...

async function get<T>(path: string): Promise<T> {
  const res = await fetch(path, { credentials: 'same-origin' })
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`
    try {
      const j = await res.json()
      if (j?.detail) detail = `${res.status} ${j.detail}`
    } catch { /* ignore */ }
    throw new Error(detail)
  }
  return res.json()
}

export interface CpuData {
  overall: number
  per_core: number[]
  load_avg: [number, number, number]
  count_logical: number
  count_physical: number
}

export interface MemoryData {
  total_gb: number
  used_gb: number
  available_gb: number
  percent: number
  cached_gb: number
}

export interface DiskData {
  mountpoint: string
  label?: string
  device: string
  fstype: string
  total_gb: number
  used_gb: number
  free_gb: number
  percent: number
}

export interface TempData {
  name: string
  temp: number
  category: string
}

export interface GpuData {
  index: number
  name: string
  util_pct: number
  mem_used_mb: number
  mem_total_mb: number
  mem_pct: number
}

export interface NetworkData {
  bytes_sent_mb: number
  bytes_recv_mb: number
  packets_sent: number
  packets_recv: number
  err_in: number
  err_out: number
}

export interface HostMetrics {
  cpu: CpuData
  memory: MemoryData
  disks: DiskData[]
  gpus: GpuData[]
  temperatures: TempData[]
  network: NetworkData
}

export interface ContainerInfo {
  id: string
  name: string
  status: string
  image: string
  started_at: string
  cpu_pct: number
  mem_mb: number
  mem_pct: number
  project: string | null
  service: string | null
}

export interface DockerDiskCategory {
  count: number
  size_mb: number
}

export interface DockerDiskUsage {
  images: DockerDiskCategory
  containers: DockerDiskCategory
  volumes: DockerDiskCategory
  build_cache: DockerDiskCategory
  total_mb: number
}

export interface DriveHealthDisk {
  path: string
  used: string
  available: string
  total: string
  percent: number
}

export interface DriveInfo {
  device: string
  drive_type: string
  nickname: string | null
  model: string | null
  overall_health: string | null
  age_years: number | null
  power_on_hours: number | null
  temperature_c: number | null
  raw_read_errors: number | null
  reallocated_sectors: number | null
  pending_sectors: number | null
  uncorrectable_sectors: number | null
  spin_retry_count: number | null
  reported_uncorrectable: number | null
  // NVMe-specific
  available_spare_pct?: number | null
  percentage_used?: number | null
  media_errors?: number | null
  critical_warning?: number
  data_read?: string | null
  data_written?: string | null
  power_cycles?: number | null
  unsafe_shutdowns?: number | null
  error_log_entries?: number | null
  estimated_health: string
  health_level: 'good' | 'warning' | 'error' | 'unknown'
}

export interface DriveHealth {
  available: boolean
  last_run: string | null
  disk_space: DriveHealthDisk[]
  drives: DriveInfo[]
  error?: string
}

export const api = {
  host: () => get<HostMetrics>('/api/system/host'),
  drives: () => get<DriveHealth>('/api/system/host/drives'),
  temperatureHistory: (hours = 24) =>
    get<Record<string, { category: string; samples: { ts: number; temp: number }[] }>>(
      `/api/system/host/temperature-history?hours=${hours}`,
    ),
  loadHistory: (hours = 24) =>
    get<{
      cores: number
      samples: { ts: number; cpu_pct: number; load1: number; load5: number; load15: number }[]
    }>(`/api/system/host/load-history?hours=${hours}`),
  usageHistory: (hours = 24) =>
    get<{
      memory: { samples: { ts: number; percent: number; used_gb: number }[] }
      gpus: { index: number; name: string; samples: { ts: number; util_pct: number; mem_pct: number }[] }[]
      disks: {
        mountpoint: string
        label: string | null
        device: string | null
        fstype: string | null
        total_gb: number | null
        samples: { ts: number; percent: number; used_gb: number }[]
      }[]
    }>(`/api/system/host/usage-history?hours=${hours}`),
  docker: {
    containers: () => get<ContainerInfo[]>('/api/system/docker/containers'),
    disk: () => get<DockerDiskUsage>('/api/system/docker/disk'),
  },
}
