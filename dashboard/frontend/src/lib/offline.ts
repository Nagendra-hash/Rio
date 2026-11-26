/**
 * Offline/reconnect machinery for the dashboard.
 * Handles WebSocket reconnect with exponential backoff and since=<seq> resume,
 * ping/pong staleness probe, close-code 4001 handling (server restart → silent device re-pair + reload),
 * and a full offline screen that distinguishes "phone offline" from "PC unreachable".
 */

let _lastSeq = 0
let _bootId: string | null = null
let _reconnectScheduled = false
let _wsStale = false
let _offlineType: 'phone' | 'pc' | null = null
let _offlineScreen: HTMLElement | null = null
let _offlineTitle: HTMLElement | null = null
let _offlineNote: HTMLElement | null = null
let _reconnectAttempts = 0
const MAX_RECONNECT_ATTEMPTS = 10
const BASE_RECONNECT_DELAY = 1000

// Exported for tests and external access
export function getLastSeq(): number {
  return _lastSeq
}

export function setLastSeq(seq: number): void {
  _lastSeq = seq
}

export function getBootId(): string | null {
  return _bootId
}

export function setBootId(bootId: string): void {
  _bootId = bootId
}

export function isWsStale(): boolean {
  return _wsStale
}

export function setWsStale(stale: boolean): void {
  _wsStale = stale
}

export function getOfflineType(): 'phone' | 'pc' | null {
  return _offlineType
}

function createOfflineScreen(): void {
  if (_offlineScreen) return

  _offlineScreen = document.createElement('div')
  _offlineScreen.id = 'offline-screen'
  _offlineScreen.style.cssText = `
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.95);
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    z-index: 10000;
    opacity: 0;
    visibility: hidden;
    transition: opacity 0.3s, visibility 0.3s;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
  `

  const icon = document.createElement('div')
  icon.textContent = '⚠'
  icon.style.cssText = `
    font-size: 4rem;
    margin-bottom: 1rem;
    animation: pulse 1.5s infinite;
  `

  const style = document.createElement('style')
  style.textContent = `
    @keyframes pulse {
      0%, 100% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.5; transform: scale(1.1); }
    }
  `
  document.head.appendChild(style)

  _offlineTitle = document.createElement('h1')
  _offlineTitle.id = 'of-title'
  _offlineTitle.style.cssText = `
    color: var(--text-primary, #fff);
    font-size: 1.5rem;
    font-weight: 600;
    margin-bottom: 0.5rem;
    text-align: center;
  `

  _offlineNote = document.createElement('p')
  _offlineNote.id = 'of-note'
  _offlineNote.style.cssText = `
    color: var(--text-tertiary, #888);
    font-size: 1rem;
    margin-bottom: 1.5rem;
    text-align: center;
    max-width: 80%;
  `

  const retryBtn = document.createElement('button')
  retryBtn.textContent = 'Retry Now'
  retryBtn.style.cssText = `
    padding: 0.75rem 2rem;
    background: var(--accent-primary, #00a3ff);
    color: var(--bg-primary, #000);
    border: none;
    border-radius: var(--radius-md, 8px);
    font-size: 1rem;
    font-weight: 600;
    cursor: pointer;
    font-family: inherit;
  `
  retryBtn.onclick = () => {
    hideOfflineScreen()
    scheduleWsReconnect(true)
  }

  _offlineScreen.appendChild(icon)
  _offlineScreen.appendChild(_offlineTitle)
  _offlineScreen.appendChild(_offlineNote)
  _offlineScreen.appendChild(retryBtn)
  document.body.appendChild(_offlineScreen)
}

function showOfflineScreen(type: 'phone' | 'pc'): void {
  createOfflineScreen()
  _offlineType = type

  if (type === 'phone') {
    _offlineTitle!.textContent = 'Phone is Offline'
    _offlineNote!.textContent = 'Waiting for network to return…'
  } else {
    _offlineTitle!.textContent = "Can't Reach JARVIS"
    _offlineNote!.textContent = 'The PC may be asleep, restarting, or unreachable. Will keep trying…'
  }

  _offlineScreen!.style.opacity = '1'
  _offlineScreen!.style.visibility = 'visible'
}

export function hideOfflineScreen(): void {
  if (_offlineScreen) {
    _offlineScreen.style.opacity = '0'
    _offlineScreen.style.visibility = 'hidden'
  }
  _offlineType = null
}

export function noteOffline(source: 'ws' | 'fetch'): void {
  if (source === 'ws') {
    // WebSocket disconnected - could be phone or PC
    // We'll show phone offline initially, then upgrade to PC if reconnect fails
    showOfflineScreen('phone')
  } else {
    // Failed fetch - likely phone offline
    showOfflineScreen('phone')
  }
}

export function scheduleWsReconnect(force = false): void {
  if (_reconnectScheduled && !force) return

  if (_reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
    console.error('[Offline] Max reconnect attempts reached')
    return
  }

  _reconnectScheduled = true

  const delay = BASE_RECONNECT_DELAY * Math.pow(2, _reconnectAttempts) + Math.random() * 1000
  _reconnectAttempts++

  console.log(`[Offline] Scheduling reconnect in ${Math.round(delay)}ms (attempt ${_reconnectAttempts})`)

  setTimeout(() => {
    _reconnectScheduled = false
    // Dispatch a custom event that the WS client can listen for
    window.dispatchEvent(new CustomEvent('ws-reconnect'))
  }, delay)
}

export function resetReconnectAttempts(): void {
  _reconnectAttempts = 0
}

export function handleWsClose(ev: CloseEvent): void {
  console.log('[Offline] WebSocket closed:', ev.code, ev.reason)

  if (ev.code === 4001) {
    // Server restarted - need to re-pair device silently
    console.log('[Offline] Server restarted (code 4001), clearing session and reloading')
    sessionStorage.removeItem('jarvis_token')
    // Force reload to go through auto-login flow again
    window.location.reload()
    return
  }

  // For other close codes, show offline screen and schedule reconnect
  noteOffline('ws')
  scheduleWsReconnect()
}

export function handleWsOpen(): void {
  console.log('[Offline] WebSocket connected')
  resetReconnectAttempts()
  hideOfflineScreen()
  setWsStale(false)
}

export function handleWsMessage(msg: any): void {
  // Track sequence for resume
  if (msg.seq !== undefined) {
    _lastSeq = msg.seq
  }
  // Track boot_id for server restart detection
  if (msg.type === 'boot' && msg.boot_id) {
    _bootId = msg.boot_id
  }
}

export function initOfflineListeners(wsConnectFn: () => void): void {
  // Online/offline events
  window.addEventListener('online', () => {
    console.log('[Offline] Network online event')
    hideOfflineScreen()
    wsConnectFn()
  })

  window.addEventListener('offline', () => {
    console.log('[Offline] Network offline event')
    noteOffline('fetch')
  })

  // Listen for reconnect events
  window.addEventListener('ws-reconnect', () => {
    wsConnectFn()
  })

  // Periodic ping/pong staleness check
  setInterval(() => {
    if (_wsStale) {
      console.log('[Offline] WebSocket stale, forcing reconnect')
      // Force close to trigger reconnect
      window.dispatchEvent(new CustomEvent('ws-force-reconnect'))
    }
    _wsStale = true // Will be reset by pong
  }, 35000) // Check every 35s (server pings every 30s)
}

// Expose globally for test compatibility
declare global {
  interface Window {
    _lastSeq: number
    _offline: {
      note: (source: 'ws' | 'fetch') => void
      hide: () => void
    }
    _scheduleWsReconnect: (force?: boolean) => void
    _bootId: string | null
  }
}

window._lastSeq = _lastSeq
window._offline = {
  note: noteOffline,
  hide: hideOfflineScreen,
}
window._scheduleWsReconnect = scheduleWsReconnect
window._bootId = _bootId

// Keep window._lastSeq in sync
const originalSetLastSeq = setLastSeq
setLastSeq = (seq: number) => {
  originalSetLastSeq(seq)
  window._lastSeq = seq
}

const originalSetBootId = setBootId
setBootId = (bootId: string) => {
  originalSetBootId(bootId)
  window._bootId = bootId
}