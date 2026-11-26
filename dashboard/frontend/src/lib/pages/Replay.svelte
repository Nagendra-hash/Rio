<script lang="ts">
  import { onMount } from 'svelte'
  import { api } from '$lib/api/rest'
  import { toast } from '$lib/stores/toast'
  import type { JournalEntry, TraceData, TraceSpan } from '$lib/api/rest'
  import { Virtualizer } from '@tanstack/svelte-virtual'
  import TraceViewer from '$lib/components/replay/TraceViewer.svelte'

  let entries = $state<JournalEntry[]>([])
  let selectedEntry = $state<JournalEntry | null>(null)
  let traceData = $state<TraceData | null>(null)
  let loading = $state(false)
  let loadingTrace = $state(false)
  let hasMore = $state(true)
  let beforeCursor = $state<string | null>(null)

  let parentRef = $state<HTMLDivElement | null>(null)

  const virtualizer = new Virtualizer({
    count: () => entries.length,
    getScrollElement: () => parentRef,
    estimateSize: () => 80,
    overscan: 5,
  })

  async function loadEntries(append = false) {
    if (loading) return
    loading = true
    try {
      const newEntries = await api.getJournal({ limit: 100, before: beforeCursor || undefined })
      if (append) {
        entries = [...entries, ...newEntries]
      } else {
        entries = newEntries
      }
      if (newEntries.length > 0) {
        beforeCursor = newEntries[newEntries.length - 1].timestamp
      }
      hasMore = newEntries.length === 100
    } catch (e) {
      toast.error('Failed to load journal', e instanceof Error ? e.message : 'Unknown error')
    } finally {
      loading = false
    }
  }

  async function selectEntry(entry: JournalEntry) {
    selectedEntry = entry
    if (entry.traceId) {
      loadingTrace = true
      try {
        traceData = await api.getTrace(entry.traceId)
      } catch (e) {
        toast.error('Failed to load trace', e instanceof Error ? e.message : 'Unknown error')
        traceData = null
      } finally {
        loadingTrace = false
      }
    } else {
      traceData = null
    }
  }

  async function replayTurn() {
    if (!selectedEntry) return
    try {
      await api.replayTurn(selectedEntry.turnId)
      toast.success('Replay initiated', `Replaying turn ${selectedEntry.turnId}`)
    } catch (e) {
      toast.error('Failed to replay', e instanceof Error ? e.message : 'Unknown error')
    }
  }

  function formatTimestamp(ts: string): string {
    return new Date(ts).toLocaleString()
  }

  function getStatusClass(status: string): string {
    return status.toLowerCase()
  }

    const selectedEntryIndex = $derived(selectedEntry ? entries.findIndex(e => e.turnId === selectedEntry.turnId) : -1)

  onMount(() => {
    loadEntries()
  })
</script>

<div class="replay-page">
  <div class="page-header">
    <h1>Conversation Replay</h1>
    <p class="page-subtitle">Browse conversation history and debug agent traces</p>
  </div>

  <div class="replay-layout">
    <aside class="journal-panel">
      <div class="journal-header">
        <h2>Journal</h2>
        <button class="btn btn-secondary" onclick={loadEntries} disabled={loading || !hasMore}>
          {loading ? 'Loading...' : hasMore ? 'Load More' : 'All Loaded'}
        </button>
      </div>

      <div class="journal-list" bind:this={parentRef} style="height: calc(100vh - 200px); overflow: auto;">
        <div style="height: {virtualizer.getTotalSize()}px; position: relative;">
          {#each virtualizer.getVirtualItems() as virtualRow}
            <button
              class="journal-item {virtualRow.index === selectedEntryIndex ? 'selected' : ''} {getStatusClass(entries[virtualRow.index].status)}"
              style="position: absolute; top: {virtualRow.start}px; left: 0; right: 0; height: {virtualRow.size}px;"
              onclick={() => selectEntry(entries[virtualRow.index])}
            >
              <div class="item-time">{formatTimestamp(entries[virtualRow.index].timestamp)}</div>
              <div class="item-agent">{entries[virtualRow.index].agent}</div>
              <div class="item-text">{entries[virtualRow.index].userText.slice(0, 60)}…</div>
              <div class="item-meta">
                <span class="status {getStatusClass(entries[virtualRow.index].status)}">{entries[virtualRow.index].status}</span>
                <span>{entries[virtualRow.index].latencyMs}ms</span>
                <span>{entries[virtualRow.index].tokensUsed} tok</span>
              </div>
            </button>
          {/each}
        </div>
      </div>
    </aside>

    <div class="detail-panel">
      {#if selectedEntry}
        <div class="detail-header">
          <h3>Turn Details</h3>
          <div class="detail-actions">
            <button class="btn btn-primary" onclick={replayTurn} disabled={loadingTrace}>
              {loadingTrace ? 'Replaying...' : 'Replay Turn'}
            </button>
          </div>
        </div>

        <div class="detail-content">
          <div class="detail-field">
            <span class="field-label">Turn ID</span>
            <code>{selectedEntry.turnId}</code>
          </div>
          <div class="detail-field">
            <span class="field-label">Timestamp</span>
            <span>{formatTimestamp(selectedEntry.timestamp)}</span>
          </div>
          <div class="detail-field">
            <span class="field-label">Agent</span>
            <span>{selectedEntry.agent}</span>
          </div>
          <div class="detail-field">
            <span class="field-label">Status</span>
            <span class="status {getStatusClass(selectedEntry.status)}">{selectedEntry.status}</span>
          </div>
          <div class="detail-field">
            <span class="field-label">Latency</span>
            <span>{selectedEntry.latencyMs} ms</span>
          </div>
          <div class="detail-field">
            <span class="field-label">Tokens</span>
            <span>{selectedEntry.tokensUsed}</span>
          </div>
          <div class="detail-field full-width">
            <span class="field-label">User Input</span>
            <pre>{selectedEntry.userText}</pre>
          </div>

          {#if traceData}
            <TraceViewer {traceData} />
          {:else if selectedEntry.traceId}
            <div class="trace-loading">
              {loadingTrace ? 'Loading trace...' : 'No trace data available'}
            </div>
          {/if}
        </div>
      {:else}
        <div class="empty-state">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M4 19.5A7 7 0 1 1 19.5 4"/><path d="M9 12l3 3 6-6"/></svg>
          <h3>Select a turn</h3>
          <p>Click on a journal entry to view details and replay</p>
        </div>
      {/if}
    </div>
  </div>
</div>

<style>
  .replay-page {
    display: flex;
    flex-direction: column;
    gap: var(--space-6);
    height: calc(100vh - 120px);
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

  .replay-layout {
    display: grid;
    grid-template-columns: 400px 1fr;
    gap: var(--space-4);
    flex: 1;
    min-height: 0;
  }

  .journal-panel {
    background: var(--bg-secondary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-lg);
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .journal-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--space-4);
    border-bottom: 1px solid var(--border-primary);
  }

  .journal-header h2 {
    font-size: var(--text-lg);
    font-weight: 600;
  }

  .journal-list {
    flex: 1;
    overflow: hidden;
  }

  .journal-item {
    display: grid;
    grid-template-columns: 1fr auto;
    grid-template-rows: auto auto;
    gap: var(--space-1) var(--space-3);
    padding: var(--space-3);
    border-bottom: 1px solid var(--border-primary);
    cursor: pointer;
    transition: background 0.1s;
  }

  .journal-item:hover {
    background: var(--bg-tertiary);
  }

  .journal-item.selected {
    background: var(--accent-primary);
    color: var(--bg-primary);
  }

  .journal-item.selected .item-time,
  .journal-item.selected .item-agent,
  .journal-item.selected .item-text,
  .journal-item.selected .item-meta span {
    color: var(--bg-primary);
  }

  .item-time {
    grid-column: 1;
    font-size: var(--text-xs);
    color: var(--text-tertiary);
  }

  .item-agent {
    grid-column: 2;
    font-size: var(--text-xs);
    font-weight: 500;
    color: var(--accent-primary);
    text-align: right;
  }

  .item-text {
    grid-column: 1 / -1;
    font-size: var(--text-sm);
    color: var(--text-secondary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .item-meta {
    grid-column: 1 / -1;
    display: flex;
    gap: var(--space-2);
    font-size: var(--text-xs);
    color: var(--text-tertiary);
  }

  .status {
    padding: var(--space-1) var(--space-2);
    border-radius: var(--radius-full);
    font-weight: 500;
  }

  .status.completed { background: var(--status-success); color: var(--bg-primary); }
  .status.failed { background: var(--status-error); color: var(--bg-primary); }
  .status.needs_input { background: var(--status-warning); color: var(--bg-primary); }
  .status.needs_approval { background: var(--accent-primary); color: var(--bg-primary); }
  .status.denied { background: var(--text-tertiary); color: var(--bg-primary); }

  .detail-panel {
    background: var(--bg-secondary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-lg);
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .detail-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--space-4);
    border-bottom: 1px solid var(--border-primary);
  }

  .detail-header h3 {
    font-size: var(--text-lg);
    font-weight: 600;
  }

  .detail-content {
    flex: 1;
    overflow-y: auto;
    padding: var(--space-4);
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
  }

  .detail-field {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  .detail-field.full-width {
    grid-column: 1 / -1;
  }

  .detail-field .field-label {
    font-size: var(--text-xs);
    font-weight: 500;
    color: var(--text-tertiary);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }

  .detail-field span, .detail-field code {
    font-size: var(--text-sm);
    color: var(--text-primary);
  }

  .detail-field code {
    background: var(--bg-tertiary);
    padding: var(--space-1) var(--space-2);
    border-radius: var(--radius-sm);
    font-family: inherit;
  }

  .detail-field pre {
    background: var(--bg-tertiary);
    padding: var(--space-3);
    border-radius: var(--radius-md);
    font-size: var(--text-sm);
    color: var(--text-primary);
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 200px;
    overflow-y: auto;
  }

  .trace-loading {
    padding: var(--space-8);
    text-align: center;
    color: var(--text-tertiary);
  }

  .empty-state {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: var(--space-4);
    color: var(--text-tertiary);
  }

  .empty-state svg {
    width: 64px;
    height: 64px;
  }

  .empty-state h3 {
    font-size: var(--text-lg);
    color: var(--text-secondary);
  }

  @media (max-width: 1024px) {
    .replay-layout {
      grid-template-columns: 1fr;
    }
    .journal-panel {
      max-height: 400px;
    }
  }
</style>
