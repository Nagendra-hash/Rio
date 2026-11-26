<script lang="ts">
  import { config } from '$lib/stores/config'
  import { api } from '$lib/api/rest'
  import { toast } from '$lib/stores/toast'
  import type { AppConfig } from '$lib/api/rest'

  let saving = $state(false)
  let localConfig = $derived($config ? { ...$config } : null)

  function updateConfig(path: string, value: any) {
    if (!localConfig) return
    const keys = path.split('.')
    let obj: any = localConfig
    for (let i = 0; i < keys.length - 1; i++) {
      obj = obj[keys[i]]
    }
    obj[keys[keys.length - 1]] = value
    localConfig = { ...localConfig }
  }

  async function save() {
    if (!localConfig) return
    saving = true
    try {
      await api.saveConfig(localConfig)
      toast.success('Settings saved')
    } catch (e) {
      toast.error('Failed to save', e instanceof Error ? e.message : 'Unknown error')
    } finally {
      saving = false
    }
  }

  async function reset() {
    try {
      await config.load()
      localConfig = $config ? { ...$config } : null
      toast.success('Settings reset')
    } catch (e) {
      toast.error('Failed to reset', e instanceof Error ? e.message : 'Unknown error')
    }
  }
</script>

<div class="settings-page">
  <div class="page-header">
    <h1>Settings</h1>
    <p class="page-subtitle">Configure JARVIS behavior and integrations</p>
  </div>

  {#if $config.loading}
    <div class="loading">Loading settings...</div>
  {:else if localConfig}
    <div class="settings-grid">
      <section class="settings-section">
        <h2>Environment</h2>
        <div class="field">
          <label for="env">Environment</label>
          <select bind:value={localConfig.env} id="env">
            <option value="development">Development</option>
            <option value="staging">Staging</option>
            <option value="production">Production</option>
          </select>
        </div>
        <div class="field">
          <label for="logLevel">Log Level</label>
          <select bind:value={localConfig.logLevel} id="logLevel">
            <option value="DEBUG">DEBUG</option>
            <option value="INFO">INFO</option>
            <option value="WARNING">WARNING</option>
            <option value="ERROR">ERROR</option>
          </select>
        </div>
      </section>

      <section class="settings-section">
        <h2>LLM Configuration</h2>
        <div class="field">
          <label for="routerMode">Router Mode</label>
          <select bind:value={localConfig.llm.routerMode} id="routerMode">
            <option value="smart">Smart (auto)</option>
            <option value="free">Free only</option>
            <option value="local">Local only</option>
            <option value="gemini">Gemini only</option>
          </select>
        </div>
        <div class="field">
          <label for="geminiApiKeys">Gemini API Keys (comma-separated)</label>
          <textarea bind:value={localConfig.llm.geminiApiKeys} rows="3" placeholder="key1,key2,..." id="geminiApiKeys"></textarea>
        </div>
        <div class="field">
          <label>
            <input type="checkbox" bind:checked={localConfig.llm.nvidiaEnabled} id="nvidiaEnabled" />
            NVIDIA NIM Enabled
          </label>
        </div>
        <div class="field">
          <label for="nvidiaApiKey">NVIDIA API Key</label>
          <input type="password" bind:value={localConfig.llm.nvidiaApiKey} placeholder="nvapi-..." id="nvidiaApiKey" />
        </div>
        <div class="field">
          <label for="openrouterApiKeys">OpenRouter API Keys (comma-separated)</label>
          <textarea bind:value={localConfig.llm.openrouterApiKeys} rows="2" placeholder="sk-or-...,sk-or-..." id="openrouterApiKeys"></textarea>
        </div>
        <div class="field">
          <label>
            <input type="checkbox" bind:checked={localConfig.llm.ollamaEnabled} id="ollamaEnabled" />
            Ollama Enabled
          </label>
        </div>
        <div class="field">
          <label for="ollamaModels">Ollama Models (comma-separated)</label>
          <input bind:value={localConfig.llm.ollamaModels} placeholder="qwen2.5:1.5b,llama3.2:3b" id="ollamaModels" />
        </div>
      </section>

      <section class="settings-section">
        <h2>UI / HUD</h2>
        <div class="field">
          <label for="assistantName">Assistant Name</label>
          <input bind:value={localConfig.ui.assistantName} id="assistantName" />
        </div>
        <div class="field">
          <label for="userName">User Name</label>
          <input bind:value={localConfig.ui.userName} id="userName" />
        </div>
        <div class="field">
          <label for="voice">Voice</label>
          <select bind:value={localConfig.ui.voice} id="voice">
            <option value="Kore">Kore</option>
            <option value="Charon">Charon</option>
            <option value="Puck">Puck</option>
            <option value="Fenrir">Fenrir</option>
            <option value="Aoede">Aoede</option>
          </select>
        </div>
        <div class="field">
          <label>
            <input type="checkbox" bind:checked={localConfig.ui.wakeWordEnabled} id="wakeWordEnabled" />
            Wake Word Enabled
          </label>
        </div>
        <div class="field">
          <label>
            <input type="checkbox" bind:checked={localConfig.ui.pushToTalkEnabled} id="pushToTalkEnabled" />
            Push-to-Talk Enabled
          </label>
        </div>
        <div class="field">
          <label>
            <input type="checkbox" bind:checked={localConfig.ui.thinkingEnabled} id="thinkingEnabled" />
            Thinking Enabled
          </label>
        </div>
        <div class="field">
          <label>
            <input type="checkbox" bind:checked={localConfig.ui.webglHud} id="webglHud" />
            WebGL HUD (experimental)
          </label>
        </div>
      </section>

      <section class="settings-section">
        <h2>Trading</h2>
        <div class="field">
          <label>
            <input type="checkbox" bind:checked={localConfig.trading.paperEnabled} id="paperEnabled" />
            Paper Trading Enabled
          </label>
        </div>
        <div class="field">
          <label>
            <input type="checkbox" bind:checked={localConfig.trading.deltaTestnet} id="deltaTestnet" />
            Delta Exchange Testnet
          </label>
        </div>
      </section>

      <section class="settings-section">
        <h2>Agent Gates</h2>
        <div class="agent-gates">
          {#each Object.entries(localConfig.agentGates).sort() as [key, enabled]}
            <label class="gate-toggle">
              <input type="checkbox" bind:checked={localConfig.agentGates[key]} id={key} />
              <span>{key.replace('_AGENT', '').toLowerCase()}</span>
            </label>
          {/each}
        </div>
      </section>

      <div class="settings-actions">
        <button class="btn btn-secondary" onclick={reset}>Reset</button>
        <button class="btn btn-primary" onclick={save} disabled={saving}>
          {saving ? 'Saving...' : 'Save Changes'}
        </button>
      </div>
    </div>
  {/if}
</div>

<style>
  .settings-page {
    display: flex;
    flex-direction: column;
    gap: var(--space-6);
  }

  .page-header {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  .page-header h1 {
    font-size: var(--text-2xl);
    font-weight: 700;
    color: var(--text-primary);
  }

  .page-subtitle {
    color: var(--text-tertiary);
  }

  .settings-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
    gap: var(--space-6);
  }

  .settings-section {
    background: var(--bg-secondary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-lg);
    padding: var(--space-6);
  }

  .settings-section h2 {
    font-size: var(--text-lg);
    font-weight: 600;
    margin-bottom: var(--space-4);
    padding-bottom: var(--space-3);
    border-bottom: 1px solid var(--border-primary);
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    margin-bottom: var(--space-4);
  }

  .field label {
    font-size: var(--text-sm);
    font-weight: 500;
    color: var(--text-secondary);
  }

  .field input,
  .field select,
  .field textarea {
    padding: var(--space-2) var(--space-3);
    background: var(--bg-tertiary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-md);
    color: var(--text-primary);
    font-size: var(--text-sm);
    font-family: inherit;
  }

  .field input[type="checkbox"] {
    width: auto;
    accent-color: var(--accent-primary);
  }

  .field label:has(input[type="checkbox"]) {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    cursor: pointer;
  }

  .agent-gates {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: var(--space-2);
  }

  .gate-toggle {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-2);
    background: var(--bg-tertiary);
    border-radius: var(--radius-md);
    cursor: pointer;
    font-size: var(--text-sm);
  }

  .gate-toggle:hover {
    background: var(--border-primary);
  }

  .settings-actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--space-3);
    padding-top: var(--space-4);
    border-top: 1px solid var(--border-primary);
  }

  .btn {
    padding: var(--space-2) var(--space-4);
    border-radius: var(--radius-md);
    font-weight: 500;
    border: none;
    cursor: pointer;
  }

  .btn-primary {
    background: var(--accent-primary);
    color: var(--bg-primary);
  }

  .btn-secondary {
    background: var(--bg-tertiary);
    color: var(--text-primary);
    border: 1px solid var(--border-primary);
  }

  .loading {
    padding: var(--space-8);
    text-align: center;
    color: var(--text-tertiary);
  }
</style>