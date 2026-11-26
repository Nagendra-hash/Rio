import { writable, derived } from 'svelte/store'

export interface AgentStatus {
  name: string
  description: string
  taskTypes: string[]
  status: 'idle' | 'running' | 'error' | 'unknown'
  enabled: boolean
  lastRun: string | null
  lastResult: AgentResult | null
  recentResults: AgentResult[]
}

export interface AgentResult {
  taskId: string
  status: 'COMPLETED' | 'FAILED' | 'NEEDS_INPUT' | 'NEEDS_APPROVAL' | 'DENIED'
  text: string
  timestamp: string
  latencyMs?: number
  tokensUsed?: number
}

function createAgentsStore() {
  const { subscribe, set, update } = writable<Record<string, AgentStatus>>({})

  const wsHandlers = new Map<string, (data: any) => void>()

  return {
    subscribe,
    setAll: (agents: Record<string, AgentStatus>) => set(agents),
    upsert: (name: string, data: Partial<AgentStatus>) =>
      update(agents => ({
        ...agents,
        [name]: { ...agents[name], ...data, name } as AgentStatus,
      })),
    setStatus: (name: string, status: AgentStatus['status']) =>
      update(agents => ({
        ...agents,
        [name]: agents[name] ? { ...agents[name], status } : { name, status, description: '', taskTypes: [], enabled: true, lastRun: null, lastResult: null, recentResults: [] },
      })),
    addResult: (name: string, result: AgentResult) =>
      update(agents => {
        const agent = agents[name]
        if (!agent) return agents
        const recent = [result, ...agent.recentResults].slice(0, 10)
        return { ...agents, [name]: { ...agent, recentResults: recent, lastResult: result, lastRun: result.timestamp, status: result.status === 'FAILED' ? 'error' : 'idle' } }
      }),
    toggleEnabled: (name: string, enabled: boolean) =>
      update(agents => ({
        ...agents,
        [name]: agent => agent ? { ...agent, enabled } : agent,
      })),
    registerWsHandler: (event: string, handler: (data: any) => void) => {
      wsHandlers.set(event, handler)
    },
    handleWsMessage: (event: string, data: any) => {
      wsHandlers.get(event)?.(data)
    },
  }
}

export const agents = createAgentsStore()

export const agentList = derived(agents, $agents => Object.values($agents))

export const enabledAgents = derived(agentList, $list => $list.filter(a => a.enabled))