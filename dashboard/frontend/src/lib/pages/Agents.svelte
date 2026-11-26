<script lang="ts">
  import { onMount } from 'svelte'
  import { agents } from '$lib/stores/agents'
  import { api } from '$lib/api/rest'
  import { toast } from '$lib/stores/toast'
  import AgentCard from '$lib/components/agents/AgentCard.svelte'
  import type { AgentStatus } from '$lib/stores/agents'
  import { DndContext, closestCorners, KeyboardSensor, PointerSensor, useSensor, useSensors } from '@dnd-kit/core'
  import { arrayMove, SortableContext, sortableKeyboardCoordinates, verticalListSortingStrategy } from '@dnd-kit/sortable'
  import { CSS } from '@dnd-kit/utilities'

  let agentOrder = $derived(Object.keys($agents))
  let editingAgent = $state<AgentStatus | null>(null)

  function handleDragEnd(event: any) {
    const { active, over } = event
    if (active.id !== over?.id) {
      const oldIndex = agentOrder.indexOf(active.id)
      const newIndex = agentOrder.indexOf(over.id)
      agentOrder = arrayMove(agentOrder, oldIndex, newIndex)
      // Persist to localStorage
      localStorage.setItem('agentOrder', JSON.stringify(agentOrder))
    }
  }

  const sensors = useSensors(
    useSensor(PointerSensor),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates })
  )

  onMount(() => {
    const saved = localStorage.getItem('agentOrder')
    if (saved) {
      try {
        const parsed = JSON.parse(saved)
        if (Array.isArray(parsed)) agentOrder = parsed
      } catch { /* ignore */ }
    }
  })
</script>

<div class="agents-page">
  <div class="page-header">
    <h1>Agent Mission Control</h1>
    <p class="page-subtitle">Monitor and manage all JARVIS agents</p>
  </div>

  <div class="stats-bar">
    <div class="stat">
      <span class="stat-value">{$agents.length}</span>
      <span class="stat-label">Total Agents</span>
    </div>
    <div class="stat">
      <span class="stat-value">{$agents.filter(a => a.enabled).length}</span>
      <span class="stat-label">Enabled</span>
    </div>
    <div class="stat">
      <span class="stat-value">{$agents.filter(a => a.status === 'running').length}</span>
      <span class="stat-label">Running</span>
    </div>
    <div class="stat">
      <span class="stat-value">{$agents.filter(a => a.status === 'error').length}</span>
      <span class="stat-label">Errors</span>
    </div>
  </div>

  <DndContext
    sensors={sensors}
    collisionDetection={closestCorners}
    onDragEnd={handleDragEnd}
  >
    <SortableContext items={agentOrder} strategy={verticalListSortingStrategy}>
      <div class="agents-grid">
        {#each agentOrder as name}
          {#if $agents[name] as agent}
            <AgentCard {agent} />
          {/if}
        {/each}
      </div>
    </SortableContext>
  </DndContext>

  {#if editingAgent}
    <div class="modal-overlay" role="button" tabindex="0" onclick={(e) => { if (e.target === e.currentTarget) editingAgent = null }} onkeydown={(e) => e.key === 'Escape' && (editingAgent = null)}>
      <div class="modal">
        <h2>Edit {editingAgent.name}</h2>
        <p>Agent configuration coming soon...</p>
        <button class="btn btn-primary" onclick={() => editingAgent = null}>Close</button>
      </div>
    </div>
  {/if}
</div>

<style>
  .agents-page {
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

  .stats-bar {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: var(--space-4);
  }

  .stat {
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: var(--space-4);
    background: var(--bg-secondary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-md);
  }

  .stat-value {
    font-size: var(--text-2xl);
    font-weight: 700;
    color: var(--text-primary);
  }

  .stat-label {
    font-size: var(--text-sm);
    color: var(--text-tertiary);
  }

  .agents-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
    gap: var(--space-4);
  }

  .modal-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }

  .modal {
    background: var(--bg-secondary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-lg);
    padding: var(--space-6);
    min-width: 400px;
    max-width: 90vw;
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
</style>