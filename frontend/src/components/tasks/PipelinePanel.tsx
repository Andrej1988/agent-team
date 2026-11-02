// Pipeline panel for a task in mode='pipeline': stage timeline (history +
// editable pending tail), exit criteria and limits, autopilot, and the
// controls (Plan / Run next / Stop / Resume / Answer / Skip / Retry).
//
// All decisions live in the backend (tasks_db pipeline section); this
// component only renders state and posts user intents. Every action goes
// through `act()` so a 409 (refused transition) surfaces as the page error.
import { useEffect, useState } from 'react'
import {
  ArrowDown, ArrowUp, ListChecks, Play, Plus, RotateCcw, Save, Send, SkipForward, Square, Trash2,
} from 'lucide-react'
import {
  api,
  type AgentBackend,
  type AgentRole,
  type AutopilotLevel,
  type RolesResponse,
  type Task,
  type TaskCost,
  type TaskStage,
  type WorkerDefaults,
} from '../../api/client'
import { ModelPicker, backendFor, modelLabel } from './shared'

interface DraftStage {
  key: number
  role: string
  goal: string
  model: string
}

interface Props {
  task: Task
  stages: TaskStage[]
  roles: RolesResponse | null
  workerDefaults: WorkerDefaults | null
  cost: TaskCost | null
  busy: boolean
  onChanged: (t: Task) => void
  // Persist pending edits of the task form (title, details, models) before
  // a (re-)plan so the orchestrator reads the latest row. null = save failed.
  onBeforePlan?: () => Promise<Task | null>
  onOpenStream: (kind: string) => void
  onError: (msg: string) => void
}

const STAGE_STYLE: Record<string, string> = {
  pending: 'bg-slate-800 text-slate-400 border-slate-700',
  running: 'bg-blue-900/40 text-blue-300 border-blue-800',
  awaiting_input: 'bg-violet-900/40 text-violet-300 border-violet-800',
  done: 'bg-green-900/40 text-green-400 border-green-800',
  failed: 'bg-red-900/40 text-red-300 border-red-800',
  skipped: 'bg-slate-800 text-slate-500 border-slate-700',
}
const VERDICT_STYLE: Record<string, string> = {
  pass: 'text-green-400',
  fail: 'text-red-300',
  needs_input: 'text-violet-300',
  blocked: 'text-orange-300',
}
const KIND_STYLE: Record<string, string> = {
  advise: 'bg-sky-900/40 text-sky-300 border-sky-800',
  implement: 'bg-fuchsia-900/40 text-fuchsia-300 border-fuchsia-800',
}
// Statuses in which the pending tail + limits may be edited (mirrors
// tasks_db.PLAN_EDITABLE_STATUS) and in which Plan / Re-plan is offered
// (tasks_db.PLAN_ELIGIBLE_STATUS).
const PLAN_EDITABLE = new Set<string>(['plan_ready', 'awaiting_input', 'blocked', 'failed'])
const PLAN_ELIGIBLE = new Set<string>(['new', 'refined', 'failed', 'limit_reached', 'plan_ready', 'blocked'])

const BTN = 'flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs disabled:opacity-50'
const SMALL_BTN = 'rounded border border-slate-700 px-1.5 py-0.5 text-[10px] text-slate-300 hover:bg-slate-700/50 disabled:opacity-40'
const INPUT = 'rounded border border-slate-700 bg-slate-950 px-2 py-1 text-[12px] text-slate-200 focus:border-blue-500 focus:outline-none disabled:opacity-60'

let draftKey = 1

function fmtTokens(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k tok` : `${n} tok`
}

// $ when the run carried a cost figure, else its tokens, else "no $ data".
function fmtCost(s: TaskStage): string {
  if (s.attempt === 0 || s.status === 'pending') return ''
  if (s.metered) return `$${(s.cost_usd || 0).toFixed(2)}${s.tokens ? ` · ${fmtTokens(s.tokens)}` : ''}`
  if (s.tokens) return fmtTokens(s.tokens)
  return s.status === 'done' || s.status === 'awaiting_input' ? 'no $ data' : ''
}

function KindBadge({ kind }: { kind: string }) {
  return (
    <span className={`rounded border px-1 py-0.5 text-[9px] uppercase tracking-wider ${KIND_STYLE[kind] ?? KIND_STYLE.advise}`}>
      {kind}
    </span>
  )
}

// ---- History row (done / running / awaiting / failed / skipped) ---------

interface HistoryRowProps {
  s: TaskStage
  role: AgentRole | undefined
  open: boolean
  disabled: boolean
  canSkip: boolean
  canRetry: boolean
  onToggle: () => void
  onOpenStream: () => void
  onSkip: () => void
  onRetry: () => void
}

function HistoryRow({ s, role, open, disabled, canSkip, canRetry, onToggle, onOpenStream, onSkip, onRetry }: HistoryRowProps) {
  const cost = fmtCost(s)
  const openFindings = s.findings.filter((f) => !f.resolved)
  return (
    <li id={`task-stage-${s.task_id}-${s.seq}`} className="rounded border border-slate-800 bg-slate-950/60 px-2 py-1.5">
      <div className="flex flex-wrap items-center gap-2 text-[12px]">
        <span className="w-5 shrink-0 text-right text-slate-500">{s.seq}.</span>
        <span className="font-medium text-slate-200">{role?.label ?? s.role}</span>
        <KindBadge kind={s.kind} />
        <span className={`rounded border px-1.5 py-0.5 text-[10px] uppercase tracking-wider ${STAGE_STYLE[s.status] ?? ''}`}>
          {s.status}
        </span>
        {s.verdict && <span className={`text-[11px] ${VERDICT_STYLE[s.verdict] ?? 'text-slate-400'}`}>{s.verdict}</span>}
        {s.attempt > 1 && <span className="text-[10px] text-slate-500">attempt {s.attempt}</span>}
        {s.model && <span className="text-[10px] text-slate-500">{modelLabel(s.model)}</span>}
        {cost && <span className="text-[10px] text-slate-500">{cost}</span>}
        {openFindings.length > 0 && (
          <span className="text-[10px] text-amber-300">{openFindings.length} open finding{openFindings.length === 1 ? '' : 's'}</span>
        )}
        <span className="ml-auto flex items-center gap-1">
          <button onClick={onOpenStream} className={SMALL_BTN} title="Stream this stage's agent log">Log</button>
          {(s.summary || s.findings.length > 0 || s.error || s.goal) && (
            <button onClick={onToggle} className={SMALL_BTN}>{open ? 'Less' : 'More'}</button>
          )}
          {canRetry && (
            <button onClick={onRetry} disabled={disabled} className={`${SMALL_BTN} border-amber-800 text-amber-300`} title="Run this stage again (new attempt)">
              Retry
            </button>
          )}
          {canSkip && (
            <button onClick={onSkip} disabled={disabled} className={SMALL_BTN} title="Skip this stage">Skip</button>
          )}
        </span>
      </div>
      {s.error && (
        <details className="mt-1 text-[11px] text-red-300">
          <summary className="cursor-pointer">Technische Fehlermeldung</summary>
          <div className="whitespace-pre-wrap break-words">{s.error}</div>
        </details>
      )}
      {open && (
        <div className="mt-1 flex flex-col gap-1 text-[11px] text-slate-300">
          {s.goal && <div><span className="text-slate-500">goal: </span>{s.goal}</div>}
          {s.summary && <div className="whitespace-pre-wrap"><span className="text-slate-500">summary: </span>{s.summary}</div>}
          {s.findings.length > 0 && (
            <ul className="flex flex-col gap-0.5">
              {s.findings.map((f) => (
                <li key={f.id} className={f.resolved ? 'text-slate-500 line-through' : ''}>
                  <span className="font-mono text-[10px] text-slate-400">[{f.id} {f.severity}]</span> {f.text}
                </li>
              ))}
            </ul>
          )}
          {s.questions.length > 0 && (
            <ul className="flex flex-col gap-0.5 text-violet-200">
              {s.questions.map((q, i) => <li key={i}>? {q}</li>)}
            </ul>
          )}
        </div>
      )}
    </li>
  )
}

// ---- Pending (editable) row ----------------------------------------------

interface DraftRowProps {
  d: DraftStage
  index: number
  total: number
  seq: number
  role: AgentRole | undefined
  isGate: boolean
  editable: boolean
  disabled: boolean
  envDefault: string | null | undefined
  envBackend: AgentBackend | undefined
  resolvedBackend: AgentBackend
  planBackend: AgentBackend
  onChange: (patch: Partial<DraftStage>) => void
  onMove: (dir: -1 | 1) => void
  onRemove: () => void
}

function DraftRow({
  d, index, total, seq, role, isGate, editable, disabled, envDefault, envBackend, resolvedBackend, planBackend, onChange, onMove, onRemove,
}: DraftRowProps) {
  const unsandboxed = role?.kind === 'advise' && resolvedBackend === 'codex'
  const locked = !editable || disabled
  return (
    <li className="rounded border border-dashed border-slate-700 bg-slate-950/40 px-2 py-1.5">
      <div className="flex flex-wrap items-center gap-2 text-[12px]">
        <span className="w-5 shrink-0 text-right text-slate-500">{seq}.</span>
        <span className="font-medium text-slate-200">{role?.label ?? d.role}</span>
        <KindBadge kind={role?.kind ?? 'advise'} />
        <span className="rounded border border-slate-700 bg-slate-800 px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-slate-400">pending</span>
        {isGate && <span className="text-[10px] text-slate-500" title="Gate stage — cannot be skipped">gate</span>}
        {unsandboxed && (
          <span
            className="rounded border border-orange-800 bg-orange-900/30 px-1 py-0.5 text-[9px] uppercase tracking-wider text-orange-300"
            title="Advise stage on Codex: the read-only rule is prompt-only (Codex has no tool allow-list). Pick a Claude model to enforce it. Cost-wise both backends are subscription-limited."
          >
            unsandboxed
          </span>
        )}
        <span className="ml-auto flex items-center gap-1">
          <ModelPicker
            compact
            label="model"
            value={d.model}
            onChange={(v) => onChange({ model: v })}
            disabled={locked}
            envDefault={envDefault}
            envBackend={envBackend}
            resolvedBackend={resolvedBackend}
            onlyBackend={planBackend}
          />
          <button onClick={() => onMove(-1)} disabled={locked || index === 0} className={SMALL_BTN} aria-label="Move up"><ArrowUp size={11} /></button>
          <button onClick={() => onMove(1)} disabled={locked || index === total - 1} className={SMALL_BTN} aria-label="Move down"><ArrowDown size={11} /></button>
          <button onClick={onRemove} disabled={locked} className={`${SMALL_BTN} border-red-900 text-red-300`} aria-label="Remove stage"><Trash2 size={11} /></button>
        </span>
      </div>
      <textarea
        value={d.goal}
        onChange={(e) => onChange({ goal: e.target.value })}
        disabled={locked}
        rows={2}
        placeholder="Goal for this stage — what to check / produce, what good looks like"
        className={`${INPUT} mt-1 w-full`}
      />
    </li>
  )
}

// ---- Panel -----------------------------------------------------------------

export default function PipelinePanel({
  task, stages, roles, workerDefaults, cost, busy, onChanged, onBeforePlan, onOpenStream, onError,
}: Props) {
  const status = task.status
  const plan = task.plan ?? {
    exit_criteria: [],
    max_rounds: roles?.defaults.max_rounds ?? 5,
    max_stages: roles?.defaults.max_stages ?? 20,
    rationale: '',
  }
  const roleById = new Map<string, AgentRole>((roles?.roles ?? []).map((r) => [r.id, r]))
  const stageRoles = (roles?.roles ?? []).filter((r) => r.is_stage)
  const gateRoles = new Set(roles?.gate_roles ?? ['backtest-auditor', 'reviewer'])
  const liveRepos = new Set(roles?.live_bot_repos ?? [])
  const touchesLive = task.repos.some((r) => liveRepos.has(r))

  const pending = stages.filter((s) => s.status === 'pending')
  const history = stages.filter((s) => s.status !== 'pending')
  const awaiting = stages.find((s) => s.status === 'awaiting_input') ?? null
  const failed = stages.filter((s) => s.status === 'failed')
  const planEditable = PLAN_EDITABLE.has(status)

  const [draft, setDraft] = useState<DraftStage[]>([])
  const [criteria, setCriteria] = useState('')
  const [maxRounds, setMaxRounds] = useState(plan.max_rounds)
  const [maxStages, setMaxStages] = useState(plan.max_stages)
  const [budget, setBudget] = useState('')
  const [dirty, setDirty] = useState(false)
  const [acting, setActing] = useState(false)
  const [answer, setAnswer] = useState('')
  const [addRole, setAddRole] = useState('implementation-engineer')
  const [addGoal, setAddGoal] = useState('')
  const [open, setOpen] = useState<Record<number, boolean>>({})

  // Re-seed the editable draft from the server state whenever it changes,
  // unless the user has unsaved edits (those are never clobbered by polls).
  const signature = JSON.stringify([
    task.id, pending.map((s) => [s.seq, s.role, s.goal, s.model]), plan, task.budget_usd,
  ])
  useEffect(() => {
    if (dirty) return
    setDraft(pending.map((s) => ({ key: draftKey++, role: s.role, goal: s.goal, model: s.model ?? '' })))
    setCriteria(plan.exit_criteria.join('\n'))
    setMaxRounds(plan.max_rounds)
    setMaxStages(plan.max_stages)
    setBudget(task.budget_usd == null ? '' : String(task.budget_usd))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature, dirty])

  const disabled = busy || acting
  const historySeqMax = history.reduce((m, s) => Math.max(m, s.seq), 0)

  const act = async (fn: () => Promise<Task>, after?: (t: Task) => void) => {
    setActing(true)
    try {
      const t = await fn()
      onChanged(t)
      after?.(t)
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e))
    } finally {
      setActing(false)
    }
  }

  const planBody = () => ({
    stages: draft.map((d) => ({ role: d.role, goal: d.goal.trim(), model: d.model || null })),
    exit_criteria: criteria.split('\n').map((l) => l.trim()).filter(Boolean),
    max_rounds: maxRounds,
    max_stages: maxStages,
    budget_usd: budget.trim() === '' ? null : Number(budget),
  })

  const savePlan = () => act(() => api.tasks.updatePlan(task.id, planBody()), () => setDirty(false))
  const runNext = () =>
    act(
      async () => {
        if (dirty) {
          await api.tasks.updatePlan(task.id, planBody())
          setDirty(false)
        }
        return api.tasks.nextStage(task.id)
      },
      (t) => {
        if (t.last_busy_kind?.startsWith('stage_')) onOpenStream(t.last_busy_kind)
      },
    )
  const replan = () => {
    const what = history.length + pending.length > 0 ? 'Re-plan' : 'Plan'
    if (!confirm(
      `${what} task #${task.id}?\n\nThe orchestrator agent (read-only) ${history.length ? 'replaces the pending stages' : 'drafts the stages'}. ` +
      'Bei aktiviertem Autopilot können geplante Stufen anschließend automatisch starten.',
    )) return
    void act(
      async () => {
        if (onBeforePlan && (await onBeforePlan()) === null) throw new Error('Could not save the task before planning.')
        return api.tasks.plan(task.id)
      },
      () => { setDirty(false); onOpenStream('plan') },
    )
  }
  const stop = () => {
    if (!confirm(`Stop the pipeline of task #${task.id}?\n\nA running stage cannot be cancelled; its result is discarded when it finishes.`)) return
    void act(() => api.tasks.stop(task.id))
  }
  const resume = () => act(() => api.tasks.resume(task.id))
  const setAutopilot = (level: AutopilotLevel) => act(() => api.tasks.setAutopilot(task.id, level))
  const skip = (seq: number) => {
    if (!confirm(`Skip stage ${seq}?`)) return
    void act(() => api.tasks.skipStage(task.id, seq))
  }
  const retry = (seq: number) => act(() => api.tasks.retryStage(task.id, seq), () => onOpenStream(`stage_${seq}`))
  const sendAnswer = () => {
    if (!awaiting) return
    const text = answer.trim()
    if (!text) return
    const seq = awaiting.seq
    void act(() => api.tasks.answerStage(task.id, seq, text), () => { setAnswer(''); onOpenStream(`stage_${seq}`) })
  }

  const patchDraft = (i: number, patch: Partial<DraftStage>) => {
    setDraft((prev) => prev.map((d, j) => (j === i ? { ...d, ...patch } : d)))
    setDirty(true)
  }
  const moveDraft = (i: number, dir: -1 | 1) => {
    const j = i + dir
    if (j < 0 || j >= draft.length) return
    const next = [...draft]
    ;[next[i], next[j]] = [next[j], next[i]]
    setDraft(next)
    setDirty(true)
  }
  const removeDraft = (i: number) => {
    setDraft((prev) => prev.filter((_, j) => j !== i))
    setDirty(true)
  }
  const addStage = () => {
    if (!addRole) return
    setDraft((prev) => [...prev, { key: draftKey++, role: addRole, goal: addGoal.trim(), model: '' }])
    setAddGoal('')
    setDirty(true)
  }

  const spent = stages.filter((s) => s.metered).reduce((a, s) => a + (s.cost_usd || 0), 0)
  const unpriced = stages.filter((s) => s.attempt > 0 && s.status !== 'pending' && !s.metered).length
  const tokens = stages.reduce((a, s) => a + (s.tokens || 0), 0)
  const newRepo = plan.new_repo ?? null
  const totalStages = history.length + draft.length

  const envBackend = workerDefaults?.backend
  // Mirrors stage_model() in claude-agent/pipeline.py: every stage runs on
  // the plan model's backend. The first of override -> role default -> env
  // default for the kind on that backend wins, else the plan model itself.
  const planModel = (task.plan_model || workerDefaults?.plan || '').trim()
  const planBackend = backendFor(planModel, envBackend ?? 'claude')
  const stageCandidates = (roleId: string, model: string): string[] => {
    const r = roleById.get(roleId)
    const kindDefault = r?.kind === 'implement' ? workerDefaults?.execute : workerDefaults?.refine
    return [model, r?.default_model, kindDefault].filter((c): c is string => !!c)
  }
  const resolveStage = (roleId: string, model: string): string =>
    stageCandidates(roleId, model).find((c) => backendFor(c, envBackend ?? 'claude') === planBackend) || planModel
  const stageEnvDefault = (roleId: string): string | null | undefined => resolveStage(roleId, '') || null
  const stageBackend = (roleId: string, model: string): AgentBackend =>
    backendFor(resolveStage(roleId, model), planBackend)

  const canRunNext = status === 'plan_ready' && !awaiting && failed.length === 0
  const nextDraft = draft[0]
  const runNextLabel = nextDraft
    ? `Run next: ${roleById.get(nextDraft.role)?.label ?? nextDraft.role}`
    : 'Finish'
  const runNextTitle = !canRunNext
    ? status === 'plan_ready'
      ? 'Retry, answer or skip the failed / waiting stage first'
      : `Not available in status '${status}'`
    : nextDraft
      ? (touchesLive && roleById.get(nextDraft.role)?.kind === 'implement'
        ? 'Implement stage on a live-bot repo — hands-off (no commit, no running containers)'
        : 'Start the next pending stage')
      : 'No pending stage — the backend applies the finish rule (done or re-plan)'

  return (
    <div id={`task-pipeline-${task.id}`} className="mt-1 flex flex-col gap-3 rounded border border-sky-900/60 bg-slate-900 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-[11px] uppercase tracking-wider text-sky-400">
          <ListChecks size={14} />
          Pipeline
          {status === 'planning' && <span className="normal-case tracking-normal text-blue-300">· orchestrator is planning…</span>}
          {status === 'running' && <span className="normal-case tracking-normal text-blue-300">· stage running…</span>}
        </div>
        <div className="flex flex-wrap items-center gap-3 text-[11px] text-slate-400">
          <span title="Failed stages so far / max_rounds">rounds {task.round_count ?? 0}/{plan.max_rounds}</span>
          <span title="Stages (history + planned) / max_stages">stages {totalStages}/{plan.max_stages}</span>
          <span title="Sum of the stages' $ figures vs. budget. Claude reports $ itself; Codex stages get a $ figure only when the worker has WORKER_CODEX_PRICES, otherwise they show tokens and are listed as unpriced (still real usage on the ChatGPT plan).">
            ${spent.toFixed(2)}{task.budget_usd != null ? ` / $${task.budget_usd.toFixed(2)}` : ''}
            {tokens > 0 ? ` · ${fmtTokens(tokens)}` : ''}
            {unpriced > 0 ? ` · ${unpriced} unpriced` : ''}
            {cost && cost.total_usd > spent + 0.005 ? ` · task total $${cost.total_usd.toFixed(2)}` : ''}
          </span>
          <label className="flex items-center gap-1">
            Ablauf
            <select
              value={task.autopilot === 'off' || !task.autopilot ? 'off' : 'full-bots'}
              onChange={(e) => setAutopilot(e.target.value as AutopilotLevel)}
              disabled={disabled}
              className={INPUT}
              title="Läuft bis zur Freigabe: alle Stufen laufen von selbst; Halt nur bei Rückfrage, Fehler, Budget, Wartezeit oder Deploy/Live (immer dein Klick). Stufen auf Repos mit Live-Bots arbeiten dabei hands-off: kein Commit, keine laufenden Container. · Jede Stufe bestätigen: jede Stufe wartet auf deinen Klick."
            >
              <option value="full-bots">Läuft bis zur Freigabe</option>
              <option value="off">Jede Stufe bestätigen</option>
            </select>
          </label>
        </div>
      </div>

      {touchesLive && task.autopilot === 'full-bots' && (
        <div className="text-[11px] text-orange-300">
          Repo mit Live-Bots — Umsetzungs-Stufen arbeiten hands-off: kein Commit, laufende Container bleiben unberührt, Deploy bleibt bei dir.
        </div>
      )}
      {newRepo && (
        <div className="rounded border border-sky-900/60 bg-sky-950/30 px-2 py-1.5 text-[11px] text-sky-200">
          <strong>New sub-repo proposed:</strong> <span className="font-mono">{newRepo.name}</span>
          {newRepo.based_on ? <> (layout from <span className="font-mono">{newRepo.based_on}</span>)</> : null}
          {newRepo.reason ? <> — {newRepo.reason}</> : null}
          <span className="text-sky-300/80"> · The implement stages scaffold and register it; autopilot never chains implement stages while it is on the task.</span>
        </div>
      )}

      <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto]">
        <label className="flex flex-col gap-1">
          <span className="text-[10px] uppercase tracking-wider text-slate-500">Exit criteria (one per line)</span>
          <textarea
            value={criteria}
            onChange={(e) => { setCriteria(e.target.value); setDirty(true) }}
            disabled={!planEditable || disabled}
            rows={3}
            placeholder="e.g. OOS Sharpe > 1.0 after costs"
            className={INPUT}
          />
        </label>
        <div className="flex flex-col gap-1 text-[11px] text-slate-500">
          <label className="flex items-center justify-between gap-2">max rounds
            <input type="number" min={1} max={50} value={maxRounds} onChange={(e) => { setMaxRounds(Number(e.target.value)); setDirty(true) }} disabled={!planEditable || disabled} className={`${INPUT} w-20`} />
          </label>
          <label className="flex items-center justify-between gap-2">max stages
            <input type="number" min={1} max={100} value={maxStages} onChange={(e) => { setMaxStages(Number(e.target.value)); setDirty(true) }} disabled={!planEditable || disabled} className={`${INPUT} w-20`} />
          </label>
          <label className="flex items-center justify-between gap-2">budget USD
            <input type="number" min={0} step="0.5" value={budget} onChange={(e) => { setBudget(e.target.value); setDirty(true) }} disabled={!planEditable || disabled} placeholder="none" className={`${INPUT} w-20`} />
          </label>
        </div>
      </div>
      {plan.rationale && (
        <div className="text-[11px] text-slate-500"><span className="uppercase tracking-wider">orchestrator:</span> {plan.rationale}</div>
      )}

      <ol className="flex flex-col gap-1">
        {history.map((s) => (
          <HistoryRow
            key={s.id}
            s={s}
            role={roleById.get(s.role)}
            open={!!open[s.seq]}
            disabled={disabled}
            canSkip={planEditable && (s.status === 'failed' || s.status === 'awaiting_input') && !gateRoles.has(s.role)}
            canRetry={(s.status === 'failed' || (s.status === 'done' && s.verdict === 'blocked')) && (status === 'failed' || status === 'limit_reached' || status === 'plan_ready' || status === 'blocked')}
            onToggle={() => setOpen((o) => ({ ...o, [s.seq]: !o[s.seq] }))}
            onOpenStream={() => onOpenStream(`stage_${s.seq}`)}
            onSkip={() => skip(s.seq)}
            onRetry={() => retry(s.seq)}
          />
        ))}
        {draft.map((d, i) => (
          <DraftRow
            key={d.key}
            d={d}
            index={i}
            total={draft.length}
            seq={historySeqMax + i + 1}
            role={roleById.get(d.role)}
            isGate={gateRoles.has(d.role)}
            editable={planEditable}
            disabled={disabled}
            envDefault={stageEnvDefault(d.role)}
            envBackend={envBackend}
            resolvedBackend={stageBackend(d.role, d.model)}
            planBackend={planBackend}
            onChange={(patch) => patchDraft(i, patch)}
            onMove={(dir) => moveDraft(i, dir)}
            onRemove={() => removeDraft(i)}
          />
        ))}
        {stages.length === 0 && draft.length === 0 && status !== 'planning' && (
          <li className="text-[11px] text-slate-500">No stages yet — press Plan to let the orchestrator propose them.</li>
        )}
      </ol>

      {planEditable && (
        <div className="flex flex-wrap items-center gap-2">
          <select value={addRole} onChange={(e) => setAddRole(e.target.value)} disabled={disabled} className={INPUT}>
            {stageRoles.map((r) => (
              <option key={r.id} value={r.id}>{r.label} ({r.kind})</option>
            ))}
          </select>
          <input
            value={addGoal}
            onChange={(e) => setAddGoal(e.target.value)}
            disabled={disabled}
            placeholder="goal (optional)"
            className={`${INPUT} min-w-[12rem] flex-1`}
          />
          <button onClick={addStage} disabled={disabled || stageRoles.length === 0} className={`${BTN} border-slate-600 bg-slate-700/40 text-slate-200 hover:bg-slate-700/70`}>
            <Plus size={13} />
            Add stage
          </button>
        </div>
      )}

      {awaiting && (status === 'awaiting_input' || status === 'plan_ready') && (
        <div id={`task-answer-${task.id}`} className="rounded border border-violet-900/60 bg-slate-950 p-2">
          <div className="mb-1 text-[10px] uppercase tracking-wider text-violet-300">
            Stage {awaiting.seq} ({roleById.get(awaiting.role)?.label ?? awaiting.role}) needs your input
          </div>
          <ul className="mb-1 flex flex-col gap-0.5 text-[12px] text-slate-200">
            {awaiting.questions.map((q, i) => <li key={i}>• {q}</li>)}
          </ul>
          <textarea
            value={answer}
            onChange={(e) => setAnswer(e.target.value)}
            onKeyDown={(e) => {
              if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
                e.preventDefault()
                sendAnswer()
              }
            }}
            rows={3}
            disabled={disabled}
            placeholder="Your answer — the stage resumes with it (Cmd/Ctrl+Enter to send)"
            className={`${INPUT} w-full`}
          />
          <div className="mt-1 flex items-center justify-end">
            <button onClick={sendAnswer} disabled={disabled || !answer.trim()} className={`${BTN} border-violet-700 bg-violet-900/30 text-violet-200 hover:bg-violet-900/50`}>
              <Send size={13} />
              Answer
            </button>
          </div>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        {dirty && planEditable && (
          <button onClick={savePlan} disabled={disabled} className={`${BTN} border-blue-600 bg-blue-900/30 text-blue-200 ring-1 ring-blue-300/40 hover:bg-blue-900/50`}>
            <Save size={13} />
            Save plan*
          </button>
        )}
        <button onClick={runNext} disabled={disabled || !canRunNext} title={runNextTitle} className={`${BTN} border-emerald-700 bg-emerald-900/20 text-emerald-300 hover:bg-emerald-900/40`}>
          <Play size={13} />
          {runNextLabel}
        </button>
        {PLAN_ELIGIBLE.has(status) && (
          <button onClick={replan} disabled={disabled} className={`${BTN} border-sky-700 bg-sky-900/20 text-sky-300 hover:bg-sky-900/40`}>
            <RotateCcw size={13} />
            {history.length + pending.length > 0 ? 'Re-plan' : 'Plan'}
          </button>
        )}
        {(status === 'planning' || status === 'running' || status === 'plan_ready' || status === 'awaiting_input') && (
          <button onClick={stop} disabled={disabled} className={`${BTN} border-orange-700 bg-orange-900/20 text-orange-300 hover:bg-orange-900/40`}>
            <Square size={13} />
            Stop
          </button>
        )}
        {status === 'blocked' && (
          <button onClick={resume} disabled={disabled} className={`${BTN} border-amber-700 bg-amber-900/20 text-amber-300 hover:bg-amber-900/40`}>
            <SkipForward size={13} />
            Resume
          </button>
        )}
        <button onClick={() => onOpenStream('plan')} className={`${SMALL_BTN} ml-auto`} title="Stream the orchestrator's log">
          Plan log
        </button>
      </div>
    </div>
  )
}
