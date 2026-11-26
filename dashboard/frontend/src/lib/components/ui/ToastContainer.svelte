<script lang="ts">
  import { toast } from '$lib/stores/toast'
  import { onMount } from 'svelte'
  import { quintOut } from 'svelte/easing'
  import type { Toast } from '$lib/stores/toast'

  function icon(type: string) {
    const icons: Record<string, string> = {
      success: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>',
      error: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>',
      warning: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
      info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>',
    }
    return icons[type] || icons.info
  }
</script>

{#if $toast.length > 0}
  <div class="toast-container" role="region" aria-label="Notifications" aria-live="polite">
    {#each $toast as t (t.id)}
      <div
        class="toast {t.type}"
        in:fly={{ x: 300, duration: 300, easing: quintOut }}
        out:fly={{ x: 300, duration: 200, easing: quintOut }}
        role="alert"
      >
        <div class="toast-icon">{@html icon(t.type)}</div>
        <div class="toast-content">
          <div class="toast-title">{t.title}</div>
          {#if t.message}
            <div class="toast-message">{t.message}</div>
          {/if}
        </div>
        <button class="toast-close" on:click={() => toast.dismiss(t.id)} aria-label="Dismiss">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
    {/each}
  </div>
{/if}

<style>
  .toast-container {
    position: fixed;
    bottom: var(--space-6);
    right: var(--space-6);
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    z-index: 1000;
    max-width: 400px;
  }

  .toast {
    display: flex;
    align-items: flex-start;
    gap: var(--space-3);
    padding: var(--space-4);
    background: var(--bg-secondary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-lg);
    box-shadow: var(--shadow-lg);
    min-width: 300px;
  }

  .toast.success { border-left: 4px solid var(--status-success); }
  .toast.error { border-left: 4px solid var(--status-error); }
  .toast.warning { border-left: 4px solid var(--status-warning); }
  .toast.info { border-left: 4px solid var(--accent-primary); }

  .toast-icon {
    flex-shrink: 0;
    width: 24px;
    height: 24px;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .toast.success .toast-icon { color: var(--status-success); }
  .toast.error .toast-icon { color: var(--status-error); }
  .toast.warning .toast-icon { color: var(--status-warning); }
  .toast.info .toast-icon { color: var(--accent-primary); }

  .toast-content {
    flex: 1;
    min-width: 0;
  }

  .toast-title {
    font-weight: 600;
    font-size: var(--text-sm);
    color: var(--text-primary);
  }

  .toast-message {
    font-size: var(--text-sm);
    color: var(--text-secondary);
    margin-top: var(--space-1);
  }

  .toast-close {
    flex-shrink: 0;
    width: 24px;
    height: 24px;
    display: flex;
    align-items: center;
    justify-content: center;
    border: none;
    background: transparent;
    color: var(--text-tertiary);
    cursor: pointer;
    border-radius: var(--radius-sm);
  }

  .toast-close:hover {
    background: var(--bg-tertiary);
    color: var(--text-primary);
  }

  @media (max-width: 480px) {
    .toast-container {
      left: var(--space-4);
      right: var(--space-4);
      bottom: var(--space-4);
      max-width: none;
    }
    .toast {
      min-width: 0;
    }
  }
</style>
