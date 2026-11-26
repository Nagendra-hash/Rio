<script lang="ts">
  import type { TraceData, TraceSpan } from '$lib/api/rest'

  const { traceData }: { traceData: TraceData } = $props()

  const spans = $derived(traceData?.spans || [])
  const spanTree = $derived(buildTree(spans))

  function buildTree(spans: TraceSpan[]): (TraceSpan & { children: TraceSpan[] })[] {
    const map = new Map(spans.map(s => [s.spanId, { ...s, children: [] as (TraceSpan & { children: TraceSpan[] })[] }]))
    const roots: (TraceSpan & { children: TraceSpan[] })[] = []
    for (const span of spans) {
      const node = map.get(span.spanId)!
      if (span.parentSpanId && map.has(span.parentSpanId)) {
        map.get(span.parentSpanId)!.children.push(node)
      } else {
        roots.push(node)
      }
    }
    return roots
  }

  function formatDuration(span: TraceSpan): string {
    const ms = span.endTime - span.startTime
    if (ms < 1000) return `${ms.toFixed(1)}ms`
    return `${(ms / 1000).toFixed(2)}s`
  }

  function getStatusColor(span: TraceSpan): string {
    const error = span.attributes?.error === true || span.events?.some(e => e.name === 'error')
    return error ? 'var(--status-error)' : 'var(--status-success)'
  }
</script>

<div class="trace-viewer">
  <h4>Trace Waterfall</h4>
  <div class="trace-tree">
    {#each spanTree as root}
      <SpanNode {root} depth={0} />
    {/each}
  </div>
</div>

{#snippet SpanNode(span: TraceSpan & { children: TraceSpan[] }, depth: number)}
  <div class="span-node" style="--depth: {depth};">
    <div class="span-header" style="color: {getStatusColor(span)}; border-left-color: {getStatusColor(span)};">
      <span class="span-name">{span.name}</span>
      <span class="span-duration">{formatDuration(span)}</span>
    </div>
    {#if span.children.length > 0}
      <div class="span-children">
        {#each span.children as child}
          <SpanNode span={child} depth={depth + 1} />
        {/each}
      </div>
    {/if}
  </div>
{/snippet}

<style>
  .trace-viewer {
    margin-top: var(--space-6);
    padding-top: var(--space-4);
    border-top: 1px solid var(--border-primary);
  }

  .trace-viewer h4 {
    font-size: var(--text-base);
    font-weight: 600;
    margin-bottom: var(--space-3);
  }

  .trace-tree {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }

  .span-node {
    background: var(--bg-tertiary);
    border-radius: var(--radius-md);
    overflow: hidden;
  }

  .span-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--space-2) var(--space-3);
    border-left: 3px solid;
  }

  .span-name {
    font-size: var(--text-sm);
    font-weight: 500;
    font-family: monospace;
  }

  .span-duration {
    font-size: var(--text-xs);
    color: var(--text-tertiary);
    font-variant-numeric: tabular-nums;
  }

  .span-children {
    padding-left: 24px;
    border-left: 1px dashed var(--border-primary);
    margin-left: 12px;
  }
</style>