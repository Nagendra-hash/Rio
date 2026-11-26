<script lang="ts">
  import { metrics } from '$lib/stores/metrics'
  import MetricTile from '$lib/components/metrics/MetricTile.svelte'
  import type { MetricSeries } from '$lib/stores/metrics'

  let timeRange = '5m'

  const metricGroups = [
    {
      title: 'LLM Metrics',
      keys: [
        { key: 'llm_latency_p50', title: 'LLM Latency (p50)', unit: 'ms', color: 'var(--status-warning)' },
        { key: 'llm_latency_p95', title: 'LLM Latency (p95)', unit: 'ms', color: 'var(--status-error)' },
        { key: 'llm_tokens_total', title: 'Token Rate', unit: 'tok/s', color: 'var(--accent-primary)', useRate: true },
        { key: 'llm_calls_total', title: 'Call Rate', unit: 'calls/s', color: 'var(--accent-secondary)', useRate: true },
      ]
    },
    {
      title: 'Agent Metrics',
      keys: [
        { key: 'agent_invocations_total', title: 'Invocation Rate', unit: 'inv/s', color: 'var(--accent-primary)', useRate: true },
        { key: 'agent_success_rate', title: 'Success Rate', unit: '%', color: 'var(--status-success)' },
        { key: 'agent_latency_p50', title: 'Latency (p50)', unit: 'ms', color: 'var(--status-warning)' },
        { key: 'agent_latency_p95', title: 'Latency (p95)', unit: 'ms', color: 'var(--status-error)' },
      ]
    },
    {
      title: 'Tool Metrics',
      keys: [
        { key: 'tool_calls_total', title: 'Tool Call Rate', unit: 'calls/s', color: 'var(--accent-tertiary)', useRate: true },
        { key: 'tool_errors_total', title: 'Error Rate', unit: 'err/s', color: 'var(--status-error)', useRate: true },
        { key: 'tool_latency_p50', title: 'Latency (p50)', unit: 'ms', color: 'var(--status-warning)' },
      ]
    },
    {
      title: 'System Metrics',
      keys: [
        { key: 'cpu', title: 'CPU Usage', unit: '%', color: 'var(--accent-primary)' },
        { key: 'memory', title: 'Memory', unit: '%', color: 'var(--accent-secondary)' },
        { key: 'disk', title: 'Disk', unit: '%', color: 'var(--accent-tertiary)' },
        { key: 'ws_reconnects_total', title: 'WS Reconnects', unit: '', color: 'var(--status-error)' },
      ]
    },
    {
      title: 'Backtest / Trading',
      keys: [
        { key: 'jobs_queued', title: 'Queued Jobs', unit: '', color: 'var(--accent-primary)' },
        { key: 'jobs_running', title: 'Running Jobs', unit: '', color: 'var(--status-warning)' },
        { key: 'trading_pnl', title: 'Trading PnL', unit: '$', color: 'var(--status-success)' },
      ]
    }
  ]
</script>

<div class="observability-page">
  <div class="page-header">
    <h1>Observability</h1>
    <p class="page-subtitle">Real-time system metrics and performance monitoring</p>
  </div>

  <div class="controls">
    <select bind:value={timeRange} aria-label="Time range">
      <option value="1m">1 minute</option>
      <option value="5m">5 minutes</option>
      <option value="15m">15 minutes</option>
      <option value="1h">1 hour</option>
    </select>
    <button class="btn btn-secondary" on:click={refreshMetrics}>Refresh</button>
  </div>

  {#each metricGroups as group}
    <section class="metric-group">
      <h2>{group.title}</h2>
      <div class="metrics-grid">
        {#each group.keys as m}
          <svelte:component
            this={MetricTile}
            title={m.title}
            value={($metrics[m.key]?.current ?? 0)}
            unit={m.unit}
            series={$metrics[m.key]}
            color={m.color}
          />
        {/each}
      </div>
    </section>
  {/each}
</div>

<style>
  .observability-page {
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

  .controls {
    display: flex;
    align-items: center;
    gap: var(--space-3);
  }

  .controls select {
    padding: var(--space-2) var(--space-3);
    background: var(--bg-tertiary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-md);
    color: var(--text-primary);
    font-size: var(--text-sm);
  }

  .btn {
    padding: var(--space-2) var(--space-4);
    border-radius: var(--radius-md);
    font-weight: 500;
    border: none;
    cursor: pointer;
  }

  .btn-secondary {
    background: var(--bg-tertiary);
    color: var(--text-primary);
    border: 1px solid var(--border-primary);
  }

  .btn-secondary:hover {
    background: var(--border-primary);
  }

  .metric-group {
    background: var(--bg-secondary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-lg);
    padding: var(--space-6);
  }

  .metric-group h2 {
    font-size: var(--text-lg);
    font-weight: 600;
    color: var(--text-primary);
    margin-bottom: var(--space-4);
    padding-bottom: var(--space-3);
    border-bottom: 1px solid var(--border-primary);
  }

  .metrics-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: var(--space-4);
  }
</style>

<script>
  function refreshMetrics() {
    // Trigger a metrics refresh via WS
    console.log('Refresh metrics for:', timeRange)
  }
</script>