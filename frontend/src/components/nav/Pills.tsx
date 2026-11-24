import { NavLink } from 'react-router-dom'
import type { ReactNode } from 'react'

// Segmented pill row: a tinted track with
// the active item raised. Scrolls sideways instead of wrapping on phones.
export function PillRow({ children, label, className = '' }: { children: ReactNode; label: string; className?: string }) {
  return (
    <nav
      aria-label={label}
      className={`inline-flex max-w-full gap-0.5 overflow-x-auto rounded-[9px] bg-slate-800/80 p-[3px] [scrollbar-width:none] ${className}`}
    >
      {children}
    </nav>
  )
}

export const pillCls = (active: boolean) =>
  `flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-[7px] px-2.5 py-1 text-[13px] transition-colors ${
    active ? 'bg-slate-600 font-semibold text-white shadow-sm' : 'text-slate-400 hover:text-slate-200'
  }`

export function Pill({ to, children, active, title, end }: {
  to: string; children: ReactNode; active?: boolean; title?: string; end?: boolean
}) {
  return (
    <NavLink
      to={to}
      end={end}
      title={title}
      className={({ isActive }) => pillCls(active ?? isActive)}
    >
      {children}
    </NavLink>
  )
}
