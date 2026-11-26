<script lang="ts">
  import { onMount, onDestroy } from 'svelte'
  import HudCanvas from '$lib/components/hud/HudCanvas.svelte'
  import { hudController } from '$lib/components/hud/HudController'
  import { config } from '$lib/stores/config'

  let state = $state<HudState>('idle')
  let audioLevel = $state(0)
  let webglEnabled = $state(false)
  let mode = $state<'face' | 'reactor'>('face')

  type HudState = 'idle' | 'listening' | 'thinking' | 'speaking'

  const unsubscribe = hudController.onUpdate((s, level) => {
    state = s
    audioLevel = level
  })

  onMount(async () => {
    await hudController.init()
    webglEnabled = hudController.isWebglEnabled()
  })

  onDestroy(() => {
    unsubscribe()
  })

  function toggleMode() {
    mode = mode === 'face' ? 'reactor' : 'face'
  }
</script>

<div class="hud-page">
  <div class="page-header">
    <h1>WebGL HUD</h1>
    <p class="page-subtitle">Real-time visual interface with audio-reactive animations</p>
  </div>

  {#if !webglEnabled}
    <div class="disabled-notice">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
        <circle cx="12" cy="12" r="10"/>
        <line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/>
      </svg>
      <h3>WebGL HUD Disabled</h3>
      <p>Enable <code>JARVIS_UI_WEBGL_HUD=1</code> in Settings to use the WebGL HUD.</p>
      <a href="/settings" class="btn btn-primary">Open Settings</a>
    </div>
  {:else}
    <div class="hud-container">
      <div class="hud-controls">
        <div class="control-group">
          <label for="hudMode">Mode</label>
          <select bind:value={mode} id="hudMode">
            <option value="face">Face</option>
            <option value="reactor">Reactor Core</option>
          </select>
        </div>
        <div class="control-group">
          <label for="hudState">State</label>
          <select bind:value={state} disabled id="hudState">
            <option value="idle">Idle</option>
            <option value="listening">Listening</option>
            <option value="thinking">Thinking</option>
            <option value="speaking">Speaking</option>
          </select>
        </div>
        <div class="control-group">
          <label for="audioLevel">Audio Level</label>
          <input type="range" min="0" max="1" step="0.01" bind:value={audioLevel} disabled id="audioLevel" />
          <span>{(audioLevel * 100).toFixed(0)}%</span>
        </div>
      </div>

      <div class="hud-canvas-wrapper">
        <HudCanvas {state} {audioLevel} {mode} />
      </div>

      <div class="hud-info">
        <h3>HUD Status</h3>
        <dl>
          <dt>Current State</dt>
          <dd>{state.toUpperCase()}</dd>
          <dt>Mode</dt>
          <dd>{mode}</dd>
          <dt>Audio Level</dt>
          <dd>{(audioLevel * 100).toFixed(1)}%</dd>
          <dt>WebGL Enabled</dt>
          <dd>{webglEnabled ? 'Yes' : 'No'}</dd>
        </dl>
      </div>
    </div>
  {/if}
</div>

<style>
  .hud-page {
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

  .disabled-notice {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: var(--space-12);
    background: var(--bg-secondary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-lg);
    text-align: center;
    gap: var(--space-4);
  }

  .disabled-notice svg {
    width: 64px;
    height: 64px;
    color: var(--text-tertiary);
  }

  .disabled-notice h3 {
    font-size: var(--text-xl);
    color: var(--text-primary);
  }

  .disabled-notice p {
    color: var(--text-secondary);
    max-width: 400px;
  }

  .disabled-notice code {
    background: var(--bg-tertiary);
    padding: var(--space-1) var(--space-2);
    border-radius: var(--radius-sm);
  }

  .hud-container {
    display: flex;
    flex-direction: column;
    gap: var(--space-6);
  }

  .hud-controls {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: var(--space-4);
    background: var(--bg-secondary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-lg);
    padding: var(--space-4);
  }

  .control-group {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }

  .control-group label {
    font-size: var(--text-sm);
    font-weight: 500;
    color: var(--text-secondary);
  }

  .control-group select,
  .control-group input[type="range"] {
    padding: var(--space-2) var(--space-3);
    background: var(--bg-tertiary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-md);
    color: var(--text-primary);
    font-size: var(--text-sm);
  }

  .control-group input[type="range"] {
    accent-color: var(--accent-primary);
  }

  .hud-canvas-wrapper {
    aspect-ratio: 16 / 9;
    max-height: 600px;
    background: var(--bg-secondary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-lg);
    overflow: hidden;
  }

  .hud-info {
    background: var(--bg-secondary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-lg);
    padding: var(--space-4);
  }

  .hud-info h3 {
    font-size: var(--text-lg);
    font-weight: 600;
    margin-bottom: var(--space-4);
  }

  .hud-info dl {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: var(--space-3);
  }

  .hud-info dt {
    font-size: var(--text-sm);
    color: var(--text-tertiary);
  }

  .hud-info dd {
    font-size: var(--text-base);
    font-weight: 500;
    color: var(--text-primary);
    margin: 0;
  }
</style>