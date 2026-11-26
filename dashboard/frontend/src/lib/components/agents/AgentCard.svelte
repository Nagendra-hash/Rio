<script lang="ts">
  import { agents } from '$lib/stores/agents'
  import { api } from '$lib/api/rest'
  import { toast } from '$lib/stores/toast'
  import type { AgentStatus, AgentResult } from '$lib/stores/agents'

  export let agent: AgentStatus
  export let compact = false

  let expanded = false
  let toggling = false

  async function toggleGate() {
    toggling = true
    try {
      await api.toggleAgentGate(agent.name, !agent.enabled)
      agents.upsert(agent.name, { enabled: !agent.enabled })
      toast.success(`${agent.name} ${!agent.enabled ? 'enabled' : 'disabled'}`)
    } catch (e) {
      toast.error(`Failed to toggle ${agent.name}`, e instanceof Error ? e.message : 'Unknown error')
    } finally {
      toggling = false
    }
  }

  function getStatusColor(status: AgentStatus['status']) {
    switch (status) {
      case 'running': return 'var(--status-warning)'
      case 'error': return 'var(--status-error)'
      case 'idle': return 'var(--status-success)'
      default: return 'var(--text-tertiary)'
    }
  }
</script>

<div class="agent-card" class:compact>
  <div class="card-header">
    <div class="agent-info">
      <h3>{agent.name}</h3>
      <span class="status-badge" style="background: {getStatusColor(agent.status)}">
        {agent.status}
      </span>
    </div>
    {#if !compact}
      <label class="toggle" title={agent.enabled ? 'Disable agent' : 'Enable agent'}>
        <input type="checkbox" bind:checked={agent.enabled} onchange={toggleGate} disabled={toggling} />
        <span class="toggle-slider"></span>
      </label>
    {/if}
  </div>

  {#if !compact && agent.description}
    <p class="description">{agent.description}</p>
  {/if}

  <div class="task-types">
    {#each agent.taskTypes as type}
      <span class="tag">{type}</span>
    {/each}
  </div>

  {#if !compact && agent.recentResults.length > 0}
    <div class="results-section">
      <button class="expand-btn" onclick={() => expanded = !expanded}>
        <span>Recent Results</span>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class:rotated={expanded}>
          <polyline points="6 9 12 15 18 9"/>
        </svg>
      </button>
      {#if expanded}
        <div class="results-list">
          {#each agent.recentResults as result}
            <div class="result-item" class:completed={result.status.toLowerCase() === 'completed'} class:failed={result.status.toLowerCase() === 'failed'} class:needs_input={result.status.toLowerCase() === 'needs_input'} class:needs_approval={result.status.toLowerCase() === 'needs_approval'} class:denied={result.status.toLowerCase() === 'denied'}>
              <span class="result-status">{result.status}</span>
              <span class="result-text">{result.text.slice(0, 80)}{result.text.length > 80 ? '…' : ''}</span>
              <span class="result-meta">
                {result.latencyMs ? `${result.latencyMs}ms` : ''}
                {result.tokensUsed ? ` · ${result.tokensUsed} tok` : ''}
              </span>
            </div>
          {/each}
        </div>
      {/if}
    </div>
  {/if}
</div>

<style>
  .agent-card {
    background: var(--bg-tertiary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-md);
    padding: var(--space-4);
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    transition: all 0.2s ease;
  }

  .agent-card.compact {
    padding: var(--space-3);
    gap: var(--space-2);
  }

  .agent-card:hover {
    border-color: var(--accent-primary);
  }

  .card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
  }

  .agent-info {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    min-width: 0;
  }

  .agent-info h3 {
    font-size: var(--text-base);
    font-weight: 600;
    color: var(--text-primary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .agent-card.compact .agent-info h3 {
    font-size: var(--text-sm);
  }

  .status-badge {
    font-size: var(--text-xs);
    font-weight: 500;
    padding: var(--space-1) var(--space-2);
    border-radius: var(--radius-full);
    color: var(--bg-primary);
    white-space: nowrap;
  }

  .description {
    font-size: var(--text-sm);
    color: var(--text-secondary);
    margin: 0;
    line-height: 1.5;
  }

  .task-types {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1);
  }

  .tag {
    font-size: var(--text-xs);
    padding: var(--space-1) var(--space-2);
    background: var(--bg-secondary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-full);
    color: var(--text-tertiary);
  }

  .results-section {
    border-top: 1px solid var(--border-primary);
    padding-top: var(--space-3);
  }

  .expand-btn {
    display: flex;
    align-items: center;
    justify-content: space-between;
    width: 100%;
    padding: var(--space-2) 0;
    background: none;
    border: none;
    color: var(--text-secondary);
    font-size: var(--text-sm);
    font-weight: 500;
    cursor: pointer;
  }

  .expand-btn svg {
    width: 16px;
    height: 16px;
    transition: transform 0.2s ease;
  }

  .expand-btn svg.rotated {
    transform: rotate(180deg);
  }

  .results-list {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    margin-top: var(--space-2);
    padding-top: var(--space-2);
    border-top: 1px solid var(--border-primary);
  }

  .result-item {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    padding: var(--space-2);
    background: var(--bg-secondary);
    border-radius: var(--radius-sm);
    font-size: var(--text-xs);
  }

  .result-item.completed { border-left: 3px solid var(--status-success); }
  .result-item.failed { border-left: 3px solid var(--status-error); }
  .result-item.needs_input { border-left: 3px solid var(--status-warning); }
  .result-item.needs_approval { border-left: 3px solid var(--accent-primary); }
  .result-item.denied { border-left: 3px solid var(--text-tertiary); }

  .result-status {
    font-weight: 600;
    text-transform: capitalize;
  }

  .result-text {
    color: var(--text-secondary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .result-meta {
    color: var(--text-tertiary);
  }

  .toggle {
    position: relative;
    width: 44px;
    height: 24px;
  }

  .toggle input {
    opacity: 0;
    width: 0;
    height: 0;
  }

  .toggle-slider {
    position: absolute;
    cursor: pointer;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: var(--border-primary);
    border-radius: 24px;
    transition: 0.2s;
  }

  .toggle-slider:before {
    position: absolute;
    content: "";
    height: 18px;
    width: 18px;
    left: 3px;
    bottom: 3px;
    background: var(--text-tertiary);
    border-radius: 50%;
    transition: 0.2s;
  }

  .toggle input:checked + .toggle-slider {
    background: var(--accent-primary);
  }

  .toggle input:checked + .toggle-slider:before {
    transform: translateX(20px);
    background: var(--bg-primary);
  }

  .toggle input:disabled + .toggle-slider {
    opacity: 0.5;
    cursor: not-allowed;
  }
</style>
