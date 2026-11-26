<script lang="ts">
  import { onMount } from 'svelte'
  import { api } from '$lib/api/rest'

  // State
  let history: VoiceEvent[] = []
  let total = 0
  let loading = false
  let error: string | null = null
  let days = 30
  let limit = 50
  let offset = 0
  let selectedEvent: VoiceEvent | null = null

  // Filters
  let filterIntent = ''
  let filterSuccess: 'all' | 'success' | 'failed' = 'all'
  let filterSession = ''
  let searchQuery = ''

  // Filtered history
  $: filteredHistory = history.filter(event => {
    if (filterIntent && event.intent !== filterIntent) return false
    if (filterSuccess === 'success' && !event.success) return false
    if (filterSuccess === 'failed' && event.success) return false
    if (filterSession && event.session_id !== filterSession) return false
    if (searchQuery) {
      const query = searchQuery.toLowerCase()
      if (!event.transcript.toLowerCase().includes(query) &&
          !event.intent.toLowerCase().includes(query) &&
          !(event.action?.toLowerCase().includes(query))) {
        return false
      }
    }
    return true
  })

  // Unique intents for filter dropdown
  $: uniqueIntents = [...new Set(history.map(e => e.intent))].sort()

  // Unique sessions for filter dropdown
  $: uniqueSessions = [...new Set(history.map(e => e.session_id))].sort()

  async function loadHistory() {
    loading = true
    error = null
    try {
      const response = await api.getVoiceHistory({ days, limit, offset })
      history = response.history
      total = response.total
    } catch (err: any) {
      error = err.message || 'Failed to load history'
      history = []
      total = 0
    } finally {
      loading = false
    }
  }

  async function loadMore() {
    if (loading || history.length >= total) return
    offset += limit
    try {
      const response = await api.getVoiceHistory({ days, limit, offset })
      history = [...history, ...response.history]
    } catch (err: any) {
      error = err.message || 'Failed to load more'
    }
  }

  function handleSelectEvent(event: VoiceEvent) {
    selectedEvent = selectedEvent === event ? null : event
  }

  function formatTime(timestamp: string): string {
    const date = new Date(timestamp)
    return date.toLocaleString()
  }

  function getConfidenceColor(confidence: number): string {
    if (confidence >= 0.8) return 'var(--status-success)'
    if (confidence >= 0.5) return 'var(--status-warning)'
    return 'var(--status-error)'
  }

  function exportHistory() {
    const data = JSON.stringify(filteredHistory, null, 2)
    const blob = new Blob([data], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `voice-history-${new Date().toISOString().split('T')[0]}.json`
    a.click()
    URL.revokeObjectURL(url)
  }

  onMount(() => {
    loadHistory()
  })
</script>

<div class="voice-history">
  <div class="panel-header">
    <h2>Voice Command History</h2>
    <div class="header-actions">
      <button class="btn btn-secondary" on:click={exportHistory} disabled={filteredHistory.length === 0}>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
          <polyline points="7 10 12 15 17 10" />
          <line x1="12" y1="15" x2="12" y2="3" />
        </svg>
        Export
      </button>
      <button class="btn btn-secondary" on:click={loadHistory}>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <polyline points="23 4 23 10 17 10" />
          <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
        </svg>
        Refresh
      </button>
    </div>
  </div>

  <div class="filters">
    <div class="filter-row">
      <div class="filter-group">
        <label for="days-filter">Time Range</label>
        <select id="days-filter" bind:value={days} on:change={loadHistory}>
          <option value={1}>Last 24 hours</option>
          <option value={7}>Last 7 days</option>
          <option value={30}>Last 30 days</option>
          <option value={90}>Last 90 days</option>
        </select>
      </div>
      <div class="filter-group">
        <label for="limit-filter">Page Size</label>
        <select id="limit-filter" bind:value={limit} on:change={() => { offset = 0; loadHistory() }}>
          <option value={25}>25</option>
          <option value={50}>50</option>
          <option value={100}>100</option>
        </select>
      </div>
    </div>

    <div class="filter-row">
      <div class="filter-group">
        <label for="intent-filter">Intent</label>
        <select id="intent-filter" bind:value={filterIntent}>
          <option value="">All Intents</option>
          {#each uniqueIntents as intent}
            <option value={intent}>{intent}</option>
          {/each}
        </select>
      </div>
      <div class="filter-group">
        <label for="success-filter">Status</label>
        <select id="success-filter" bind:value={filterSuccess}>
          <option value="all">All</option>
          <option value="success">Success</option>
          <option value="failed">Failed</option>
        </select>
      </div>
      <div class="filter-group">
        <label for="session-filter">Session</label>
        <select id="session-filter" bind:value={filterSession}>
          <option value="">All Sessions</option>
          {#each uniqueSessions as session}
            <option value={session}>{session.slice(0, 20)}...</option>
          {/each}
        </select>
      </div>
      <div class="filter-group search-group">
        <label for="search-filter">Search</label>
        <div class="search-input-wrapper">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <circle cx="11" cy="11" r="8" />
            <line x1="21" y1="21" x2="16.65" y2="16.65" />
          </svg>
          <input
            id="search-filter"
            type="text"
            bind:value={searchQuery}
            placeholder="Search transcript, intent, action..."
          />
        </div>
      </div>
    </div>

    {#if filterIntent || filterSuccess !== 'all' || filterSession || searchQuery}
      <div class="active-filters">
        <span>Active filters:</span>
        {#if filterIntent}
          <span class="filter-tag" on:click={() => filterIntent = ''}>
            Intent: {filterIntent}
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </span>
        {/if}
        {#if filterSuccess !== 'all'}
          <span class="filter-tag" on:click={() => filterSuccess = 'all'}>
            Status: {filterSuccess}
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </span>
        {/if}
        {#if filterSession}
          <span class="filter-tag" on:click={() => filterSession = ''}>
            Session: {filterSession.slice(0, 15)}...
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </span>
        {/if}
        {#if searchQuery}
          <span class="filter-tag" on:click={() => searchQuery = ''}>
            Search: {searchQuery}
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </span>
        {/if}
        <button class="btn btn-ghost btn-sm" on:click={() => { filterIntent = ''; filterSuccess = 'all'; filterSession = ''; searchQuery = '' }}>
          Clear all
        </button>
      </div>
    {/if}
  </div>

  {#if loading && history.length === 0}
    <div class="loading-state">
      <div class="spinner"></div>
      <p>Loading voice history...</p>
    </div>
  {:else if error}
    <div class="error-state">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <circle cx="12" cy="12" r="10" />
        <line x1="15" y1="9" x2="9" y2="15" />
        <line x1="9" y1="9" x2="15" y2="15" />
      </svg>
      <p>{error}</p>
      <button class="btn btn-primary" on:click={loadHistory}>Retry</button>
    </div>
  {:else if filteredHistory.length === 0}
    <div class="empty-state">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
        <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z" />
        <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
        <line x1="12" y1="19" x2="12" y2="22" />
      </svg>
      <p>No voice commands found</p>
      <span>Try adjusting your filters or time range</span>
    </div>
  {:else}
    <div class="table-container">
      <table class="history-table">
        <thead>
          <tr>
            <th>Time</th>
            <th>Transcript</th>
            <th>Intent</th>
            <th>Action</th>
            <th>Confidence</th>
            <th>Latency</th>
            <th>Status</th>
            <th>Session</th>
          </tr>
        </thead>
        <tbody>
          {#each filteredHistory as event}
            <tr class:selected={selectedEvent === event} on:click={() => handleSelectEvent(event)}>
              <td class="time-cell">{formatTime(event.timestamp)}</td>
              <td class="transcript-cell" title={event.transcript}>{event.transcript}</td>
              <td class="intent-cell">
                <span class="intent-badge">{event.intent}</span>
              </td>
              <td class="action-cell">{event.action || '-'}</td>
              <td class="confidence-cell">
                <span class="confidence-bar" style="--confidence: {event.confidence}; --color: {getConfidenceColor(event.confidence)}">
                  <span class="confidence-fill"></span>
                  <span class="confidence-text">{Math.round(event.confidence * 100)}%</span>
                </span>
              </td>
              <td class="latency-cell">{event.latency_ms}ms</td>
              <td class="status-cell">
                <span class="status-badge" class:success={event.success} class:failed={!event.success}>
                  {event.success ? 'Success' : 'Failed'}
                </span>
              </td>
              <td class="session-cell">{event.session_id.slice(0, 12)}...</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>

    {#if history.length < total}
      <div class="load-more">
        <button class="btn btn-secondary" on:click={loadMore} disabled={loading}>
          {loading ? 'Loading...' : `Load more (${history.length} / ${total})`}
        </button>
      </div>
    {/if}
  {/if}

  {#if selectedEvent}
    <div class="detail-modal-overlay" on:click={() => selectedEvent = null}>
      <div class="detail-modal" on:click|stopPropagation={() => {}}>
        <div class="modal-header">
          <h3>Command Details</h3>
          <button class="close-btn" on:click={() => selectedEvent = null}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>
        <div class="modal-content">
          <div class="detail-section">
            <h4>Basic Info</h4>
            <dl>
              <div><dt>Timestamp</dt><dd>{formatTime(selectedEvent.timestamp)}</dd></div>
              <div><dt>Session ID</dt><dd>{selectedEvent.session_id}</dd></div>
              <div><dt>User ID</dt><dd>{selectedEvent.user_id || 'N/A'}</dd></div>
            </dl>
          </div>
          <div class="detail-section">
            <h4>Recognition</h4>
            <dl>
              <div><dt>Transcript</dt><dd>{selectedEvent.transcript}</dd></div>
              <div><dt>Intent</dt><dd>{selectedEvent.intent}</dd></div>
              <div><dt>Action</dt><dd>{selectedEvent.action || 'N/A'}</dd></div>
              <div><dt>Confidence</dt><dd>{Math.round(selectedEvent.confidence * 100)}%</dd></div>
              <div><dt>Latency</dt><dd>{selectedEvent.latency_ms}ms</dd></div>
            </dl>
          </div>
          <div class="detail-section">
            <h4>Result</h4>
            <dl>
              <div><dt>Success</dt><dd>{selectedEvent.success ? 'Yes' : 'No'}</dd></div>
              {#if selectedEvent.error}
                <div><dt>Error</dt><dd class="error-text">{selectedEvent.error}</dd></div>
              {/if}
            </dl>
          </div>
        </div>
      </div>
    </div>
  {/if}
</div>

<style>
  .voice-history {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
    height: 100%;
  }

  .panel-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: var(--space-3);
  }

  .panel-header h2 {
    font-size: var(--text-xl);
    font-weight: 600;
    color: var(--text-primary);
    margin: 0;
  }

  .header-actions {
    display: flex;
    gap: var(--space-2);
  }

  .btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: var(--space-2);
    padding: var(--space-2) var(--space-4);
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

  .btn-ghost {
    background: transparent;
    color: var(--text-secondary);
  }

  .btn-ghost:hover:not(:disabled) {
    background: var(--bg-tertiary);
    color: var(--text-primary);
  }

  .btn-sm {
    padding: var(--space-1) var(--space-3);
    font-size: var(--text-xs);
  }

  .btn svg {
    width: 16px;
    height: 16px;
  }

  .filters {
    background: var(--bg-secondary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-md);
    padding: var(--space-4);
  }

  .filter-row {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-4);
    align-items: flex-end;
  }

  .filter-group {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    min-width: 140px;
    flex: 1;
  }

  .filter-group label {
    font-size: var(--text-xs);
    font-weight: 500;
    color: var(--text-tertiary);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }

  .filter-group select {
    padding: var(--space-2) var(--space-3);
    background: var(--bg-tertiary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    font-family: inherit;
    font-size: var(--text-sm);
    cursor: pointer;
  }

  .filter-group select:focus {
    outline: none;
    border-color: var(--accent-primary);
  }

  .search-group {
    flex: 1;
    min-width: 200px;
  }

  .search-input-wrapper {
    position: relative;
  }

  .search-input-wrapper svg {
    position: absolute;
    left: var(--space-3);
    top: 50%;
    transform: translateY(-50%);
    width: 16px;
    height: 16px;
    color: var(--text-tertiary);
    pointer-events: none;
  }

  .search-input-wrapper input {
    width: 100%;
    padding: var(--space-2) var(--space-3) var(--space-2) var(--space-10);
    background: var(--bg-tertiary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    font-family: inherit;
    font-size: var(--text-sm);
  }

  .search-input-wrapper input:focus {
    outline: none;
    border-color: var(--accent-primary);
  }

  .active-filters {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
    margin-top: var(--space-3);
    padding-top: var(--space-3);
    border-top: 1px solid var(--border-primary);
  }

  .active-filters span:first-child {
    font-size: var(--text-xs);
    font-weight: 500;
    color: var(--text-tertiary);
  }

  .filter-tag {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    padding: var(--space-1) var(--space-2);
    background: var(--accent-primary-light);
    color: var(--accent-primary);
    border-radius: var(--radius-full);
    font-size: var(--text-xs);
    cursor: pointer;
    transition: background 0.15s ease;
  }

  .filter-tag:hover {
    background: var(--accent-primary);
    color: var(--bg-primary);
  }

  .filter-tag svg {
    width: 12px;
    height: 12px;
  }

  .loading-state,
  .error-state,
  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: var(--space-12);
    gap: var(--space-4);
    color: var(--text-tertiary);
    text-align: center;
  }

  .loading-state .spinner {
    width: 32px;
    height: 32px;
    border: 3px solid var(--border-primary);
    border-top-color: var(--accent-primary);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }

  @keyframes spin {
    to { transform: rotate(360deg); }
  }

  .error-state svg {
    width: 48px;
    height: 48px;
    color: var(--status-error);
  }

  .empty-state svg {
    width: 48px;
    height: 48px;
    color: var(--text-tertiary);
    opacity: 0.5;
  }

  .empty-state p {
    font-size: var(--text-lg);
    color: var(--text-secondary);
    margin: 0;
  }

  .empty-state span {
    font-size: var(--text-sm);
    color: var(--text-tertiary);
  }

  .table-container {
    flex: 1;
    overflow: auto;
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-md);
    background: var(--bg-secondary);
  }

  .history-table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--text-sm);
  }

  .history-table th {
    position: sticky;
    top: 0;
    background: var(--bg-tertiary);
    padding: var(--space-3) var(--space-4);
    text-align: left;
    font-weight: 600;
    font-size: var(--text-xs);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-tertiary);
    border-bottom: 1px solid var(--border-primary);
    white-space: nowrap;
  }

  .history-table td {
    padding: var(--space-3) var(--space-4);
    border-bottom: 1px solid var(--border-primary);
    vertical-align: middle;
  }

  .history-table tbody tr {
    cursor: pointer;
    transition: background 0.1s ease;
  }

  .history-table tbody tr:hover {
    background: var(--bg-tertiary);
  }

  .history-table tbody tr.selected {
    background: var(--accent-primary-light);
  }

  .time-cell {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: var(--text-xs);
    color: var(--text-secondary);
    white-space: nowrap;
  }

  .transcript-cell {
    max-width: 300px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--text-primary);
  }

  .intent-badge {
    display: inline-block;
    padding: var(--space-1) var(--space-2);
    background: var(--accent-primary-light);
    color: var(--accent-primary);
    border-radius: var(--radius-full);
    font-size: var(--text-xs);
    font-weight: 500;
    max-width: 150px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .action-cell {
    color: var(--text-secondary);
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: var(--text-xs);
  }

  .confidence-bar {
    position: relative;
    display: inline-flex;
    align-items: center;
    width: 80px;
    height: 20px;
    background: var(--bg-tertiary);
    border-radius: var(--radius-full);
    overflow: hidden;
  }

  .confidence-fill {
    position: absolute;
    left: 0;
    top: 0;
    bottom: 0;
    background: var(--color);
    width: calc(var(--confidence) * 100%);
    border-radius: var(--radius-full);
    transition: width 0.3s ease;
  }

  .confidence-text {
    position: relative;
    z-index: 1;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: var(--text-xs);
    font-weight: 600;
    color: var(--text-primary);
    padding: 0 var(--space-2);
  }

  .latency-cell {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: var(--text-xs);
    color: var(--text-secondary);
  }

  .status-badge {
    display: inline-flex;
    align-items: center;
    padding: var(--space-1) var(--space-2);
    border-radius: var(--radius-full);
    font-size: var(--text-xs);
    font-weight: 500;
  }

  .status-badge.success {
    background: var(--status-success-light);
    color: var(--status-success);
  }

  .status-badge.failed {
    background: var(--status-error-light);
    color: var(--status-error);
  }

  .session-cell {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: var(--text-xs);
    color: var(--text-tertiary);
  }

  .load-more {
    display: flex;
    justify-content: center;
    padding: var(--space-4);
  }

  .detail-modal-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    padding: var(--space-4);
    z-index: 1000;
    animation: fadeIn 0.15s ease;
  }

  @keyframes fadeIn {
    from { opacity: 0; }
    to { opacity: 1; }
  }

  .detail-modal {
    background: var(--bg-secondary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-lg);
    width: 100%;
    max-width: 500px;
    max-height: 80vh;
    overflow-y: auto;
    animation: slideUp 0.2s ease;
  }

  @keyframes slideUp {
    from { opacity: 0; transform: translateY(20px); }
    to { opacity: 1; transform: translateY(0); }
  }

  .modal-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--space-4) var(--space-5);
    border-bottom: 1px solid var(--border-primary);
  }

  .modal-header h3 {
    margin: 0;
    font-size: var(--text-lg);
    font-weight: 600;
  }

  .close-btn {
    padding: var(--space-1);
    background: transparent;
    border: none;
    color: var(--text-tertiary);
    cursor: pointer;
    border-radius: var(--radius-sm);
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .close-btn:hover {
    background: var(--bg-tertiary);
    color: var(--text-primary);
  }

  .close-btn svg {
    width: 20px;
    height: 20px;
  }

  .modal-content {
    padding: var(--space-5);
  }

  .detail-section {
    margin-bottom: var(--space-5);
  }

  .detail-section:last-child {
    margin-bottom: 0;
  }

  .detail-section h4 {
    margin: 0 0 var(--space-3);
    font-size: var(--text-sm);
    font-weight: 600;
    color: var(--text-tertiary);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }

  .detail-section dl {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: var(--space-2) var(--space-4);
    margin: 0;
  }

  .detail-section dt {
    font-size: var(--text-sm);
    color: var(--text-tertiary);
    font-weight: 500;
  }

  .detail-section dd {
    margin: 0;
    font-size: var(--text-sm);
    color: var(--text-primary);
    word-break: break-word;
  }

  .error-text {
    color: var(--status-error);
  }

  @media (max-width: 768px) {
    .filter-row {
      flex-direction: column;
      align-items: stretch;
    }

    .filter-group {
      min-width: 0;
    }

    .history-table {
      display: block;
      overflow-x: auto;
    }

    .detail-section dl {
      grid-template-columns: 1fr;
      gap: var(--space-1) var(--space-0);
    }
  }
</style>