const API_BASE = '/api'

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...options.headers },
    ...options,
  })

  if (!res.ok) {
    const text = await res.text()
    throw new Error(`API ${res.status}: ${text}`)
  }

  if (res.status === 204) return undefined as T
  return res.json()
}

export const api = {
  // Config
  getConfig: () => request<AppConfig>('/admin/config'),
  saveConfig: (partial: Partial<AppConfig>) => request<AppConfig>('/admin/config', { method: 'PUT', body: JSON.stringify(partial) }),

  // Agent gates
  toggleAgentGate: (name: string, enabled: boolean) =>
    request<{ ok: boolean }>(`/admin/agents/${name}/gate`, { method: 'POST', body: JSON.stringify({ enabled }) }),

  // Journal / Conversation replay
  getJournal: (params?: { limit?: number; before?: string; agent?: string; status?: string }) => {
    const search = new URLSearchParams()
    if (params?.limit) search.set('limit', String(params.limit))
    if (params?.before) search.set('before', params.before)
    if (params?.agent) search.set('agent', params.agent)
    if (params?.status) search.set('status', params.status)
    return request<JournalEntry[]>(`/journal?${search}`)
  },
  getTrace: (traceId: string) => request<TraceData>(`/trace/${traceId}`),
  replayTurn: (turnId: string) => request<{ ok: boolean }>(`/orchestrator/replay`, { method: 'POST', body: JSON.stringify({ turn_id: turnId, replay: true }) }),

  // Backtest
  getBacktestJobs: () => request<BacktestJob[]>('/backtest/jobs'),
  getBacktestReport: (jobId: string) => request<string>(`/backtest/report/${jobId}`),

  // Trading
  getTradingState: () => request<TradingState>('/trading/state'),
  getTradingPositions: () => request<Position[]>('/trading/positions'),

  // Voice Commands
  getVoiceStats: (days?: number) => request<VoiceStats>(`/voice/stats${days ? `?days=${days}` : ''}`),
  getVoiceHistory: (params?: { days?: number; limit?: number; offset?: number }) => {
    const search = new URLSearchParams()
    if (params?.days) search.set('days', String(params.days))
    if (params?.limit) search.set('limit', String(params.limit))
    if (params?.offset) search.set('offset', String(params.offset))
    return request<VoiceHistoryResponse>(`/voice/history?${search}`)
  },
  getVoiceSessionHistory: (sessionId: string, days?: number) =>
    request<VoiceSessionHistory>(`/voice/history/${sessionId}${days ? `?days=${days}` : ''}`),
  getVoiceTrends: (days?: number, topN?: number) =>
    request<VoiceTrendsResponse>(`/voice/trends${days || topN ? `?${new URLSearchParams({ days: String(days || 30), top_n: String(topN || 5) })}` : ''}`),
  getVoiceHourly: (days?: number) => request<VoiceHourlyResponse>(`/voice/hourly${days ? `?days=${days}` : ''}`),
  getVoiceSessions: (days?: number, limit?: number) =>
    request<VoiceSessionsResponse>(`/voice/sessions${days || limit ? `?${new URLSearchParams({ days: String(days || 30), limit: String(limit || 50) })}` : ''}`),
  executeVoiceCommand: (command: string) => request<VoiceCommandResponse>('/voice/command', { method: 'POST', body: JSON.stringify({ command }) }),

  // Wake Word
  getWakeWordModels: () => request<WakeWordModelsResponse>('/voice/wake-word/models'),
  trainWakeWord: (data: WakeWordTrainRequest) => request<WakeWordTrainResponse>('/voice/wake-word/train', { method: 'POST', body: JSON.stringify(data) }),
  deleteWakeWord: (name: string) => request<{ ok: boolean; message: string }>(`/voice/wake-word/${name}`, { method: 'DELETE' }),
}

export interface AppConfig {
  env: 'development' | 'staging' | 'production'
  logLevel: 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR'
  llm: {
    routerMode: 'smart' | 'free' | 'local' | 'gemini'
    geminiApiKeys: string
    nvidiaEnabled: boolean
    nvidiaApiKey: string
    openrouterApiKeys: string
    ollamaEnabled: boolean
    ollamaModels: string
  }
  ui: {
    assistantName: string
    userName: string
    voice: string
    wakeWordEnabled: boolean
    pushToTalkEnabled: boolean
    thinkingEnabled: boolean
    webglHud: boolean
  }
  trading: {
    paperEnabled: boolean
    deltaTestnet: boolean
  }
  agentGates: Record<string, boolean>
}

export interface JournalEntry {
  turnId: string
  timestamp: string
  userText: string
  agent: string
  status: 'COMPLETED' | 'FAILED' | 'NEEDS_INPUT' | 'NEEDS_APPROVAL' | 'DENIED'
  latencyMs: number
  tokensUsed: number
  traceId?: string
}

export interface TraceData {
  traceId: string
  spans: TraceSpan[]
}

export interface TraceSpan {
  spanId: string
  parentSpanId: string | null
  name: string
  startTime: number
  endTime: number
  attributes: Record<string, any>
  events: TraceEvent[]
}

export interface TraceEvent {
  time: number
  name: string
  attributes: Record<string, any>
}

export interface BacktestJob {
  jobId: string
  symbol: string
  market: string
  status: string
  progress: number
  phase: string
  startedAt: string
  completedAt?: string
  result?: any
}

export interface TradingState {
  balance: number
  equity: number
  pnl: number
  positions: number
}

export interface Position {
  symbol: string
  side: 'long' | 'short'
  size: number
  entryPrice: number
  markPrice: number
  pnl: number
}

// Voice API Types
export interface VoiceStats {
  ok: boolean
  total_commands: number
  successful_commands: number
  success_rate: number
  avg_confidence: number
  avg_latency_ms: number
  unique_intents: number
  unique_sessions: number
  top_intents: Array<{ intent: string; count: number }>
  top_actions: Array<{ action: string; count: number }>
}

export interface VoiceEvent {
  timestamp: string
  session_id: string
  transcript: string
  intent: string
  action: string | null
  success: boolean
  confidence: number
  latency_ms: number
  user_id: string | null
  error: string | null
}

export interface VoiceHistoryResponse {
  ok: boolean
  history: VoiceEvent[]
  total: number
  limit: number
  offset: number
}

export interface VoiceSessionHistory {
  ok: boolean
  session_id: string
  summary: VoiceSessionSummary | null
  history: Array<VoiceEvent & { entities: Record<string, any>; params: Record<string, any> }>
}

export interface VoiceSessionSummary {
  session_id: string
  first_seen: string
  last_seen: string
  command_count: number
  success_count: number
  success_rate: number
  unique_intents: string[]
  user_id: string | null
}

export interface VoiceTrendsResponse {
  ok: boolean
  trends: Array<{
    intent: string
    data: Array<{ date: string; count: number }>
  }>
}

export interface VoiceHourlyResponse {
  ok: boolean
  hourly: Array<{ hour: number; count: number }>
}

export interface VoiceSessionsResponse {
  ok: boolean
  sessions: Array<{
    session_id: string
    first_seen: string
    last_seen: string
    command_count: number
    success_count: number
    intents: string[]
    user_id: string | null
    success_rate: number
  }>
}

export interface VoiceCommandResponse {
  ok: boolean
  intent: string
  action: string
  params: Record<string, any>
  confidence: number
  result?: any
  error?: string
}

export interface WakeWordModelsResponse {
  ok: boolean
  models: Array<{
    name: string
    phrases: string[]
    threshold: number
    sensitivity: number
    sample_count: number
    created_at: number
    updated_at: number
  }>
}

export interface WakeWordTrainRequest {
  name: string
  phrases: string[]
  threshold?: number
  sensitivity?: number
  audio_samples?: number[][]
}

export interface WakeWordTrainResponse {
  ok: boolean
  model: {
    name: string
    phrases: string[]
    threshold: number
    sensitivity: number
    sample_count: number
  }
}

// Usage & Budget API Types
export interface UsageSummary {
  [provider: string]: {
    total_tokens: number
    total_requests: number
    total_cost_usd: number
    avg_latency_ms: number
  }
}

export interface UsageDetail {
  summary: UsageSummary
  data: UsageRecord[]
}

export interface UsageRecord {
  timestamp: string
  provider: string
  model: string
  task: string
  ok: boolean
  total_tokens: number
  prompt_tokens: number
  completion_tokens: number
  latency_s: number
  cost_usd: number
}

export interface BudgetConfig {
  version: number
  daily_limits: Record<string, number>
  monthly_limits: Record<string, number>
  per_request_cap: number
  alert_thresholds: number[]
  hard_block_at: number
  exclude_local: boolean
  token_cost_usd_per_1k: Record<string, number>
}

export interface ProviderBudgetStatus {
  provider: string
  daily: { used: number; limit: number; remaining: number; percent: number }
  monthly: { used: number; limit: number; remaining: number; percent: number }
  is_local: boolean
  alerts?: string[]
}

export interface BudgetStatus {
  [provider: string]: ProviderBudgetStatus
  config: BudgetConfig
}

export interface RouterStatus {
  mode: string
  providers: Record<string, any>
  models: Array<{
    name: string
    model_id: string
    provider: string
    cost_tier: number
    healthy: boolean
  }>
  budget: Record<string, any>
}

export interface UsageBudgetResponse {
  ok: boolean
  providers: Record<string, ProviderBudgetStatus>
  config: BudgetConfig
}

// Add usage endpoints to api
export const usageApi = {
  getSummary: () => request<UsageSummary>('/usage/summary'),
  getDetail: (params?: { provider?: string; days?: number }) => {
    const search = new URLSearchParams()
    if (params?.provider) search.set('provider', params.provider)
    if (params?.days) search.set('days', String(params.days))
    return request<UsageDetail>(`/usage/detail?${search}`)
  },
  getBudget: () => request<UsageBudgetResponse>('/usage/budget'),
  updateBudget: (config: Partial<BudgetConfig>) =>
    request<UsageBudgetResponse>('/usage/budget', { method: 'POST', body: JSON.stringify(config) }),
  getRouterStatus: () => request<RouterStatus>('/usage/router-status'),
}

// ── Model Router / FreeLLMAPI management APIs ────────────────────────────────

export interface ProviderHealthInfo {
  configured: boolean
  healthy: boolean
  reason: string
}

export interface FreeLLMAPIGatewayStatus {
  connected: boolean
  gateway_url: string
  available_models: number
  last_error: string
  auth_configured: boolean
  probe_ok: boolean
  probe_reason: string
}

export interface ModelProviderList {
  [provider: string]: ProviderHealthInfo
}

export const modelRouterApi = {
  getProviders: () => request<ModelProviderList>('/model_router/providers'),
  getFreeLLMAPIHealth: () => request<FreeLLMAPIGatewayStatus>('/model_router/freellmapi_health'),
  testConnection: (provider: string) =>
    request<{ ok: boolean; reason: string }>(
      '/model_router/test_connection',
      { method: 'POST', body: JSON.stringify({ provider }) }
    ),
  getTaskPrefs: () => request<Record<string, string>>('/model_router/task_prefs'),
  setTaskPref: (task: string, provider: string) =>
    request<{ ok: boolean }>('/model_router/set_task_pref', {
      method: 'POST',
      body: JSON.stringify({ task, provider }),
    }),
}