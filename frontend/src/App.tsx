import { useEffect, useRef, useState } from 'react'
import { NavLink, Navigate, Route, Routes, useLocation, useParams, useSearchParams } from 'react-router-dom'
import { Bell, Lightbulb, Settings as SettingsIcon } from 'lucide-react'
import { api, setProject, type ProjectInfo } from './api/client'
import { Pill, PillRow } from './components/nav/Pills'
import { HeaderBudget } from './components/Budget'
import Chat from './pages/Chat'
import ChatList from './pages/ChatList'
import Inbox from './pages/Inbox'
import Logs from './pages/Logs'
import System from './pages/System'
import Settings from './pages/Settings'
import Share from './pages/Share'
import Tasks from './pages/Tasks'

function ProjectTasks({ projects }: { projects: ProjectInfo[] }) {
  const { project = '' } = useParams()
  const [params] = useSearchParams()
  if (projects.length === 0) return <div className="text-sm text-slate-400">Lade …</div>
  if (!projects.some((p) => p.id === project)) {
    return <div className="p-4 text-sm text-slate-400">Unbekanntes Projekt „{project}“.</div>
  }
  // Module-level project for the API client; key remounts the page so no
  // state leaks between projects.
  setProject(project)
  try { localStorage.setItem('at-last-project', project) } catch { /* private mode */ }
  const task = Number(params.get('task'))
  const withTasks = projects.filter((p) => p.tasks)
  return (
    <div className="space-y-3">
      {withTasks.length > 1 && (
        <PillRow label="Projekt">
          {withTasks.map((p) => <Pill key={p.id} to={`/p/${p.id}/tasks`} active={p.id === project}>{p.name}</Pill>)}
        </PillRow>
      )}
      <Tasks key={project} initialTaskId={Number.isFinite(task) && task > 0 ? task : undefined} />
    </div>
  )
}

function TasksHome({ projects }: { projects: ProjectInfo[] }) {
  const withTasks = projects.filter((p) => p.tasks)
  if (withTasks.length === 0) return <div className="text-sm text-slate-400">Lade …</div>
  let last = ''
  try { last = localStorage.getItem('at-last-project') ?? '' } catch { /* private mode */ }
  const target = withTasks.find((p) => p.id === last) ?? withTasks[0]
  return <Navigate to={`/p/${target.id}/tasks`} replace />
}

const Badge = ({ n, cls }: { n: number; cls: string }) =>
  n > 0 ? <span className={`rounded-full px-1.5 text-[11px] font-semibold ${cls}`}>{n}</span> : null

const iconCls = ({ isActive }: { isActive: boolean }) =>
  `rounded-[7px] p-1.5 transition-colors ${isActive ? 'bg-slate-700 text-white' : 'text-slate-400 hover:bg-slate-800 hover:text-slate-200'}`

export default function App() {
  const location = useLocation()
  const section = location.pathname.startsWith('/p/') || location.pathname.startsWith('/tasks') ? 'tasks'
    : location.pathname.startsWith('/logs') ? 'logs'
    : location.pathname.startsWith('/system') ? 'system' : ''
  const [projects, setProjects] = useState<ProjectInfo[]>([])
  const [inboxCount, setInboxCount] = useState(0)
  const [chatUnread, setChatUnread] = useState(0)

  useEffect(() => {
    api.projects().then(setProjects, () => setProjects([]))
  }, [])
  useEffect(() => {
    const load = () => {
      api.inbox().then((i) => setInboxCount(i.length), () => {})
      api.chat.channels().then((c) => setChatUnread(c.filter((x) => !x.archived).reduce((n, x) => n + x.unread, 0)), () => {})
    }
    load()
    const id = window.setInterval(load, 30_000)
    return () => window.clearInterval(id)
  }, [])

  // The nav wraps to several rows on phones; full-height pages (chat) size
  // themselves from the real header height instead of a fixed guess.
  const headerRef = useRef<HTMLElement | null>(null)
  useEffect(() => {
    const el = headerRef.current
    if (!el) return
    const set = () => document.documentElement.style.setProperty('--header-h', `${el.offsetHeight}px`)
    set()
    const ro = new ResizeObserver(set)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  return (
    <div className="min-h-screen">
      <header ref={headerRef} className="sticky top-0 z-20 border-b border-slate-800 bg-slate-900/95 backdrop-blur pt-[env(safe-area-inset-top)]">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-x-3 gap-y-2 px-4 py-2">
          <NavLink to="/inbox" className="flex items-center gap-2" aria-label="agent-team">
            <img src="/static/icon-192.png" alt="" className="h-7 w-7 rounded-md" />
            <span className="hidden font-semibold text-white sm:inline">agent-team</span>
          </NavLink>
          <PillRow label="Bereich" className="order-last w-full sm:order-none sm:w-auto [&>a]:flex-1 sm:[&>a]:flex-none [&>a]:justify-center">
            <Pill to="/inbox">Inbox <Badge n={inboxCount} cls="bg-amber-500 text-slate-900" /></Pill>
            <Pill to="/chat">Chats <Badge n={chatUnread} cls="bg-sky-600 text-white" /></Pill>
            <Pill to="/tasks" active={section === 'tasks'}>Tasks</Pill>
            <Pill to="/logs" active={section === 'logs'}>Logs</Pill>
            <Pill to="/system" active={section === 'system'}>System</Pill>
          </PillRow>
          <div className="ml-auto flex items-center gap-1">
            <HeaderBudget />
            <NavLink to="/share" className={iconCls} title="Link oder Idee als Task speichern" aria-label="Idee speichern">
              <Lightbulb size={17} />
            </NavLink>
            <NavLink to="/settings" className={iconCls} title="Einstellungen" aria-label="Einstellungen">
              <SettingsIcon size={17} />
            </NavLink>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-4 py-4 pb-[calc(1rem+env(safe-area-inset-bottom))]">
        <Routes>
          <Route path="/" element={<Navigate to="/inbox" replace />} />
          <Route path="/inbox" element={<Inbox onCount={setInboxCount} />} />
          <Route path="/tasks" element={<TasksHome projects={projects} />} />
          <Route path="/p/:project/tasks" element={<ProjectTasks projects={projects} />} />
          <Route path="/logs" element={<Logs />} />
          <Route path="/logs/:group" element={<Logs />} />
          <Route path="/logs/:group/:container" element={<Logs />} />
          <Route path="/system" element={<System />} />
          <Route path="/system/:part" element={<System />} />
          <Route path="/chat" element={<ChatList onUnread={setChatUnread} />} />
          <Route path="/chat/:channel" element={<Chat key={location.pathname} />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/share" element={<Share projects={projects} />} />
          <Route path="*" element={<div className="text-sm text-slate-400"><Bell size={14} className="inline" /> Seite nicht gefunden.</div>} />
        </Routes>
      </main>
    </div>
  )
}
