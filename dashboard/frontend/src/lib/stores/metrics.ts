import { writable, derived } from 'svelte/store'

export interface MetricPoint {
  timestamp: number
  value: number
}

export interface MetricSeries {
  name: string
  unit: string
  points: MetricPoint[]
  current: number
  p50?: number
  p95?: number
  rate?: number
}

export interface SystemMetrics {
  cpu: number
  memory: number
  disk: number
}

function createMetricsStore() {
  const { subscribe, set, update } = writable<Record<string, MetricSeries>>({})
  const systemMetrics = writable<SystemMetrics>({ cpu: 0, memory: 0, disk: 0 })

  const MAX_POINTS = 180 // 3 minutes at 1s interval

  return {
    subscribe,
    systemMetrics,
    ingest: (metrics: Record<string, { value: number; unit: string; p50?: number; p95?: number; rate?: number }>) => {
      const now = Date.now()
      update(series => {
        const next = { ...series }
        for (const [name, data] of Object.entries(metrics)) {
          const existing = next[name] || { name, unit: data.unit, points: [], current: 0 }
          const points = [...existing.points, { timestamp: now, value: data.value }]
          if (points.length > MAX_POINTS) points.shift()
          next[name] = { ...existing, points, current: data.value, p50: data.p50, p95: data.p95, rate: data.rate }
        }
        return next
      })
    },
    ingestSystem: (sys: SystemMetrics) => systemMetrics.set(sys),
    clear: () => set({}),
  }
}

export const metrics = createMetricsStore()

export const metricNames = derived(metrics, $m => Object.keys($m))