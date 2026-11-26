<script lang="ts">
  import { onMount, onDestroy } from 'svelte'
  import * as THREE from 'three'
  import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'

  export let state: 'idle' | 'listening' | 'thinking' | 'speaking' = 'idle'
  export let audioLevel = 0

  let canvas: HTMLCanvasElement
  let scene: THREE.Scene
  let camera: THREE.PerspectiveCamera
  let renderer: THREE.WebGLRenderer
  let controls: OrbitControls
  let animationId: number
  let particles: THREE.Points | null = null
  let faceMesh: THREE.Mesh | null = null
  let reactorCore: THREE.Group | null = null
  let mode: 'face' | 'reactor' = 'face'
  let fullscreen = false

  const stateColors = {
    idle: new THREE.Color(0x00a3ff),
    listening: new THREE.Color(0x00ff88),
    thinking: new THREE.Color(0xffaa00),
    speaking: new THREE.Color(0xff4444),
  }

  onMount(() => {
    if (!canvas) return
    initThree()
    animationId = requestAnimationFrame(animate)
  })

  onDestroy(() => {
    cancelAnimationFrame(animationId)
    renderer?.dispose()
    scene?.traverse(obj => {
      if (obj instanceof THREE.Mesh) {
        obj.geometry?.dispose()
        ;(obj.material as THREE.Material | THREE.Material[]).forEach?.(m => m.dispose?.())
      }
    })
  })

  function initThree() {
    scene = new THREE.Scene()
    scene.background = new THREE.Color(0x0a0a0f)

    camera = new THREE.PerspectiveCamera(60, canvas.clientWidth / canvas.clientHeight, 0.1, 100)
    camera.position.set(0, 0, 5)

    renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true })
    renderer.setSize(canvas.clientWidth, canvas.clientHeight)
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))

    controls = new OrbitControls(camera, canvas)
    controls.enableDamping = true
    controls.dampingFactor = 0.05
    controls.enablePan = false
    controls.minDistance = 3
    controls.maxDistance = 10

    // Lights
    const ambient = new THREE.AmbientLight(0x404060, 0.5)
    scene.add(ambient)

    const dirLight = new THREE.DirectionalLight(0xffffff, 1)
    dirLight.position.set(5, 10, 7)
    scene.add(dirLight)

    createFace()
    createReactor()

    window.addEventListener('resize', onResize)
  }

  function createFace() {
    const geometry = new THREE.SphereGeometry(1.5, 64, 64)
    const material = new THREE.MeshPhysicalMaterial({
      color: 0x00a3ff,
      metalness: 0.3,
      roughness: 0.4,
      transmission: 0.3,
      thickness: 0.5,
      clearcoat: 1,
      clearcoatRoughness: 0.1,
    })
    faceMesh = new THREE.Mesh(geometry, material)
    faceMesh.visible = true
    scene.add(faceMesh)

    // Add glow ring
    const ringGeo = new THREE.RingGeometry(1.8, 2.2, 64)
    const ringMat = new THREE.MeshBasicMaterial({
      color: 0x00a3ff,
      transparent: true,
      opacity: 0.3,
      side: THREE.DoubleSide,
    })
    const ring = new THREE.Mesh(ringGeo, ringMat)
    ring.rotation.x = -Math.PI / 2
    ring.position.y = -0.2
    faceMesh.add(ring)
    ;(faceMesh as any).ring = ring
  }

  function createReactor() {
    reactorCore = new THREE.Group()

    // Core sphere
    const coreGeo = new THREE.SphereGeometry(0.5, 32, 32)
    const coreMat = new THREE.MeshBasicMaterial({
      color: 0x00a3ff,
      transparent: true,
      opacity: 0.8,
    })
    const core = new THREE.Mesh(coreGeo, coreMat)
    reactorCore.add(core)
    ;(reactorCore as any).core = core

    // Particle system
    const particleCount = 2000
    const positions = new Float32Array(particleCount * 3)
    const sizes = new Float32Array(particleCount)
    const colors = new Float32Array(particleCount * 3)

    for (let i = 0; i < particleCount; i++) {
      const r = 0.5 + Math.random() * 2.5
      const theta = Math.random() * Math.PI * 2
      const phi = Math.acos(2 * Math.random() - 1)
      positions[i * 3] = r * Math.sin(phi) * Math.cos(theta)
      positions[i * 3 + 1] = r * Math.sin(phi) * Math.sin(theta)
      positions[i * 3 + 2] = r * Math.cos(phi)
      sizes[i] = Math.random() * 2 + 0.5
      colors[i * 3] = 0
      colors[i * 3 + 1] = 0.64 + Math.random() * 0.36
      colors[i * 3 + 2] = 1
    }

    const particleGeo = new THREE.BufferGeometry()
    particleGeo.setAttribute('position', new THREE.BufferAttribute(positions, 3))
    particleGeo.setAttribute('size', new THREE.BufferAttribute(sizes, 1))
    particleGeo.setAttribute('color', new THREE.BufferAttribute(colors, 3))

    const particleMat = new THREE.PointsMaterial({
      size: 0.05,
      vertexColors: true,
      transparent: true,
      opacity: 0.6,
      blending: THREE.AdditiveBlending,
      sizeAttenuation: true,
    })

    particles = new THREE.Points(particleGeo, particleMat)
    reactorCore.add(particles)
    reactorCore.visible = false
    scene.add(reactorCore)
  }

  function onResize() {
    if (!canvas || !camera || !renderer) return
    camera.aspect = canvas.clientWidth / canvas.clientHeight
    camera.updateProjectionMatrix()
    renderer.setSize(canvas.clientWidth, canvas.clientHeight)
  }

  function animate() {
    animationId = requestAnimationFrame(animate)

    const time = performance.now() * 0.001
    const color = stateColors[state]

    if (faceMesh && faceMesh.visible) {
      faceMesh.rotation.y += 0.002
      faceMesh.rotation.x = Math.sin(time * 0.5) * 0.1

      const pulse = 1 + Math.sin(time * 3) * 0.05 * getStateIntensity()
      faceMesh.scale.setScalar(pulse)

      ;(faceMesh.material as THREE.MeshPhysicalMaterial).color.lerp(color, 0.1)
      ;((faceMesh as any).ring.material as THREE.MeshBasicMaterial).color.lerp(color, 0.1)
      ;((faceMesh as any).ring.material as THREE.MeshBasicMaterial).opacity = 0.3 + getStateIntensity() * 0.4
    }

    if (reactorCore && reactorCore.visible) {
      reactorCore.rotation.y += 0.001

      const core = (reactorCore as any).core
      if (core) {
        const pulse = 1 + Math.sin(time * 4) * 0.1 * getStateIntensity()
        core.scale.setScalar(pulse)
        ;(core.material as THREE.MeshBasicMaterial).color.lerp(color, 0.1)
      }

      if (particles) {
        particles.rotation.y += 0.0005
        particles.rotation.x += 0.0002

        const positions = particles.geometry.attributes.position.array as Float32Array
        const intensities = new Float32Array(positions.length / 3)
        for (let i = 0; i < positions.length / 3; i++) {
          const y = positions[i * 3 + 1]
          const wave = Math.sin(time * 2 + y * 2 + i * 0.1) * 0.5 + 0.5
          positions[i * 3 + 1] += (wave - 0.5) * 0.002 * getStateIntensity()
        }
        particles.geometry.attributes.position.needsUpdate = true

        const colorAttr = particles.geometry.attributes.color.array as Float32Array
        for (let i = 0; i < colorAttr.length; i += 3) {
          colorAttr[i] = color.r * (0.5 + getStateIntensity() * 0.5)
          colorAttr[i + 1] = color.g * (0.5 + getStateIntensity() * 0.5)
          colorAttr[i + 2] = color.b
        }
        particles.geometry.attributes.color.needsUpdate = true
        ;(particles.material as THREE.PointsMaterial).opacity = 0.4 + getStateIntensity() * 0.4
      }
    }

    // Audio-reactive pulse
    if (audioLevel > 0.01) {
      const boost = 1 + audioLevel * 2
      if (faceMesh && faceMesh.visible) faceMesh.scale.multiplyScalar(boost)
      if (reactorCore && reactorCore.visible) {
        const core = (reactorCore as any).core
        if (core) core.scale.multiplyScalar(boost)
      }
    }

    controls.update()
    renderer.render(scene, camera)
  }

  function getStateIntensity(): number {
    switch (state) {
      case 'idle': return 0.2
      case 'listening': return 0.6
      case 'thinking': return 0.8
      case 'speaking': return 1.0
      default: return 0.2
    }
  }

  $: {
    if (faceMesh) faceMesh.visible = mode === 'face'
    if (reactorCore) reactorCore.visible = mode === 'reactor'
  }

  function toggleFullscreen() {
    const container = canvas?.parentElement
    if (!container) return

    if (!fullscreen) {
      container.requestFullscreen?.()
    } else {
      document.exitFullscreen?.()
    }
    fullscreen = !fullscreen
  }

  document.addEventListener('fullscreenchange', () => {
    if (!document.fullscreenElement) {
      fullscreen = false
    }
  })
</script>

<div class="hud-canvas-container" class:fullscreen={fullscreen}>
  <canvas
    bind:this={canvas}
    class="hud-canvas"
    ondblclick={toggleFullscreen}
    title="Double-click for fullscreen"
  ></canvas>
  <div class="hud-overlay">
    <div class="hud-status">{state.toUpperCase()}</div>
    <div class="hud-mode">{mode.toUpperCase()}</div>
    <div class="hud-hint">Double-click for fullscreen</div>
  </div>
</div>

<style>
  .hud-canvas-container {
    width: 100%;
    height: 100%;
    position: relative;
    border-radius: var(--radius-lg);
    overflow: hidden;
    background: #0a0a0f;
  }

  .hud-canvas {
    width: 100%;
    height: 100%;
    display: block;
    cursor: grab;
  }

  .hud-canvas:active {
    cursor: grabbing;
  }

  .hud-overlay {
    position: absolute;
    top: var(--space-4);
    left: var(--space-4);
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    pointer-events: none;
  }

  .hud-status {
    font-family: 'JetBrains Mono', monospace;
    font-size: var(--text-xs);
    font-weight: 600;
    padding: var(--space-1) var(--space-2);
    background: rgba(0, 0, 0, 0.6);
    border-radius: var(--radius-sm);
    text-transform: uppercase;
    letter-spacing: 0.1em;
  }

  .hud-mode {
    font-family: 'JetBrains Mono', monospace;
    font-size: var(--text-xs);
    color: var(--text-tertiary);
    padding: var(--space-1) var(--space-2);
    background: rgba(0, 0, 0, 0.4);
    border-radius: var(--radius-sm);
  }

  .hud-hint {
    font-size: var(--text-xs);
    color: var(--text-tertiary);
    opacity: 0.6;
  }

  .hud-canvas-container.fullscreen {
    position: fixed;
    inset: 0;
    z-index: 9999;
    border-radius: 0;
    background: #000;
  }
</style>
