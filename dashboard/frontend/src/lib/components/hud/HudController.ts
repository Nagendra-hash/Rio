import { ws } from '$lib/api/ws'
import { config } from '$lib/stores/config'

type HudState = 'idle' | 'listening' | 'thinking' | 'speaking'

class HudController {
  private currentState: HudState = 'idle'
  private audioContext: AudioContext | null = null
  private analyser: AnalyserNode | null = null
  private dataArray: Uint8Array | null = null
  private animationFrame: number | null = null
  private callbacks = new Set<(state: HudState, audioLevel: number) => void>()
  private webglEnabled = false

  async init() {
    // Check if WebGL HUD is enabled
    const cfg = await config.load()
    this.webglEnabled = cfg?.ui?.webglHud ?? false

    // Listen for WS events
    ws.on('thinking_start', () => this.setState('thinking'))
    ws.on('thinking_end', () => this.setState('idle'))
    ws.on('speech_start', () => this.setState('speaking'))
    ws.on('speech_end', () => this.setState('listening'))
    ws.on('wake_word_detected', () => this.setState('listening'))
    ws.on('user_speech_start', () => this.setState('listening'))
    ws.on('user_speech_end', () => this.setState('thinking'))

    // Initialize audio analyser for microphone visualization
    await this.initAudio()
  }

  private async initAudio() {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false })
      this.audioContext = new AudioContext()
      this.analyser = this.audioContext.createAnalyser()
      this.analyser.fftSize = 256
      this.dataArray = new Uint8Array(this.analyser.frequencyBinCount)

      const source = this.audioContext.createMediaStreamSource(stream)
      source.connect(this.analyser)

      this.startAudioLoop()
    } catch (e) {
      console.warn('[HUD] Could not initialize audio analyser:', e)
    }
  }

  private startAudioLoop() {
    const loop = () => {
      if (!this.analyser || !this.dataArray) return

      this.analyser.getByteFrequencyData(this.dataArray)

      // Calculate average volume (0-1)
      let sum = 0
      for (let i = 0; i < this.dataArray.length; i++) {
        sum += this.dataArray[i]
      }
      const avg = sum / this.dataArray.length / 255

      this.callbacks.forEach(cb => cb(this.currentState, avg))
      this.animationFrame = requestAnimationFrame(loop)
    }
    loop()
  }

  setState(state: HudState) {
    this.currentState = state
  }

  getState(): HudState {
    return this.currentState
  }

  isWebglEnabled(): boolean {
    return this.webglEnabled
  }

  onUpdate(callback: (state: HudState, audioLevel: number) => void) {
    this.callbacks.add(callback)
    return () => this.callbacks.delete(callback)
  }

  destroy() {
    if (this.animationFrame) cancelAnimationFrame(this.animationFrame)
    this.audioContext?.close()
    this.callbacks.clear()
  }
}

export const hudController = new HudController()