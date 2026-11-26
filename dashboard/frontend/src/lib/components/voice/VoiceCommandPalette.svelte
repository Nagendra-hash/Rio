<script lang="ts">
  import { onMount } from 'svelte'
  import { api } from '$lib/api/rest'

  // Component state
  let command = ''
  let isExecuting = false
  let lastResult: any = null
  let lastError: string | null = null
  let history: string[] = []
  let historyIndex = -1
  let suggestions: string[] = []
  let showSuggestions = false
  let selectedSuggestion = -1

  // Example commands for suggestions
  const exampleCommands = [
    'play jazz music',
    'pause music',
    'turn up the volume',
    'set volume to 50',
    'what time is it',
    'what\'s the weather',
    'set timer for 5 minutes',
    'good morning',
    'schedule play jazz every day at 9am',
    'list schedules',
    'search for python tutorials',
    'open calculator',
    'turn on wifi',
    'brightness up',
    'remind me to call mom at 5pm',
    'what\'s on my calendar today',
    'next song',
    'previous track',
    'shutdown',
    'go to sleep',
  ]

  // Filter suggestions based on input
  $: filteredSuggestions = exampleCommands.filter(cmd =>
    cmd.toLowerCase().includes(command.toLowerCase())
  ).slice(0, 8)

  // Handle keyboard navigation
  function handleKeydown(event: KeyboardEvent) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      executeCommand()
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      if (showSuggestions && filteredSuggestions.length > 0) {
        selectedSuggestion = Math.max(0, selectedSuggestion - 1)
      } else if (history.length > 0 && historyIndex < history.length - 1) {
        historyIndex++
        command = history[historyIndex]
      }
    } else if (event.key === 'ArrowDown') {
      event.preventDefault()
      if (showSuggestions && filteredSuggestions.length > 0) {
        selectedSuggestion = Math.min(filteredSuggestions.length - 1, selectedSuggestion + 1)
      } else if (historyIndex > 0) {
        historyIndex--
        command = history[historyIndex]
      } else if (historyIndex === 0) {
        historyIndex = -1
        command = ''
      }
    } else if (event.key === 'Tab' && filteredSuggestions.length > 0) {
      event.preventDefault()
      if (selectedSuggestion >= 0) {
        command = filteredSuggestions[selectedSuggestion]
      } else {
        command = filteredSuggestions[0]
      }
      showSuggestions = false
      selectedSuggestion = -1
    } else if (event.key === 'Escape') {
      showSuggestions = false
      selectedSuggestion = -1
    }
  }

  function handleInput() {
    showSuggestions = command.length > 0
    selectedSuggestion = -1
  }

  function handleFocus() {
    if (command.length > 0) {
      showSuggestions = true
    }
  }

  function handleBlur() {
    // Delay to allow click on suggestion
    setTimeout(() => { showSuggestions = false }, 200)
  }

  function selectSuggestion(cmd: string) {
    command = cmd
    showSuggestions = false
    selectedSuggestion = -1
  }

  async function executeCommand() {
    if (!command.trim() || isExecuting) return

    const cmd = command.trim()
    isExecuting = true
    lastError = null
    lastResult = null

    try {
      const result = await api.executeVoiceCommand(cmd)
      lastResult = result

      // Add to history
      if (!history.includes(cmd)) {
        history.unshift(cmd)
        if (history.length > 50) history.pop()
      }
      historyIndex = -1
      command = ''
    } catch (err: any) {
      lastError = err.message || 'Failed to execute command'
    } finally {
      isExecuting = false
    }
  }

  function clearResult() {
    lastResult = null
    lastError = null
  }

  function formatResult(result: any): string {
    if (!result) return ''
    try {
      return JSON.stringify(result, null, 2)
    } catch {
      return String(result)
    }
  }

  function getStatusColor(result: any): string {
    if (!result) return 'var(--text-tertiary)'
    if (result.ok) return 'var(--status-success)'
    return 'var(--status-error)'
  }
</script>

<div class="voice-palette">
  <div class="palette-header">
    <h2>Voice Command Palette</h2>
    <span class="subtitle">Type or speak a command to test voice recognition</span>
  </div>

  <div class="input-area">
    <div class="input-wrapper">
      <svg class="mic-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z" />
        <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
        <line x1="12" y1="19" x2="12" y2="22" />
      </svg>
      <input
        type="text"
        bind:value={command}
        placeholder="Type a voice command... (Enter to execute, ↑↓ for history)"
        on:keydown={handleKeydown}
        on:input={handleInput}
        on:focus={handleFocus}
        on:blur={handleBlur}
        disabled={isExecuting}
        autocomplete="off"
        spellcheck="false"
        aria-label="Voice command input"
      />
      {#if command}
        <button class="clear-btn" on:click={() => { command = ''; showSuggestions = false }} aria-label="Clear">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="18" y1="6" x2="6" y2="18" />
            <line x1="6" y1="6" x2="18" y2="18" />
          </svg>
        </button>
      {/if}
    </div>

    {#if showSuggestions && filteredSuggestions.length > 0}
      <ul class="suggestions" role="listbox">
        {#each filteredSuggestions as suggestion, i}
          <li
            class="suggestion-item"
            class:selected={i === selectedSuggestion}
            on:click={() => selectSuggestion(suggestion)}
            on:mouseenter={() => selectedSuggestion = i}
            role="option"
            aria-selected={i === selectedSuggestion}
          >
            <svg class="suggestion-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
              <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z" />
              <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
              <line x1="12" y1="19" x2="12" y2="22" />
            </svg>
            {suggestion}
          </li>
        {/each}
      </ul>
    {/if}

    <div class="action-buttons">
      <button
        class="btn btn-primary"
        on:click={executeCommand}
        disabled={!command.trim() || isExecuting}
      >
        {#if isExecuting}
          <span class="spinner" aria-hidden="true"></span>
          Executing...
        {:else}
          Execute Command
        {/if}
      </button>
      <button class="btn btn-secondary" on:click={clearResult} disabled={!lastResult && !lastError}>
        Clear Result
      </button>
    </div>
  </div>

  {#if lastError}
    <div class="result error" style="--status-color: var(--status-error)">
      <div class="result-header">
        <span class="status-dot" style="background: var(--status-error)"></span>
        <strong>Error</strong>
      </div>
      <pre class="result-content">{lastError}</pre>
    </div>
  {/if}

  {#if lastResult}
    <div class="result" style="--status-color: {getStatusColor(lastResult)}">
      <div class="result-header">
        <span class="status-dot" style="background: var(--status-color)"></span>
        <strong>{lastResult.ok ? 'Success' : 'Failed'}</strong>
        <span class="intent-badge">{lastResult.intent || 'unknown'}</span>
        {#if lastResult.action}
          <span class="action-badge">{lastResult.action}</span>
        {/if}
        {#if lastResult.confidence !== undefined}
          <span class="confidence-badge">{Math.round(lastResult.confidence * 100)}%</span>
        {/if}
      </div>
      <pre class="result-content">{formatResult(lastResult)}</pre>
    </div>
  {/if}

  {#if history.length > 0}
    <details class="history-panel">
      <summary>Command History ({history.length})</summary>
      <ul class="history-list">
        {#each history as cmd, i}
          <li on:click={() => { command = cmd; showSuggestions = false }}>
            <span class="history-cmd">{cmd}</span>
            <button class="history-use" on:click|stopPropagation={() => { command = cmd; showSuggestions = false }} title="Use this command">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z" />
                <line x1="4" y1="22" x2="4" y2="15" />
              </svg>
            </button>
          </li>
        {/each}
      </ul>
    </details>
  {/if}
</div>

<style>
  .voice-palette {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
    max-width: 800px;
  }

  .palette-header {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  .palette-header h2 {
    font-size: var(--text-xl);
    font-weight: 600;
    color: var(--text-primary);
    margin: 0;
  }

  .subtitle {
    font-size: var(--text-sm);
    color: var(--text-tertiary);
  }

  .input-area {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }

  .input-wrapper {
    position: relative;
    display: flex;
    align-items: center;
  }

  .mic-icon {
    position: absolute;
    left: var(--space-3);
    width: 20px;
    height: 20px;
    color: var(--text-tertiary);
    pointer-events: none;
  }

  .input-wrapper input {
    width: 100%;
    padding: var(--space-3) var(--space-10) var(--space-3) var(--space-10);
    background: var(--bg-tertiary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-md);
    color: var(--text-primary);
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: var(--text-base);
    transition: border-color 0.15s ease, box-shadow 0.15s ease;
  }

  .input-wrapper input:focus {
    outline: none;
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 3px var(--accent-primary-light);
  }

  .input-wrapper input::placeholder {
    color: var(--text-tertiary);
  }

  .input-wrapper input:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .clear-btn {
    position: absolute;
    right: var(--space-3);
    padding: var(--space-1);
    background: transparent;
    border: none;
    color: var(--text-tertiary);
    cursor: pointer;
    border-radius: var(--radius-sm);
    display: flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
  }

  .clear-btn:hover {
    background: var(--bg-secondary);
    color: var(--text-primary);
  }

  .clear-btn svg {
    width: 16px;
    height: 16px;
  }

  .suggestions {
    position: absolute;
    top: calc(100% + var(--space-2));
    left: 0;
    right: 0;
    background: var(--bg-secondary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-md);
    list-style: none;
    margin: 0;
    padding: var(--space-2);
    max-height: 240px;
    overflow-y: auto;
    z-index: 100;
    box-shadow: var(--shadow-lg);
  }

  .suggestion-item {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-2) var(--space-3);
    border-radius: var(--radius-sm);
    cursor: pointer;
    color: var(--text-secondary);
    font-size: var(--text-sm);
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    transition: background 0.1s ease;
  }

  .suggestion-item:hover,
  .suggestion-item.selected {
    background: var(--bg-tertiary);
    color: var(--text-primary);
  }

  .suggestion-icon {
    width: 16px;
    height: 16px;
    color: var(--accent-primary);
    flex-shrink: 0;
  }

  .action-buttons {
    display: flex;
    gap: var(--space-3);
  }

  .btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: var(--space-2);
    padding: var(--space-3) var(--space-5);
    border-radius: var(--radius-md);
    font-size: var(--text-sm);
    font-weight: 500;
    font-family: inherit;
    cursor: pointer;
    transition: all 0.15s ease;
    border: none;
  }

  .btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .btn-primary {
    background: var(--accent-primary);
    color: var(--bg-primary);
  }

  .btn-primary:hover:not(:disabled) {
    filter: brightness(1.1);
  }

  .btn-secondary {
    background: var(--bg-tertiary);
    color: var(--text-primary);
    border: 1px solid var(--border-primary);
  }

  .btn-secondary:hover:not(:disabled) {
    background: var(--bg-secondary);
  }

  .spinner {
    width: 14px;
    height: 14px;
    border: 2px solid transparent;
    border-top-color: currentColor;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }

  @keyframes spin {
    to { transform: rotate(360deg); }
  }

  .result {
    background: var(--bg-secondary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-md);
    overflow: hidden;
  }

  .result-header {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-3) var(--space-4);
    background: var(--bg-tertiary);
    border-bottom: 1px solid var(--border-primary);
    flex-wrap: wrap;
  }

  .status-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
  }

  .result-header strong {
    font-size: var(--text-sm);
    text-transform: capitalize;
  }

  .intent-badge,
  .action-badge,
  .confidence-badge {
    font-size: var(--text-xs);
    padding: var(--space-1) var(--space-2);
    border-radius: var(--radius-full);
    font-weight: 500;
  }

  .intent-badge {
    background: var(--accent-primary-light);
    color: var(--accent-primary);
  }

  .action-badge {
    background: var(--status-info-light);
    color: var(--status-info);
  }

  .confidence-badge {
    background: var(--status-success-light);
    color: var(--status-success);
  }

  .result-content {
    margin: 0;
    padding: var(--space-4);
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: var(--text-sm);
    line-height: 1.6;
    color: var(--text-primary);
    overflow-x: auto;
    max-height: 400px;
    overflow-y: auto;
  }

  .history-panel {
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-md);
    background: var(--bg-secondary);
  }

  .history-panel summary {
    padding: var(--space-3) var(--space-4);
    cursor: pointer;
    font-weight: 500;
    color: var(--text-secondary);
    user-select: none;
  }

  .history-panel summary:hover {
    color: var(--text-primary);
  }

  .history-list {
    list-style: none;
    margin: 0;
    padding: 0;
    max-height: 200px;
    overflow-y: auto;
  }

  .history-list li {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--space-2) var(--space-4);
    cursor: pointer;
    border-top: 1px solid var(--border-primary);
    transition: background 0.1s ease;
  }

  .history-list li:first-child {
    border-top: none;
  }

  .history-list li:hover {
    background: var(--bg-tertiary);
  }

  .history-cmd {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: var(--text-sm);
    color: var(--text-primary);
    flex: 1;
    text-align: left;
  }

  .history-use {
    padding: var(--space-1);
    background: transparent;
    border: none;
    color: var(--text-tertiary);
    cursor: pointer;
    border-radius: var(--radius-sm);
    display: flex;
    align-items: center;
    justify-content: center;
    opacity: 0;
    transition: opacity 0.15s ease, color 0.15s ease;
  }

  .history-list li:hover .history-use {
    opacity: 1;
  }

  .history-use:hover {
    color: var(--accent-primary);
    background: var(--accent-primary-light);
  }

  .history-use svg {
    width: 16px;
    height: 16px;
  }

  @media (max-width: 640px) {
    .action-buttons {
      flex-direction: column;
    }
    .btn {
      width: 100%;
    }
  }
</style>