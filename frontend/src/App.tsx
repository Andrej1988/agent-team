import { useEffect, useRef, useState } from 'react'
import { NavLink, Navigate, Route, Routes, useLocation, useParams, useSearchParams } from 'react-router-dom'
import { Bell, Lightbulb, Inbox as InboxIcon, ListTodo, MessagesSquare, Settings as SettingsIcon } from 'lucide-react'
import { api, setProject, type ProjectInfo } from './api/client'
import Chat from './pages/Chat'
import ChatList from './pages/ChatList'
import Inbox from './pages/Inbox'
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
  const task = Number(params.get('task'))
  return <Tasks key={project} initialTaskId={Number.isFinite(task) && task > 0 ? task : undefined} />
}

const navCls = ({ isActive }: { isActive: boolean }) =>
  `flex items-center gap-1.5 rounded px-2.5 py-1.5 text-sm transition-colors ${
    isActive ? 'bg-slate-700 text-white' : 'text-slate-300 hover:bg-slate-800'
  }`

export default function App() {
  const location = useLocation()
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
        <nav className="mx-auto flex max-w-7xl flex-wrap items-center gap-1 px-4 py-2">
          <span className="mr-2 font-semibold text-white">agent-team</span>
          <NavLink to="/inbox" className={navCls}>
            <InboxIcon size={15} /> Inbox
            {inboxCount > 0 && (
              <span className="ml-0.5 rounded-full bg-amber-500 px-1.5 text-[11px] font-semibold text-slate-900">{inboxCount}</span>
            )}
          </NavLink>
          <NavLink to="/chat" className={navCls}>
            <MessagesSquare size={15} /> Chat
            {chatUnread > 0 && (
              <span className="ml-0.5 rounded-full bg-sky-600 px-1.5 text-[11px] font-semibold text-white">{chatUnread}</span>
            )}
          </NavLink>
          {projects.filter((p) => p.tasks).map((p) => (
            <NavLink key={p.id} to={`/p/${p.id}/tasks`} className={navCls}>
              <ListTodo size={15} /> {p.name}
            </NavLink>
          ))}
          <NavLink to="/share" className={navCls} title="Link oder Idee als Task speichern">
            <Lightbulb size={15} /> Idee
          </NavLink>
          <NavLink to="/settings" className={navCls} aria-label="Einstellungen">
            <SettingsIcon size={15} />
          </NavLink>
        </nav>
      </header>
      <main className="mx-auto max-w-7xl px-4 py-4 pb-[calc(1rem+env(safe-area-inset-bottom))]">
        <Routes>
          <Route path="/" element={<Navigate to="/inbox" replace />} />
          <Route path="/inbox" element={<Inbox onCount={setInboxCount} />} />
          <Route path="/p/:project/tasks" element={<ProjectTasks projects={projects} />} />
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
