/* RIO System Control Center (Phase 8) — metrics · services · activity ·
 * notifications, all measured.
 *
 * Data:   GET /api/rio/system/{metrics,services,activity,notifications}
 * Live:   the EXISTING /ws carries {type:"system_event"} messages pushed by
 *         the server-side journal watcher (activity, notifications, and a
 *         15 s metrics sample). This page NEVER polls on a timer — the only
 *         HTTP fetches are the initial load and the explicit Refresh button.
 * Honest: a value the system cannot know renders N/A, never a guess; a
 * failed fetch surfaces an error strip instead of silently keeping stale
 * numbers.
 */
"use strict";

const RIOSystem = (() => {

  let metrics = null;   // GET /api/rio/system/metrics
  let services = null;  // GET /api/rio/system/services
  let activity = null;  // GET /api/rio/system/activity?category=…
  let notes = null;     // GET /api/rio/system/notifications
  let filter = "all";
  let busy = false;
  let flash = null;
  let ws = null;
  let dismissed = new Set();

  const FILTERS = ["all", "ai", "agents", "hermes", "files", "trading",
                   "system", "errors"];
  const FILTER_LABELS = {
    all: "All", ai: "AI", agents: "Agents", hermes: "Hermes",
    files: "Files", trading: "Trading", system: "System",
    errors: "Errors",
  };
  const CAT_CLASS = {
    ai: "cat-ai", agents: "cat-agents", hermes: "cat-hermes",
    files: "cat-files", trading: "cat-trading", system: "cat-system",
  };
  // thresholds mirror actions/system_monitor.DEFAULT_THRESHOLDS (+ disk)
  const THRESH = { cpu: 90, ram: 90, temp: 85, gpu: 95, disk: 90 };

  function token() {
    try { return sessionStorage.getItem("jarvis_token") || ""; } catch (_) { return ""; }
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function loadDismissed() {
    try {
      dismissed = new Set(JSON.parse(
        localStorage.getItem("rio_dismissed_notes") || "[]"));
    } catch (_) { dismissed = new Set(); }
  }

  function saveDismissed() {
    try {
      localStorage.setItem("rio_dismissed_notes",
        JSON.stringify([...dismissed].slice(-200)));
    } catch (_) { /* private mode — dismissals just don't persist */ }
  }

  async function api(path, opts = {}) {
    const r = await fetch(path, Object.assign({
      headers: { "Authorization": `Bearer ${token()}`,
                 "Content-Type": "application/json" },
      cache: "no-store",
    }, opts));
    let j = null;
    try { j = await r.json(); } catch (_) { /* empty */ }
    if (r.status === 401) throw Object.assign(new Error("pairing required"), { code: 401 });
    if (!r.ok) throw Object.assign(
      new Error((j && (j.detail || j.error)) || `HTTP ${r.status}`),
      { code: r.status, body: j });
    return j;
  }

  function say(text, kind) { flash = text ? { text, kind: kind || "ok" } : null; }

  /* ── data (on demand only: load + Refresh) ─────────────────────────── */
  async function refresh() {
    busy = true; paint();
    const errs = [];
    try { metrics = await api("/api/rio/system/metrics"); }
    catch (e) { metrics = null; errs.push(`metrics: ${e.message}`); }
    try { services = await api("/api/rio/system/services"); }
    catch (e) { services = null; errs.push(`services: ${e.message}`); }
    try {
      activity = await api(`/api/rio/system/activity?category=${filter}&limit=60`);
    } catch (e) { activity = null; errs.push(`activity: ${e.message}`); }
    try { notes = await api("/api/rio/system/notifications?limit=40"); }
    catch (e) { notes = null; errs.push(`notifications: ${e.message}`); }
    say(errs.length ? errs.join(" · ") : null, errs.length ? "err" : null);
    busy = false; paint();
  }

  /* ── live events (existing /ws — no timer polling anywhere) ────────── */
  function connectWS() {
    try {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(token())}`);
    } catch (_) { return; }
    ws.onopen = () => paintLive();
    ws.onmessage = (ev) => {
      let m = null;
      try { m = JSON.parse(ev.data); } catch (_) { return; }
      if (!m || m.type !== "system_event" || !m.kind) return;
      if (m.kind === "metrics" && m.metrics) {
        metrics = m.metrics;
        paintMetrics(); paintChecked();
      } else if (m.kind === "activity" && m.item) {
        if (!activity) activity = { counts: {}, items: [] };
        activity.items = activity.items || [];
        const it = m.item;
        const match = filter === "all"
          || (filter === "errors" ? it.error : it.category === filter);
        if (match) {
          if (!activity.items.some(x => x.id === it.id)) {
            activity.items.unshift(it);
            activity.items = activity.items.slice(0, 80);
          }
          paintActivity();
        }
        if (activity.counts) {
          activity.counts.all = (activity.counts.all || 0) + 1;
          activity.counts[it.category] = (activity.counts[it.category] || 0) + 1;
          if (it.error) activity.counts.errors = (activity.counts.errors || 0) + 1;
          paintFilters();
        }
      } else if (m.kind === "notification" && m.item) {
        if (!notes) notes = { counts: {}, items: [] };
        notes.items = notes.items || [];
        if (!notes.items.some(x => x.id === m.item.id)) {
          notes.items.unshift(m.item);
          notes.items = notes.items.slice(0, 80);
          if (notes.counts) notes.counts[m.item.severity] =
            (notes.counts[m.item.severity] || 0) + 1;
        }
        paintNotes();
      }
    };
    ws.onclose = () => { ws = null; paintLive(); };
  }

  function paintLive() {
    const el = document.getElementById("sy-live");
    if (!el) return;
    const on = ws && ws.readyState === 1;
    el.className = "sy-live" + (on ? " on" : " off");
    el.textContent = on ? "● LIVE (ws)" : "○ live stream down";
  }

  /* ── shell ─────────────────────────────────────────────────────────── */
  function build() {
    loadDismissed();
    const root = document.createElement("div");
    root.className = "sy-wrap";
    root.innerHTML = `
      <h1>System</h1>
      <p class="sub">The System Control Center — host metrics, service
        status, the cross-cutting activity timeline and the notification
        centre. Every number is measured from the OS, the probes or the
        journals this system already keeps; values that cannot be known
        show <b>N/A</b>, never a guess. Live updates arrive over the
        existing WebSocket — this page never polls on a timer.</p>

      <div class="sy-bar">
        <button class="btn" id="sy-refresh">↻ Refresh</button>
        <span class="sy-live off" id="sy-live">○ connecting…</span>
        <span class="hm-checked" id="sy-checked"></span>
      </div>
      <div class="mem-flash" id="sy-flash" hidden></div>

      <h2>Metrics</h2>
      <div class="sy-metrics" id="sy-metrics"></div>

      <h2>Processes</h2>
      <div class="sy-procs" id="sy-procs"></div>

      <h2>Services</h2>
      <div class="sy-services" id="sy-services"></div>

      <h2>Activity</h2>
      <div class="sy-filters" id="sy-filters"></div>
      <div class="sy-activity" id="sy-activity"></div>

      <h2>Notifications</h2>
      <div class="sy-notes" id="sy-notes"></div>`;

    const frag = document.createDocumentFragment();
    frag.append(root);
    queueMicrotask(() => {
      document.getElementById("sy-refresh")?.addEventListener("click", refresh);
      connectWS();
      refresh();
    });
    return frag;
  }

  /* ── paint ─────────────────────────────────────────────────────────── */
  function paint() {
    if (!document.querySelector(".sy-wrap")) return;
    paintLive(); paintChecked(); paintFlash();
    paintMetrics(); paintProcs(); paintServices();
    paintFilters(); paintActivity(); paintNotes();
  }

  function paintFlash() {
    const fb = document.getElementById("sy-flash");
    if (!fb) return;
    if (!flash) { fb.hidden = true; return; }
    fb.hidden = false;
    fb.textContent = flash.text;
    fb.className = "mem-flash fb-" + flash.kind;
  }

  function paintChecked() {
    const el = document.getElementById("sy-checked");
    if (!el) return;
    const bits = [];
    if (metrics && metrics.ts) bits.push(`metrics ${String(metrics.ts).replace("T", " ")}`);
    if (services && services.checked_at)
      bits.push(`services ${String(services.checked_at).replace("T", " ")}` +
        (services.cached ? ` (cached ${services.age_s}s)` : ""));
    el.textContent = bits.join(" · ");
  }

  function metricCell(label, value, pct, warnKey, unit, sub) {
    const na = value == null || value === "" || Number.isNaN(value);
    const warn = !na && warnKey != null && pct != null && pct >= THRESH[warnKey];
    const bar = (!na && pct != null)
      ? `<span class="sy-track"><span class="sy-fill${warn ? " warn" : ""}"
              style="width:${Math.min(100, Math.max(0, pct))}%"></span></span>` : "";
    return `<div class="sy-metric${na ? " is-na" : ""}${warn ? " sy-warn" : ""}">
        <span class="sy-metric-val">${na ? "N/A" : esc(value) + (unit || "")}</span>
        <span class="sy-metric-label">${esc(label)}</span>${bar}
        ${sub ? `<span class="sy-sub">${sub}</span>` : ""}
      </div>`;
  }

  function paintMetrics() {
    const box = document.getElementById("sy-metrics");
    if (!box) return;
    if (busy && !metrics) { box.innerHTML = `<div class="empty">Measuring…</div>`; return; }
    if (!metrics) {
      box.innerHTML = `<div class="empty">Metrics unavailable — see the
        error strip above.</div>`;
      return;
    }
    const cpu = metrics.cpu, ram = metrics.ram, gpu = metrics.gpu,
          tmp = metrics.temp, disk = metrics.disk, net = metrics.net;
    box.innerHTML = [
      metricCell("CPU", cpu && cpu.percent, cpu && cpu.percent, "cpu", "%"),
      metricCell("RAM", ram && ram.percent, ram && ram.percent, "ram", "%",
        ram && ram.used_gb != null
          ? `${esc(ram.used_gb)} / ${esc(ram.total_gb)} GB` : ""),
      metricCell("GPU", gpu && gpu.percent, gpu && gpu.percent, "gpu", "%"),
      metricCell("Temperature", tmp && tmp.c, tmp && tmp.c, "temp", "°C"),
      metricCell("Disk", disk && disk.percent, disk && disk.percent, "disk", "%",
        disk ? `${esc(disk.used_gb)} / ${esc(disk.total_gb)} GB` : ""),
      metricCell("Network", net && `${net.rx_kbps}↓ / ${net.tx_kbps}↑`,
        null, null, " kbps",
        net ? `total ${esc(net.rx_gb)} GB in / ${esc(net.tx_gb)} GB out` : ""),
      `<div class="sy-metric${metrics.uptime == null ? " is-na" : ""}">
         <span class="sy-metric-val">${metrics.uptime == null ? "N/A" : esc(metrics.uptime)}</span>
         <span class="sy-metric-label">Uptime</span></div>`,
      `<div class="sy-metric${metrics.process_count == null ? " is-na" : ""}">
         <span class="sy-metric-val">${metrics.process_count == null ? "N/A" : esc(metrics.process_count)}</span>
         <span class="sy-metric-label">Processes</span></div>`,
    ].join("");
    if (metrics.error) {
      box.innerHTML += `<div class="empty">${RIOIcons.get("alert-triangle", "ico-sm")} ${esc(metrics.error)}</div>`;
    }
  }

  function paintProcs() {
    const box = document.getElementById("sy-procs");
    if (!box) return;
    const rows = (metrics && metrics.procs) || null;
    if (!rows) {
      box.innerHTML = `<div class="empty">${metrics
        ? "Process list unavailable — N/A." : "Metrics unavailable."}</div>`;
      return;
    }
    if (!rows.length) { box.innerHTML = `<div class="empty">No processes.</div>`; return; }
    box.innerHTML = `<table class="sy-table"><thead><tr>
        <th>PID</th><th>Process</th><th>CPU %</th><th>RAM %</th><th>State</th>
      </tr></thead><tbody>` +
      rows.map(p => `<tr><td>${esc(p.pid)}</td><td>${esc(p.name)}</td>
        <td>${esc(p.cpu)}</td><td>${esc(p.mem)}</td>
        <td>${esc(p.status)}</td></tr>`).join("") +
      `</tbody></table>`;
  }

  function pillCls(status) {
    return status === "ONLINE" ? "hm-pill sy-on"
         : status === "WARNING" ? "hm-pill sy-warnpill"
         : status === "ERROR" ? "hm-pill sy-errpill"
         : "hm-pill sy-offpill";
  }

  function paintServices() {
    const box = document.getElementById("sy-services");
    if (!box) return;
    if (busy && !services) { box.innerHTML = `<div class="empty">Probing…</div>`; return; }
    if (!services) {
      box.innerHTML = `<div class="empty">Service status unavailable — see
        the error strip above.</div>`;
      return;
    }
    box.replaceChildren(...(services.services || []).map(s => {
      const c = document.createElement("div");
      c.className = "sy-svc";
      c.innerHTML = `
        <div class="hm-card-head">
          <span class="hm-card-label">${esc(s.name)}</span>
          <span class="${pillCls(s.status)}">${esc(s.status)}</span>
        </div>
        <div class="hm-card-detail">${esc(s.detail || "")}</div>`;
      return c;
    }));
  }

  function paintFilters() {
    const box = document.getElementById("sy-filters");
    if (!box) return;
    const counts = (activity && activity.counts) || {};
    box.replaceChildren(...FILTERS.map(f => {
      const b = document.createElement("button");
      b.className = "btn sy-chip" + (f === filter ? " active" : "");
      const n = counts[f];
      b.innerHTML = `${esc(FILTER_LABELS[f])}` +
        (n != null ? ` <span class="sy-chip-n">${n}</span>` : "");
      b.addEventListener("click", async () => {
        if (filter === f) return;
        filter = f;
        say(null);
        // user-driven fetch for the chosen category — not polling
        try {
          activity = await api(`/api/rio/system/activity?category=${filter}&limit=60`);
        } catch (e) { activity = null; say(`activity: ${e.message}`, "err"); }
        paintFilters(); paintActivity(); paintFlash();
      });
      return b;
    }));
  }

  function ageLabel(a) {
    if (a == null) return "";
    if (a < 90) return `${Math.max(0, Math.round(a))}s ago`;
    if (a < 5400) return `${Math.round(a / 60)}m ago`;
    if (a < 172800) return `${Math.round(a / 3600)}h ago`;
    return `${Math.round(a / 86400)}d ago`;
  }

  function paintActivity() {
    const box = document.getElementById("sy-activity");
    if (!box) return;
    if (busy && !activity) { box.innerHTML = `<div class="empty">Loading…</div>`; return; }
    const items = (activity && activity.items) || [];
    if (!items.length) {
      box.innerHTML = `<div class="empty">${activity
        ? `No ${filter === "all" ? "" : FILTER_LABELS[filter] + " "}activity recorded yet.`
        : "Activity unavailable — see the error strip above."}</div>`;
      return;
    }
    box.replaceChildren(...items.map(it => {
      const row = document.createElement("div");
      row.className = "sy-act" + (it.error ? " sy-err" : "");
      row.innerHTML = `
        <span class="sy-act-age">${esc(ageLabel(it.age_s))}</span>
        <span class="sy-cat ${CAT_CLASS[it.category] || ""}">${esc(it.category)}</span>
        <span class="sy-act-text">${esc(it.text)}</span>`;
      return row;
    }));
  }

  function paintNotes() {
    const box = document.getElementById("sy-notes");
    if (!box) return;
    if (busy && !notes) { box.innerHTML = `<div class="empty">Loading…</div>`; return; }
    const all = (notes && notes.items) || [];
    const items = all.filter(n => !dismissed.has(n.id));
    const summary = notes && notes.counts
      ? `<div class="sy-note-sum">` +
        ["Success", "Warning", "Error", "Information"].map(s =>
          `<span class="sy-sum sev-${s.toLowerCase()}">${esc(s)} ${notes.counts[s] || 0}</span>`
        ).join("") + `</div>` : "";
    if (!items.length) {
      box.innerHTML = summary + `<div class="empty">${notes
        ? "No notifications. Every card is derived from a real journal record."
        : "Notifications unavailable — see the error strip above."}</div>`;
      return;
    }
    const frag = document.createDocumentFragment();
    if (summary) {
      const d = document.createElement("div");
      d.innerHTML = summary;
      frag.append(d.firstChild);
    }
    for (const n of items) {
      const c = document.createElement("div");
      c.className = "sy-note sev-" + n.severity.toLowerCase();
      c.innerHTML = `
        <div class="sy-note-head">
          <span class="sy-sev">${esc(n.severity)}</span>
          <span class="sy-dom">${esc(n.domain)}</span>
          <span class="sy-note-age">${esc(ageLabel(n.age_s))}</span>
          <button class="sy-dismiss" title="dismiss">×</button>
        </div>
        <div class="sy-note-title">${esc(n.title)}</div>
        <div class="sy-note-detail">${esc(n.detail)}</div>`;
      c.querySelector(".sy-dismiss").addEventListener("click", () => {
        dismissed.add(n.id);
        saveDismissed();
        paintNotes();
      });
      frag.append(c);
    }
    box.replaceChildren(frag);
  }

  return { build };
})();

window.RIOSystem = RIOSystem;
