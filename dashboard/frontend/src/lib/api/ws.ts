import { session } from '$lib/stores/session'
import { agents } from '$lib/stores/agents'
import { metrics } from '$lib/stores/metrics'
import { config } from '$lib/stores/config'
import {
  getLastSeq,
  setLastSeq,
  getBootId,
  setBootId,
  setWsStale,
  handleWsClose,
  handleWsOpen,
  handleWsMessage,
  initOfflineListeners,
} from '$lib/offline'

type MessageHandler = (data: any) => void

class WebSocketClient {
  private ws: WebSocket | null = null
  private url: string
  private reconnectAttempts = 0
  private maxReconnectAttempts = 10
  private reconnectDelay = 1000
  private handlers = new Map<string, Set<MessageHandler>>()
  private heartbeatInterval: ReturnType<typeof setInterval> | null = null
  private pongTimeout: ReturnType<typeof setTimeout> | null = null
  private _connectFn: (() => void) | null = null

  constructor(url: string = '/ws') {
    this.url = url
  }

  connect() {
    if (this.ws?.readyState === WebSocket.OPEN) return

    // Build WebSocket URL with since= parameter for resume
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const baseWsUrl = `${protocol}//${window.location.host}${this.url}`
    const lastSeq = getLastSeq()
    const wsUrl = lastSeq > 0 ? `${baseWsUrl}?since=${lastSeq}` : baseWsUrl

    this.ws = new WebSocket(wsUrl)

    this.ws.onopen = () => {
      console.log('[WS] Connected')
      this.reconnectAttempts = 0
      session.update(s => ({ ...s, connected: true }))
      this.startHeartbeat()
      this.send({ type: 'subscribe', channels: ['agents', 'metrics', 'config'] })
      handleWsOpen()
    }

    this.ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data)
        this.handleMessage(msg)
      } catch (e) {
        console.error('[WS] Failed to parse message:', e)
      }
    }

    this.ws.onclose = (ev) => {
      console.log('[WS] Disconnected:', ev.code, ev.reason)
      session.update(s => ({ ...s, connected: false }))
      this.stopHeartbeat()
      handleWsClose(ev)
    }

    this.ws.onerror = (err) => {
      console.error('[WS] Error:', err)
    }
  }

  private handleMessage(msg: any) {
    const { type, channel, payload } = msg

    // Track sequence and boot_id for resume/restart detection
    handleWsMessage(msg)

    // Handle pong for staleness detection
    if (type === 'pong') {
      setWsStale(false)
      return
    }

    // Route to channel handlers
    this.handlers.get(channel)?.forEach(h => h(payload))
    this.handlers.get('*')?.forEach(h => msg)

    // Built-in handlers
    switch (channel) {
      case 'session':
        if (payload.sessionId) session.update(s => ({ ...s, sessionId: payload.sessionId, userId: payload.userId }))
        break
      case 'agent_status':
        agents.upsert(payload.name, { status: payload.status, enabled: payload.enabled, lastRun: payload.lastRun })
        break
      case 'agent_result':
        agents.addResult(payload.name, payload.result)
        break
      case 'metrics':
        metrics.ingest(payload)
        break
      case 'system':
        metrics.ingestSystem(payload)
        break
      case 'config':
        config.load()
        break
    }
  }

  private startHeartbeat() {
    this.heartbeatInterval = setInterval(() => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.send({ type: 'ping' })
        // Set a timeout for pong response
        this.pongTimeout = setTimeout(() => {
          console.log('[WS] Pong timeout, marking stale')
          setWsStale(true)
        }, 10000) // 10s pong timeout
      }
    }, 30000)
  }

  private stopHeartbeat() {
    if (this.heartbeatInterval) {
      clearInterval(this.heartbeatInterval)
      this.heartbeatInterval = null
    }
    if (this.pongTimeout) {
      clearTimeout(this.pongTimeout)
      this.pongTimeout = null
    }
  }

  send(msg: any) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg))
    }
  }

  on(channel: string, handler: MessageHandler) {
    if (!this.handlers.has(channel)) this.handlers.set(channel, new Set())
    this.handlers.get(channel)!.add(handler)
    return () => this.off(channel, handler)
  }

  off(channel: string, handler: MessageHandler) {
    this.handlers.get(channel)?.delete(handler)
  }

  disconnect() {
    this.stopHeartbeat()
    this.ws?.close()
    this.ws = null
  }

  // Allow offline module to trigger reconnect
  setConnectFn(fn: () => void) {
    this._connectFn = fn
  }

  // Force reconnect (used by offline module on staleness)
  forceReconnect() {
    if (this.ws) {
      this.ws.close()
      this.ws = null
    }
    this.connect()
  }
}

export const ws = new WebSocketClient()

// Initialize offline listeners with the WS connect function
initOfflineListeners(() => ws.connect())

// Expose forceReconnect for the offline module's staleness check
window.addEventListener('ws-force-reconnect', () => {
  ws.forceReconnect()
})