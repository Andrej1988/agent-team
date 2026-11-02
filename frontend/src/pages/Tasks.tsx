import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Archive, ArchiveRestore, ArrowLeft, Ban, Check, Columns3, GitBranch, MessageSquare, Plus, Play, RotateCcw, Save, Send, Trash2, X } from 'lucide-react'
import { api, currentProject, type BoardTask, type ModelCatalog, type TaskChanges, type RolesResponse, type Task, type TaskCost, type TaskPrio, type TaskStage, type WorkerDefaults } from '../api/client'
import { CatalogHint, ModelCatalogProvider, ModelPicker, StatusBadge, setRepoPrefix, shortRepo, type StreamEvent } from '../components/tasks/shared'
import PipelinePanel from '../components/tasks/PipelinePanel'
import Board from '../components/tasks/Board'
import ClaudeLoginNotice from '../components/tasks/ClaudeLoginNotice'
import TaskAttentionNotice, { AttentionCard } from '../components/tasks/TaskAttentionNotice'
import { errorHelp } from '../components/tasks/taskHelp'

// Mirrors tasks_db.BUSY_STATUS / EDITABLE_STATUS (simple + pipeline mode).
const BUSY_STATUS = new Set<string>(['refining', 'planning', 'running'])
const EDITABLE_STATUS = new Set<string>([
  'new', 'refined', 'failed', 'limit_reached', 'backlog',
  'plan_ready', 'awaiting_input', 'blocked',
])

function streamLabel(kind: string): string {
  if (kind === 'execute') return 'Execute log'
  if (kind === 'refine') return 'Refine log'
  if (kind === 'plan') return 'Plan log'
  if (kind.startsWith('stage_')) return `Stage ${kind.slice('stage_'.length)} log`
  return `${kind} log`
}

const PRIO_LABEL: Record<TaskPrio, string> = { 1: 'High', 2: 'Normal', 3: 'Low' }

interface DraftTask {
  id: number | null
  title: string
  details: string
  prio: TaskPrio
  repos: string[]
  // '' = worker env default; otherwise a model ID passed straight to the
  // Codex CLI (e.g. "gpt-5.6-luna", "gpt-5.6-terra").
  refineModel: string
  executeModel: string
  // Pipeline mode: model for the orchestrator's plan / re-plan runs.
  planModel: string
}

const EMPTY_DRAFT: DraftTask = {
  id: null,
  title: '',
  details: '',
  prio: 2,
  repos: [],
  refineModel: '',
  executeModel: '',
  planModel: '',
}

function toDraft(t: Task): DraftTask {
  return {
    id: t.id,
    title: t.title,
    details: t.details,
    prio: t.prio,
    repos: t.repos,
    refineModel: t.refine_model || '',
    executeModel: t.execute_model || '',
    planModel: t.plan_model || '',
  }
}

// List rows: priority as a slim left bar (red = high) instead of a word badge.
const PRIO_BAR: Record<TaskPrio, string> = { 1: 'border-l-red-500', 2: 'border-l-slate-600', 3: 'border-l-transparent' }

// "#113", "113" or words of the title (all must match, case-insensitive).
function matchesQuery(t: Task, q: string): boolean {
  const s = q.trim().toLowerCase()
  if (!s) return true
  const num = /^#?(\d+)$/.exec(s)
  if (num) return String(t.id).startsWith(num[1])
  const hay = `#${t.id} ${t.title} ${t.repos.join(' ')}`.toLowerCase()
  return s.split(/\s+/).every((w) => hay.includes(w))
}


export default function Tasks({ initialTaskId }: { initialTaskId?: number } = {}) {
  const [tasks, setTasks] = useState<BoardTask[]>([])
  // 'board' = Kanban of open + recently done tasks; the others list one status.
  const [view, setView] = useState<'board' | 'backlog' | 'done' | 'closed'>('board')
  const [leadChannel, setLeadChannel] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [doneCount, setDoneCount] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [claudeAuthRequired, setClaudeAuthRequired] = useState(false)
  const [draft, setDraft] = useState<DraftTask | null>(null)
  const [saving, setSaving] = useState(false)
  const [streamEvents, setStreamEvents] = useState<StreamEvent[]>([])
  const [streamOpen, setStreamOpen] = useState(false)
  const [streamKind, setStreamKind] = useState<string>('refine')
  const [replyText, setReplyText] = useState('')
  const [sendingReply, setSendingReply] = useState(false)
  const [cost, setCost] = useState<TaskCost | null>(null)
  const [workerDefaults, setWorkerDefaults] = useState<WorkerDefaults | null>(null)
  // Live model catalog of the agent CLIs (dropdown contents + CLI-update hint).
  const [modelCatalog, setModelCatalog] = useState<ModelCatalog | null>(null)
  const [catalogRefreshing, setCatalogRefreshing] = useState(false)
  const [cliUpgrading, setCliUpgrading] = useState(false)
  const [cliUpgradeLog, setCliUpgradeLog] = useState<string[] | null>(null)
  const loadCatalog = (refresh = false) => {
    setCatalogRefreshing(true)
    api.tasks
      .models(refresh)
      .then(setModelCatalog)
      .catch(() => setModelCatalog(null))
      .finally(() => setCatalogRefreshing(false))
  }
  const upgradeClis = () => {
    setCliUpgrading(true)
    setCliUpgradeLog(null)
    api.tasks
      .upgradeClis()
      .then((r) => {
        setCliUpgradeLog(r.log)
        setModelCatalog(r.catalog)
      })
      .catch((e) => setCliUpgradeLog([`upgrade failed: ${e instanceof Error ? e.message : String(e)}`]))
      .finally(() => setCliUpgrading(false))
  }
  // Pipeline mode: stages of the selected task + the role catalog.
  const [stages, setStages] = useState<TaskStage[]>([])
  const [roles, setRoles] = useState<RolesResponse | null>(null)
  // The project's sub-repos come from its team.yaml (via /roles), not a
  // hard-coded list — the page is shared by all projects.
  const SUB_REPOS = roles?.repos ?? []
  const esRef = useRef<EventSource | null>(null)
  const pollRef = useRef<number | null>(null)
  const streamPaneRef = useRef<HTMLDivElement | null>(null)
  const streamFollowRef = useRef(true)

  const load = async () => {
    try {
      if (view === 'board') {
        const b = await api.tasks.board()
        setTasks(b.tasks)
        setLeadChannel(b.lead_channel)
        api.chat.channels().then((l) => setChatIds(new Set(l.filter((c) => c.task_id != null).map((c) => c.id))), () => {})
        setDoneCount(b.counts.done ?? 0)
      } else {
        const list = await api.tasks.list(view)
        setTasks(list)
        // Keep the tab badges fresh: the loaded list for this tab, the other lazily.
        if (view === 'done') setDoneCount(list.length)
        else api.tasks.list('done').then((d) => setDoneCount(d.length)).catch(() => {})
      }
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  // Reload whenever the view (Active / Backlog / Done) toggles.
  useEffect(() => {
    setLoading(true)
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view])

  useEffect(() => {
    api.tasks
      .workerDefaults()
      .then(setWorkerDefaults)
      .catch(() => setWorkerDefaults(null))
    api.tasks
      .roles()
      .then((r) => { setRepoPrefix(r.repo_prefix); setRoles(r) })
      .catch(() => setRoles(null))
    loadCatalog()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Close any open SSE/poll when the selected task changes or the component unmounts.
  useEffect(() => {
    return () => {
      esRef.current?.close()
      esRef.current = null
      if (pollRef.current !== null) {
        window.clearInterval(pollRef.current)
        pollRef.current = null
      }
    }
  }, [])

  // Follow the stream pane as new events arrive — only while the pane is
  // already scrolled to its bottom, and only the pane itself (scrollIntoView
  // would drag the whole page down and make the form above unusable).
  useEffect(() => {
    const el = streamPaneRef.current
    if (streamOpen && el && streamFollowRef.current) el.scrollTop = el.scrollHeight
  }, [streamEvents, streamOpen])

  // Refresh the cost tally when the user selects a task and again
  // whenever a stream closes (so post-run totals catch up).
  useEffect(() => {
    if (!draft || draft.id === null) {
      setCost(null)
      return
    }
    let cancelled = false
    api.tasks.cost(draft.id).then(
      (c) => { if (!cancelled) setCost(c) },
      () => { if (!cancelled) setCost(null) },
    )
    return () => { cancelled = true }
  }, [draft?.id, streamOpen])

  // Background poll of the whole task list while any task is mid refine/
  // execute, so sidebar badges flip (e.g. refining -> refined/failed) even when
  // the detail stream is closed or the run was kicked off elsewhere.
  // Idle when nothing is in flight, so it costs nothing at rest. Updates the
  // list only — never the draft — so unsaved edits are never clobbered.
  // The board also refreshes slowly at rest: lead runs, scouts and stage
  // results change it without the page doing anything.
  const anyInFlight = view === 'board' && tasks.some((t) => BUSY_STATUS.has(t.status))
  useEffect(() => {
    if (view !== 'board') return
    const id = window.setInterval(() => {
      if (document.hidden) return
      api.tasks.board().then(
        (b) => {
          setTasks(b.tasks)
          setDoneCount(b.counts.done ?? 0)
        },
        () => { /* swallow; next tick retries */ },
      )
    }, anyInFlight ? 3000 : 20000)
    return () => window.clearInterval(id)
  }, [anyInFlight, view])

  const closeStream = () => {
    esRef.current?.close()
    esRef.current = null
    if (pollRef.current !== null) {
      window.clearInterval(pollRef.current)
      pollRef.current = null
    }
    setStreamOpen(false)
    setStreamEvents([])
  }

  const openStream = (taskId: number, kind: string = 'refine') => {
    closeStream()
    setStreamOpen(true)
    streamFollowRef.current = true
    setStreamKind(kind)
    setStreamEvents([])
    const es = new EventSource(api.tasks.streamUrl(taskId, kind))
    esRef.current = es
    const push = (raw: string) => {
      try {
        const obj = JSON.parse(raw) as StreamEvent
        setStreamEvents((prev) => [...prev, obj])
      } catch {
        setStreamEvents((prev) => [...prev, { event: 'raw', text: raw }])
      }
    }
    const events = [
      'hello', 'start', 'init', 'text', 'tool_use', 'tool_result',
      'thinking', 'result', 'complete', 'error', 'done', 'msg', 'raw',
      'repos_updated', 'details_updated', 'user_reply', 'limit_reached',
    ]
    for (const name of events) es.addEventListener(name, (e) => push((e as MessageEvent).data))
    es.onmessage = (e) => push(e.data) // catches any unnamed events
    es.onerror = () => {
      // Browser auto-reconnects; the server closes the stream after a terminal
      // event, which manifests here. Reload the task list to pick up the final
      // status + refined_spec.
      es.close()
      esRef.current = null
      void refreshDraft(taskId)
    }
    // Also poll the task row every 2s while streaming, so the badge in the
    // sidebar flips from refining -> refined/failed even before the SSE ends.
    pollRef.current = window.setInterval(() => {
      void refreshDraft(taskId)
    }, 2000)
  }

  const refreshDraft = async (taskId: number) => {
    try {
      const fresh = await api.tasks.get(taskId)
      setTasks((prev) => prev.map((t) => (t.id === fresh.id ? { ...fresh, board: t.board } : t)))
      setDraft((d) => (d && d.id === fresh.id ? toDraft(fresh) : d))
      if (fresh.mode === 'pipeline') api.tasks.stages(fresh.id).then(setStages, () => {})
      if (!BUSY_STATUS.has(fresh.status) && pollRef.current !== null) {
        window.clearInterval(pollRef.current)
        pollRef.current = null
      }
    } catch {
      /* swallow; next poll will retry */
    }
  }

  const selected = draft && draft.id !== null ? tasks.find((t) => t.id === draft.id) : null
  const isNew = draft !== null && draft.id === null
  const isPipeline = !!selected && selected.mode === 'pipeline'
  const isEditable = !selected || EDITABLE_STATUS.has(selected.status)
  const canRefine = !!selected && !isPipeline && (selected.status === 'new' || selected.status === 'refined' || selected.status === 'failed' || selected.status === 'limit_reached')
  const canReopen = !!selected && (selected.status === 'done' || selected.status === 'failed')
  // Park / unpark. Backlog accepts the same editable, non-busy states as
  // Mark Done (but is reversible and keeps the task in the system).
  const canBacklog = !!selected && (
    selected.status === 'new' ||
    selected.status === 'refined' ||
    selected.status === 'failed' ||
    selected.status === 'limit_reached' ||
    selected.status === 'plan_ready' ||
    selected.status === 'awaiting_input' ||
    selected.status === 'blocked'
  )
  const canRestore = !!selected && (selected.status === 'backlog' || selected.status === 'closed')
  // Close = will not be done (obsolete, duplicate, solved elsewhere); kept
  // under "Geschlossen" with the reason, reopenable.
  const canClose = canBacklog || (!!selected && selected.status === 'backlog')
  const canMarkDone = !!selected && (
    selected.status === 'new' ||
    selected.status === 'refined' ||
    selected.status === 'failed' ||
    selected.status === 'limit_reached' ||
    selected.status === 'plan_ready' ||
    selected.status === 'awaiting_input' ||
    selected.status === 'blocked'
  )
  const canReply = !!selected && !isPipeline && selected.status === 'refined' && !!(selected.session_id || '').trim()
  // Switch a simple-mode task over to the role pipeline (orchestrator plans,
  // user approves each stage). Same entry states as Refine.
  const canPlan = !!selected && !isPipeline && (
    selected.status === 'new' ||
    selected.status === 'refined' ||
    selected.status === 'failed' ||
    selected.status === 'limit_reached'
  )
  const selectedId = selected ? selected.id : null
  const pipelineBusy = isPipeline && (selected.status === 'planning' || selected.status === 'running')

  // Load the stage list when a pipeline task is selected or changes status.
  useEffect(() => {
    if (selectedId === null || !isPipeline) {
      setStages([])
      return
    }
    let cancelled = false
    api.tasks.stages(selectedId).then((s) => { if (!cancelled) setStages(s) }, () => {})
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId, isPipeline, selected?.updated_at])

  // While the orchestrator or a stage runs, refresh the selected row +
  // stages (list only — never the draft — so unsaved edits survive).
  useEffect(() => {
    if (!pipelineBusy || selectedId === null) return
    const id = window.setInterval(() => {
      api.tasks.get(selectedId).then(
        (fresh) => setTasks((prev) => prev.map((t) => (t.id === fresh.id ? fresh : t))),
        () => {},
      )
      api.tasks.stages(selectedId).then(setStages, () => {})
    }, 3000)
    return () => window.clearInterval(id)
  }, [pipelineBusy, selectedId])

  const sameRepos = (a: string[], b: string[]) =>
    a.length === b.length && a.every((r, i) => r === b[i])
  const draftDirty =
    !!draft && (
      draft.id === null
        ? !!(
          draft.title.trim() ||
          draft.details.trim() ||
          draft.repos.length ||
          draft.prio !== 2 ||
          draft.refineModel ||
          draft.executeModel ||
          draft.planModel
        )
        : !!selected && (
          draft.title.trim() !== selected.title ||
          draft.details !== selected.details ||
          draft.prio !== selected.prio ||
          !sameRepos(draft.repos, selected.repos) ||
          draft.refineModel !== (selected.refine_model || '') ||
          draft.executeModel !== (selected.execute_model || '') ||
          draft.planModel !== (selected.plan_model || '')
        )
    )

  const confirmDiscardIfDirty = (): boolean => {
    if (!draftDirty) return true
    return confirm('Discard unsaved changes?')
  }
  const startNew = () => {
    if (!confirmDiscardIfDirty()) return
    closeStream()
    setDraft({ ...EMPTY_DRAFT })
  }
  const selectTask = (t: Task) => {
    if (draft && draft.id === t.id) return
    if (!confirmDiscardIfDirty()) return
    closeStream()
    setDraft(toDraft(t))
  }
  const closeDraft = () => {
    if (!confirmDiscardIfDirty()) return
    closeStream()
    setDraft(null)
  }

  // Escape closes the detail panel (asks first if there are unsaved edits).
  useEffect(() => {
    if (!draft) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') closeDraft() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  // Phones show list or detail, not both stacked: opening a task starts at the top.
  const draftKey = draft ? draft.id ?? 'new' : null
  useEffect(() => {
    if (draftKey !== null && window.matchMedia('(max-width: 1023px)').matches) window.scrollTo({ top: 0 })
  }, [draftKey])

  // Deep link (?task=<id>, from the inbox or a push): select it once the
  // list contains it.
  // Task chats (Tasks v2): which tasks have one.
  const [chatIds, setChatIds] = useState<Set<string>>(new Set())
  useEffect(() => {
    api.chat.channels().then((l) => setChatIds(new Set(l.filter((c) => c.task_id != null).map((c) => c.id))), () => {})
  }, [selected?.mode])
  const chatId = selected ? `task-${currentProject()}-${selected.id}` : ''
  const hasChat = !!selected && chatIds.has(chatId)
  const deepLinkDone = useRef(false)
  useEffect(() => {
    if (deepLinkDone.current || initialTaskId == null) return
    const t = tasks.find((x) => x.id === initialTaskId)
    if (t) {
      deepLinkDone.current = true
      selectTask(t)
    }
  }, [tasks, initialTaskId])

  // Persist any pending draft edits before triggering an agent run, so the
  // worker reads the user's latest details (not the stale DB row). Returns
  // the task as it is right after the save (or the unchanged selected row).
  const saveDraftIfDirty = async (): Promise<Task | null> => {
    if (!draft || draft.id === null || !selected) return selected ?? null
    if (!draftDirty) return selected
    if (!draft.title.trim()) {
      setError('Title cannot be empty.')
      return null
    }
    const saved = await api.tasks.update(draft.id, {
      title: draft.title.trim(),
      details: draft.details,
      prio: draft.prio,
      repos: draft.repos,
      refine_model: draft.refineModel,
      execute_model: draft.executeModel,
      plan_model: draft.planModel,
    })
    setTasks((prev) => prev.map((t) => (t.id === saved.id ? saved : t)))
    return saved
  }

  const refine = async () => {
    if (!selected) return
    setSaving(true)
    setError(null)
    try {
      const base = await saveDraftIfDirty()
      if (!base) return
      const updated = await api.tasks.refine(base.id)
      setTasks((prev) => prev.map((t) => (t.id === updated.id ? updated : t)))
      setDraft(toDraft(updated))
      openStream(updated.id, 'refine')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  const sendReply = async () => {
    if (!selected || !canReply) return
    const text = replyText.trim()
    if (!text) return
    setSendingReply(true)
    setError(null)
    try {
      const updated = await api.tasks.reply(selected.id, text)
      setTasks((prev) => prev.map((t) => (t.id === updated.id ? updated : t)))
      setDraft(toDraft(updated))
      setReplyText('')
      openStream(updated.id, 'refine')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSendingReply(false)
    }
  }

  const reopen = async () => {
    if (!selected || !canReopen) return
    setSaving(true)
    setError(null)
    try {
      const updated = await api.tasks.reopen(selected.id)
      setTasks((prev) => prev.map((t) => (t.id === updated.id ? updated : t)))
      setDraft(toDraft(updated))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  const markDone = async () => {
    if (!selected || !canMarkDone) return
    if (!confirm(
      `Mark task #${selected.id} "${selected.title}" as done?\n\n` +
      `Use it when the work was done manually or the task is no longer ` +
      `relevant. Reopen later if needed.`
    )) return
    setSaving(true)
    setError(null)
    try {
      const updated = await api.tasks.markDone(selected.id)
      setTasks((prev) => prev.map((t) => (t.id === updated.id ? updated : t)))
      setDraft(toDraft(updated))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  const moveToBacklog = async () => {
    if (!selected || !canBacklog) return
    setSaving(true)
    setError(null)
    try {
      await api.tasks.backlog(selected.id)
      // Task leaves the active list — deselect and refresh. Find it again
      // under the Backlog tab.
      closeStream()
      setDraft(null)
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  const closeTask = async () => {
    if (!selected || !canClose) return
    const reason = window.prompt(
      `Task #${selected.id} schließen — wird nicht gemacht, bleibt unter „Geschlossen“ nachlesbar.\n\nGrund (optional):`, '')
    if (reason === null) return
    setSaving(true)
    setError(null)
    try {
      await api.tasks.close(selected.id, reason.trim())
      closeStream()
      setDraft(null)
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  const restoreFromBacklog = async () => {
    if (!selected || !canRestore) return
    setSaving(true)
    setError(null)
    try {
      await api.tasks.restore(selected.id)
      // Task leaves the backlog list — drop selection and refresh so it
      // reappears on the board.
      closeStream()
      setDraft(null)
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  const startPipeline = async () => {
    if (!selected || !canPlan) return
    if (!confirm(
      `Task #${selected.id} "${selected.title}" starten?\n\n` +
      `Ein Planungs-Agent legt die Stufen fest (z. B. Umsetzung, Tests, Review), ` +
      `danach laufen sie bis zur nächsten Freigabe (Deploy, Rückfrage, Fehler) ` +
      `von selbst — sofern das Wochenbudget es erlaubt.`
    )) return
    setSaving(true)
    setError(null)
    try {
      const base = await saveDraftIfDirty()
      if (!base) return
      const updated = await api.tasks.plan(base.id)
      setTasks((prev) => prev.map((t) => (t.id === updated.id ? updated : t)))
      setDraft(toDraft(updated))
      openStream(updated.id, 'plan')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  // Pipeline-panel transitions only touch pipeline state, never the task's
  // editable fields — so update the row + stages and leave the draft alone.
  const onPipelineChanged = (t: Task) => {
    setTasks((prev) => prev.map((x) => (x.id === t.id ? t : x)))
    api.tasks.stages(t.id).then(setStages, () => {})
  }

  const save = async () => {
    if (!draft || !draft.title.trim()) return
    setSaving(true)
    setError(null)
    try {
      let result: Task
      if (draft.id === null) {
        result = await api.tasks.create({
          title: draft.title.trim(),
          details: draft.details,
          prio: draft.prio,
          repos: draft.repos,
          refine_model: draft.refineModel,
          execute_model: draft.executeModel,
          plan_model: draft.planModel,
        })
      } else {
        result = await api.tasks.update(draft.id, {
          title: draft.title.trim(),
          details: draft.details,
          prio: draft.prio,
          repos: draft.repos,
          refine_model: draft.refineModel,
          execute_model: draft.executeModel,
          plan_model: draft.planModel,
        })
      }
      await load()
      setDraft(toDraft(result))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  const remove = async () => {
    if (!draft?.id) return
    if (!confirm(`Delete task #${draft.id} "${draft.title}"?`)) return
    setSaving(true)
    setError(null)
    try {
      await api.tasks.remove(draft.id)
      setDraft(null)
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  const toggleRepo = (repo: string) => {
    if (!draft) return
    const has = draft.repos.includes(repo)
    setDraft({
      ...draft,
      repos: has ? draft.repos.filter((r) => r !== repo) : [...draft.repos, repo],
    })
  }

  return (
    <ModelCatalogProvider catalog={modelCatalog}>
    <div className="flex flex-col p-1 sm:p-2 lg:h-[calc(100dvh-var(--header-h,49px)-2rem)]">
      {/* Header */}
      <div className="mb-4 flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <h1 className="hidden text-lg font-semibold text-white sm:block">Tasks</h1>
          <div className="flex min-w-0 overflow-x-auto whitespace-nowrap rounded-md border border-slate-700 bg-slate-800 p-0.5 text-xs">
            <button
              onClick={() => { if (view !== 'board' && confirmDiscardIfDirty()) { setDraft(null); setView('board') } }}
              className={`flex items-center gap-1 rounded px-2.5 py-1 ${view === 'board' ? 'bg-slate-700 text-white' : 'text-slate-400 hover:text-slate-200'}`}
            >
              <Columns3 size={12} />
              Board
            </button>
            <button
              onClick={() => { if (view !== 'done' && confirmDiscardIfDirty()) { setDraft(null); setView('done') } }}
              className={`flex items-center gap-1 rounded px-2.5 py-1 ${view === 'done' ? 'bg-slate-700 text-white' : 'text-slate-400 hover:text-slate-200'}`}
            >
              <Check size={12} />
              Erledigt
              {doneCount > 0 && (
                <span className="rounded-full bg-green-900/60 px-1.5 text-[10px] text-green-300">{doneCount}</span>
              )}
            </button>
            <button
              onClick={() => { if (view !== 'closed' && confirmDiscardIfDirty()) { setDraft(null); setView('closed') } }}
              title="Geschlossen — wird nicht gemacht (zusammengelegt, überholt, anderweitig gelöst)"
              className={`flex items-center gap-1 rounded px-2.5 py-1 ${view === 'closed' ? 'bg-slate-700 text-white' : 'text-slate-400 hover:text-slate-200'}`}
            >
              <Ban size={12} />
              Geschlossen
            </button>
          </div>
        </div>
        <button
          onClick={startNew}
          title="New Task"
          aria-label="New Task"
          className="flex shrink-0 items-center gap-1.5 rounded-md bg-blue-600 px-2 py-1.5 text-xs text-white hover:bg-blue-500 sm:px-3"
        >
          <Plus size={14} />
          <span className="hidden whitespace-nowrap sm:inline">New Task</span>
        </button>
      </div>

      {error && (
        <div className="mb-3">
          <AttentionCard help={errorHelp(error, selected?.mode === 'pipeline')} taskId={selected?.id} />
        </div>
      )}

      <div className="flex min-h-0 flex-1 flex-col">
        {view === 'board' && !loading && !error ? (
          <Board
            tasks={tasks}
            selectedId={draft?.id ?? null}
            onSelect={selectTask}
            chatFor={(t) => (chatIds.has(`task-${currentProject()}-${t.id}`) ? `task-${currentProject()}-${t.id}` : null)}
            leadChannel={leadChannel}
            onAllDone={() => { if (confirmDiscardIfDirty()) { setDraft(null); setView('done') } }}
          />
        ) : (
        <div className="overflow-y-auto rounded-lg border border-slate-700 bg-slate-800">
          {loading ? (
            <div className="p-4 text-sm text-slate-500">Loading…</div>
          ) : tasks.length === 0 ? (
            <div className="p-4 text-sm text-slate-500">
              {view === 'backlog' ? (
                <>Nichts geparkt.</>
              ) : view === 'done' ? (
                <>No completed tasks yet. Finished tasks (<strong>Done</strong>) collect here.</>
              ) : view === 'closed' ? (
                <>Nichts geschlossen. Hier landen Tasks, die nicht gemacht werden — zusammengelegt, überholt oder anderweitig gelöst.</>
              ) : (
                <>No tasks yet. Click <strong>New Task</strong> to add one.</>
              )}
            </div>
          ) : (
            <>
              <div className="sticky top-0 z-10 border-b border-slate-700 bg-slate-800 p-2">
                <input value={query} onChange={(e) => setQuery(e.target.value)} type="search" inputMode="search"
                  placeholder={`Suchen: #Nummer oder Stichwort (${tasks.length})`}
                  className="w-full rounded border border-slate-600 bg-slate-900 px-2.5 py-1.5 text-sm text-slate-200 placeholder:text-slate-500" />
              </div>
              <ul className="divide-y divide-slate-700/70">
                {tasks.filter((t) => matchesQuery(t, query)).map((t) => (
                  <li key={t.id}>
                    <button
                      onClick={() => selectTask(t)}
                      title={`Priorität: ${PRIO_LABEL[t.prio]}`}
                      className={`flex w-full items-start gap-2.5 border-l-4 px-3 py-2 text-left hover:bg-slate-700/40 ${PRIO_BAR[t.prio]} ${
                        draft?.id === t.id ? 'bg-slate-700/60' : ''
                      }`}
                    >
                      <span className="w-10 shrink-0 pt-0.5 text-right font-mono text-xs text-slate-400">#{t.id}</span>
                      <span className="min-w-0 flex-1">
                        <span className={`block truncate text-sm ${t.prio === 1 ? 'font-medium text-slate-100' : 'text-slate-200'}`}>{t.title}</span>
                        {t.repos.length > 0 && (
                          <span className="block truncate text-[11px] text-slate-500">{t.repos.map(shortRepo).join(' · ')}</span>
                        )}
                      </span>
                      <span className="shrink-0 pt-0.5"><StatusBadge task={t} /></span>
                    </button>
                  </li>
                ))}
                {!tasks.some((t) => matchesQuery(t, query)) && (
                  <li className="p-4 text-sm text-slate-500">Kein Task passt zu „{query}“.</li>
                )}
              </ul>
            </>
          )}
        </div>
        )}
      </div>

      {/* Detail / Editor: a panel over the board (full screen on phones) */}
      {draft && (
      <div className="fixed inset-0 z-30 flex justify-end bg-black/50" onClick={closeDraft}>
        <div
          className="h-full w-full overflow-y-auto border-l border-slate-700 bg-slate-800 p-4 pt-[calc(1rem+env(safe-area-inset-top))] shadow-2xl lg:w-[min(820px,64vw)]"
          onClick={(e) => e.stopPropagation()}
        >
          {(
            <div className="flex flex-col gap-3">
              <div className="flex items-center justify-between gap-2">
                <h2 className="flex items-center gap-2 text-sm font-semibold text-slate-200">
                  <button onClick={closeDraft} className="-m-1.5 p-1.5 text-slate-400 hover:text-white lg:hidden" aria-label="Zurück zum Board">
                    <ArrowLeft size={18} />
                  </button>
                  {isNew ? 'New Task' : `Task #${draft.id}`}
                </h2>
                <div className="flex items-center gap-2">
                  {cost && cost.total_usd > 0 && (
                    <span
                      title={`${cost.runs_count} agent run${cost.runs_count === 1 ? '' : 's'} on this task — sum of Anthropic cost_usd from the result events`}
                      className="rounded border border-slate-700 bg-slate-900 px-1.5 py-0.5 text-[10px] tracking-wider text-slate-300"
                    >
                      ${cost.total_usd.toFixed(2)} · {cost.runs_count}×
                    </span>
                  )}
                  {selected && <StatusBadge task={selected} />}
                  <button
                    onClick={closeDraft}
                    className="rounded p-1 text-slate-500 hover:bg-slate-700 hover:text-slate-300"
                    aria-label="Close"
                  >
                    <X size={14} />
                  </button>
                </div>
              </div>

              {!isEditable && selected && (
                <div className="rounded border border-amber-800 bg-amber-900/20 px-2 py-1 text-[11px] text-amber-300">
                  Task is in status <strong>{selected.status}</strong> and cannot be edited.
                </div>
              )}

              {selected && <ClaudeLoginNotice task={selected} onRequired={setClaudeAuthRequired} />}
              {selected && !claudeAuthRequired && <TaskAttentionNotice task={selected} stages={stages} />}

              <label className="flex flex-col gap-1">
                <span className="text-[11px] uppercase tracking-wider text-slate-500">Title</span>
                <input
                  type="text"
                  value={draft.title}
                  onChange={(e) => setDraft({ ...draft, title: e.target.value })}
                  disabled={!isEditable}
                  maxLength={200}
                  placeholder="Short summary"
                  className="rounded border border-slate-700 bg-slate-900 px-2 py-1.5 text-sm text-slate-200 focus:border-blue-500 focus:outline-none disabled:opacity-60"
                />
              </label>

              <label className="flex flex-col gap-1">
                <span className="text-[11px] uppercase tracking-wider text-slate-500">Details</span>
                <textarea
                  value={draft.details}
                  onChange={(e) => setDraft({ ...draft, details: e.target.value })}
                  disabled={!isEditable}
                  rows={6}
                  placeholder="What needs to happen, context, constraints…"
                  className="rounded border border-slate-700 bg-slate-900 px-2 py-1.5 text-sm text-slate-200 focus:border-blue-500 focus:outline-none disabled:opacity-60"
                />
              </label>

              <label className="flex flex-col gap-1">
                <span className="text-[11px] uppercase tracking-wider text-slate-500">Priority</span>
                <select
                  value={draft.prio}
                  onChange={(e) => setDraft({ ...draft, prio: Number(e.target.value) as TaskPrio })}
                  disabled={!isEditable}
                  className="rounded border border-slate-700 bg-slate-900 px-2 py-1.5 text-sm text-slate-200 focus:border-blue-500 focus:outline-none disabled:opacity-60"
                >
                  <option value={1}>1 — High</option>
                  <option value={2}>2 — Normal</option>
                  <option value={3}>3 — Low</option>
                </select>
              </label>

              <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                <ModelPicker
                  label="Modell Ausarbeiten"
                  value={draft.refineModel}
                  onChange={(v) => setDraft({ ...draft, refineModel: v })}
                  disabled={!isEditable}
                  envDefault={workerDefaults?.refine}
                  envBackend={workerDefaults?.backend}
                  resolvedBackend={workerDefaults?.refine_backend}
                />
                <ModelPicker
                  label="Modell Planung"
                  value={draft.planModel}
                  onChange={(v) => setDraft({ ...draft, planModel: v })}
                  disabled={!isEditable}
                  envDefault={workerDefaults?.plan}
                  envBackend={workerDefaults?.backend}
                  resolvedBackend={workerDefaults?.plan_backend}
                />
              </div>
              <CatalogHint
                onRefresh={() => loadCatalog(true)}
                refreshing={catalogRefreshing}
                onUpgrade={upgradeClis}
                upgrading={cliUpgrading}
                upgradeLog={cliUpgradeLog}
              />

              <fieldset className="flex flex-col gap-1">
                <legend className="text-[11px] uppercase tracking-wider text-slate-500">
                  Sub-repos ({draft.repos.length} selected)
                </legend>
                <div className="flex flex-wrap gap-1.5">
                  {SUB_REPOS.map((repo) => {
                    const selectedRepo = draft.repos.includes(repo)
                    return (
                      <button
                        type="button"
                        key={repo}
                        onClick={() => toggleRepo(repo)}
                        disabled={!isEditable}
                        className={`rounded border px-2 py-0.5 text-[11px] transition-colors disabled:opacity-60 ${
                          selectedRepo
                            ? 'border-blue-600 bg-blue-900/40 text-blue-300'
                            : 'border-slate-700 bg-slate-900 text-slate-400 hover:border-slate-600'
                        }`}
                      >
                        {shortRepo(repo)}
                      </button>
                    )
                  })}
                  {/* A repo the orchestrator proposed as new (not in SUB_REPOS yet):
                      shown so it can be seen and removed; it only exists once an
                      implement stage scaffolds it. */}
                  {draft.repos.filter((r) => !SUB_REPOS.includes(r)).map((repo) => (
                    <button
                      type="button"
                      key={repo}
                      onClick={() => toggleRepo(repo)}
                      disabled={!isEditable}
                      title="Proposed new sub-repo (not created yet) — click to drop it from the task"
                      className="rounded border border-dashed border-sky-600 bg-sky-900/30 px-2 py-0.5 text-[11px] text-sky-300 disabled:opacity-60"
                    >
                      {shortRepo(repo)} · new
                    </button>
                  ))}
                </div>
              </fieldset>

              {selected && (
                <div className="grid grid-cols-2 gap-2 border-t border-slate-700 pt-3 text-[11px] text-slate-500">
                  <div>
                    <div className="uppercase tracking-wider">Created</div>
                    <div className="text-slate-400">{selected.created_at}</div>
                  </div>
                  <div>
                    <div className="uppercase tracking-wider">Updated</div>
                    <div className="text-slate-400">{selected.updated_at}</div>
                  </div>
                </div>
              )}

              {hasChat && (
                <Link to={`/chat/${chatId}`}
                  className="mt-1 flex items-center justify-center gap-2 rounded-lg border border-sky-700 bg-sky-900/30 px-3 py-2.5 text-sm text-sky-100 hover:bg-sky-900/50">
                  <MessageSquare size={16} /> Chat zum Task öffnen — dort siehst du jede Stufe
                </Link>
              )}
              {selected && isPipeline && hasChat && (
                <details className="mt-1 rounded border border-slate-700 bg-slate-900/40 px-2 py-1.5">
                  <summary className="cursor-pointer text-xs text-slate-400">Details: Stufen, Plan bearbeiten, Limits</summary>
                  <PipelinePanel
                    task={selected}
                    stages={stages}
                    roles={roles}
                    workerDefaults={workerDefaults}
                    cost={cost}
                    busy={saving}
                    onChanged={onPipelineChanged}
                    onBeforePlan={saveDraftIfDirty}
                    onOpenStream={(kind) => openStream(selected.id, kind)}
                    onError={setError}
                  />
                </details>
              )}
              {selected && isPipeline && !hasChat && (
                <PipelinePanel
                  task={selected}
                  stages={stages}
                  roles={roles}
                  workerDefaults={workerDefaults}
                  cost={cost}
                  busy={saving}
                  onChanged={onPipelineChanged}
                  onBeforePlan={saveDraftIfDirty}
                  onOpenStream={(kind) => openStream(selected.id, kind)}
                  onError={setError}
                />
              )}

              <div id={`task-actions-${selected?.id}`} className="mt-2 flex flex-wrap items-center gap-2">
                <button
                  onClick={save}
                  disabled={saving || !draft.title.trim() || !isEditable}
                  title={draftDirty ? 'Unsaved changes' : 'No changes to save'}
                  className={`flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs text-white hover:bg-blue-500 disabled:opacity-50 ${
                    draftDirty ? 'bg-blue-600 ring-1 ring-blue-300/40' : 'bg-blue-600'
                  }`}
                >
                  <Save size={14} />
                  {saving ? 'Saving…' : isNew ? 'Create' : draftDirty ? 'Save*' : 'Save'}
                </button>
                {!isNew && !isPipeline && (
                  <button
                    onClick={refine}
                    disabled={saving || !canRefine}
                    title={
                      !canRefine && selected
                        ? `Ausarbeiten geht nicht im Status '${selected.status}'`
                        : selected?.status === 'limit_reached'
                          ? 'Erneut — setzt eine laufende Rückfrage fort, sonst neu ausarbeiten'
                          : selected?.status === 'refined' || selected?.status === 'failed'
                            ? 'Neu ausarbeiten — ersetzt die bisherige Ausarbeitung'
                            : 'Ausarbeiten — ein Agent macht aus der Idee eine kurze Spezifikation (liest nur, ändert nichts)'
                    }
                    className={`flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs disabled:opacity-50 ${
                      selected?.status === 'limit_reached'
                        ? 'border-orange-700 bg-orange-900/20 text-orange-300 hover:bg-orange-900/40'
                        : 'border-amber-700 bg-amber-900/20 text-amber-300 hover:bg-amber-900/40'
                    }`}
                  >
                    <Play size={14} />
                    {selected?.status === 'refining'
                      ? 'Wird ausgearbeitet…'
                      : selected?.status === 'limit_reached'
                        ? 'Erneut'
                        : selected?.refined_spec
                          ? 'Neu ausarbeiten'
                          : 'Ausarbeiten'}
                  </button>
                )}
                {!isNew && canPlan && (
                  <button
                    onClick={startPipeline}
                    disabled={saving}
                    title="Starten — ein Planungs-Agent legt die Stufen fest, danach läuft der Task bis zur nächsten Freigabe von selbst."
                    className="flex items-center gap-1.5 rounded-md border border-sky-700 bg-sky-900/20 px-3 py-1.5 text-xs text-sky-300 hover:bg-sky-900/40 disabled:opacity-50"
                  >
                    <GitBranch size={14} />
                    Starten
                  </button>
                )}
                {!isNew && canReopen && (
                  <button
                    onClick={reopen}
                    disabled={saving}
                    title="Wieder öffnen — landet in „Zur Freigabe“, wenn es eine Ausarbeitung gibt, sonst als Idee. Bisherige Ausarbeitung und Zusammenfassung bleiben als Verlauf."
                    className="flex items-center gap-1.5 rounded-md border border-slate-600 bg-slate-700/40 px-3 py-1.5 text-xs text-slate-200 hover:bg-slate-700/70 disabled:opacity-50"
                  >
                    <RotateCcw size={14} />
                    Reopen
                  </button>
                )}
                {!isNew && canMarkDone && (
                  <button
                    onClick={markDone}
                    disabled={saving}
                    title="Als erledigt markieren — wenn die Arbeit von Hand erledigt wurde oder der Task nicht mehr nötig ist. Wieder öffnen stellt ihn her."
                    className="flex items-center gap-1.5 rounded-md border border-green-700 bg-green-900/20 px-3 py-1.5 text-xs text-green-300 hover:bg-green-900/40 disabled:opacity-50"
                  >
                    <Check size={14} />
                    Mark Done
                  </button>
                )}
                {!isNew && canBacklog && (
                  <button
                    onClick={moveToBacklog}
                    disabled={saving}
                    title="Backlog — soll später noch gemacht werden. Verlässt die aktive Liste (der Worker nimmt ihn nicht), behält aber Spec und Verlauf."
                    className="flex items-center gap-1.5 rounded-md border border-indigo-700 bg-indigo-900/20 px-3 py-1.5 text-xs text-indigo-300 hover:bg-indigo-900/40 disabled:opacity-50"
                  >
                    <Archive size={14} />
                    Backlog
                  </button>
                )}
                {!isNew && canClose && (
                  <button
                    onClick={closeTask}
                    disabled={saving}
                    title="Schließen — wird nicht gemacht (überholt, Duplikat, anderweitig gelöst). Bleibt unter „Geschlossen“ nachlesbar und lässt sich wieder öffnen."
                    className="flex items-center gap-1.5 rounded-md border border-slate-600 bg-slate-700/30 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-700/60 disabled:opacity-50"
                  >
                    <Ban size={14} />
                    Schließen
                  </button>
                )}
                {!isNew && canRestore && (
                  <button
                    onClick={restoreFromBacklog}
                    disabled={saving}
                    title="Zurückholen — landet in „Zur Freigabe“, wenn es eine Ausarbeitung gibt, sonst als Idee."
                    className="flex items-center gap-1.5 rounded-md border border-indigo-700 bg-indigo-900/20 px-3 py-1.5 text-xs text-indigo-300 hover:bg-indigo-900/40 disabled:opacity-50"
                  >
                    <ArchiveRestore size={14} />
                    {selected?.status === 'closed' ? 'Wieder öffnen' : 'Restore'}
                  </button>
                )}
                {!isNew && (
                  <button
                    onClick={remove}
                    disabled={saving}
                    className="flex items-center gap-1.5 rounded-md border border-red-800 bg-red-900/20 px-3 py-1.5 text-xs text-red-300 hover:bg-red-900/40 disabled:opacity-50"
                  >
                    <Trash2 size={14} />
                    Delete
                  </button>
                )}
                {!isNew && !streamOpen && selected && !isPipeline && (
                  <div className="flex flex-wrap gap-1 sm:ml-auto">
                    <button
                      onClick={() => openStream(selected.id, 'refine')}
                      className="rounded-md border border-slate-600 px-2 py-1 text-[11px] text-slate-300 hover:bg-slate-700/50"
                    >
                      View refine log
                    </button>
                    {!!selected.execute_summary && (
                      <button
                        onClick={() => openStream(selected.id, 'execute')}
                        className="rounded-md border border-slate-600 px-2 py-1 text-[11px] text-slate-300 hover:bg-slate-700/50"
                      >
                        View execute log
                      </button>
                    )}
                  </div>
                )}
              </div>

              {selected?.lead_note && !streamOpen && (
                <div className="mt-1 rounded border border-amber-800/60 bg-amber-900/10 p-3">
                  <div className="mb-1 text-[10px] uppercase tracking-wider text-amber-400">
                    {selected.status === 'backlog' ? 'Geparkt (für später)'
                      : selected.status === 'closed' ? 'Geschlossen — Grund'
                      : 'Empfehlung des Lead'}
                  </div>
                  <p className="whitespace-pre-wrap break-words text-[12px] text-slate-300">{selected.lead_note}</p>
                </div>
              )}

              {selected && !streamOpen && <BranchPanel taskId={selected.id} status={selected.status} />}

              {selected?.refined_spec && !streamOpen && (
                <div className="mt-1 rounded border border-slate-700 bg-slate-900 p-3">
                  <div className="mb-1 text-[10px] uppercase tracking-wider text-slate-500">
                    {selected.status === 'new' ? 'Previous refined spec' : 'Refined spec'}
                  </div>
                  <pre className="whitespace-pre-wrap break-words text-[12px] text-slate-300">
                    {selected.refined_spec}
                  </pre>
                </div>
              )}

              {canReply && !streamOpen && (
                <div className="mt-1 rounded border border-amber-900/60 bg-slate-900 p-2">
                  <div className="mb-1 flex items-center justify-between text-[10px] uppercase tracking-wider text-amber-400">
                    <span>Reply to agent</span>
                    <span className="text-slate-600 normal-case tracking-normal">
                      answers Open questions or asks for spec changes — Cmd/Ctrl+Enter to send
                    </span>
                  </div>
                  <textarea
                    value={replyText}
                    onChange={(e) => setReplyText(e.target.value)}
                    onKeyDown={(e) => {
                      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
                        e.preventDefault()
                        void sendReply()
                      }
                    }}
                    rows={3}
                    disabled={sendingReply}
                    placeholder="e.g. 'For Q1: pick option B. For Q2: keep the original universe.'"
                    className="w-full rounded border border-slate-700 bg-slate-950 px-2 py-1.5 text-[12px] text-slate-200 focus:border-amber-500 focus:outline-none disabled:opacity-60"
                  />
                  <div className="mt-1 flex items-center justify-end">
                    <button
                      onClick={sendReply}
                      disabled={sendingReply || !replyText.trim()}
                      className="flex items-center gap-1.5 rounded-md bg-amber-700 px-3 py-1 text-xs text-white hover:bg-amber-600 disabled:opacity-50"
                    >
                      <Send size={13} />
                      {sendingReply ? 'Sending…' : 'Send'}
                    </button>
                  </div>
                </div>
              )}

              {selected?.execute_summary && !streamOpen && (
                <div className="mt-1 rounded border border-emerald-900/60 bg-emerald-950/30 p-3">
                  <div className="mb-1 text-[10px] uppercase tracking-wider text-emerald-500">
                    {selected.status === 'done' ? 'Execute summary' : 'Previous execute summary'}
                  </div>
                  <pre className="whitespace-pre-wrap break-words text-[12px] text-slate-200">
                    {selected.execute_summary}
                  </pre>
                </div>
              )}

              {streamOpen && (
                <div className="mt-1 flex max-h-[420px] flex-col rounded border border-slate-700 bg-slate-950">
                  <div className="flex items-center justify-between border-b border-slate-700 px-3 py-1.5">
                    <span className="text-[11px] uppercase tracking-wider text-slate-400">
                      {streamLabel(streamKind)}
                      {selected?.status && (
                        <span className="ml-2 text-slate-500">
                          status: {selected.status}
                        </span>
                      )}
                    </span>
                    <button
                      onClick={closeStream}
                      className="rounded p-0.5 text-slate-500 hover:bg-slate-800 hover:text-slate-300"
                      aria-label="Close stream"
                    >
                      <X size={12} />
                    </button>
                  </div>
                  <div
                    ref={streamPaneRef}
                    onScroll={(e) => {
                      const el = e.currentTarget
                      streamFollowRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24
                    }}
                    className="overflow-y-auto p-2 font-mono text-[11px] leading-relaxed"
                  >
                    {streamEvents.length === 0 ? (
                      <div className="text-slate-600">Waiting for agent…</div>
                    ) : (
                      streamEvents.map((ev, i) => <StreamLine key={i} ev={ev} />)
                    )}
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
      )}
    </div>
    </ModelCatalogProvider>
  )
}

// The task's own branch: what it holds compared to the live checkout, the
// diff, and "Übernehmen" (the worker merges it on its next poll).
function BranchPanel({ taskId, status }: { taskId: number; status: string }) {
  const [c, setC] = useState<TaskChanges | null>(null)
  const [showDiff, setShowDiff] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  useEffect(() => {
    let live = true
    setC(null)
    setShowDiff(false)
    setMsg(null)
    const load = () => api.tasks.changes(taskId).then((r) => { if (live) setC(r) }, () => {})
    load()
    const id = window.setInterval(load, 20_000)
    return () => { live = false; window.clearInterval(id) }
  }, [taskId, status])
  const st = c?.state
  if (!st) return null
  const repos = Object.entries(st.repos).filter(([, r]) => r.ahead || r.error)
  const apply = async () => {
    try {
      await api.tasks.apply(taskId)
      setMsg('Wird übernommen — das Ergebnis steht gleich im Task-Chat.')
    } catch (e) { setMsg(e instanceof Error ? e.message : String(e)) }
  }
  return (
    <div className="mt-1 rounded border border-slate-700 bg-slate-900 p-3">
      <div className="mb-1 flex items-center gap-1 text-[10px] uppercase tracking-wider text-slate-500">
        <GitBranch size={11} /> Branch {st.branch}
      </div>
      {st.pending ? (
        <ul className="space-y-0.5 text-[12px] text-slate-300">
          {repos.map(([name, r]) => (
            <li key={name}>
              {r.error ? <span className="text-red-400">{name}: {r.error}</span> : <>
                <span className="font-mono">{name}</span>: {r.ahead} Commit{r.ahead === 1 ? '' : 's'} vor {r.base}
                {r.stat ? <span className="text-slate-500"> · {r.stat}</span> : null}
                {r.behind ? <span className="text-amber-400"> · {r.base} ist {r.behind} weiter</span> : null}
              </>}
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-[12px] text-slate-400">
          {st.applied?.length ? `Übernommen: ${st.applied.join(', ')}` : 'Noch keine Änderungen auf dem Branch.'}
        </p>
      )}
      <div className="mt-2 flex flex-wrap gap-2">
        {c.diff && (
          <button onClick={() => setShowDiff((v) => !v)}
            className="rounded-md border border-slate-600 px-2 py-1 text-[11px] text-slate-300 hover:bg-slate-700/50">
            {showDiff ? 'Diff ausblenden' : 'Diff anzeigen'}
          </button>
        )}
        {st.pending && status !== 'running' && (
          <button onClick={apply} title="In den Live-Branch einspielen (Rebase + Fast-Forward), kein Push, kein Deploy"
            className="rounded-md bg-green-700 px-2 py-1 text-[11px] text-white hover:bg-green-600">
            Übernehmen
          </button>
        )}
      </div>
      {msg && <p className="mt-1 text-[11px] text-slate-400">{msg}</p>}
      {showDiff && (
        <pre className="mt-2 max-h-[60vh] overflow-auto rounded bg-slate-950 p-2 text-[11px] leading-snug">
          {c.diff.split('\n').map((l, i) => (
            <div key={i} className={l.startsWith('+') && !l.startsWith('+++') ? 'text-green-400'
              : l.startsWith('-') && !l.startsWith('---') ? 'text-red-400'
              : l.startsWith('@@') || l.startsWith('#####') ? 'text-sky-400' : 'text-slate-400'}>{l || ' '}</div>
          ))}
        </pre>
      )}
    </div>
  )
}

function StreamLine({ ev }: { ev: StreamEvent }) {
  const kind = ev.event ?? 'msg'
  const time = ev.ts ? ev.ts.slice(11, 19) : ''
  const tagColor: Record<string, string> = {
    text: 'text-slate-200',
    tool_use: 'text-blue-300',
    tool_result: 'text-emerald-300',
    error: 'text-red-300',
    complete: 'text-amber-300',
    done: 'text-amber-300',
    init: 'text-slate-500',
    hello: 'text-slate-600',
    start: 'text-slate-500',
    thinking: 'text-slate-600',
    result: 'text-slate-500',
    raw: 'text-slate-400',
    repos_updated: 'text-cyan-300',
    details_updated: 'text-cyan-300',
    user_reply: 'text-amber-200',
    limit_reached: 'text-orange-300',
  }
  const colour = tagColor[kind] ?? 'text-slate-400'

  let body: string
  if (kind === 'text') {
    body = ev.text ?? ''
  } else if (kind === 'tool_use') {
    const detail = formatToolInput(ev.name, ev.input)
    body = `${ev.name}${detail ? '(' + detail + ')' : ''}`
  } else if (kind === 'tool_result') {
    const head = (ev.text ?? '').split('\n')[0] ?? ''
    body = (ev.is_error ? '! ' : '  ') + (head || '(no output)')
  } else if (kind === 'error') {
    body = ev.message ?? ''
  } else if (kind === 'complete') {
    const chars = ev.summary_chars ?? ev.spec_chars ?? ev.plan_chars ?? ev.result_chars ?? 0
    const what = ev.summary_chars != null
      ? 'execute summary'
      : ev.plan_chars != null
        ? 'plan'
        : ev.result_chars != null
          ? 'stage result'
          : 'refined spec'
    body = `wrote ${what} (${chars} chars)`
  } else if (kind === 'done') {
    const stage = ev.stage ? ` stage ${ev.stage.seq ?? '?'}=${ev.stage.status ?? '?'}${ev.stage.verdict ? '/' + ev.stage.verdict : ''}` : ''
    body = `status=${ev.status ?? '?'}${stage}${ev.error ? ' — ' + ev.error : ''}${ev.stop_reason ? ' — ' + ev.stop_reason : ''}`
  } else if (kind === 'init') {
    body = `model=${ev.model ?? '?'}`
  } else if (kind === 'result') {
    const c = ev.cost_usd != null ? `💰 $${ev.cost_usd.toFixed(2)}` : ''
    const ms = ev.duration_ms != null ? `${(ev.duration_ms / 1000).toFixed(1)}s` : ''
    body = `${ev.num_turns ?? '?'} turns  ${ms}  ${c}`.trim()
  } else if (kind === 'thinking') {
    body = '(thinking…)'
  } else if (kind === 'hello' || kind === 'start') {
    body = ''
  } else if (kind === 'repos_updated') {
    const before = (ev.before ?? []).map(shortRepo).join(', ') || '(none)'
    const after = (ev.after ?? []).map(shortRepo).join(', ') || '(none)'
    body = `repos: ${before} → ${after}`
  } else if (kind === 'details_updated') {
    body = `details auto-filled (${ev.chars ?? 0} chars)`
  } else if (kind === 'user_reply') {
    body = `▶ ${ev.text ?? ''}`
  } else if (kind === 'limit_reached') {
    body = `usage limit hit — ${ev.message ?? 'click Retry after reset'}`
  } else {
    body = ev.text ?? JSON.stringify(ev)
  }

  return (
    <div className={`flex gap-2 ${colour}`}>
      {time && <span className="shrink-0 text-slate-700">{time}</span>}
      <span className="shrink-0 text-slate-600">{kind}</span>
      <span className="min-w-0 flex-1 whitespace-pre-wrap break-words">{body}</span>
    </div>
  )
}

function formatToolInput(name: string | undefined, input: Record<string, unknown> | undefined): string {
  if (!input) return ''
  if (name === 'Read' || name === 'Write' || name === 'Edit') return String(input.file_path ?? '')
  if (name === 'Bash') return String(input.command ?? '').slice(0, 200)
  if (name === 'Grep') return `${input.pattern ?? ''} in ${input.path ?? '.'}`
  if (name === 'Glob') return String(input.pattern ?? '')
  if (name === 'WebFetch' || name === 'WebSearch') return String(input.url ?? input.query ?? '')
  const first = Object.values(input)[0]
  return first === undefined ? '' : String(first).slice(0, 200)
}
