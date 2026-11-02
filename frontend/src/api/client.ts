// API client for agent-team.
//
// The tasks API is per project: /p/<project>/api/tasks/... (same paths below
// the prefix as the original /api/tasks, which this was ported
// from). The current project is module state, set by the app shell before a
// project page renders (setProject).

let project = ''

export function setProject(id: string) {
  project = id
}

export function currentProject(): string {
  return project
}

const P = () => {
  if (!project) throw new Error('no project selected')
  return `/p/${project}`
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(path, { credentials: 'same-origin' })
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json()
}

async function send<T>(path: string, method: 'POST' | 'PUT' | 'PATCH' | 'DELETE', body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    credentials: 'same-origin',
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`
    try {
      const j = await res.json()
      if (j?.detail) detail = `${res.status} ${j.detail}`
    } catch {
      /* ignore */
    }
    throw new Error(detail)
  }
  if (res.status === 204) return undefined as T
  return res.json()
}

// ---------------------------------------------------------------------------
// API client
// ---------------------------------------------------------------------------

export const api = {
  me: () => get<{ user: string }>('/api/me'),
  projects: () => get<ProjectInfo[]>('/api/projects'),
  inbox: () => get<InboxItem[]>('/api/inbox'),
  chat: {
    channels: () => get<ChatChannel[]>('/api/chat/channels'),
    info: (c: string) => get<ChatChannel>(`/api/chat/${c}`),
    events: (c: string, after = 0, limit = 400) => get<ChatEvent[]>(`/api/chat/${c}/events?after=${after}&limit=${limit}`),
    streamUrl: (c: string, after: number) => `/api/chat/${c}/stream?after=${after}`,
    read: (c: string, seq: number) => send<{ read_seq: number }>(`/api/chat/${c}/read`, 'POST', { seq }),
    message: (c: string, body: { text: string; quote?: string | null; images?: { name: string; data: string }[] }) =>
      send<Record<string, unknown>>(`/api/chat/${c}/message`, 'POST', body),
    settings: (c: string, body: { backend?: string; model?: string; perm?: string }) =>
      send<ChatLive>(`/api/chat/${c}/settings`, 'POST', body),
    stop: (c: string) => send<{ stopped: boolean }>(`/api/chat/${c}/stop`, 'POST'),
    reset: (c: string) => send<{ ok: boolean }>(`/api/chat/${c}/reset`, 'POST'),
    models: () => get<Record<'claude' | 'codex', { models?: CatalogModel[]; error?: string | null }>>('/api/chat-models'),
    // "Mit Agent lesen" on the Idee page: the project's lead reads the link and files an idea.
    idea: (body: { project: string; link: string; text?: string; note?: string }) =>
      send<{ channel: string }>('/api/chat/idea', 'POST', body),
    // "Prüfen" on an alert: opens the check chat (agent examines, recommends fix/delete).
    check: (c: string, seq: number) => send<{ channel: string }>(`/api/chat/${c}/check`, 'POST', { seq }),
    scouts: (project: string) =>
      get<{ id: string; role: string; lead: boolean; report: boolean; last_run: number | null; requested: boolean }[]>(`/p/${project}/api/scouts`),
    runScout: (project: string, id: string) => send<{ requested: boolean }>(`/p/${project}/api/scouts/${id}/run`, 'POST'),
    newOptions: () => get<{ project: string; name: string; root: string; repos: string[] }[]>('/api/chat/new-options'),
    createCustom: (body: { project: string; repo: string; title: string; message?: string }) =>
      send<ChatChannel>('/api/chat/custom', 'POST', body),
    archive: (c: string, archived: boolean) =>
      send<{ archived: boolean }>(`/api/chat/${c}/archive`, 'POST', { archived }),
    delete: (c: string, body: { seqs?: number[]; all_alerts?: boolean; all?: boolean }) =>
      send<{ deleted: number[] }>(`/api/chat/${c}/delete`, 'POST', body),
    permission: (c: string, req: string, decision: 'once' | 'always' | 'deny') =>
      send<{ ok: boolean }>(`/api/chat/${c}/permission/${req}`, 'POST', { decision }),
    question: (c: string, req: string, answers: { selected: number[]; text?: string }[]) =>
      send<{ ok: boolean }>(`/api/chat/${c}/question/${req}`, 'POST', { answers }),
  },
  // Service channels: a program, not an agent, behind the channel.
  service: {
    channels: () => get<ServiceChannel[]>('/api/service-channels'),
    command: (c: string, body: { text: string; label?: string; button_seq?: number }) =>
      send<Record<string, unknown>>(`/api/service/${c}/command`, 'POST', body),
  },
  // Lead channel cards (Tasks v2): my tap runs the action on the channel's
  // project, then is recorded in the transcript via service.command.
  lead: {
    get: (project: string, id: number) => get<Task>(`/p/${project}/api/tasks/${id}`),
    start: (project: string, id: number) => send<Task>(`/p/${project}/api/tasks/${id}/plan`, 'POST'),
    park: (project: string, id: number) => send<Task>(`/p/${project}/api/tasks/${id}/backlog`, 'POST'),
    close: (project: string, id: number) => send<Task>(`/p/${project}/api/tasks/${id}/close`, 'POST'),
    create: (project: string, body: TaskCreateBody) => send<Task>(`/p/${project}/api/tasks`, 'POST', body),
    dismissWatchdog: (project: string, key: string) =>
      send<{ deleted: number; already?: number }>(`/p/${project}/api/watchdog/dismiss/${key}`, 'POST'),
    merge: (project: string, id: number, into: number) =>
      send<Task>(`/p/${project}/api/tasks/${id}/merge`, 'POST', { into }),
    option: (project: string, id: number, seq: number, index: number) =>
      send<Task>(`/p/${project}/api/tasks/${id}/stages/${seq}/answer-option`, 'POST', { index }),
    remove: (project: string, id: number) => send<void>(`/p/${project}/api/tasks/${id}`, 'DELETE'),
    done: (project: string, id: number) => send<Task>(`/p/${project}/api/tasks/${id}/done`, 'POST'),
    next: (project: string, id: number) => send<Task>(`/p/${project}/api/tasks/${id}/stages/next`, 'POST'),
    retry: (project: string, id: number, seq: number) =>
      send<Task>(`/p/${project}/api/tasks/${id}/stages/${seq}/retry`, 'POST'),
    apply: (project: string, id: number) => send<{ requested: boolean }>(`/p/${project}/api/tasks/${id}/apply`, 'POST'),
  },
  budget: {
    get: () => get<BudgetInfo>('/api/budget'),
    config: (c: Partial<BudgetConfig>) => send<BudgetInfo>('/api/budget/config', 'POST', c),
    probe: () => send<BudgetInfo>('/api/budget/probe', 'POST'),
  },
  claudeAuth: {
    status: (fresh = false) => get<ClaudeAuth>(`/api/claude-auth${fresh ? '?fresh=1' : ''}`),
    start: () => send<ClaudeLoginState>('/api/claude-auth/start', 'POST'),
    code: (id: string, code: string) => send<{ ok: boolean }>('/api/claude-auth/code', 'POST', { id, code }),
    cancel: () => send<{ ok: boolean }>('/api/claude-auth/cancel', 'POST'),
  },
  push: {
    key: () => get<{ key: string }>('/api/push/key'),
    subscribe: (sub: PushSubscriptionJSON) => send<{ subscriptions: number }>('/api/push/subscribe', 'POST', sub),
    test: (delay = 0) => send<Record<string, unknown>>('/api/push/test', 'POST', { delay }),
  },
  tasks: {
    list: (status?: TaskStatus) =>
      get<Task[]>(status ? `${P()}/api/tasks?status=${status}` : `${P()}/api/tasks`),
    board: (doneDays = 7) => get<TaskBoard>(`${P()}/api/tasks/board?done_days=${doneDays}`),
    get: (id: number) => get<Task>(`${P()}/api/tasks/${id}`),
    create: (body: TaskCreateBody) => send<Task>(`${P()}/api/tasks`, 'POST', body),
    update: (id: number, body: TaskUpdateBody) =>
      send<Task>(`${P()}/api/tasks/${id}`, 'PATCH', body),
    remove: (id: number) => send<void>(`${P()}/api/tasks/${id}`, 'DELETE'),
    refine: (id: number) => send<Task>(`${P()}/api/tasks/${id}/refine`, 'POST'),
    reopen: (id: number) => send<Task>(`${P()}/api/tasks/${id}/reopen`, 'POST'),
    backlog: (id: number) => send<Task>(`${P()}/api/tasks/${id}/backlog`, 'POST'),
    restore: (id: number) => send<Task>(`${P()}/api/tasks/${id}/restore`, 'POST'),
    close: (id: number, reason: string) => send<Task>(`${P()}/api/tasks/${id}/close`, 'POST', { reason }),
    markDone: (id: number) => send<Task>(`${P()}/api/tasks/${id}/done`, 'POST'),
    changes: (id: number) => get<TaskChanges>(`${P()}/api/tasks/${id}/changes`),
    apply: (id: number) => send<{ requested: boolean }>(`${P()}/api/tasks/${id}/apply`, 'POST'),
    reply: (id: number, text: string) =>
      send<Task>(`${P()}/api/tasks/${id}/reply`, 'POST', { text }),
    cost: (id: number) =>
      get<TaskCost>(`${P()}/api/tasks/${id}/cost`),
    workerDefaults: () => get<WorkerDefaults>(`${P()}/api/tasks/worker-defaults`),
    authError: (id: number) => get<{ required: boolean; kind?: string }>(`${P()}/api/tasks/${id}/auth-error`),
    claudeLoginStatus: () => get<ClaudeLoginState>(`${P()}/api/tasks/auth/claude`),
    startClaudeLogin: () => send<ClaudeLoginState>(`${P()}/api/tasks/auth/claude`, 'POST'),
    submitClaudeLoginCode: (id: string, code: string) =>
      send<{ ok: boolean }>(`${P()}/api/tasks/auth/claude/code`, 'POST', { id, code }),
    cancelClaudeLogin: (id: string) =>
      send<{ ok: boolean }>(`${P()}/api/tasks/auth/claude/cancel`, 'POST', { id }),
    // Live model catalog of both agent CLIs (see claude-agent/model_catalog.py).
    models: (refresh = false) =>
      get<ModelCatalog>(refresh ? `${P()}/api/tasks/models?refresh=1` : `${P()}/api/tasks/models`),
    // npm-upgrade both CLIs inside the bridge container, then re-read the catalog.
    upgradeClis: () =>
      send<{ ok: boolean; exit_code: number; log: string[]; catalog: ModelCatalog }>(`${P()}/api/tasks/models/upgrade`, 'POST'),
    // Stream kinds: 'refine' | 'execute' (simple mode), 'plan' and
    // 'stage_<seq>' (pipeline mode) — validated server-side.
    streamUrl: (id: number, kind: string) =>
      `${P()}/api/tasks/${id}/stream/${kind}`,
    // ---- Pipeline mode (role-based multi-stage runs) ----
    roles: () => get<RolesResponse>(`${P()}/api/tasks/roles`),
    stages: (id: number) => get<TaskStage[]>(`${P()}/api/tasks/${id}/stages`),
    plan: (id: number) => send<Task>(`${P()}/api/tasks/${id}/plan`, 'POST'),
    updatePlan: (id: number, body: PlanUpdateBody) =>
      send<Task>(`${P()}/api/tasks/${id}/plan`, 'PUT', body),
    nextStage: (id: number) => send<Task>(`${P()}/api/tasks/${id}/stages/next`, 'POST'),
    answerStage: (id: number, seq: number, text: string) =>
      send<Task>(`${P()}/api/tasks/${id}/stages/${seq}/answer`, 'POST', { text }),
    skipStage: (id: number, seq: number) =>
      send<Task>(`${P()}/api/tasks/${id}/stages/${seq}/skip`, 'POST'),
    retryStage: (id: number, seq: number) =>
      send<Task>(`${P()}/api/tasks/${id}/stages/${seq}/retry`, 'POST'),
    setAutopilot: (id: number, level: AutopilotLevel) =>
      send<Task>(`${P()}/api/tasks/${id}/autopilot`, 'POST', { level }),
    stop: (id: number) => send<Task>(`${P()}/api/tasks/${id}/stop`, 'POST'),
    resume: (id: number) => send<Task>(`${P()}/api/tasks/${id}/resume`, 'POST'),
  },}

// ---------------------------------------------------------------------------
// Projects / inbox
// ---------------------------------------------------------------------------

export interface BudgetConfig {
  reserve_pct: number
  early_lead_pct: number
  release_hours: number
  five_hour_max: number
  user_active_minutes: number
  paused: boolean
}

export interface BudgetDecision {
  allowed: boolean
  reason: string
  week_used: number | null
  week_target: number | null
  week_resets_at: number | null
  five_hour_used: number | null
  five_hour_resets_at: number | null
  sample_age_s: number | null
  reserve_released: boolean
}

export interface BudgetSample {
  ts: number
  source: string
  five_hour: { util: number; resets_at: number | null } | null
  seven_day: { util: number; resets_at: number | null } | null
}

export interface BudgetInfo {
  decision: BudgetDecision
  config: BudgetConfig
  latest: BudgetSample | null
  history?: BudgetSample[]
}

export interface ClaudeAuth {
  logged_in: boolean | null
  method?: string | null
  email?: string | null
  error?: string
  login: ClaudeLoginState
}

export interface ProjectInfo {
  id: string
  name: string
  worker: string
  tasks: boolean
  channels: { id: string; label: string }[]
}

// ---- Chat -------------------------------------------------------------------

export interface ChatLive {
  id: string
  project: string
  label: string
  cwd: string
  backend: 'claude' | 'codex'
  model: string | null
  model_override: string | null
  active_model: string | null
  perm: string
  busy: boolean
  session: string | null
  defaults: { backend: string; claude_model: string | null; codex_model: string | null; perm: string }
}

export interface ChatChannel {
  id: string
  project: string
  label: string
  cwd: string
  lead?: boolean
  task_id?: number | null   // task chat (Tasks v2)
  // Task chat: the task's status; working = the task worker runs an agent.
  task?: { status: TaskStatus; working: boolean } | null
  check_of?: string | null  // check chat: "<source channel>:<alert seq>"
  custom?: boolean          // own chat the user opened for a repo
  archived?: boolean        // hidden from the list, transcript kept
  last_seq: number
  read_seq: number
  unread: number
  last_ts: string | null
  preview: string | null
  open_requests: number
  live: ChatLive | null
}

export interface ChatQuestion {
  question: string
  header: string
  multi: boolean
  options: { label: string; description: string }[]
}

export interface ServiceButton { label: string; data: string }
export interface ServiceChannel { id: string; menu: [string, string][] }

export interface ChatEvent {
  seq: number
  ts: string
  type: 'user' | 'assistant' | 'tool_use' | 'tool_result' | 'bg' | 'permission' | 'question'
    | 'resolved' | 'alert' | 'system' | 'error' | 'busy' | 'turn_end' | 'command'
  buttons?: ServiceButton[][] | null   // alert from a service channel
  label?: string | null                // command: the tapped button's label
  button_seq?: number | null           // command: the message the button was under
  text?: string
  author?: string | null               // task chat: the stage agent that wrote it
  data?: string | null                 // resolved card marker: the button's data
  quote?: string | null
  images?: string[] | null
  name?: string
  error?: boolean
  req_id?: string
  tool?: string
  title?: string | null
  detail?: string
  blocked_path?: string | null
  rules?: string[]
  can_always?: boolean
  questions?: ChatQuestion[]
  outcome?: string
  source?: string | null
  busy?: boolean
  auth?: boolean
}

// Something that waits for me: a task (or one of its stages) in a state
// only a human click resolves. See backend inbox.py.
export interface InboxItem {
  project: string
  project_name: string
  // Where tapping the item goes (task or chat channel).
  link: string
  key: string
  channel?: string
  // Transcript seq of a chat alert (lets the inbox delete it).
  seq?: number
  task_id: number | null
  title: string
  status: TaskStatus
  reason: string
  // 'question' | 'approval' | 'problem' | 'review'
  kind: string
  detail: string | null
  updated_at: string
  prio: TaskPrio
}

// ---------------------------------------------------------------------------
// Tasks
// ---------------------------------------------------------------------------

export type TaskStatus =
  | 'new' | 'refining' | 'refined' | 'done' | 'failed' | 'limit_reached' | 'backlog' | 'closed'
  // pipeline mode
  | 'planning' | 'plan_ready' | 'running' | 'awaiting_input' | 'blocked'
export type TaskPrio = 1 | 2 | 3 // 1=high, 2=normal, 3=low

export interface ClaudeLoginState {
  id?: string
  status: 'idle' | 'starting' | 'awaiting_code' | 'verifying' | 'success' | 'failed' | 'expired' | 'cancelled'
  message?: string
  url?: string | null
  expires_at?: number
}

export interface Task {
  id: number
  title: string
  details: string
  prio: TaskPrio
  repos: string[]
  status: TaskStatus
  created_at: string
  updated_at: string
  refined_spec: string | null
  execute_summary: string | null
  error: string | null
  session_id?: string | null
  // Which agent backend minted session_id. A Codex thread can't be resumed
  // by the Claude SDK, so the worker starts a fresh turn on a mismatch.
  session_backend?: AgentBackend | null
  pending_reply?: string | null
  // 'refine' | 'execute' | 'plan' | 'stage_<seq>' — which run was in flight
  // when limit_reached / failed was set (Retry dispatch hint).
  last_busy_kind?: string | null
  // Per-task model overrides. null/'' = use worker env default
  // (WORKER_REFINE_MODEL / WORKER_EXECUTE_MODEL). The model ID also picks
  // the agent backend — see MODEL_OPTIONS in pages/Tasks.tsx.
  refine_model?: string | null
  execute_model?: string | null
  // Model for the orchestrator's plan / re-plan runs (pipeline mode).
  plan_model?: string | null
  // Pipeline mode (see components/tasks/PipelinePanel.tsx). Absent/simple
  // for every task that never clicked "Plan (pipeline)".
  mode?: TaskMode
  plan?: TaskPlan
  autopilot?: AutopilotLevel
  round_count?: number
  budget_usd?: number | null
  stop_reason?: string | null
  // Budget pacer / wait period parked autopilot here (NULL = not paused).
  paused_reason?: string | null
  // Tasks v2 lead: recommended for start ("Zur Freigabe") and why / park reason.
  proposed_at?: string | null
  lead_note?: string | null
}

// GET /api/tasks/board: open tasks + recently done, with stage progress.
export interface BoardStage { seq: number; index: number; role: string; label: string; status: StageStatus; confirm: boolean }
export interface TaskBoardInfo {
  stages: { done: number; total: number; current: BoardStage | null } | null
  pending_apply: boolean
}
export type BoardTask = Task & { board?: TaskBoardInfo }
export interface TaskBoard { tasks: BoardTask[]; counts: Partial<Record<TaskStatus, number>>; lead_channel: string | null }

// The task's own branch (worker/workspace.py); state null = ran in the live checkout.
export interface TaskBranchRepo { base?: string; ahead?: number; behind?: number; stat?: string; dirty?: boolean; error?: string }
export interface TaskChanges {
  state: { branch: string; workspace: string; pending: boolean; repos: Record<string, TaskBranchRepo>;
           applied?: string[]; applied_at?: string } | null
  diff: string
}

export interface TaskCreateBody {
  title: string
  details?: string
  prio?: TaskPrio
  repos?: string[]
  refine_model?: string
  execute_model?: string
  plan_model?: string
}

export interface TaskUpdateBody {
  title?: string
  details?: string
  prio?: TaskPrio
  repos?: string[]
  refine_model?: string
  execute_model?: string
  plan_model?: string
}

export interface TaskCostRun {
  ts: string | null
  kind: string // 'refine' | 'execute' | 'plan' | 'stage_<seq>'
  cost_usd: number
  priced?: boolean
  tokens?: number | null
  backend?: AgentBackend | null
  duration_ms: number | null
  num_turns: number | null
}

export interface TaskCost {
  total_usd: number
  runs_count: number
  runs: TaskCostRun[]
  // input+output tokens over all runs (Codex has no $ figure unless the
  // worker has WORKER_CODEX_PRICES; tokens are always there).
  total_tokens?: number
  // Runs that happened but carry no $ figure — real work the total does
  // not account for.
  unpriced_runs?: number
  unpriced_kinds?: string[]
}

export type AgentBackend = 'claude' | 'codex'

// GET /api/tasks/models — what the agent CLIs in the bridge container can
// run right now. `models` entries with `disabled` are placeholders for
// generations the installed CLI is too old for (hint says which version).
export interface CatalogModel {
  value: string
  label: string
  description: string
  disabled: boolean
  hint: string
  backend: AgentBackend
  aliases?: string[]
}

export interface CatalogSide {
  cli_version: string | null
  latest_version: string | null
  update_available: boolean
  models: CatalogModel[]
  error: string | null
}

export interface ModelCatalog {
  generated_at: string | null
  claude: CatalogSide & { aliases: Record<string, string>; default_resolved: string | null }
  codex: CatalogSide & { fetched_at: string | null }
  error: string | null
  cached?: boolean
}

export interface WorkerDefaults {
  container: string
  container_running: boolean
  // Resolved model IDs (after the WORKER_REFINE_MODEL || WORKER_MODEL
  // cascade). null = no env override; the worker falls back to the
  // CLI's built-in default.
  refine: string | null
  execute: string | null
  // TASKS_AGENT_BACKEND — the fallback for tasks with no model override.
  backend: AgentBackend
  // What the env defaults resolve to once the model ID is accounted for
  // (a gpt-* WORKER_REFINE_MODEL runs on Codex whatever the env says).
  refine_backend: AgentBackend
  execute_backend: AgentBackend
  // Orchestrator (pipeline plan runs): WORKER_ORCHESTRATOR_MODEL or the
  // role default, and the backend it resolves to.
  plan: string | null
  plan_backend: AgentBackend
  error: string | null
}

// ---------------------------------------------------------------------------
// Tasks — pipeline mode (role-based multi-stage runs)
// ---------------------------------------------------------------------------

export type TaskMode = 'simple' | 'pipeline'
// Tasks v2: off = jede Stufe bestätigen, full-bots = läuft bis zur Freigabe
export type AutopilotLevel = 'off' | 'full-bots'
export type StageKind = 'advise' | 'implement'
export type StageStatus = 'pending' | 'running' | 'awaiting_input' | 'done' | 'failed' | 'skipped'
export type StageVerdict = 'pass' | 'fail' | 'needs_input' | 'blocked'

export interface StageFinding {
  id: string
  severity: 'high' | 'med' | 'low' | string
  text: string
  resolved: boolean
}

export interface TaskStage {
  id: number
  task_id: number
  seq: number
  role: string
  kind: StageKind
  goal: string
  status: StageStatus
  attempt: number
  model: string | null
  session_id: string | null
  session_backend: AgentBackend | null
  output: string | null
  verdict: StageVerdict | null
  summary: string | null
  findings: StageFinding[]
  questions: string[]
  pending_answer: string | null
  cost_usd: number
  // true when the run carried a $ figure (Claude always; Codex only with a
  // price table). Unpriced stages show their tokens instead.
  metered: boolean
  tokens: number | null
  duration_ms: number | null
  num_turns: number | null
  error: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  updated_at: string
}

export interface TaskPlan {
  exit_criteria: string[]
  max_rounds: number
  max_stages: number
  rationale: string
  // Orchestrator's proposal for a brand-new sub-repo (when nothing fits).
  new_repo?: { name: string; based_on?: string; reason?: string } | null
}

export interface AgentRole {
  id: string
  label: string
  description: string
  kind: StageKind
  default_model: string | null
  is_stage: boolean
}

export interface RolesResponse {
  roles: AgentRole[]
  gate_roles: string[]
  live_bot_repos: string[]
  // The project's sub-repos (team.yaml `repos`).
  repos: string[]
  // team.yaml `repo_prefix` ("" = none), stripped when repo names are shown.
  repo_prefix?: string
  autopilot_levels: AutopilotLevel[]
  defaults: { max_rounds: number; max_stages: number }
}

export interface PlanUpdateBody {
  stages: { role: string; goal: string; model: string | null }[]
  exit_criteria?: string[]
  max_rounds?: number
  max_stages?: number
  budget_usd?: number | null
}
