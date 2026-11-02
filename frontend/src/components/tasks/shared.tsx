// Shared building blocks of the Tasks page (pages/Tasks.tsx) and the
// pipeline panel (PipelinePanel.tsx): status/backend badges, the model
// picker and the stream-event shape. Lifted out of Tasks.tsx so the two
// don't import each other.
import { createContext, useContext, type ReactNode } from 'react'
import { Loader2 } from 'lucide-react'
import type { AgentBackend, ModelCatalog, TaskStatus } from '../../api/client'

// Tasks v2 (docs/TASKS-V2.md): the internal statuses collapse into seven
// German states that say whose turn it is.
export type TaskPhase = 'idee' | 'ausarbeitung' | 'freigabe' | 'arbeit' | 'wartet' | 'erledigt' | 'geparkt' | 'geschlossen'

export const PHASE_LABEL: Record<TaskPhase, string> = {
  idee: 'Idee',
  ausarbeitung: 'Wird ausgearbeitet',
  freigabe: 'Zur Freigabe',
  arbeit: 'In Arbeit',
  wartet: 'Wartet auf dich',
  erledigt: 'Erledigt',
  geparkt: 'Geparkt',
  geschlossen: 'Geschlossen',
}

const PHASE_STYLE: Record<TaskPhase, string> = {
  idee: 'bg-slate-700 text-slate-300 border-slate-600',
  ausarbeitung: 'bg-blue-900/40 text-blue-300 border-blue-800',
  freigabe: 'bg-amber-900/40 text-amber-300 border-amber-800',
  arbeit: 'bg-blue-900/40 text-blue-300 border-blue-800',
  wartet: 'bg-violet-900/40 text-violet-300 border-violet-800',
  erledigt: 'bg-green-900/40 text-green-400 border-green-800',
  geparkt: 'bg-indigo-900/40 text-indigo-300 border-indigo-800',
  geschlossen: 'bg-slate-800 text-slate-500 border-slate-700',
}

type PhaseInput = { status: TaskStatus; paused_reason?: string | null; proposed_at?: string | null }

export function taskPhase(t: PhaseInput): TaskPhase {
  switch (t.status) {
    case 'new': return t.proposed_at ? 'freigabe' : 'idee'
    case 'refining': return 'ausarbeitung'
    case 'refined': return 'freigabe'
    case 'planning':
    case 'running': return 'arbeit'
    // Idle between stages: autopilot parked it (budget / wait period) = still
    // in progress; otherwise the next stage waits for a click.
    case 'plan_ready': return t.paused_reason ? 'arbeit' : 'wartet'
    case 'done': return 'erledigt'
    case 'backlog': return 'geparkt'
    case 'closed': return 'geschlossen'
    default: return 'wartet' // awaiting_input, blocked, failed, limit_reached
  }
}

const ACTIVE_STATUSES = new Set<TaskStatus>(['refining', 'planning', 'running'])

export function StatusBadge({ task }: { task: PhaseInput }) {
  const phase = taskPhase(task)
  return (
    <span
      className={`inline-flex items-center gap-1 whitespace-nowrap rounded border px-1.5 py-0.5 text-[10px] tracking-wide ${PHASE_STYLE[phase]}`}
      title={task.paused_reason ? `${task.status} · ${task.paused_reason}` : task.status}
    >
      {/* An agent is running right now (not just "in progress" between stages). */}
      {ACTIVE_STATUSES.has(task.status) && <Loader2 size={10} className="animate-spin" />}
      {PHASE_LABEL[phase]}
    </span>
  )
}

// team.yaml `repo_prefix` (from /roles), stripped for display: "demo-swing" -> "swing".
let repoPrefix = ''
export function setRepoPrefix(prefix: string | undefined) {
  repoPrefix = prefix ?? ''
}

export function shortRepo(name: string) {
  if (!repoPrefix) return name
  if (name === repoPrefix.replace(/-$/, '')) return 'core'
  return name.startsWith(repoPrefix) ? name.slice(repoPrefix.length) || 'core' : name
}

// One line of a session JSONL as re-emitted by the SSE endpoint.
export interface StreamEvent {
  ts?: string
  event?: string
  text?: string
  name?: string
  input?: Record<string, unknown>
  is_error?: boolean
  message?: string
  model?: string
  spec_chars?: number
  summary_chars?: number
  plan_chars?: number
  result_chars?: number
  chars?: number
  duration_ms?: number
  num_turns?: number
  cost_usd?: number
  status?: TaskStatus
  refined_spec?: string | null
  execute_summary?: string | null
  error?: string | null
  stop_reason?: string | null
  stage?: { seq?: number; role?: string; status?: string; verdict?: string | null; summary?: string | null }
  before?: string[]
  after?: string[]
}

// Model picker presets, grouped by the agent backend that runs them. The
// worker derives the backend from the model ID itself (_backend_for() in
// worker.py) — there is no separate backend switch, picking a claude-* model
// routes the task through the Claude SDK and a gpt-* one through the Codex
// CLI. Keep each group ordered cheap→expensive so the dropdown reads
// naturally. Claude bills the claude.ai subscription (and can hit the
// monthly cap → limit_reached); Codex bills the ChatGPT plan.
//
// These static lists are only the FALLBACK. The live list comes from
// GET /api/tasks/models (the CLIs' own catalogs, see
// claude-agent/model_catalog.py) via <ModelCatalogProvider>; the statics
// cover the moments before that request resolves or when the bridge
// container is down. Bump them when a generation ships, but the dropdown
// no longer depends on it.
export interface ModelOption {
  value: string
  label: string
  backend: AgentBackend
  // From the live catalog: alias names the CLI maps onto this id
  // ("opus" → claude-opus-5-5), tooltip text, and placeholder entries for
  // models the installed CLI is too old for (disabled + hint).
  aliases?: string[]
  description?: string
  disabled?: boolean
  hint?: string
}

export const CLAUDE_MODELS: ModelOption[] = [
  { value: 'claude-haiku-4-5', label: 'Haiku 4.5 (cheapest)', backend: 'claude' },
  { value: 'claude-sonnet-5', label: 'Sonnet 5 (balanced)', backend: 'claude' },
  { value: 'claude-opus-5', label: 'Opus 5 (strong coding)', backend: 'claude' },
  { value: 'claude-opus-5-5', label: 'Opus 5.5 (strong coding, cheaper)', backend: 'claude' },
  { value: 'claude-fable-5-1', label: 'Fable 5.1 (most capable)', backend: 'claude' },
]

export const CODEX_MODELS: ModelOption[] = [
  { value: 'gpt-5.6-luna', label: 'Luna 5.6 (cheapest)', backend: 'codex' },
  { value: 'gpt-5.6-terra', label: 'Terra 5.6 (balanced)', backend: 'codex' },
  { value: 'gpt-5.6-sol', label: 'Sol 5.6 (strong coding)', backend: 'codex' },
  { value: 'gpt-6-luna', label: 'Luna 6', backend: 'codex' },
  { value: 'gpt-6-sol', label: 'Sol 6', backend: 'codex' },
  { value: 'gpt-6-astra', label: 'Astra 6 (most capable)', backend: 'codex' },
]

export const MODEL_OPTIONS: ModelOption[] = [...CLAUDE_MODELS, ...CODEX_MODELS]

// ---- Live catalog -------------------------------------------------------
// Tasks.tsx fetches /api/tasks/models once and publishes it through this
// context; ModelPicker (also inside PipelinePanel's stage rows) reads it.
// A module-level mirror lets the plain modelLabel() helper resolve live
// ids too (it's called from non-component code paths).
let liveOptions: ModelOption[] | null = null

export function catalogToOptions(cat: ModelCatalog | null | undefined): ModelOption[] | null {
  if (!cat) return null
  const claude = (cat.claude?.models ?? []).map((m) => ({
    value: m.value, label: m.label, backend: 'claude' as const,
    aliases: m.aliases, description: m.description, disabled: m.disabled, hint: m.hint,
  }))
  const codex = (cat.codex?.models ?? []).map((m) => ({
    value: m.value, label: m.label, backend: 'codex' as const,
    aliases: m.aliases, description: m.description, disabled: m.disabled, hint: m.hint,
  }))
  // Either half may be empty (CLI missing, container down) — keep the
  // static presets for that half so the picker never loses a backend.
  const claudeOpts = claude.some((o) => !o.disabled) ? claude : [...CLAUDE_MODELS, ...claude.filter((o) => o.disabled)]
  const codexOpts = codex.length ? codex : CODEX_MODELS
  return [...claudeOpts, ...codexOpts]
}

export const ModelCatalogContext = createContext<ModelCatalog | null>(null)

export function ModelCatalogProvider({ catalog, children }: { catalog: ModelCatalog | null; children: ReactNode }) {
  liveOptions = catalogToOptions(catalog)
  return <ModelCatalogContext.Provider value={catalog}>{children}</ModelCatalogContext.Provider>
}

function currentOptions(): ModelOption[] {
  return liveOptions ?? MODEL_OPTIONS
}

// Find the preset for an id: exact match, then alias ("opus"), then the
// date-suffixed/bare pair (claude-haiku-4-5 ↔ claude-haiku-4-5-20251001).
function findOption(id: string): ModelOption | undefined {
  const opts = currentOptions()
  // Same id modulo a date suffix (haiku-4-5-20251001) or the CLI's
  // [1m] context flavour (claude-opus-5-5[1m]).
  const norm = (v: string) => v.replace(/\[.*?\]$/, '').replace(/-\d{8}$/, '')
  const bare = norm(id)
  return (
    opts.find((o) => o.value === id)
    ?? opts.find((o) => o.aliases?.includes(id))
    ?? opts.find((o) => !!o.value && norm(o.value) === bare)
  )
}

// Mirrors resolve_backend() in backend/routers/tasks.py and _backend_for()
// in worker.py. Only used for the UI hint — the worker is authoritative.
export function backendFor(
  id: string | null | undefined,
  envDefault: AgentBackend,
): AgentBackend {
  const m = (id || '').trim().toLowerCase()
  if (!m) return envDefault
  const preset = findOption(m)
  if (preset) return preset.backend
  if (m.startsWith('claude-') || ['haiku', 'sonnet', 'opus', 'fable', 'default'].includes(m)) return 'claude'
  if (/^(gpt-|gpt5|gpt6|o1|o3|o4|codex)/.test(m)) return 'codex'
  return envDefault
}

// Short, human-friendly name for a resolved model ID. Falls back to the raw
// ID for anything we don't have a preset for (so a custom WORKER_MODEL still
// shows up legibly).
export function modelLabel(id: string | null | undefined): string {
  if (!id) return 'CLI fallback'
  const preset = findOption(id)
  if (!preset) return id
  // Strip the parenthetical price hint from the preset label so the inline
  // "Default (env: …)" rendering stays compact. An alias ("opus") shows
  // what it resolves to right now, since that moves with CLI upgrades.
  const label = preset.label.replace(/\s*\(.*\)$/, '')
  return preset.value !== id && preset.aliases?.includes(id) ? `${id} → ${label}` : label
}

// The Default entry names both the model the env resolves to AND the backend
// it lands on, so "Default" is never an opaque label.
export function defaultOptionLabel(
  envDefault: string | null | undefined,
  envBackend: AgentBackend | undefined,
): string {
  const backend = envBackend || 'claude'
  if (!envDefault) return `Default (${backend} CLI fallback)`
  return `Default (env: ${modelLabel(envDefault)} · ${backend})`
}

export const BACKEND_STYLE: Record<AgentBackend, string> = {
  claude: 'bg-amber-900/40 text-amber-300 border-amber-800',
  codex: 'bg-teal-900/40 text-teal-300 border-teal-800',
}

export function BackendBadge({ backend }: { backend: AgentBackend }) {
  return (
    <span className={`rounded border px-1 py-0.5 text-[9px] uppercase tracking-wider ${BACKEND_STYLE[backend]}`}>
      {backend}
    </span>
  )
}

export interface ModelPickerProps {
  label: string
  value: string
  onChange: (value: string) => void
  disabled: boolean
  envDefault: string | null | undefined
  envBackend: AgentBackend | undefined
  resolvedBackend: AgentBackend | undefined
  // Compact variant for stage rows (no uppercase caption).
  compact?: boolean
  // Pipeline stages are pinned to the plan model's backend — offer only
  // that backend's models (the worker coerces anything else anyway).
  onlyBackend?: AgentBackend
}

/** Model dropdown grouped by agent backend. Selecting a model IS selecting
 *  the backend — the badge next to the label shows where the current choice
 *  routes so the coupling isn't hidden. */
export function ModelPicker({
  label, value, onChange, disabled, envDefault, envBackend, resolvedBackend, compact, onlyBackend,
}: ModelPickerProps) {
  const envFallback = envBackend || 'claude'
  // For the Default entry the worker resolves the env model's own backend,
  // which can differ from TASKS_AGENT_BACKEND; prefer what the API computed.
  const active = value ? backendFor(value, envFallback) : (resolvedBackend || envFallback)
  const catalog = useContext(ModelCatalogContext)
  const options = catalogToOptions(catalog) ?? MODEL_OPTIONS
  const claudeOpts = options.filter((o) => o.backend === 'claude')
  const codexOpts = options.filter((o) => o.backend === 'codex')
  const ver = (b: AgentBackend) => {
    const v = catalog?.[b]?.cli_version
    return v ? ` · CLI ${v}` : ''
  }
  const optionTitle = (o: ModelOption) =>
    [o.aliases?.length ? `alias: ${o.aliases.join(', ')}` : '', o.description, o.hint].filter(Boolean).join(' — ')
  return (
    <label className={compact ? 'flex items-center gap-1.5' : 'flex flex-col gap-1'}>
      <span className={`flex items-center gap-1.5 text-[11px] tracking-wider text-slate-500 ${compact ? '' : 'uppercase'}`}>
        {label}
        <BackendBadge backend={active} />
      </span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        className={`rounded border border-slate-700 bg-slate-900 text-slate-200 focus:border-blue-500 focus:outline-none disabled:opacity-60 ${
          compact ? 'px-1.5 py-0.5 text-[11px]' : 'px-2 py-1.5 text-sm'
        }`}
      >
        <option value="">{defaultOptionLabel(envDefault, resolvedBackend || envFallback)}</option>
        {onlyBackend !== 'codex' && <optgroup label={`Claude (claude.ai plan)${ver('claude')}`}>
          {claudeOpts.map((o, i) => (
            <option key={o.value || `disabled-${i}`} value={o.value} disabled={!!o.disabled} title={optionTitle(o)}>
              {o.disabled ? `${o.label} — ${o.hint || 'not available'}` : o.label}
            </option>
          ))}
        </optgroup>}
        {onlyBackend !== 'claude' && <optgroup label={`Codex (ChatGPT plan)${ver('codex')}`}>
          {codexOpts.map((o, i) => (
            <option key={o.value || `disabled-${i}`} value={o.value} disabled={!!o.disabled} title={optionTitle(o)}>
              {o.disabled ? `${o.label} — ${o.hint || 'not available'}` : o.label}
            </option>
          ))}
        </optgroup>}
        {value && !options.some((o) => o.value === value) && (
          // Preserve an unknown override (hand-set via the API) instead of
          // silently snapping the select back to Default.
          <optgroup label="Custom">
            <option value={value}>{value}</option>
          </optgroup>
        )}
      </select>
    </label>
  )
}

/** One-line status of the live catalog under the model pickers: CLI
 *  versions, a "newer CLI published" hint (models like a fresh Opus are
 *  gated behind it → rebuild the bridge image), and any catalog error. */
export function CatalogHint({
  onRefresh, refreshing, onUpgrade, upgrading, upgradeLog,
}: {
  onRefresh?: () => void
  refreshing?: boolean
  onUpgrade?: () => void
  upgrading?: boolean
  upgradeLog?: string[] | null
}) {
  const cat = useContext(ModelCatalogContext)
  if (!cat) return null
  const parts: string[] = []
  const warn: string[] = []
  let updateAvailable = false
  for (const b of ['claude', 'codex'] as const) {
    const s = cat[b]
    if (s.cli_version) parts.push(`${b} ${s.cli_version}`)
    if (s.update_available && s.latest_version) {
      updateAvailable = true
      warn.push(`${b} CLI ${s.latest_version} published (installed ${s.cli_version})`)
    }
    for (const m of s.models) if (m.disabled) warn.push(`${m.label}: ${m.hint}`)
    if (s.error) warn.push(`${b}: ${s.error}`)
  }
  if (cat.error) warn.push(cat.error)
  const busy = !!refreshing || !!upgrading
  return (
    <div className="flex flex-col gap-0.5 text-[10px] text-slate-500">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5">
        <span>Models from CLI catalog{parts.length ? ` (${parts.join(' · ')})` : ''}{cat.cached ? ', cached' : ''}</span>
        {onRefresh && (
          <button type="button" onClick={onRefresh} disabled={busy} className="underline decoration-dotted hover:text-slate-300 disabled:opacity-50">
            {refreshing ? 'refreshing…' : 'refresh'}
          </button>
        )}
        {onUpgrade && (updateAvailable || upgrading) && (
          <button
            type="button"
            onClick={onUpgrade}
            disabled={busy}
            title="npm-upgrade both CLIs inside the bridge container (~1 min). Running sessions keep their binary; the next task uses the new CLI. The worker also does this daily when idle."
            className="rounded border border-amber-800 bg-amber-900/30 px-1.5 py-0.5 text-amber-300 hover:bg-amber-900/50 disabled:opacity-50"
          >
            {upgrading ? 'upgrading CLIs…' : 'upgrade CLIs now'}
          </button>
        )}
        {warn.map((w) => (
          <span key={w} className="text-amber-400/90">⚠ {w}</span>
        ))}
      </div>
      {upgradeLog && upgradeLog.length > 0 && (
        <pre className="whitespace-pre-wrap font-mono text-[10px] text-slate-400">{upgradeLog.join('\n')}</pre>
      )}
    </div>
  )
}
