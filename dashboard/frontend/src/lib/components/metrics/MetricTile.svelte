<script lang="ts">
  import * as uPlot from 'uplot'
  import { onMount, onDestroy } from 'svelte'
  import type { MetricSeries } from '$lib/stores/metrics'

  export let title: string
  export let value: number
  export let unit: string = ''
  export let series: MetricSeries | undefined
  export let color: string = 'var(--accent-primary)'

  let u: uPlot | null = null
  let canvas: HTMLCanvasElement

  $: formattedValue = formatValue(value)

  function formatValue(v: number): string {
    if (v >= 1e9) return (v / 1e9).toFixed(2) + 'B'
    if (v >= 1e6) return (v / 1e6).toFixed(2) + 'M'
    if (v >= 1e3) return (v / 1e3).toFixed(2) + 'K'
    if (v < 1 && v > 0) return v.toFixed(4)
    return v.toFixed(v % 1 === 0 ? 0 : 2)
  }

  onMount(() => {
    if (!canvas) return
    const data: [number[], number[]] = [[], []]
    if (series?.points) {
      const now = Date.now()
      const cutoff = now - 60000 // 1 minute window
      const filtered = series.points.filter(p => p.timestamp > cutoff)
      data[0] = filtered.map(p => p.timestamp)
      data[1] = filtered.map(p => p.value)
    }
    u = new uPlot({
      width: 240,
      height: 80,
      scales: {
        x: { time: true },
        y: { auto: true },
      },
      series: [
        {},
        {
          stroke: color,
          fill: color + '20',
          width: 2,
          points: { show: false },
        },
      ],
      axes: [
        { show: false },
        { show: false },
      ],
      hooks: {
        draw: [
          (u: uPlot) => {
            // Draw current value label
            if (series?.points.length) {
              const last = series.points[series.points.length - 1]
              const [x, y] = [u.posToVal(last.timestamp, 'x'), u.posToVal(last.value, 'y')]
              const [px, py] = [u.valToPos(x, 'x'), u.valToPos(y, 'y')]
              u.ctx.fillStyle = color
              u.ctx.font = '10px monospace'
              u.ctx.textAlign = 'right'
              u.ctx.fillText(formattedValue + unit, px - 4, py - 4)
            }
          },
        ],
      },
    }, data, canvas)
  })

  onDestroy(() => {
    u?.destroy()
    u = null
  })

  $: if (u && series?.points) {
    const now = Date.now()
    const cutoff = now - 60000
    const filtered = series.points.filter(p => p.timestamp > cutoff)
    const data: [number[], number[]] = [
      filtered.map(p => p.timestamp),
      filtered.map(p => p.value),
    ]
    u.setData(data)
    u.redraw()
  }
</script>

<div class="metric-tile">
  <div class="tile-header">
    <h3>{title}</h3>
    <span class="value">{formattedValue}<span class="unit">{unit}</span></span>
  </div>
  <div class="tile-chart">
    <canvas bind:this={canvas} width="240" height="80"></canvas>
  </div>
  <div class="tile-footer">
    {#if series?.p50 !== undefined || series?.p95 !== undefined}
      <span class="percentile">p50: {series.p50?.toFixed(1) ?? '—'}{unit} · p95: {series.p95?.toFixed(1) ?? '—'}{unit}</span>
    {:else if series?.rate !== undefined}
      <span class="rate">{series.rate.toFixed(1)}/s</span>
    {/if}
  </div>
</div>

<style>
  .metric-tile {
    background: var(--bg-tertiary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-md);
    padding: var(--space-4);
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }

  .tile-header {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--space-4);
  }

  .tile-header h3 {
    font-size: var(--text-sm);
    font-weight: 500;
    color: var(--text-secondary);
  }

  .value {
    font-size: var(--text-2xl);
    font-weight: 700;
    color: var(--text-primary);
    font-variant-numeric: tabular-nums;
  }

  .unit {
    font-size: var(--text-sm);
    font-weight: 400;
    color: var(--text-tertiary);
    margin-left: var(--space-1);
  }

  .tile-chart {
    height: 80px;
    width: 100%;
  }

  .tile-footer {
    display: flex;
    justify-content: flex-end;
  }

  .percentile,
  .rate {
    font-size: var(--text-xs);
    color: var(--text-tertiary);
  }
</style>