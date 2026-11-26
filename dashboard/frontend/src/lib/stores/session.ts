import { writable, derived } from 'svelte/store'

export interface SessionState {
  connected: boolean
  sessionId: string | null
  userId: string | null
  agent: string | null
}

function createSessionStore() {
  const { subscribe, set, update } = writable<SessionState>({
    connected: false,
    sessionId: null,
    userId: null,
    agent: null,
  })

  return {
    subscribe,
    connect: (sessionId: string, userId: string) => update(s => ({ ...s, connected: true, sessionId, userId })),
    disconnect: () => set({ connected: false, sessionId: null, userId: null, agent: null }),
    setAgent: (agent: string | null) => update(s => ({ ...s, agent })),
  }
}

export const session = createSessionStore()