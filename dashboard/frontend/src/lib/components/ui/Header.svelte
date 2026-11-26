<script lang="ts">
  import { currentPath } from '$lib/stores/router'
  import { config } from '$lib/stores/config'
  import { session } from '$lib/stores/session'
  import { theme } from '$lib/stores/theme'

  $: pageTitle = getPageTitle($currentPath)
  $: breadcrumbs = getBreadcrumbs($currentPath)

  function getPageTitle(path: string): string {
    const titles: Record<string, string> = {
      '/': 'Dashboard',
      '/agents': 'Agents',
      '/observability': 'Observability',
      '/replay': 'Conversation Replay',
      '/settings': 'Settings',
    }
    for (const [route, title] of Object.entries(titles)) {
      if (path === route || (route !== '/' && path.startsWith(route))) return title
    }
    return 'JARVIS'
  }

  function getBreadcrumbs(path: string) {
    const segments = path.split('/').filter(Boolean)
    const crumbs = [{ label: 'Home', href: '/' }]
    let current = ''
    for (const seg of segments) {
      current += '/' + seg
      crumbs.push({ label: seg.charAt(0).toUpperCase() + seg.slice(1), href: current })
    }
    return crumbs
  }

  $: themeIcon = $theme === 'dark'
    ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg>'
    : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>'

  function toggleTheme() {
    theme.toggle()
  }

  function showNotifications() {
    // TODO: Implement notifications panel
    console.log('Notifications clicked')
  }
</script>

<header class="header">
  <div class="header-left">
    <h1 class="page-title">{pageTitle}</h1>
    <nav class="breadcrumb" aria-label="Breadcrumb">
      {#each breadcrumbs as crumb, i}
        {#if i > 0}<span class="separator" aria-hidden="true">/</span>{/if}
        {#if i === breadcrumbs.length - 1}
          <span aria-current="page">{crumb}</span>
        {:else}
          <a href={crumb.href}>{crumb.label}</a>
        {/if}
      {/each}
    </nav>
  </div>
  <div class="header-right">
    <div class="header-actions">
      <button class="icon-btn" aria-label="Theme toggle" on:click={toggleTheme}>
        {@html themeIcon}
      </button>
      <button class="icon-btn" aria-label="Notifications" on:click={showNotifications}>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>
      </button>
      <div class="user-menu">
        <button class="user-avatar" aria-label="User menu">{$session.userId?.[0]?.toUpperCase() || 'J'}</button>
      </div>
    </div>
  </div>
</header>

<style>
  .header {
    height: 64px;
    background: var(--bg-secondary);
    border-bottom: 1px solid var(--border-primary);
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 var(--space-6);
    position: sticky;
    top: 0;
    z-index: 50;
  }

  .header-left {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    min-width: 0;
  }

  .page-title {
    font-size: var(--text-xl);
    font-weight: 600;
    color: var(--text-primary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .breadcrumb {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    font-size: var(--text-sm);
    color: var(--text-tertiary);
  }

  .breadcrumb a {
    color: var(--text-secondary);
    text-decoration: none;
  }

  .breadcrumb a:hover {
    color: var(--accent-primary);
  }

  .separator {
    color: var(--text-tertiary);
  }

  .header-right {
    display: flex;
    align-items: center;
    gap: var(--space-4);
  }

  .header-actions {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }

  .icon-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 40px;
    height: 40px;
    border: none;
    background: var(--bg-tertiary);
    border-radius: var(--radius-md);
    color: var(--text-secondary);
    cursor: pointer;
    transition: all 0.15s ease;
  }

  .icon-btn:hover {
    background: var(--accent-primary);
    color: var(--bg-primary);
  }

  .icon-btn svg {
    width: 20px;
    height: 20px;
  }

  .user-avatar {
    width: 40px;
    height: 40px;
    border-radius: 50%;
    background: var(--accent-primary);
    color: var(--bg-primary);
    border: none;
    font-weight: 600;
    font-size: var(--text-sm);
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  @media (max-width: 768px) {
    .header {
      padding: 0 var(--space-4);
    }
    .breadcrumb {
      display: none;
    }
  }
</style>
