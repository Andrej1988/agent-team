import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import './index.css'
import { watchVersion } from './version'

watchVersion()

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {})
  // Tap on a push while this window is open but not controlled by the worker:
  // sw.js asks the page to go to the push's target itself.
  navigator.serviceWorker.addEventListener('message', (e) => {
    const d = e.data as { type?: string; url?: string } | null
    if (d?.type === 'open' && d.url && new URL(d.url).origin === location.origin && d.url !== location.href) {
      location.assign(d.url)
    }
  })
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>,
)
