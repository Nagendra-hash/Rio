<script lang="ts">
  import { onMount } from 'svelte'
  import { api } from '$lib/api/rest'

  // State
  let stats: VoiceStats | null = null
  let trends: VoiceTrendsResponse | null = null
  let hourly: VoiceHourlyResponse | null = null
  let sessions: VoiceSessionsResponse | null = null
  let loading = false
  let error: string | null = null
  let days = 30

  // Chart data (computed)
  $: topIntentsChartData = stats?.top_intents?.slice(0, 10).map(item => ({
    label: item.intent,
    value: item.count
  })) || []

  $: topActionsChartData = stats?.top_actions?.slice(0, 10).map(item => ({
    label: item.action,
    value: item.count
  })) || []

  $: trendsChartData = trends?.trends?.map(t => ({
    intent: t.intent,
    data: t.data
  })) || []

  $: hourlyChartData = hourly?.hourly?.map(h => ({
    hour: h.hour,
    count: h.count
  })) || []

  async function loadAnalytics() {
    loading = true
    error = null
    try {
      const [statsRes, trendsRes, hourlyRes, sessionsRes] = await Promise.all([
        api.getVoiceStats(days),
        api.getVoiceTrends(days, 8),
        api.getVoiceHourly(days),
        api.getVoiceSessions(days, 20)
      ])
      stats = statsRes
      trends = trendsRes
      hourly = hourlyRes
      sessions = sessionsRes
    } catch (err: any) {
      error = err.message || 'Failed to load analytics'
    } finally {
      loading = false
    }
  }

  function formatNumber(num: number): string {
    if (num >= 1000000) return (num / 1000000).toFixed(1) + 'M'
    if (num >= 1000) return (num / 1000).toFixed(1) + 'K'
    return String(num)
  }

  function formatDuration(ms: number): string {
    if (ms < 1000) return `${ms}ms`
    return `${(ms / 1000).toFixed(1)}s`
  }

  // SVG Chart helpers
  function renderBarChart(data: Array<{label: string, value: number}>, width = 100%, height = 200) {
    if (!data.length) return ''
    const maxValue = Math.max(...data.map(d => d.value))
    const barHeight = (height - 40) / data.length
    const maxBarWidth = width - 120

    return data.map((d, i) => {
      const barWidth = (d.value / maxValue) * maxBarWidth
      const y = 30 + i * barHeight
      return `
        <g transform="translate(100, ${y})">
          <rect x="0" y="4" width="${barWidth}" height="${barHeight - 8}"
                fill="var(--accent-primary)" rx="2" />
          <text x="-8" y="${barHeight / 2}" dominant-baseline="middle" text-anchor="end"
                font-size="11" fill="var(--text-secondary)" font-family="inherit">${d.label}</text>
          <text x="${barWidth + 8}" y="${barHeight / 2}" dominant-baseline="middle"
                font-size="11" fill="var(--text-primary)" font-family="inherit" font-weight="500">${d.value}</text>
        </g>
      `
    }).join('')
  }

  function renderLineChart(data: Array<{hour: number, count: number}>, width = 100%, height = 200) {
    if (!data.length) return ''
    const maxValue = Math.max(...data.map(d => d.count), 1)
    const points = data.map((d, i) => {
      const x = (i / (data.length - 1)) * (width - 80) + 50
      const y = height - 30 - (d.count / maxValue) * (height - 60)
      return `${x},${y}`
    }).join(' ')

    return `
      <polyline fill="none" stroke="var(--accent-primary)" stroke-width="2" points="${points}" />
      ${data.map((d, i) => {
        const x = (i / (data.length - 1)) * (width - 80) + 50
        const y = height - 30 - (d.count / maxValue) * (height - 60)
        return `
          <circle cx="${x}" cy="${y}" r="3" fill="var(--accent-primary)" />
          <text x="${x}" y="${height - 10}" text-anchor="middle" font-size="10" fill="var(--text-tertiary)">${d.hour}:00</text>
        `
      }).join('')}
    `
  }

  function renderTrendChart(data: Array<{intent: string, data: Array<{date: string, count: number}>}>, width = 100%, height = 250) {
    if (!data.length) return ''
    const colors = [
      'var(--accent-primary)', 'var(--status-success)', 'var(--status-warning)',
      'var(--status-info)', 'var(--status-error)', '#a855f7', '#f97316', '#ec4899'
    ]
    let maxValue = 1
    data.forEach(t => {
      t.data.forEach(d => { if (d.count > maxValue) maxValue = d.count })
    })

    return data.map((t, idx) => {
      const color = colors[idx % colors.length]
      const points = t.data.map((d, i) => {
        const x = (i / (t.data.length - 1)) * (width - 100) + 80
        const y = height - 40 - (d.count / maxValue) * (height - 80)
        return `${x},${y}`
      }).join(' ')
      const lastPoint = t.data[t.data.length - 1]
      const lastX = (t.data.length - 1) / (t.data.length - 1) * (width - 100) + 80
      const lastY = height - 40 - (lastPoint.count / maxValue) * (height - 80)

      return `
        <g class="trend-line" style="--color: ${color}">
          <polyline fill="none" stroke="var(--color)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" points="${points}" />
          ${t.data.map((d, i) => {
            const x = (i / (t.data.length - 1)) * (width - 100) + 80
            const y = height - 40 - (d.count / maxValue) * (height - 80)
            return `<circle cx="${x}" cy="${y}" r="2.5" fill="var(--color)" />`
          }).join('')}
          <text x="${lastX + 8}" y="${lastY}" dominant-baseline="middle"
                font-size="11" fill="var(--color)" font-weight="500">${t.intent}</text>
        </g>
      `
    }).join('')
  }

  onMount(() => {
    loadAnalytics()
  })
</script>

<div class="voice-analytics">
  <div class="page-header">
    <div>
      <h1>Voice Analytics</h1>
      <p class="subtitle">Insights into voice command usage and performance</p>
    </div>
    <div class="header-controls">
      <select bind:value={days} on:change={loadAnalytics}>
        <option value={1}>Last 24 hours</option>
        <option value={7}>Last 7 days</option>
        <option value={30}>Last 30 days</option>
        <option value={90}>Last 90 days</option>
      </select>
      <button class="btn btn-secondary" on:click={loadAnalytics}>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <polyline points="23 4 23 10 17 10" />
          <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
        </svg>
        Refresh
      </button>
    </div>
  </div>

  {#if loading && !stats}
    <div class="loading-grid">
      {#each [1,2,3,4] as _}
        <div class="stat-card skeleton"></div>
      {/each}
    </div>
  {:else if error}
    <div class="error-state">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <circle cx="12" cy="12" r="10" />
        <line x1="15" y1="9" x2="9" y2="15" />
        <line x1="9" y1="9" x2="15" y2="15" />
      </svg>
      <p>{error}</p>
      <button class="btn btn-primary" on:click={loadAnalytics}>Retry</button>
    </div>
  {:else}
    <!-- Key Metrics -->
    <div class="stats-grid">
      <div class="stat-card">
        <div class="stat-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z" />
            <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
            <line x1="12" y1="19" x2="12" y2="22" />
          </svg>
        </div>
        <div class="stat-content">
          <span class="stat-value">{formatNumber(stats?.total_commands || 0)}</span>
          <span class="stat-label">Total Commands</span>
        </div>
      </div>

      <div class="stat-card">
        <div class="stat-icon success">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <polyline points="20 6 9 17 4 12" />
          </svg>
        </div>
        <div class="stat-content">
          <span class="stat-value">{(stats?.success_rate || 0) * 100}%</span>
          <span class="stat-label">Success Rate</span>
        </div>
      </div>

      <div class="stat-card">
        <div class="stat-icon info">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <circle cx="12" cy="12" r="10" />
            <line x1="12" y1="16" x2="12" y2="12" />
            <line x1="12" y1="8" x2="12.01" y2="8" />
          </svg>
        </div>
        <div class="stat-content">
          <span class="stat-value">{(stats?.avg_confidence || 0) * 100}%</span>
          <span class="stat-label">Avg Confidence</span>
        </div>
      </div>

      <div class="stat-card">
        <div class="stat-icon warning">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <circle cx="12" cy="12" r="10" />
            <polyline points="12 6 12 12 16 14" />
          </svg>
        </div>
        <div class="stat-content">
          <span class="stat-value">{formatDuration(stats?.avg_latency_ms || 0)}</span>
          <span class="stat-label">Avg Latency</span>
        </div>
      </div>
    </div>

    <!-- Charts -->
    <div class="charts-grid">
      <!-- Top Intents -->
      <div class="chart-card">
        <div class="chart-header">
          <h3>Top Intents</h3>
        </div>
        <div class="chart-container">
          <svg class="chart-svg" viewBox="0 0 400 300" preserveAspectRatio="none">
            <g transform="translate(0, 0)">
              {@html renderBarChart(topIntentsChartData, 400, 300)}
            </g>
          </svg>
        </div>
      </div>

      <!-- Top Actions -->
      <div class="chart-card">
        <div class="chart-header">
          <h3>Top Actions</h3>
        </div>
        <div class="chart-container">
          <svg class="chart-svg" viewBox="0 0 400 300" preserveAspectRatio="none">
            <g transform="translate(0, 0)">
              {@html renderBarChart(topActionsChartData, 400, 300)}
            </g>
          </svg>
        </div>
      </div>

      <!-- Hourly Distribution -->
      <div class="chart-card wide">
        <div class="chart-header">
          <h3>Hourly Distribution</h3>
        </div>
        <div class="chart-container">
          <svg class="chart-svg" viewBox="0 0 800 300" preserveAspectRatio="none">
            <g transform="translate(0, 0)">
              {@html renderLineChart(hourlyChartData, 800, 300)}
            </g>
            <text x="400" y="290" text-anchor="middle" font-size="11" fill="var(--text-tertiary)">Hour of Day</text>
            <text x="20" y="150" text-anchor="middle" transform="rotate(-90, 20, 150)" font-size="11" fill="var(--text-tertiary)">Commands</text>
          </svg>
        </div>
      </div>

      <!-- Intent Trends -->
      <div class="chart-card wide">
        <div class="chart-header">
          <h3>Intent Trends (Top 8)</h3>
        </div>
        <div class="chart-container">
          <svg class="chart-svg" viewBox="0 0 800 350" preserveAspectRatio="none">
            <g transform="translate(0, 0)">
              {@html renderTrendChart(trendsChartData, 800, 350)}
            </g>
          </svg>
        </div>
      </div>
    </div>

    <!-- Sessions Table -->
    <div class="chart-card sessions-card">
      <div class="chart-header">
        <h3>Recent Sessions</h3>
      </div>
      <div class="table-container">
        <table class="sessions-table">
          <thead>
            <tr>
              <th>Session ID</th>
              <th>Duration</th>
              <th>Commands</th>
              <th>Success Rate</th>
              <th>Intents</th>
              <th>Last Active</th>
            </tr>
          </thead>
          <tbody>
            {#each sessions?.sessions || [] as session}
              <tr>
                <td><code>{session.session_id.slice(0, 20)}...</code></td>
                <td>{formatDuration(new Date(session.last_seen).getTime() - new Date(session.first_seen).getTime())}</td>
                <td>{session.command_count}</td>
                <td><span class="success-rate">{Math.round(session.success_rate * 100)}%</span></td>
                <td>
                  <div class="intent-tags">
                    {#each session.intents.slice(0, 3) as intent}
                      <span class="intent-tag">{intent}</span>
                    {/each}
                    {#if session.intents.length > 3}
                      <span class="intent-tag more">+{session.intents.length - 3}</span>
                    {/if}
                  </div>
                </td>
                <td>{new Date(session.last_seen).toLocaleString()}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </div>
  {/if}
</div>

<style>
  .voice-analytics {
    display: flex;
    flex-direction: column;
    gap: var(--space-6);
    padding: var(--space-6);
  }

  .page-header {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: var(--space-4);
  }

  .page-header h1 {
    font-size: var(--text-2xl);
    font-weight: 700;
    color: var(--text-primary);
    margin: 0 0 var(--space-1);
  }

  .subtitle {
    font-size: var(--text-base);
    color: var(--text-tertiary);
    margin: 0;
  }

  .header-controls {
    display: flex;
    align-items: center;
    gap: var(--space-3);
  }

  .header-controls select {
    padding: var(--space-2) var(--space-4);
    background: var(--bg-secondary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-md);
    color: var(--text-primary);
    font-family: inherit;
    font-size: var(--text-sm);
    cursor: pointer;
  }

  .header-controls select:focus {
    outline: none;
    border-color: var(--accent-primary);
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

  .btn-primary {
    background: var(--accent-primary);
    color: var(--bg-primary);
  }

  .btn-primary:hover:not(:disabled) {
    filter: brightness(1.1);
  }

  .btn-secondary {
    background: var(--bg-secondary);
    color: var(--text-primary);
    border: 1px solid var(--border-primary);
  }

  .btn-secondary:hover:not(:disabled) {
    background: var(--bg-tertiary);
  }

  .btn svg {
    width: 16px;
    height: 16px;
  }

  .stats-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: var(--space-4);
  }

  .stat-card {
    display: flex;
    align-items: center;
    gap: var(--space-4);
    padding: var(--space-5);
    background: var(--bg-secondary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-lg);
    transition: border-color 0.15s ease, box-shadow 0.15s ease;
  }

  .stat-card:hover {
    border-color: var(--accent-primary);
    box-shadow: var(--shadow-md);
  }

  .stat-card.skeleton {
    background: linear-gradient(90deg, var(--bg-tertiary) 25%, var(--bg-secondary) 50%, var(--bg-tertiary) 75%);
    background-size: 200% 100%;
    animation: shimmer 1.5s infinite;
  }

  @keyframes shimmer {
    0% { background-position: 200% 0; }
    100% { background-position: -200% 0; }
  }

  .stat-icon {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 48px;
    height: 48px;
    border-radius: var(--radius-md);
    background: var(--accent-primary-light);
    color: var(--accent-primary);
  }

  .stat-icon svg {
    width: 24px;
    height: 24px;
  }

  .stat-icon.success {
    background: var(--status-success-light);
    color: var(--status-success);
  }

  .stat-icon.info {
    background: var(--status-info-light);
    color: var(--status-info);
  }

  .stat-icon.warning {
    background: var(--status-warning-light);
    color: var(--status-warning);
  }

  .stat-content {
    display: flex;
    flex-direction: column;
  }

  .stat-value {
    font-size: var(--text-2xl);
    font-weight: 700;
    color: var(--text-primary);
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    line-height: 1.2;
  }

  .stat-label {
    font-size: var(--text-sm);
    color: var(--text-tertiary);
  }

  .charts-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
    gap: var(--space-4);
  }

  .chart-card {
    background: var(--bg-secondary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-lg);
    overflow: hidden;
  }

  .chart-card.wide {
    grid-column: 1 / -1;
  }

  .chart-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--space-4) var(--space-5);
    border-bottom: 1px solid var(--border-primary);
  }

  .chart-header h3 {
    margin: 0;
    font-size: var(--text-lg);
    font-weight: 600;
    color: var(--text-primary);
  }

  .chart-container {
    padding: var(--space-5);
    overflow-x: auto;
  }

  .chart-svg {
    width: 100%;
    height: auto;
    display: block;
  }

  .sessions-card {
    grid-column: 1 / -1;
  }

  .table-container {
    overflow-x: auto;
  }

  .sessions-table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--text-sm);
  }

  .sessions-table th {
    padding: var(--space-3) var(--space-4);
    text-align: left;
    font-weight: 600;
    font-size: var(--text-xs);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-tertiary);
    border-bottom: 1px solid var(--border-primary);
    background: var(--bg-tertiary);
    white-space: nowrap;
  }

  .sessions-table td {
    padding: var(--space-3) var(--space-4);
    border-bottom: 1px solid var(--border-primary);
    vertical-align: middle;
  }

  .sessions-table tbody tr:hover {
    background: var(--bg-tertiary);
  }

  .sessions-table code {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: var(--text-xs);
    background: var(--bg-tertiary);
    padding: var(--space-1) var(--space-2);
    border-radius: var(--radius-sm);
  }

  .success-rate {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-weight: 600;
    color: var(--status-success);
  }

  .intent-tags {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1);
  }

  .intent-tag {
    padding: var(--space-1) var(--space-2);
    background: var(--accent-primary-light);
    color: var(--accent-primary);
    border-radius: var(--radius-full);
    font-size: var(--text-xs);
    font-weight: 500;
  }

  .intent-tag.more {
    background: var(--bg-tertiary);
    color: var(--text-tertiary);
  }

  .loading-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: var(--space-4);
  }

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

  .error-state svg {
    width: 48px;
    height: 48px;
    color: var(--status-error);
  }

  @media (max-width: 900px) {
    .charts-grid {
      grid-template-columns: 1fr;
    }

    .chart-card.wide {
      grid-column: auto;
    }
  }

  @media (max-width: 640px) {
    .voice-analytics {
      padding: var(--space-4);
    }

    .page-header {
      flex-direction: column;
      align-items: stretch;
    }

    .header-controls {
      flex-direction: column;
      align-items: stretch;
    }

    .header-controls select,
    .header-controls .btn {
      width: 100%;
    }

    .stat-card {
      flex-direction: column;
      text-align: center;
      gap: var(--space-3);
    }
  }
</style>