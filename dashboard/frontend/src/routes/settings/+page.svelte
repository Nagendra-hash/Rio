<script lang="ts">
  import { config } from '$lib/stores/config'
  import { api, modelRouterApi } from '$lib/api/rest'
  import { toast } from '$lib/stores/toast'
  import type { AppConfig } from '$lib/api/rest'

  let saving = false
  let localConfig = $derived($config ? { ...$config } : null)

  // ── AI Model Management state ───────────────────────────────────────
  let modelProviders: Record<string, { configured: boolean; healthy: boolean; reason: string }> = {}
  let modelLoading = false
  let freellmapiHealth: any = null
  let freellmapiLoading = false
  let taskPrefs: Record<string, string> = {}
  let taskPrefsLoading = false
  let testResult: { provider: string; ok: boolean; reason: string } | null = null
  let testingProvider = ''
  let testLoading = false

  const VOICE_ENGINE_INFO = {
    provider: 'Google Gemini Live',
    stt: 'Gemini (Live API)',
    tts: 'Gemini (Live API)',
    status: 'Locked — permanent voice engine',
    model: 'gemini-3.1-flash-live-preview',
  }

  const TASK_OPTIONS = [
    { value: 'general_conversation', label: 'General Conversation' },
    { value: 'coding',              label: 'Coding & Debugging' },
    { value: 'research',            label: 'Research' },
    { value: 'trading_analysis',    label: 'Trading Analysis' },
    { value: 'backtesting',         label: 'Backtesting' },
    { value: 'rag',                 label: 'RAG / Document Analysis' },
    { value: 'summarization',       label: 'Summarization' },
    { value: 'vision',              label: 'Vision' },
    { value: 'complex_reasoning',   label: 'Complex Reasoning' },
  ] as const

  async function loadModelStatus() {
    modelLoading = true
    try {
      const [providers, prefs] = await Promise.all([
        modelRouterApi.getProviders(),
        modelRouterApi.getTaskPrefs(),
      ])
      modelProviders = providers
      taskPrefs = prefs
    } catch (e) {
      console.error('Failed to load model status', e)
    } finally {
      modelLoading = false
    }
  }

  async function loadFreellmapiHealth() {
    freellmapiLoading = true
    try {
      freellmapiHealth = await modelRouterApi.getFreeLLMAPIHealth()
    } catch (e) {
      console.error('Failed to load FreeLLMAPI health', e)
      freellmapiHealth = { connected: false, last_error: String(e) }
    } finally {
      freellmapiLoading = false
    }
  }

  async function refreshAll() {
    await Promise.all([loadModelStatus(), loadFreellmapiHealth()])
  }

  async function testConnection(provider: string) {
    testingProvider = provider
    testLoading = true
    testResult = null
    try {
      const res = await modelRouterApi.testConnection(provider)
      testResult = { provider, ok: res.ok, reason: res.reason }
      if (res.ok) {
        toast.success(`${provider}: connected`)
      } else {
        toast.error(`${provider}: ${res.reason}`)
      }
    } catch (e) {
      testResult = { provider, ok: false, reason: String(e) }
      toast.error(`Test failed: ${e}`)
    } finally {
      testLoading = false
    }
  }

  async function setTaskPref(task: string, provider: string) {
    try {
      await modelRouterApi.setTaskPref(task, provider)
      taskPrefs[task] = provider
      toast.success(`Saved preference for ${task}`)
    } catch (e) {
      toast.error(`Failed to save: ${e}`)
    }
  }

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

  $: _ = loadModelStatus()
  $: __ = loadFreellmapiHealth()
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

      <!-- ═══ AI MODEL MANAGEMENT ═══ -->
      <section class="settings-section full-width">
        <h2>AI Model Management</h2>

        <!-- A. Voice Engine (read-only) -->
        <div class="subsection">
          <h3>Voice Engine</h3>
          <div class="voice-info-grid">
            <div class="info-row"><span class="info-key">Provider:</span><span class="info-val">{VOICE_ENGINE_INFO.provider}</span></div>
            <div class="info-row"><span class="info-key">STT:</span><span class="info-val">{VOICE_ENGINE_INFO.stt}</span></div>
            <div class="info-row"><span class="info-key">TTS:</span><span class="info-val">{VOICE_ENGINE_INFO.tts}</span></div>
            <div class="info-row"><span class="info-key">Status:</span><span class="info-val voice-locked">{VOICE_ENGINE_INFO.status}</span></div>
            <div class="info-row"><span class="info-key">Model:</span><span class="info-val">{VOICE_ENGINE_INFO.model}</span></div>
          </div>
          <p class="voice-note">⚠ Gemini is locked as the permanent voice engine. Switching reasoning models does NOT affect voice.</p>
        </div>

        <!-- B. Provider Health -->
        <div class="subsection">
          <h3>Provider Health</h3>
          {#if modelLoading}
            <p class="muted">Loading…</p>
          {:else}
            <div class="provider-table">
              {#each Object.keys(modelProviders).sort() as pname}
                {@const p = modelProviders[pname]}
                <div class="provider-row" class:healthy={p.configured && p.healthy} class:not-configured={!p.configured}>
                  <span class="pname">{pname}</span>
                  <span class="status">{p.configured ? (p.healthy ? '✓ Healthy' : '✗ Unhealthy') : '○ Not configured'}</span>
                  <span class="reason">{p.reason || ''}</span>
                  <button class="btn btn-small" on:click={() => testConnection(pname)} disabled={testLoading && testingProvider === pname}>
                    {testLoading && testingProvider === pname ? 'Testing…' : 'Test'}
                  </button>
                </div>
              {/each}
            </div>
            {#if testResult}
              <div class="test-result" class:ok={testResult.ok} class:error={!testResult.ok}>
                {testResult.provider}: {testResult.ok ? 'OK' : 'FAIL — ' + testResult.reason}
              </div>
            {/if}
          {/if}
        </div>

        <!-- C. FreeLLMAPI Gateway -->
        <div class="subsection">
          <h3>FreeLLMAPI Gateway</h3>
          {#if freellmapiLoading}
            <p class="muted">Checking gateway…</p>
          {:else if freellmapiHealth}
            <div class="gateway-status">
              <div class="info-row">
                <span class="info-key">Gateway URL:</span>
                <span class="info-val">{freellmapiHealth.gateway_url || '(not configured)'}</span>
              </div>
              <div class="info-row">
                <span class="info-key">Status:</span>
                <span class="info-val gateway-ok" class:connected={freellmapiHealth.connected} class:disconnected={!freellmapiHealth.connected}>
                  {freellmapiHealth.connected ? '● Connected' : '● Disconnected'}
                </span>
              </div>
              <div class="info-row">
                <span class="info-key">Available Models:</span>
                <span class="info-val">{freellmapiHealth.available_models ?? '—'}</span>
              </div>
              <div class="info-row">
                <span class="info-key">Auth:</span>
                <span class="info-val">{freellmapiHealth.auth_configured ? 'Key configured' : 'No key'}</span>
              </div>
              {#if freellmapiHealth.last_error && !freellmapiHealth.connected}
                <div class="info-row">
                  <span class="info-key">Last Error:</span>
                  <span class="info-val error">{freellmapiHealth.last_error}</span>
                </div>
              {/if}
            </div>
          {/if}
        </div>

        <!-- D. Task-Based Configuration -->
        <div class="subsection">
          <h3>Task-Based Model Selection</h3>
          <p class="muted">Choose the preferred reasoning provider for each task. "auto" lets the router pick the cheapest capable model.</p>
          <div class="task-prefs-grid">
            {#each TASK_OPTIONS as opt}
              <div class="task-pref-row">
                <label class="task-label">{opt.label}</label>
                <select bind:value={taskPrefs[opt.value]} on:change={(e) => setTaskPref(opt.value, e.currentTarget.value)}>
                  <option value="">auto</option>
                  <option value="ollama">Ollama</option>
                  <option value="nvidia">NVIDIA NIM</option>
                  <option value="freellm">FreeLLMAPI</option>
                  <option value="openrouter">OpenRouter</option>
                  <option value="deepseek">DeepSeek</option>
                  <option value="gemini">Gemini</option>
                  <option value="local">Local GGUF</option>
                </select>
              </div>
            {/each}
          </div>
        </div>

        <!-- E. Action Buttons -->
        <div class="action-buttons">
          <button class="btn btn-secondary" on:click={refreshAll}>↻ Refresh Status</button>
          <button class="btn btn-primary" on:click={() => refreshAll()} style="--accent: #22c55e;">✓ Test All Providers</button>
        </div>
      </section>

      <!-- ═══ EXISTING SECTIONS (preserved) ═══ -->
      <section class="settings-section">
        <h2>Environment</h2>
        <div class="field">
          <label>Environment</label>
          <select bind:value={localConfig.env}>
            <option value="development">Development</option>
            <option value="staging">Staging</option>
            <option value="production">Production</option>
          </select>
        </div>
        <div class="field">
          <label>Log Level</label>
          <select bind:value={localConfig.logLevel}>
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
          <label>Router Mode</label>
          <select bind:value={localConfig.llm.routerMode}>
            <option value="smart">Smart (auto)</option>
            <option value="free">Free only</option>
            <option value="local">Local only</option>
            <option value="gemini">Gemini only</option>
          </select>
        </div>
        <div class="field">
          <label>Gemini API Keys (comma-separated)</label>
          <textarea bind:value={localConfig.llm.geminiApiKeys} rows="3" placeholder="key1,key2,..." />
        </div>
        <div class="field">
          <label>
            <input type="checkbox" bind:checked={localConfig.llm.nvidiaEnabled} />
            NVIDIA NIM Enabled
          </label>
        </div>
        <div class="field">
          <label>NVIDIA API Key</label>
          <input type="password" bind:value={localConfig.llm.nvidiaApiKey} placeholder="nvapi-..." />
        </div>
        <div class="field">
          <label>OpenRouter API Keys (comma-separated)</label>
          <textarea bind:value={localConfig.llm.openrouterApiKeys} rows="2" placeholder="sk-or-...,sk-or-..." />
        </div>
        <div class="field">
          <label>
            <input type="checkbox" bind:checked={localConfig.llm.ollamaEnabled} />
            Ollama Enabled
          </label>
        </div>
        <div class="field">
          <label>Ollama Models (comma-separated)</label>
          <input bind:value={localConfig.llm.ollamaModels} placeholder="qwen2.5:1.5b,llama3.2:3b" />
        </div>
      </section>

      <section class="settings-section">
        <h2>UI / HUD</h2>
        <div class="field">
          <label>Assistant Name</label>
          <input bind:value={localConfig.ui.assistantName} />
        </div>
        <div class="field">
          <label>User Name</label>
          <input bind:value={localConfig.ui.userName} />
        </div>
        <div class="field">
          <label>Voice</label>
          <select bind:value={localConfig.ui.voice}>
            <option value="Kore">Kore</option>
            <option value="Charon">Charon</option>
            <option value="Puck">Puck</option>
            <option value="Fenrir">Fenrir</option>
            <option value="Aoede">Aoede</option>
          </select>
        </div>
        <div class="field">
          <label>
            <input type="checkbox" bind:checked={localConfig.ui.wakeWordEnabled} />
            Wake Word Enabled
          </label>
        </div>
        <div class="field">
          <label>
            <input type="checkbox" bind:checked={localConfig.ui.pushToTalkEnabled} />
            Push-to-Talk Enabled
          </label>
        </div>
        <div class="field">
          <label>
            <input type="checkbox" bind:checked={localConfig.ui.thinkingEnabled} />
            Thinking Enabled
          </label>
        </div>
        <div class="field">
          <label>
            <input type="checkbox" bind:checked={localConfig.ui.webglHud} />
            WebGL HUD (experimental)
          </label>
        </div>
      </section>

      <section class="settings-section">
        <h2>Trading</h2>
        <div class="field">
          <label>
            <input type="checkbox" bind:checked={localConfig.trading.paperEnabled} />
            Paper Trading Enabled
          </label>
        </div>
        <div class="field">
          <label>
            <input type="checkbox" bind:checked={localConfig.trading.deltaTestnet} />
            Delta Exchange Testnet
          </label>
        </div>
      </section>

      <section class="settings-section">
        <h2>Agent Gates</h2>
        <div class="agent-gates">
          {#each Object.entries(localConfig.agentGates).sort() as [key, enabled]}
            <label class="gate-toggle">
              <input type="checkbox" bind:checked={localConfig.agentGates[key]} />
              <span>{key.replace('_AGENT', '').toLowerCase()}</span>
            </label>
          {/each}
        </div>
      </section>

      <div class="settings-actions">
        <button class="btn btn-secondary" on:click={reset}>Reset</button>
        <button class="btn btn-primary" on:click={save} disabled={saving}>
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

  .settings-section.full-width {
    grid-column: 1 / -1;
  }

  .settings-section h2 {
    font-size: var(--text-lg);
    font-weight: 600;
    margin-bottom: var(--space-4);
    padding-bottom: var(--space-3);
    border-bottom: 1px solid var(--border-primary);
  }

  .subsection {
    margin-bottom: var(--space-6);
    padding-bottom: var(--space-6);
    border-bottom: 1px dashed var(--border-primary);
  }

  .subsection:last-child {
    margin-bottom: 0;
    padding-bottom: 0;
    border-bottom: none;
  }

  .subsection h3 {
    font-size: var(--text-base);
    font-weight: 600;
    color: var(--text-secondary);
    margin-bottom: var(--space-3);
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

  /* Voice engine info */
  .voice-info-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
    gap: var(--space-2) var(--space-6);
  }

  .info-row {
    display: flex;
    align-items: baseline;
    gap: var(--space-2);
    font-size: var(--text-sm);
  }

  .info-key {
    font-weight: 600;
    color: var(--text-secondary);
    min-width: 100px;
  }

  .info-val {
    color: var(--text-primary);
  }

  .voice-locked {
    color: var(--accent-primary);
    font-weight: 600;
  }

  .voice-note {
    font-size: var(--text-xs);
    color: var(--text-tertiary);
    margin-top: var(--space-2);
    font-style: italic;
  }

  /* Provider table */
  .provider-table {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }

  .provider-row {
    display: flex;
    align-items: center;
    gap: var(--space-4);
    padding: var(--space-2) var(--space-3);
    background: var(--bg-tertiary);
    border-radius: var(--radius-md);
    font-size: var(--text-sm);
  }

  .provider-row.healthy { border-left: 3px solid #22c55e; }
  .provider-row.not-configured { opacity: 0.6; border-left: 3px solid var(--border-primary); }

  .pname { font-weight: 600; min-width: 90px; }
  .status { min-width: 120px; }
  .reason { flex: 1; color: var(--text-tertiary); font-size: var(--text-xs); }

  /* Gateway status */
  .gateway-status {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }

  .gateway-ok.connected { color: #22c55e; font-weight: 600; }
  .gateway-ok.disconnected { color: #ef4444; font-weight: 600; }
  .info-val.error { color: #ef4444; }

  /* Task prefs */
  .task-prefs-grid {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }

  .task-pref-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-4);
    padding: var(--space-2) var(--space-3);
    background: var(--bg-tertiary);
    border-radius: var(--radius-md);
  }

  .task-label {
    font-size: var(--text-sm);
    color: var(--text-secondary);
    min-width: 180px;
  }

  .task-pref-row select {
    padding: var(--space-1) var(--space-2);
    background: var(--bg-primary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    font-size: var(--text-sm);
  }

  /* Action buttons */
  .action-buttons {
    display: flex;
    gap: var(--space-3);
    margin-top: var(--space-4);
  }

  /* Test result */
  .test-result {
    margin-top: var(--space-3);
    padding: var(--space-2) var(--space-3);
    border-radius: var(--radius-md);
    font-size: var(--text-sm);
  }
  .test-result.ok { background: rgba(34,197,94,0.15); color: #22c55e; }
  .test-result.error { background: rgba(239,68,68,0.15); color: #ef4444; }

  .btn {
    padding: var(--space-2) var(--space-4);
    border-radius: var(--radius-md);
    font-weight: 500;
    border: none;
    cursor: pointer;
    font-size: var(--text-sm);
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

  .btn-small {
    padding: var(--space-1) var(--space-2);
    font-size: var(--text-xs);
    background: var(--bg-primary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-sm);
    color: var(--text-secondary);
    cursor: pointer;
  }

  .btn-small:hover { background: var(--border-primary); }
  .btn-small:disabled { opacity: 0.5; cursor: not-allowed; }

  .muted {
    font-size: var(--text-sm);
    color: var(--text-tertiary);
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

  .loading {
    padding: var(--space-8);
    text-align: center;
    color: var(--text-tertiary);
  }
</style>
