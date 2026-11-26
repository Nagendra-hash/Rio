<script lang="ts">
  import WakeWordTrainingUI from '$lib/components/voice/WakeWordTrainingUI.svelte'
</script>

<div class="page">
  <header class="page-header">
    <h1>Wake Word Training</h1>
    <p>Create and manage custom wake word models for voice activation</p>
  </header>
  <WakeWordTrainingUI />
</div>

<style>
  .page {
    max-width: 1000px;
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