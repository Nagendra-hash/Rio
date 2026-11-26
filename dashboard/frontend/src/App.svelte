<script lang="ts">
  import { onMount } from 'svelte'
  import Sidebar from '$lib/components/ui/Sidebar.svelte'
  import Header from '$lib/components/ui/Header.svelte'
  import ToastContainer from '$lib/components/ui/ToastContainer.svelte'
  import { config } from '$lib/stores/config'
  import { session } from '$lib/stores/session'
  import { currentPath, initRouter } from '$lib/stores/router'

  // Page components
  import Dashboard from '$lib/pages/Dashboard.svelte'
  import Agents from '$lib/pages/Agents.svelte'
  import Observability from '$lib/pages/Observability.svelte'
  import Replay from '$lib/pages/Replay.svelte'
  import Settings from '$lib/pages/Settings.svelte'
  import Hud from '$lib/pages/Hud.svelte'
  import VoiceCommands from '$lib/pages/VoiceCommands.svelte'
  import VoiceHistory from '$lib/pages/VoiceHistory.svelte'
  import VoiceAnalytics from '$lib/pages/VoiceAnalytics.svelte'
  import WakeWord from '$lib/pages/WakeWord.svelte'
  import Usage from '$lib/pages/Usage.svelte'

  const routes = {
    '/': Dashboard,
    '/agents': Agents,
    '/observability': Observability,
    '/replay': Replay,
    '/settings': Settings,
    '/hud': Hud,
    '/usage': Usage,
    '/voice/commands': VoiceCommands,
    '/voice/history': VoiceHistory,
    '/voice/analytics': VoiceAnalytics,
    '/voice/wake-word': WakeWord,
  }

  onMount(() => {
    initRouter()
  })

  function navigate(path: string) {
    currentPath.set(path)
    window.history.pushState({}, '', path)
  }

  // Make navigate available globally for Sidebar links
  // We'll use a custom event for navigation
  function handleClick(event: MouseEvent) {
    const target = event.target as HTMLAnchorElement
    if (target.tagName === 'A' && target.href.startsWith(window.location.origin)) {
      event.preventDefault()
      navigate(target.pathname)
    }
  }
</script>

<svelte:window on:click={handleClick} />

<div class="app">
  <Header />
  <div class="main-container">
    <Sidebar />
    <main class="content" aria-label="Main content">
      <svelte:component this={routes[currentRoute] || Dashboard} />
    </main>
  </div>
  <ToastContainer />
</div>

<style>
  .app {
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    background: var(--bg-primary);
    color: var(--text-primary);
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
  }

  .main-container {
    display: flex;
    flex: 1;
    overflow: hidden;
  }

  .content {
    flex: 1;
    overflow-y: auto;
    padding: var(--space-6);
    min-width: 0;
  }

  @media (max-width: 768px) {
    .main-container {
      flex-direction: column;
    }
    .content {
      padding: var(--space-4);
    }
  }
</style>
