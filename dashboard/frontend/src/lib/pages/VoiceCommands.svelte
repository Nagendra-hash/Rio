<script lang="ts">
  import VoiceCommandPalette from '$lib/components/voice/VoiceCommandPalette.svelte'
</script>

<div class="page">
  <header class="page-header">
    <h1>Voice Command Palette</h1>
    <p>Test and execute voice commands directly from the dashboard</p>
  </header>
  <VoiceCommandPalette />
</div>

<style>
  .page {
    max-width: 900px;
    margin: 0 auto;
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