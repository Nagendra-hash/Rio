<script lang="ts">
  import { onMount } from 'svelte'
  import { api } from '$lib/api/rest'

  // State
  let models: WakeWordModelInfo[] = []
  let loading = false
  let error: string | null = null
  let selectedModel: WakeWordModelInfo | null = null

  // Training form
  let newModelName = ''
  let newModelPhrases: string[] = ['']
  let newModelThreshold = 0.5
  let newModelSensitivity = 0.5
  let training = false
  let trainingStep = 0 // 0: form, 1: recording, 2: complete
  let recordedSamples: Float32Array[] = []
  let recordingMediaStream: MediaStream | null = null
  let audioContext: AudioContext | null = null
  let processor: ScriptProcessorNode | null = null
  let isRecording = false
  let recordingTime = 0
  let recordingTimer: ReturnType<typeof setInterval> | null = null

  // Delete confirmation
  let deleteConfirmName = ''

  async function loadModels() {
    loading = true
    error = null
    try {
      const response = await api.getWakeWordModels()
      models = response.models
    } catch (err: any) {
      error = err.message || 'Failed to load models'
    } finally {
      loading = false
    }
  }

  function addPhrase() {
    newModelPhrases.push('')
  }

  function removePhrase(index: number) {
    if (newModelPhrases.length > 1) {
      newModelPhrases.splice(index, 1)
    }
  }

  async function startTraining() {
    if (!newModelName.trim()) {
      error = 'Model name is required'
      return
    }
    if (newModelPhrases.filter(p => p.trim()).length === 0) {
      error = 'At least one phrase is required'
      return
    }

    training = true
    trainingStep = 1
    recordedSamples = []
    error = null

    try {
      // Get microphone access
      recordingMediaStream = await navigator.mediaDevices.getUserMedia({ audio: true })
      audioContext = new AudioContext({ sampleRate: 16000 })
      const source = audioContext.createMediaStreamSource(recordingMediaStream)

      // Create script processor to capture audio
      processor = audioContext.createScriptProcessor(4096, 1, 1)
      source.connect(processor)
      processor.connect(audioContext.destination)

      processor.onaudioprocess = (e) => {
        if (isRecording) {
          const inputData = e.inputBuffer.getChannelData(0)
          // Copy the data
          recordedSamples.push(new Float32Array(inputData))
        }
      }

      startRecording()
    } catch (err: any) {
      error = `Microphone access failed: ${err.message}`
      stopTraining()
    }
  }

  function startRecording() {
    isRecording = true
    recordingTime = 0
    recordingTimer = setInterval(() => {
      recordingTime++
    }, 1000)
  }

  function stopRecording() {
    isRecording = false
    if (recordingTimer) {
      clearInterval(recordingTimer)
      recordingTimer = null
    }
  }

  async function finishTraining() {
    stopRecording()

    // Combine all recorded samples
    let totalLength = 0
    for (const sample of recordedSamples) {
      totalLength += sample.length
    }
    const combined = new Float32Array(totalLength)
    let offset = 0
    for (const sample of recordedSamples) {
      combined.set(sample, offset)
      offset += sample.length
    }

    // Clean up audio
    cleanupAudio()

    // Send to server
    try {
      const audioArrays: number[][] = []
      // Split into ~2 second chunks for training
      const chunkSize = 32000 // 2 seconds at 16kHz
      for (let i = 0; i < combined.length; i += chunkSize) {
        const chunk = combined.slice(i, i + chunkSize)
        if (chunk.length > 8000) { // At least 0.5 seconds
          audioArrays.push(Array.from(chunk))
        }
      }

      if (audioArrays.length === 0) {
        error = 'No valid audio recorded. Please try again.'
        trainingStep = 0
        return
      }

      await api.trainWakeWord({
        name: newModelName.trim(),
        phrases: newModelPhrases.filter(p => p.trim()),
        threshold: newModelThreshold,
        sensitivity: newModelSensitivity,
        audio_samples: audioArrays
      })

      trainingStep = 2
      newModelName = ''
      newModelPhrases = ['']
      newModelThreshold = 0.5
      newModelSensitivity = 0.5

      // Reload models after a delay
      setTimeout(() => {
        loadModels()
        training = false
        trainingStep = 0
      }, 1000)

    } catch (err: any) {
      error = `Training failed: ${err.message}`
      trainingStep = 0
      cleanupAudio()
    }
  }

  function cancelTraining() {
    stopRecording()
    cleanupAudio()
    training = false
    trainingStep = 0
    recordedSamples = []
  }

  function cleanupAudio() {
    if (processor) {
      processor.disconnect()
      processor.onaudioprocess = null
      processor = null
    }
    if (audioContext) {
      audioContext.close()
      audioContext = null
    }
    if (recordingMediaStream) {
      recordingMediaStream.getTracks().forEach(track => track.stop())
      recordingMediaStream = null
    }
  }

  async function deleteModel(name: string) {
    if (deleteConfirmName !== name) {
      deleteConfirmName = name
      return
    }

    try {
      await api.deleteWakeWord(name)
      loadModels()
    } catch (err: any) {
      error = `Delete failed: ${err.message}`
    } finally {
      deleteConfirmName = ''
    }
  }

  function formatTime(seconds: number): string {
    const mins = Math.floor(seconds / 60)
    const secs = seconds % 60
    return `${mins}:${secs.toString().padStart(2, '0')}`
  }

  function getModelStatusColor(model: WakeWordModelInfo): string {
    if (model.sample_count > 0) return 'var(--status-success)'
    return 'var(--status-warning)'
  }

  onMount(() => {
    loadModels()
  })

  // Cleanup on unmount
  import { onDestroy } from 'svelte'
  onDestroy(() => {
    cleanupAudio()
  })
</script>

<div class="wake-word-training">
  <div class="panel-header">
    <h2>Wake Word Training</h2>
    <p class="subtitle">Create and manage custom wake word models</p>
  </div>

  {#if loading && models.length === 0}
    <div class="loading-state">
      <div class="spinner"></div>
      <p>Loading wake word models...</p>
    </div>
  {:else if error}
    <div class="error-state">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <circle cx="12" cy="12" r="10" />
        <line x1="15" y1="9" x2="9" y2="15" />
        <line x1="9" y1="9" x2="15" y2="15" />
      </svg>
      <p>{error}</p>
      <button class="btn btn-primary" on:click={loadModels}>Retry</button>
    </div>
  {:else}
    <!-- Existing Models -->
    <section class="section">
      <h3>Existing Models ({models.length})</h3>

      {#if models.length === 0}
        <div class="empty-state">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
            <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z" />
            <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
            <line x1="12" y1="19" x2="12" y2="22" />
          </svg>
          <p>No wake word models created yet</p>
          <span>Create your first model below</span>
        </div>
      {:else}
        <div class="models-grid">
          {#each models as model}
            <div class="model-card">
              <div class="model-header">
                <div class="model-info">
                  <h4>{model.name}</h4>
                  <p class="model-phrases">{model.phrases.join(', ')}</p>
                </div>
                <span class="status-dot" style="background: {getModelStatusColor(model)}"></span>
              </div>

              <div class="model-stats">
                <div class="stat">
                  <span class="stat-value">{model.sample_count}</span>
                  <span class="stat-label">Samples</span>
                </div>
                <div class="stat">
                  <span class="stat-value">{model.threshold.toFixed(2)}</span>
                  <span class="stat-label">Threshold</span>
                </div>
                <div class="stat">
                  <span class="stat-value">{model.sensitivity.toFixed(2)}</span>
                  <span class="stat-label">Sensitivity</span>
                </div>
              </div>

              <div class="model-actions">
                {#if model.sample_count === 0}
                  <button class="btn btn-sm btn-secondary" on:click={() => retrainModel(model)}>
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                      <path d="M1 4v16M5 4v16M9 4v16" />
                    </svg>
                    Train
                  </button>
                {:else}
                  <button class="btn btn-sm btn-secondary" on:click={() => retrainModel(model)}>
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                      <polyline points="23 4 23 10 17 10" />
                      <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
                    </svg>
                    Retrain
                  </button>
                {/if}
                <button
                  class="btn btn-sm btn-ghost btn-danger"
                  on:click={() => deleteModel(model.name)}
                  class:confirming={deleteConfirmName === model.name}
                >
                  {#if deleteConfirmName === model.name}
                    <span>Confirm?</span>
                  {:else}
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                      <polyline points="3 6 5 6 21 6" />
                      <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                    </svg>
                  {/if}
                </button>
              </div>

              <div class="model-meta">
                Created: {new Date(model.created_at * 1000).toLocaleDateString()}
                {#if model.updated_at !== model.created_at}
                  • Updated: {new Date(model.updated_at * 1000).toLocaleDateString()}
                {/if}
              </div>
            </div>
          {/each}
        </div>
      {/if}
    </section>

    <!-- Create New Model -->
    <section class="section">
      <h3>{training ? (trainingStep === 1 ? 'Recording Samples' : trainingStep === 2 ? 'Training Complete' : 'Create New Model') : 'Create New Model'}</h3>

      {#if !training}
        <form class="training-form" on:submit|preventDefault={startTraining}>
          <div class="form-group">
            <label for="model-name">Model Name</label>
            <input
              id="model-name"
              type="text"
              bind:value={newModelName}
              placeholder="e.g., hey_jarvis, computer, alexa"
              required
            />
            <span class="help-text">Unique identifier for this wake word</span>
          </div>

          <div class="form-group">
            <label>Trigger Phrases</label>
            <div class="phrases-list">
              {#each newModelPhrases as phrase, i}
                <div class="phrase-input">
                  <input
                    type="text"
                    bind:value={newModelPhrases[i]}
                    placeholder={`Phrase ${i + 1} (e.g., "hey jarvis")`}
                    required
                  />
                  {#if newModelPhrases.length > 1}
                    <button type="button" class="btn btn-sm btn-ghost btn-danger" on:click={() => removePhrase(i)}>
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <line x1="18" y1="6" x2="6" y2="18" />
                        <line x1="6" y1="6" x2="18" y2="18" />
                      </svg>
                    </button>
                  {/if}
                </div>
              {/each}
            </div>
            <button type="button" class="btn btn-sm btn-secondary" on:click={addPhrase}>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <line x1="12" y1="5" x2="12" y2="19" />
                <line x1="5" y1="12" x2="19" y2="12" />
              </svg>
              Add Phrase
            </button>
            <span class="help-text">Add multiple variations of the wake word (e.g., "hey jarvis", "jarvis", "okay jarvis")</span>
          </div>

          <div class="form-row">
            <div class="form-group">
              <label for="threshold">Detection Threshold</label>
              <input
                id="threshold"
                type="range"
                min="0.1"
                max="0.9"
                step="0.05"
                bind:value={newModelThreshold}
              />
              <span class="range-value">{newModelThreshold.toFixed(2)}</span>
              <span class="help-text">Lower = more sensitive, more false positives</span>
            </div>

            <div class="form-group">
              <label for="sensitivity">Sensitivity</label>
              <input
                id="sensitivity"
                type="range"
                min="0.1"
                max="0.9"
                step="0.05"
                bind:value={newModelSensitivity}
              />
              <span class="range-value">{newModelSensitivity.toFixed(2)}</span>
              <span class="help-text">Adjusts threshold dynamically</span>
            </div>
          </div>

          <button type="submit" class="btn btn-primary" disabled={!newModelName.trim() || newModelPhrases.filter(p => p.trim()).length === 0}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z" />
              <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
              <line x1="12" y1="19" x2="12" y2="22" />
            </svg>
            Start Training
          </button>
        </form>
      {:else if trainingStep === 1}
        <div class="recording-panel">
          <div class="recording-status">
            <div class="recording-indicator" class:pulse={isRecording}>
              <span class="recording-dot" class:active={isRecording}></span>
              {isRecording ? 'Recording...' : 'Paused'}
            </div>
            <div class="recording-time">{formatTime(recordingTime)}</div>
          </div>

          <div class="recording-instructions">
            <p>Speak your wake word phrases clearly. Record at least 3-5 samples per phrase.</p>
            <p>Total samples recorded: <strong>{recordedSamples.length}</strong></p>
          </div>

          <div class="recording-controls">
            <button
              class="btn btn-primary"
              on:click={isRecording ? stopRecording : startRecording}
              disabled={trainingStep !== 1}
            >
              {isRecording
                ? '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" /></svg> Stop Recording'
                : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10" /><polygon points="10 8 16 12 10 16 10 8" /></svg> Start Recording'}
            </button>
            <button class="btn btn-secondary" on:click={finishTraining} disabled={recordedSamples.length === 0}>
              Finish & Train ({recordedSamples.length} samples)
            </button>
            <button class="btn btn-ghost" on:click={cancelTraining}>
              Cancel
            </button>
          </div>

          {#if recordedSamples.length > 0}
            <div class="waveform-preview">
              <canvas id="waveform" width="600" height="100"></canvas>
            </div>
          {/if}
        </div>
      {:else if trainingStep === 2}
        <div class="success-panel">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <polyline points="20 6 9 17 4 12" />
          </svg>
          <h4>Model "{newModelName}" trained successfully!</h4>
          <p>You can now use this wake word to activate JARVIS.</p>
          <button class="btn btn-primary" on:click={() => { training = false; trainingStep = 0; newModelName = ''; newModelPhrases = [''] }}>
            Create Another
          </button>
        </div>
      {/if}
    </section>
  {/if}
</div>

<style>
  .wake-word-training {
    display: flex;
    flex-direction: column;
    gap: var(--space-6);
    padding: var(--space-6);
  }

  .panel-header {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  .panel-header h2 {
    font-size: var(--text-xl);
    font-weight: 600;
    color: var(--text-primary);
    margin: 0;
  }

  .subtitle {
    font-size: var(--text-base);
    color: var(--text-tertiary);
    margin: 0;
  }

  .section {
    background: var(--bg-secondary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-lg);
    padding: var(--space-5);
  }

  .section h3 {
    margin: 0 0 var(--space-4);
    font-size: var(--text-lg);
    font-weight: 600;
    color: var(--text-primary);
  }

  /* Models Grid */
  .models-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
    gap: var(--space-4);
  }

  .model-card {
    background: var(--bg-tertiary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-md);
    padding: var(--space-4);
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }

  .model-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--space-3);
  }

  .model-info h4 {
    margin: 0 0 var(--space-1);
    font-size: var(--text-base);
    font-weight: 600;
    color: var(--text-primary);
  }

  .model-phrases {
    margin: 0;
    font-size: var(--text-sm);
    color: var(--text-secondary);
    max-width: 300px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .status-dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    flex-shrink: 0;
    margin-top: 2px;
  }

  .model-stats {
    display: flex;
    gap: var(--space-4);
    padding-top: var(--space-3);
    border-top: 1px solid var(--border-primary);
  }

  .stat {
    display: flex;
    flex-direction: column;
    align-items: center;
    flex: 1;
  }

  .stat-value {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: var(--text-lg);
    font-weight: 600;
    color: var(--text-primary);
  }

  .stat-label {
    font-size: var(--text-xs);
    color: var(--text-tertiary);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }

  .model-actions {
    display: flex;
    gap: var(--space-2);
    padding-top: var(--space-2);
  }

  .btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: var(--space-2);
    padding: var(--space-2) var(--space-4);
    border-radius: var(--radius-md);
    font-size: var(--text-sm);
    font-weight: 500;
    font-family: inherit;
    cursor: pointer;
    transition: all 0.15s ease;
    border: none;
  }

  .btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .btn-primary {
    background: var(--accent-primary);
    color: var(--bg-primary);
  }

  .btn-primary:hover:not(:disabled) {
    filter: brightness(1.1);
  }

  .btn-secondary {
    background: var(--bg-tertiary);
    color: var(--text-primary);
    border: 1px solid var(--border-primary);
  }

  .btn-secondary:hover:not(:disabled) {
    background: var(--bg-secondary);
  }

  .btn-ghost {
    background: transparent;
    color: var(--text-secondary);
  }

  .btn-ghost:hover:not(:disabled) {
    background: var(--bg-tertiary);
    color: var(--text-primary);
  }

  .btn-danger {
    color: var(--status-error);
  }

  .btn-danger:hover:not(:disabled) {
    background: var(--status-error-light);
  }

  .btn-sm {
    padding: var(--space-1) var(--space-3);
    font-size: var(--text-xs);
  }

  .btn svg {
    width: 16px;
    height: 16px;
  }

  .btn.confirming {
    background: var(--status-error-light);
    color: var(--status-error);
  }

  .model-meta {
    font-size: var(--text-xs);
    color: var(--text-tertiary);
    padding-top: var(--space-2);
    border-top: 1px solid var(--border-primary);
  }

  /* Empty State */
  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: var(--space-12);
    gap: var(--space-4);
    color: var(--text-tertiary);
    text-align: center;
  }

  .empty-state svg {
    width: 48px;
    height: 48px;
    opacity: 0.5;
  }

  .empty-state p {
    font-size: var(--text-lg);
    color: var(--text-secondary);
    margin: 0;
  }

  .empty-state span {
    font-size: var(--text-sm);
  }

  /* Training Form */
  .training-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
    max-width: 600px;
  }

  .form-group {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  .form-group label {
    font-size: var(--text-sm);
    font-weight: 500;
    color: var(--text-secondary);
  }

  .form-group input[type="text"],
  .form-group input[type="range"] {
    padding: var(--space-2) var(--space-3);
    background: var(--bg-tertiary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-md);
    color: var(--text-primary);
    font-family: inherit;
    font-size: var(--text-base);
  }

  .form-group input[type="text"]:focus {
    outline: none;
    border-color: var(--accent-primary);
  }

  .form-group input[type="range"] {
    padding: 0;
    -webkit-appearance: none;
    appearance: none;
    height: 6px;
    background: var(--bg-primary);
    border-radius: 3px;
  }

  .form-group input[type="range"]::-webkit-slider-thumb {
    -webkit-appearance: none;
    width: 18px;
    height: 18px;
    border-radius: 50%;
    background: var(--accent-primary);
    cursor: pointer;
  }

  .help-text {
    font-size: var(--text-xs);
    color: var(--text-tertiary);
  }

  .form-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: var(--space-4);
  }

  .range-value {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: var(--text-sm);
    color: var(--accent-primary);
    font-weight: 500;
  }

  .phrases-list {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }

  .phrase-input {
    display: flex;
    gap: var(--space-2);
  }

  .phrase-input input {
    flex: 1;
  }

  /* Recording Panel */
  .recording-panel {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
    max-width: 700px;
  }

  .recording-status {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: var(--space-4);
    padding: var(--space-4);
    background: var(--bg-tertiary);
    border-radius: var(--radius-md);
  }

  .recording-indicator {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    font-size: var(--text-lg);
    font-weight: 600;
    color: var(--text-primary);
  }

  .recording-dot {
    width: 12px;
    height: 12px;
    border-radius: 50%;
    background: var(--text-tertiary);
    transition: background 0.2s ease;
  }

  .recording-dot.active {
    background: var(--status-error);
    animation: pulse 1s infinite;
  }

  @keyframes pulse {
    0%, 100% { opacity: 1; transform: scale(1); }
    50% { opacity: 0.5; transform: scale(1.1); }
  }

  .recording-indicator.pulse .recording-dot.active {
    animation: pulse 1s infinite;
  }

  .recording-time {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: var(--text-xl);
    font-weight: 600;
    color: var(--accent-primary);
  }

  .recording-instructions {
    padding: var(--space-4);
    background: var(--accent-primary-light);
    border-radius: var(--radius-md);
    color: var(--accent-primary);
    font-size: var(--text-sm);
    line-height: 1.6;
  }

  .recording-instructions strong {
    color: var(--text-primary);
  }

  .recording-controls {
    display: flex;
    gap: var(--space-3);
    flex-wrap: wrap;
  }

  .waveform-preview {
    padding: var(--space-4);
    background: var(--bg-tertiary);
    border-radius: var(--radius-md);
  }

  .waveform-preview canvas {
    width: 100%;
    height: 100px;
    background: var(--bg-primary);
    border-radius: var(--radius-sm);
  }

  /* Success Panel */
  .success-panel {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: var(--space-12);
    gap: var(--space-4);
    text-align: center;
    background: var(--status-success-light);
    border-radius: var(--radius-md);
    border: 1px solid var(--status-success);
  }

  .success-panel svg {
    width: 48px;
    height: 48px;
    color: var(--status-success);
  }

  .success-panel h4 {
    margin: 0;
    font-size: var(--text-lg);
    font-weight: 600;
    color: var(--text-primary);
  }

  .success-panel p {
    margin: 0;
    color: var(--text-secondary);
  }

  /* Loading/Error States */
  .loading-state,
  .error-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: var(--space-12);
    gap: var(--space-4);
    color: var(--text-tertiary);
    text-align: center;
    grid-column: 1 / -1;
  }

  .loading-state .spinner {
    width: 32px;
    height: 32px;
    border: 3px solid var(--border-primary);
    border-top-color: var(--accent-primary);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }

  @keyframes spin {
    to { transform: rotate(360deg); }
  }

  .error-state svg {
    width: 48px;
    height: 48px;
    color: var(--status-error);
  }

  @media (max-width: 768px) {
    .wake-word-training {
      padding: var(--space-4);
    }

    .models-grid {
      grid-template-columns: 1fr;
    }

    .recording-controls {
      flex-direction: column;
    }

    .recording-controls .btn {
      width: 100%;
    }
  }
</style>