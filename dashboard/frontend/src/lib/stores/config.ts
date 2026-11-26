import { writable, derived } from 'svelte/store'
import { api } from '../api/rest'

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

function createConfigStore() {
  const { subscribe, set, update } = writable<AppConfig | null>(null)
  const loading = writable(false)
  const error = writable<string | null>(null)

  return {
    subscribe,
    loading,
    error,
    async load() {
      loading.set(true)
      error.set(null)
      try {
        const config = await api.getConfig()
        set(config)
      } catch (e) {
        error.set(e instanceof Error ? e.message : 'Failed to load config')
      } finally {
        loading.set(false)
      }
    },
    async save(partial: Partial<AppConfig>) {
      loading.set(true)
      error.set(null)
      try {
        const config = await api.saveConfig(partial)
        set(config)
      } catch (e) {
        error.set(e instanceof Error ? e.message : 'Failed to save config')
        throw e
      } finally {
        loading.set(false)
      }
    },
    async toggleAgentGate(agentName: string, enabled: boolean) {
      const key = agentName.toUpperCase().replace('-', '_') + '_AGENT'
      await this.save({ agentGates: { [key]: enabled } } as Partial<AppConfig>)
    },
  }
}

export const config = createConfigStore()