<script lang="ts">
  import { currentPath, navigate } from '$lib/stores/router'
  import { config } from '$lib/stores/config'
  import { session } from '$lib/stores/session'

  const navItems = [
    { path: '/', label: 'Dashboard', icon: 'dashboard' },
    { path: '/agents', label: 'Agents', icon: 'cpu' },
    { path: '/observability', label: 'Observability', icon: 'chart' },
    { path: '/replay', label: 'Replay', icon: 'rewind' },
    { path: '/usage', label: 'Usage & Budget', icon: 'chart' },
    { path: '/hud', label: 'HUD', icon: 'monitor' },
    { path: '/settings', label: 'Settings', icon: 'settings' },
    { path: '/voice/commands', label: 'Voice Commands', icon: 'mic' },
    { path: '/voice/history', label: 'Voice History', icon: 'history' },
    { path: '/voice/analytics', label: 'Voice Analytics', icon: 'chart' },
    { path: '/voice/wake-word', label: 'Wake Word', icon: 'volume' },
  ] as const

  $: activePath = $currentPath

  function icon(name: string) {
    const icons: Record<string, string> = {
      dashboard: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="13" y="3" width="7" height="7" rx="1"/><rect x="3" y="13" width="7" height="7" rx="1"/><rect x="13" y="13" width="7" height="7" rx="1"/>',
      cpu: '<rect x="4" y="4" width="16" height="16" rx="2"/><path d="M4 12h16M12 4v16" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>',
      chart: '<path d="M3 21h18M21 3v18"/><path d="m19 9-5 5-4-4-3 3" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
      rewind: '<polygon points="11 19 2 12 11 5 11 19"/><polygon points="22 19 13 12 22 5 22 19"/>',
      monitor: '<rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/>',
      settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06-.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
      mic: '<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="22"/>',
      history: '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
      volume: '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>',
    }
    return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="icon-svg">${icons[name] || ''}</svg>`
  }
</script>

<aside class="sidebar" aria-label="Main navigation">
  <div class="sidebar-header">
    <svg class="logo" viewBox="0 0 32 32" fill="none" aria-hidden="true">
      <circle cx="16" cy="16" r="14" stroke="currentColor" stroke-width="2"/>
      <path d="M16 8V24M8 16H24" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
    </svg>
    <span class="title">JARVIS</span>
  </div>
  <nav class="nav">
    {#each navItems as item}
      <a
        href={item.path}
        onclick={(e) => { e.preventDefault(); navigate(item.path) }}
        class:active={activePath === item.path || (item.path !== '/' && activePath.startsWith(item.path))}
        aria-current={activePath === item.path ? 'page' : undefined}
      >
        <span class="icon" aria-hidden="true">{@html icon(item.icon)}</span>
        <span>{item.label}</span>
      </a>
    {/each}
  </nav>
  <div class="sidebar-footer">
    <div class="status-indicator" aria-label="Connection status">
      <span class="dot" class:connected={$session.connected}></span>
      <span>{$session.connected ? 'Connected' : 'Disconnected'}</span>
    </div>
    <div class="env-badge">{$config?.env || 'development'}</div>
  </div>
</aside>

<style>
  .sidebar {
    width: 260px;
    height: 100vh;
    background: var(--bg-secondary);
    border-right: 1px solid var(--border-primary);
    display: flex;
    flex-direction: column;
    position: sticky;
    top: 0;
    z-index: 100;
  }

  .sidebar-header {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-5) var(--space-4);
    border-bottom: 1px solid var(--border-primary);
  }

  .logo {
    width: 32px;
    height: 32px;
    color: var(--accent-primary);
  }

  .title {
    font-size: var(--text-lg);
    font-weight: 600;
    color: var(--text-primary);
  }

  .nav {
    flex: 1;
    padding: var(--space-4);
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  .nav a {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-3) var(--space-4);
    border-radius: var(--radius-md);
    color: var(--text-secondary);
    text-decoration: none;
    transition: all 0.15s ease;
  }

  .nav a:hover {
    background: var(--bg-tertiary);
    color: var(--text-primary);
  }

  .nav a.active {
    background: var(--accent-primary);
    color: var(--bg-primary);
  }

  .icon {
    display: flex;
    width: 20px;
    height: 20px;
  }

  .sidebar-footer {
    padding: var(--space-4);
    border-top: 1px solid var(--border-primary);
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }

  .status-indicator {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    font-size: var(--text-sm);
    color: var(--text-tertiary);
  }

  .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--status-error);
  }

  .dot.connected {
    background: var(--status-success);
  }

  .env-badge {
    font-size: var(--text-xs);
    padding: var(--space-1) var(--space-2);
    border-radius: var(--radius-full);
    background: var(--bg-tertiary);
    color: var(--text-tertiary);
    text-transform: uppercase;
    align-self: flex-start;
  }
</style>
