// Keep open tabs on the current frontend: after a deploy an old bundle keeps
// running and silently ignores buttons it doesn't know (user 2026-10-29).
// Coming back to the app (tab visible again) reloads at once; while it is in
// view a small banner offers the reload instead of yanking the page away.

let loaded: string | null = null
let stale = false

async function serverBuild(): Promise<string | null> {
  try {
    const r = await fetch('/api/version', { cache: 'no-store' })
    if (!r.ok) return null
    return ((await r.json()) as { build?: string }).build || null
  } catch {
    return null
  }
}

function typing(): boolean {
  const el = document.activeElement as HTMLInputElement | HTMLTextAreaElement | null
  return !!el && (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') && !!el.value
}

function banner(): void {
  if (document.getElementById('at-new-version')) return
  const b = document.createElement('button')
  b.id = 'at-new-version'
  b.textContent = 'Neue Version der App — tippen zum Neuladen'
  b.style.cssText = 'position:fixed;left:50%;transform:translateX(-50%);bottom:calc(84px + env(safe-area-inset-bottom));'
    + 'z-index:50;padding:10px 16px;border-radius:9999px;border:1px solid #38bdf8;background:#0c4a6e;color:#e0f2fe;'
    + 'font-size:14px;box-shadow:0 4px 16px rgba(0,0,0,.4)'
  b.onclick = () => location.reload()
  document.body.appendChild(b)
}

async function check(): Promise<void> {
  const v = await serverBuild()
  if (!v) return
  if (loaded === null) { loaded = v; return }
  if (v === loaded) return
  stale = true
  if (document.visibilityState === 'visible') banner()
}

export function watchVersion(): void {
  void check()
  window.setInterval(check, 60_000)
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible') return
    if (stale && !typing()) location.reload()
    else void check()
  })
}
