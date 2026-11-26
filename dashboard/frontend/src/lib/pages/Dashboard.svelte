<script lang="ts">
  import { onMount } from 'svelte'
  import { metrics } from '$lib/stores/metrics'
  import { agents } from '$lib/stores/agents'
  import { session } from '$lib/stores/session'
  import { api } from '$lib/api/rest'
  import MetricTile from '$lib/components/metrics/MetricTile.svelte'
  import AgentCard from '$lib/components/agents/AgentCard.svelte'
  import { toast } from '$lib/stores/toast'
  import type { MetricSeries } from '$lib/stores/metrics'
  import type { AgentStatus } from '$lib/stores/agents'
  import type { TradingState, Position } from '$lib/api/rest'

  let tradingState = $state<TradingState | null>(null)
  let positions = $state<Position[]>([])
  let loading = $state(true)
  let systemMetrics = $derived($metrics.systemMetrics)
  let timeRange = $state('5m')

  async function loadData() {
    try {
      const [tState, pos] = await Promise.all([
        api.getTradingState(),
        api.getTradingPositions(),
      ])
      tradingState = tState
      positions = pos
    } catch (e) {
      console.error('Failed to load trading data:', e)
    } finally {
      loading = false
    }
  }

  function updateTimeRange() {
    // TODO: Send time range to WS
    console.log('Time range:', timeRange)
  }

  onMount(() => {
    loadData()
    const interval = setInterval(loadData, 30000)
    return () => clearInterval(interval)
  })
</script>

<div class="dashboard">
  <section class="section" aria-labelledby="metrics-heading">
    <header class="section-header">
      <h2 id="metrics-heading">System Metrics</h2>
      <div class="section-actions">
        <select bind:value={timeRange} onchange={updateTimeRange} aria-label="Time range">
          <option value="1m">1 minute</option>
          <option value="5m">5 minutes</option>
          <option value="15m">15 minutes</option>
          <option value="1h">1 hour</option>
        </select>
      </div>
    </header>
    <div class="metrics-grid">
      <MetricTile
        title="CPU Usage"
        value={systemMetrics.cpu}
        unit="%"
        series={$metrics['cpu']}
        color="var(--accent-primary)"
      />
      <MetricTile
        title="Memory"
        value={systemMetrics.memory}
        unit="%"
        series={$metrics['memory']}
        color="var(--accent-secondary)"
      />
      <MetricTile
        title="LLM Latency (p50)"
        value={$metrics['llm_latency_p50']?.current ?? 0}
        unit="ms"
        series={$metrics['llm_latency_p50']}
        color="var(--status-warning)"
      />
      <MetricTile
        title="LLM Latency (p95)"
        value={$metrics['llm_latency_p95']?.current ?? 0}
        unit="ms"
        series={$metrics['llm_latency_p95']}
        color="var(--status-error)"
      />
      <MetricTile
        title="Agent Success Rate"
        value={$metrics['agent_success_rate']?.current ?? 0}
        unit="%"
        series={$metrics['agent_success_rate']}
        color="var(--status-success)"
      />
      <MetricTile
        title="Token Rate"
        value={$metrics['token_rate']?.rate ?? 0}
        unit="tok/s"
        series={$metrics['token_rate']}
        color="var(--accent-tertiary)"
      />
    </div>
  </section>

  <section class="section" aria-labelledby="trading-heading">
    <header class="section-header">
      <h2 id="trading-heading">Trading</h2>
    </header>
    {#if loading}
      <div class="loading">Loading trading data...</div>
    {:else if tradingState}
      <div class="trading-grid">
        <div class="stat-card">
          <span class="stat-label">Balance</span>
          <span class="stat-value">${tradingState.balance.toFixed(2)}</span>
        </div>
        <div class="stat-card">
          <span class="stat-label">Equity</span>
          <span class="stat-value">${tradingState.equity.toFixed(2)}</span>
        </div>
        <div class="stat-card">
          <span class="stat-label">PnL</span>
          <span class="stat-value" class:positive={tradingState.pnl >= 0} class:negative={tradingState.pnl < 0}>
            ${tradingState.pnl.toFixed(2)}
          </span>
        </div>
        <div class="stat-card">
          <span class="stat-label">Open Positions</span>
          <span class="stat-value">{tradingState.positions}</span>
        </div>
      </div>
      {#if positions.length > 0}
        <div class="positions-table-container">
          <table class="positions-table">
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Side</th>
                <th>Size</th>
                <th>Entry</th>
                <th>Mark</th>
                <th>PnL</th>
              </tr>
            </thead>
            <tbody>
              {#each positions as pos}
                <tr>
                  <td>{pos.symbol}</td>
                  <td><span class="badge" class:long={pos.side === 'long'} class:short={pos.side === 'short'}>{pos.side}</span></td>
                  <td>{pos.size}</td>
                  <td>${pos.entryPrice.toFixed(4)}</td>
                  <td>${pos.markPrice.toFixed(4)}</td>
                  <td class:positive={pos.pnl >= 0} class:negative={pos.pnl < 0}>${pos.pnl.toFixed(2)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    {/if}
  </section>

  <section class="section" aria-labelledby="agents-heading">
    <header class="section-header">
      <h2 id="agents-heading">Active Agents</h2>
      <a href="/agents" class="link">View all →</a>
    </header>
    <div class="agents-preview">
      {#each $agents.slice(0, 4) as agent}
        <AgentCard {agent} compact />
      {/each}
    </div>
  </section>
</div>

<style>
  .dashboard {
    display: flex;
    flex-direction: column;
    gap: var(--space-6);
  }

  .section {
    background: var(--bg-secondary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-lg);
    overflow: hidden;
  }

  .section-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--space-4) var(--space-6);
    border-bottom: 1px solid var(--border-primary);
  }

  .section-header h2 {
    font-size: var(--text-lg);
    font-weight: 600;
    color: var(--text-primary);
  }

  .section-actions select {
    padding: var(--space-2) var(--space-3);
    background: var(--bg-tertiary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-md);
    color: var(--text-primary);
    font-size: var(--text-sm);
  }

  .link {
    color: var(--accent-primary);
    text-decoration: none;
    font-size: var(--text-sm);
  }

  .link:hover {
    text-decoration: underline;
  }

  .metrics-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: var(--space-4);
    padding: var(--space-4) var(--space-6);
  }

  .trading-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: var(--space-4);
    padding: var(--space-4) var(--space-6);
  }

  .stat-card {
    padding: var(--space-4);
    background: var(--bg-tertiary);
    border-radius: var(--radius-md);
  }

  .stat-label {
    display: block;
    font-size: var(--text-sm);
    color: var(--text-tertiary);
    margin-bottom: var(--space-1);
  }

  .stat-value {
    font-size: var(--text-xl);
    font-weight: 600;
    color: var(--text-primary);
    font-variant-numeric: tabular-nums;
  }

  .stat-value.positive { color: var(--status-success); }
  .stat-value.negative { color: var(--status-error); }

  .positions-table-container {
    overflow-x: auto;
    padding: var(--space-4) var(--space-6);
  }

  .positions-table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--text-sm);
  }

  .positions-table th,
  .positions-table td {
    padding: var(--space-3) var(--space-4);
    text-align: left;
    border-bottom: 1px solid var(--border-primary);
  }

  .positions-table th {
    color: var(--text-tertiary);
    font-weight: 500;
  }

  .badge {
    display: inline-block;
    padding: var(--space-1) var(--space-2);
    border-radius: var(--radius-full);
    font-size: var(--text-xs);
    font-weight: 500;
  }

  .badge.long { background: var(--status-success); color: var(--bg-primary); }
  .badge.short { background: var(--status-error); color: var(--bg-primary); }

  td.positive { color: var(--status-success); }
  td.negative { color: var(--status-error); }

  .agents-preview {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: var(--space-3);
    padding: var(--space-4) var(--space-6);
  }

  .loading {
    padding: var(--space-8);
    text-align: center;
    color: var(--text-tertiary);
  }
</style>
