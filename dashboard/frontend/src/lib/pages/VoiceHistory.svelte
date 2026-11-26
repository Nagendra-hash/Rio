<script lang="ts">
  import VoiceHistoryPanel from '$lib/components/voice/VoiceHistoryPanel.svelte'
</script>

<div class="page">
  <header class="page-header">
    <h1>Voice Command History</h1>
    <p>Browse and search through all voice command interactions</p>
  </header>
  <VoiceHistoryPanel />
</div>

<style>
  .page {
    max-width: 1400px;
    margin: 0 auto;
    height: calc(100vh - 120px);
  }

  .page-header {
    margin-bottom: var(--space-6);
  }

  .page-header h1 {
    font-size: var(--text-2xl);
    font-weight: 700;
    color: var(--text-primary);
    margin: 0 0 var(--space-1);
  }

  .page-header p {
    font-size: var(--text-base);
    color: var(--text-tertiary);
    margin: 0;
  }
</style>