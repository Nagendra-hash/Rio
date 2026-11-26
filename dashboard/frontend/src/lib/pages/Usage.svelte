<script lang="ts">
  import { onMount } from 'svelte'
  import { usageApi } from '$lib/api/rest'
  import { toast } from '$lib/stores/toast'

  import {
    AreaChart,
    Area,
    BarChart,
    Bar,
    LineChart,
    Line,
    PieChart,
    Pie,
    Cell,
    XAxis,
    YAxis,
    CartesianGrid,
    Tooltip,
    Legend,
    ResponsiveContainer,
    ComposedChart,
  } from 'recharts'

  import type { UsageSummary, UsageDetail, BudgetConfig, RouterStatus, UsageBudgetResponse } from '$lib/api/rest'

  // State
  let loading = true
  let summary: UsageSummary | null = null
  let detail: UsageDetail | null = null
  let budget: UsageBudgetResponse | null = null
  let routerStatus: RouterStatus | null = null
  let timeRange = '7d'
  let selectedProvider = 'all'

  // Derived data for charts
  $: dailyData = detail?.data
    ?.filter(d => selectedProvider === 'all' || d.provider === selectedProvider)
    ?.reduce((acc, d) => {
      const date = d.timestamp.split('T')[0]
      if (!acc[date]) acc[date] = { date, total_tokens: 0, requests: 0, providers: {} }
      acc[date].total_tokens += d.total_tokens
      acc[date].requests += 1
      acc[date].providers[d.provider] = (acc[date].providers[d.provider] || 0) + d.total_tokens
      return acc
    }, {} as Record<string, any>)
    ?.map(v => Object.values(v))
    ?.sort((a, b) => a.date.localeCompare(b.date)) || []

  $: providerBreakdown = detail?.data
    ?.reduce((acc, d) => {
      acc[d.provider] = (acc[d.provider] || 0) + d.total_tokens
      return acc
    }, {} as Record<string, number>) || {}

  $: providerData = Object.entries(providerBreakdown).map(([provider, tokens]) => ({
    provider,
    tokens,
    percentage: detail?.summary?.[provider] ? (detail.summary[provider].total_tokens / detail?.data?.reduce((s, d) => s + d.total_tokens, 1) * 100 || 0) : 0,
  }))

  $: taskBreakdown = detail?.data
    ?.reduce((acc, d) => {
      acc[d.task] = (acc[d.task] || 0) + d.total_tokens
      return acc
    }, {} as Record<string, number>) || {}

  $: taskData = Object.entries(taskBreakdown).map(([task, tokens]) => ({ task, tokens }))

  async function loadData() {
    try {
      loading = true
      const [sum, det, bud, rt] = await Promise.all([
        usageApi.getSummary(),
        usageApi.getDetail({ provider: selectedProvider === 'all' ? '' : selectedProvider, days: parseInt(timeRange) }),
        usageApi.getBudget(),
        usageApi.getRouterStatus(),
      ])
      summary = sum
      detail = det
      budget = bud
      routerStatus = rt
    } catch (e) {
      console.error('Failed to load usage data:', e)
      toast.error('Failed to load usage data')
    } finally {
      loading = false
    }
  }

  async function updateBudget() {
    if (!budget) return
    try {
      const config = {
        daily_limits: budget.config.daily_limits,
        monthly_limits: budget.config.monthly_limits,
        per_request_cap: budget.config.per_request_cap,
        alert_thresholds: budget.config.alert_thresholds,
        hard_block_at: budget.config.hard_block_at,
        exclude_local: budget.config.exclude_local,
      }
      await usageApi.updateBudget(config)
      toast.success('Budget updated')
      await loadData()
    } catch (e) {
      console.error('Failed to update budget:', e)
      toast.error('Failed to update budget')
    }
  }

  function formatTokens(n: number): string {
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
    if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
    return n.toString()
  }

  function formatCost(n: number): string {
    if (n >= 1) return `$${n.toFixed(2)}`
    if (n >= 0.01) return `$${n.toFixed(3)}`
    return `$${n.toFixed(5)}`
  }

  function getBudgetColor(percent: number): string {
    if (percent >= 90) return 'var(--status-error)'
    if (percent >= 75) return 'var(--status-warning)'
    if (percent >= 50) return 'var(--accent-secondary)'
    return 'var(--status-success)'
  }

  onMount(() => {
    loadData()
    const interval = setInterval(loadData, 60000)
    return () => clearInterval(interval)
  })
</script>

<div class="usage-page">
  <header class="page-header">
    <h1>Cloud LLM Usage & Budget</h1>
    <div class="header-actions">
      <select bind:value={timeRange} on:change={loadData} class="select">
        <option value="1d">Last 24 hours</option>
        <option value="7d">Last 7 days</option>
        <option value="30d">Last 30 days</option>
      </select>
      <select bind:value={selectedProvider} on:change={loadData} class="select">
        <option value="all">All Providers</option>
        {#each summary ? Object.keys(summary) : [] as provider}
          <option value={provider}>{provider}</option>
        {/each}
      </select>
      <button class="btn btn-primary" on:click={loadData} disabled={loading}>
        {loading ? 'Refreshing...' : 'Refresh'}
      </button>
    </div>
  </header>

  {#if loading && !summary}
    <div class="loading">Loading usage data...</div>
  {:else}
    <!-- Summary Cards -->
    <section class="section">
      <header class="section-header"><h2>Token Usage Summary</h2></header>
      <div class="summary-grid">
        {#if detail}
          <div class="stat-card">
            <span class="stat-label">Total Tokens</span>
            <span class="stat-value">{formatTokens(detail?.data?.reduce((s, d) => s + d.total_tokens, 0) || 0)}</span>
          </div>
          <div class="stat-card">
            <span class="stat-label">Total Requests</span>
            <span class="stat-value">{detail?.data?.length || 0}</span>
          </div>
          <div class="stat-card">
            <span class="stat-label">Total Cost</span>
            <span class="stat-value">{formatCost(detail?.data?.reduce((s, d) => s + (d.cost_usd || 0), 0) || 0)}</span>
          </div>
          <div class="stat-card">
            <span class="stat-label">Avg Latency</span>
            <span class="stat-value">
              {detail?.data?.length
                ? ((detail.data.reduce((s, d) => s + (d.latency_s || 0), 0) / detail.data.length) * 1000).toFixed(0)
                : 0} ms
            </span>
          </div>
        {/if}
      </div>
    </section>

    <!-- Daily Token Usage Chart -->
    <section class="section">
      <header class="section-header"><h2>Daily Token Usage</h2></header>
      <div class="chart-container">
        {#if dailyData && dailyData.length > 0}
          <ResponsiveContainer width="100%" height={300}>
            <ComposedChart data={dailyData}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border-primary)" />
              <XAxis
                dataKey="date"
                tickFormatter={(d) => new Date(d).toLocaleDateString()}
                stroke="var(--text-tertiary)"
                tick={{ fill: 'var(--text-tertiary)', fontSize: 11 }}
              />
              <YAxis
                tickFormatter={formatTokens}
                stroke="var(--border-primary)"
                tick={{ fill: 'var(--text-tertiary)', fontSize: 11 }}
              />
              <Tooltip
                contentStyle={{
                  background: 'var(--bg-tertiary)',
                  border: '1px solid var(--border-primary)',
                  borderRadius: 'var(--radius-md)',
                }}
                formatter={(value: number, name: string) => [
                  name === 'total_tokens' ? formatTokens(value) : value,
                  name === 'total_tokens' ? 'Tokens' : name,
                ]}
              />
              <Legend />
              <Area
                type="monotone"
                dataKey="total_tokens"
                name="Total Tokens"
                stroke="var(--accent-primary)"
                fill="var(--accent-primary)"
                fillOpacity={0.15}
                strokeWidth={2}
              />
              <Bar
                dataKey="requests"
                name="Requests"
                fill="var(--accent-secondary)"
                fillOpacity={0.6}
                yAxisId={1}
                radius={[4, 4, 0, 0]}
              />
              <YAxis
                yAxisId={1}
                orientation="right"
                tickFormatter={(v) => v}
                stroke="var(--border-primary)"
                tick={{ fill: 'var(--text-tertiary)', fontSize: 11 }}
                hide={true}
              />
            </ComposedChart>
          </ResponsiveContainer>
        {:else}
          <div class="empty-state">No usage data for selected period</div>
        {/if}
      </div>
    </section>

    <!-- Provider Breakdown -->
    <section class="section">
      <header class="section-header"><h2>Usage by Provider</h2></header>
      <div class="charts-row">
        <div class="chart-container half">
          <ResponsiveContainer width="100%" height={280}>
            <PieChart>
              <Pie
                data={providerData}
                cx="50%"
                cy="50%"
                innerRadius={60}
                outerRadius={100}
                paddingAngle={2}
                dataKey="tokens"
                nameKey="provider"
                label={({ provider, tokens, percent }) => `${provider}: ${formatTokens(tokens)} (${(percent * 100).toFixed(1)}%)`}
                labelLine={false}
              >
                {providerData.map((_, i) => (
                  <Cell
                    key={`cell-${i}`}
                    fill={[
                      'var(--accent-primary)',
                      'var(--accent-secondary)',
                      'var(--accent-tertiary)',
                      'var(--status-success)',
                      'var(--status-warning)',
                      'var(--status-error)',
                    ][i % 6]}
                  />
                ))}
              </Pie>
              <Tooltip
                contentStyle={{
                  background: 'var(--bg-tertiary)',
                  border: '1px solid var(--border-primary)',
                  borderRadius: 'var(--radius-md)',
                }}
                formatter={(value: number) => [formatTokens(value), 'Tokens']}
              />
            </PieChart>
          </ResponsiveContainer>
        </div>

        <div class="chart-container half">
          <ResponsiveContainer width="100%" height={280}>
            <BarChart data={providerData} layout="vertical">
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border-primary)" />
              <XAxis
                type="number"
                tickFormatter={formatTokens}
                tick={{ fill: 'var(--text-tertiary)', fontSize: 11 }}
                stroke="var(--border-primary)"
              />
              <YAxis
                dataKey="provider"
                type="category"
                width={100}
                tick={{ fill: 'var(--text-primary)', fontSize: 11 }}
              />
              <Tooltip
                contentStyle={{
                  background: 'var(--bg-tertiary)',
                  border: '1px solid var(--border-primary)',
                  borderRadius: 'var(--radius-md)',
                }}
                formatter={(value: number) => [formatTokens(value), 'Tokens']}
              />
              <Bar
                dataKey="tokens"
                fill="var(--accent-primary)"
                radius={[0, 4, 4, 0]}
                maxBarSize={40}
              />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>
    </section>

    <!-- Task Breakdown -->
    <section class="section">
      <header class="section-header"><h2>Usage by Task Type</h2></header>
      <div class="chart-container">
        {#if taskData && taskData.length > 0}
          <ResponsiveContainer width="100%" height={250}>
            <BarChart data={taskData} layout="vertical">
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border-primary)" />
              <XAxis
                type="number"
                tickFormatter={formatTokens}
                tick={{ fill: 'var(--text-tertiary)', fontSize: 11 }}
                stroke="var(--border-primary)"
              />
              <YAxis
                dataKey="task"
                type="category"
                width={140}
                tick={{ fill: 'var(--text-primary)', fontSize: 11 }}
              />
              <Tooltip
                contentStyle={{
                  background: 'var(--bg-tertiary)',
                  border: '1px solid var(--border-primary)',
                  borderRadius: 'var(--radius-md)',
                }}
                formatter={(value: number) => [formatTokens(value), 'Tokens']}
              />
              <Bar
                dataKey="tokens"
                fill="var(--accent-tertiary)"
                radius={[0, 4, 4, 0]}
                maxBarSize={40}
              />
            </BarChart>
          </ResponsiveContainer>
        {:else}
          <div class="empty-state">No task breakdown available</div>
        {/if}
      </div>
    </section>

    <!-- Budget Status -->
    <section class="section">
      <header class="section-header">
        <h2>Budget Status</h2>
        <button class="btn btn-secondary" on:click={updateBudget} disabled={!budget || loading}>
          Save Budget Changes
        </button>
      </header>
      <div class="budget-grid">
        {#if budget && budget.providers}
          {#each Object.entries(budget.providers) as [provider, status]}
            <div class="budget-card">
              <div class="budget-header">
                <span class="provider-name">{provider}</span>
                <span class="provider-status" style="color: {getBudgetColor(status.daily.percent || 0)}">
                  {status.is_local ? 'LOCAL' : (status.daily.percent >= 90 ? 'CRITICAL' : status.daily.percent >= 75 ? 'WARNING' : 'OK')}
                </span>
              </div>

              <div class="budget-metric">
                <label>Daily Limit</label>
                <div class="progress-bar">
                  <div
                    class="progress-fill"
                    style="width: {Math.min(100, status.daily.percent)}%; background: {getBudgetColor(status.daily.percent || 0)}"
                  ></div>
                </div>
                <span class="progress-text">
                  {formatTokens(status.daily.used || 0)} / {formatTokens(status.daily.limit || 0)}
                  ({(status.daily.percent || 0).toFixed(1)}%)
                </span>
              </div>

              <div class="budget-metric">
                <label>Monthly Limit</label>
                <div class="progress-bar">
                  <div
                    class="progress-fill"
                    style="width: {Math.min(100, status.monthly.percent)}%; background: {getBudgetColor(status.monthly.percent || 0)}"
                  ></div>
                </div>
                <span class="progress-text">
                  {formatTokens(status.monthly.used || 0)} / {formatTokens(status.monthly.limit || 0)}
                  ({(status.monthly.percent || 0).toFixed(1)}%)
                </span>
              </div>

              <div class="budget-config">
                <label>Daily Limit (tokens)</label>
                <input
                  type="number"
                  bind:value={budget.config.daily_limits[provider]}
                  class="input"
                  min="0"
                  step="1000"
                />
                <label>Monthly Limit (tokens)</label>
                <input
                  type="number"
                  bind:value={budget.config.monthly_limits[provider]}
                  class="input"
                  min="0"
                  step="10000"
                />
              </div>

              {#if status.alerts && status.alerts.length > 0}
                <div class="alerts">
                  {#each status.alerts as alert}
                    <div class="alert alert-warning">{alert}</div>
                  {/each}
                </div>
              {/if}
            </div>
          {/each}
        {/if}
      </div>

      <!-- Global Budget Settings -->
      <div class="budget-global">
        <h3>Global Settings</h3>
        <div class="budget-config-row">
          <label>Per-Request Cap (tokens)</label>
          <input
            type="number"
            bind:value={budget?.config.per_request_cap}
            class="input"
            min="100"
            step="100"
          />
        </div>
        <div class="budget-config-row">
          <label>Alert Thresholds (comma-separated, 0-1)</label>
          <input
            type="text"
            bind:value={budget?.config.alert_thresholds?.join(', ')}
            class="input"
            placeholder="0.5, 0.75, 0.9"
          />
        </div>
        <div class="budget-config-row">
          <label>Hard Block At (0-1)</label>
          <input
            type="number"
            bind:value={budget?.config.hard_block_at}
            class="input"
            min="0.1"
            max="1"
            step="0.05"
          />
        </div>
        <div class="budget-config-row">
          <label>
            <input
              type="checkbox"
              bind:value={budget?.config.exclude_local}
            />
            Exclude Local Models from Budget
          </label>
        </div>
      </div>
    </section>

    <!-- Router Status -->
    <section class="section">
      <header class="section-header"><h2>Model Router Status</h2></header>
      <div class="router-status">
        {#if routerStatus}
          <div class="router-metric">
            <label>Mode</label>
            <span class="badge badge-primary">{routerStatus.mode}</span>
          </div>
          <div class="router-metric">
            <label>Model Chain</label>
            <span>{routerStatus.models?.map((m: any) => m.name).join(' → ') || 'Not configured'}</span>
          </div>

          <div class="provider-health">
            <h3>Provider Health</h3>
            <table class="health-table">
              <thead>
                <tr>
                  <th>Provider</th>
                  <th>Model</th>
                  <th>Cost Tier</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {#each routerStatus.models || [] as model}
                  <tr>
                    <td>{model.name}</td>
                    <td>{model.model_id}</td>
                    <td>{model.cost_tier === 0 ? 'Free' : model.cost_tier === 1 ? 'Low' : model.cost_tier === 2 ? 'Medium' : 'High'}</td>
                    <td>
                      <span class="status-badge" class:healthy={model.healthy} class:unhealthy={!model.healthy}>
                        {model.healthy ? 'Healthy' : 'Degraded'}
                      </span>
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}
      </div>
    </section>

    <!-- Recent Requests Table -->
    <section class="section">
      <header class="section-header"><h2>Recent Requests</h2></header>
      <div class="table-container">
        {#if detail && detail.data && detail.data.length > 0}
          <table class="requests-table">
            <thead>
              <tr>
                <th>Time</th>
                <th>Provider</th>
                <th>Model</th>
                <th>Task</th>
                <th>Status</th>
                <th>Tokens</th>
                <th>Latency</th>
                <th>Cost</th>
              </tr>
            </thead>
            <tbody>
              {#each detail.data.slice(0, 50) as req}
                <tr>
                  <td>{new Date(req.timestamp).toLocaleTimeString()}</td>
                  <td>{req.provider}</td>
                  <td>{req.model}</td>
                  <td><span class="task-badge">{req.task}</span></td>
                  <td>
                    <span class="status-badge" class:ok={req.ok} class:error={!req.ok}>
                      {req.ok ? 'OK' : 'Error'}
                    </span>
                  </td>
                  <td>{formatTokens(req.total_tokens)}</td>
                  <td>{(req.latency_s * 1000).toFixed(0)} ms</td>
                  <td>{formatCost(req.cost_usd || 0)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        {:else}
          <div class="empty-state">No recent requests</div>
        {/if}
      </div>
    </section>
  {/if}
</div>

<style>
  .usage-page {
    display: flex;
    flex-direction: column;
    gap: var(--space-6);
    max-width: 1400px;
  }

  .page-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: var(--space-4);
  }

  .page-header h1 {
    font-size: var(--text-2xl);
    font-weight: 600;
    color: var(--text-primary);
  }

  .header-actions {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    flex-wrap: wrap;
  }

  .select {
    padding: var(--space-2) var(--space-3);
    background: var(--bg-tertiary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-md);
    color: var(--text-primary);
    font-size: var(--text-sm);
    min-width: 160px;
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

  .summary-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
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

  .chart-container {
    padding: var(--space-4) var(--space-6);
  }

  .chart-container.half {
    min-width: 400px;
  }

  .charts-row {
    display: flex;
    gap: var(--space-6);
    flex-wrap: wrap;
    padding: var(--space-4) var(--space-6);
  }

  .empty-state {
    padding: var(--space-12);
    text-align: center;
    color: var(--text-tertiary);
  }

  .budget-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(350px, 1fr));
    gap: var(--space-4);
    padding: var(--space-4) var(--space-6);
  }

  .budget-card {
    background: var(--bg-tertiary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-md);
    padding: var(--space-4);
  }

  .budget-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: var(--space-4);
    padding-bottom: var(--space-3);
    border-bottom: 1px solid var(--border-primary);
  }

  .provider-name {
    font-weight: 600;
    color: var(--text-primary);
  }

  .provider-status {
    font-size: var(--text-xs);
    font-weight: 600;
    padding: var(--space-1) var(--space-2);
    border-radius: var(--radius-full);
    background: var(--bg-primary);
  }

  .budget-metric {
    margin-bottom: var(--space-3);
  }

  .budget-metric label {
    display: block;
    font-size: var(--text-sm);
    color: var(--text-tertiary);
    margin-bottom: var(--space-1);
  }

  .progress-bar {
    height: 8px;
    background: var(--bg-primary);
    border-radius: var(--radius-full);
    overflow: hidden;
    margin-bottom: var(--space-1);
  }

  .progress-fill {
    height: 100%;
    border-radius: var(--radius-full);
    transition: width 0.3s ease, background 0.3s ease;
  }

  .progress-text {
    font-size: var(--text-xs);
    color: var(--text-secondary);
    font-variant-numeric: tabular-nums;
  }

  .budget-config {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    margin-top: var(--space-3);
    padding-top: var(--space-3);
    border-top: 1px solid var(--border-primary);
  }

  .budget-config label {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    font-size: var(--text-sm);
    color: var(--text-tertiary);
  }

  .input {
    padding: var(--space-2) var(--space-3);
    background: var(--bg-primary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-md);
    color: var(--text-primary);
    font-size: var(--text-sm);
    font-family: inherit;
  }

  .alerts {
    margin-top: var(--space-3);
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  .alert {
    padding: var(--space-2) var(--space-3);
    border-radius: var(--radius-md);
    font-size: var(--text-xs);
  }

  .alert-warning {
    background: var(--status-warning);
    background: color-mix(in srgb, var(--status-warning) 15%, transparent);
    border: 1px solid var(--status-warning);
    color: var(--status-warning);
  }

  .budget-global {
    margin-top: var(--space-6);
    padding: var(--space-4) var(--space-6);
    background: var(--bg-tertiary);
    border-radius: var(--radius-md);
    border: 1px solid var(--border-primary);
  }

  .budget-global h3 {
    font-size: var(--text-base);
    font-weight: 600;
    margin-bottom: var(--space-4);
    color: var(--text-primary);
  }

  .budget-config-row {
    display: flex;
    align-items: center;
    gap: var(--space-4);
    flex-wrap: wrap;
    margin-bottom: var(--space-3);
  }

  .budget-config-row label {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    font-size: var(--text-sm);
    color: var(--text-secondary);
    cursor: pointer;
  }

  .router-status {
    padding: var(--space-4) var(--space-6);
  }

  .router-metric {
    display: flex;
    align-items: center;
    gap: var(--space-4);
    padding: var(--space-3) 0;
    border-bottom: 1px solid var(--border-primary);
  }

  .router-metric label {
    min-width: 140px;
    font-size: var(--text-sm);
    color: var(--text-tertiary);
  }

  .router-metric span {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: var(--text-sm);
    color: var(--text-primary);
  }

  .provider-health {
    margin-top: var(--space-4);
  }

  .provider-health h3 {
    font-size: var(--text-base);
    font-weight: 600;
    margin-bottom: var(--space-3);
    color: var(--text-primary);
  }

  .health-table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--text-sm);
  }

  .health-table th,
  .health-table td {
    padding: var(--space-2) var(--space-3);
    text-align: left;
    border-bottom: 1px solid var(--border-primary);
  }

  .health-table th {
    color: var(--text-tertiary);
    font-weight: 500;
  }

  .status-badge {
    display: inline-block;
    padding: var(--space-1) var(--space-2);
    border-radius: var(--radius-full);
    font-size: var(--text-xs);
    font-weight: 500;
  }

  .status-badge.healthy {
    background: color-mix(in srgb, var(--status-success) 15%, transparent);
    color: var(--status-success);
  }

  .status-badge.unhealthy {
    background: color-mix(in srgb, var(--status-error) 15%, transparent);
    color: var(--status-error);
  }

  .task-badge {
    background: var(--bg-primary);
    color: var(--text-secondary);
    padding: var(--space-1) var(--space-2);
    border-radius: var(--radius-full);
    font-size: var(--text-xs);
    font-weight: 500;
  }

  .table-container {
    overflow-x: auto;
    padding: var(--space-4) var(--space-6);
  }

  .requests-table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--text-sm);
  }

  .requests-table th,
  .requests-table td {
    padding: var(--space-2) var(--space-3);
    text-align: left;
    border-bottom: 1px solid var(--border-primary);
  }

  .requests-table th {
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

  .badge-primary {
    background: var(--accent-primary);
    color: var(--bg-primary);
  }

  .loading {
    padding: var(--space-12);
    text-align: center;
    color: var(--text-tertiary);
  }

  .btn {
    padding: var(--space-2) var(--space-4);
    border-radius: var(--radius-md);
    font-size: var(--text-sm);
    font-weight: 500;
    cursor: pointer;
    transition: all 0.15s ease;
    border: none;
    font-family: inherit;
  }

  .btn-primary {
    background: var(--accent-primary);
    color: var(--bg-primary);
  }

  .btn-primary:hover:not(:disabled) {
    filter: brightness(1.1);
  }

  .btn-primary:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .btn-secondary {
    background: var(--bg-tertiary);
    color: var(--text-primary);
    border: 1px solid var(--border-primary);
  }

  .btn-secondary:hover:not(:disabled) {
    background: var(--border-primary);
  }

  @media (max-width: 768px) {
    .charts-row {
      flex-direction: column;
    }
    .chart-container.half {
      min-width: 100%;
    }
    .budget-grid {
      grid-template-columns: 1fr;
    }
    .budget-config-row {
      flex-direction: column;
      align-items: stretch;
    }
    .budget-config-row .input {
      flex: 1;
    }
  }
</style>