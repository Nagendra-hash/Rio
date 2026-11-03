/* RIO Hermes Control Center (Phase 7) — built on the EXISTING bridge.
 *
 * Statuses come from /api/rio/hermes (real probes + the gateway's own log),
 * statistics from /api/rio/hermes/stats (gateway.log + state.db), recent
 * messages from the gateway's record of what actually passed through, and
 * the pipeline lights up from memory/hermes_events.jsonl — pushed live over
 * the SAME /ws the chat log uses ({type:"hermes_event"}). Nothing here is
 * simulated: a stage with no event simply stays unlit.
 */
"use strict";

const RIOHermes = (() => {

  let status = null;      // GET /api/rio/hermes
  let stats = null;       // GET /api/rio/hermes/stats
  let recent = null;      // GET /api/rio/hermes/recent
  let events = [];        // GET /api/rio/hermes/events (newest first)
  let busy = false;
  let flash = null;
  let ws = null;
  let poll = null;

  // Pipeline definitions — exactly the two flows from the spec.
  const PIPELINES = [
    { id: "telegram",
      nodes: ["Telegram", "Hermes", "RIO", "Agent", "Tool", "Response",
              "Hermes", "Telegram"] },
    { id: "whatsapp",
      nodes: ["WhatsApp", "Hermes", "RIO", "Agent", "Response", "Hermes",
              "WhatsApp"] },
  ];

  // stage → pipeline node names it lights up
  const STAGE_NODES = {
    message_received: ["Telegram", "WhatsApp", "Hermes"],
    processing: ["RIO"],
    agent_selected: ["Agent"],
    response_generated: ["Response"],
    response_sent: ["Hermes", "Telegram", "WhatsApp"],
  };
  const STAGE_LABELS = {
    message_received: "Message received",
    processing: "Processing",
    agent_selected: "Agent selected",
    response_generated: "Response generated",
    response_sent: "Response sent",
  };

  function token() {
    try { return sessionStorage.getItem("jarvis_token") || ""; } catch (_) { return ""; }
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
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
      new Error((j && (j.error || j.detail)) || (`HTTP ${r.status}`)),
      { code: r.status, body: j });
    return j;
  }

  function say(text, kind) { flash = text ? { text, kind: kind || "ok" } : null; }

  // ── data ──────────────────────────────────────────────────────────────
  async function refresh() {
    busy = true; paint();
    const errs = [];
    try { status = await api("/api/rio/hermes"); }
    catch (e) { status = null; errs.push(`status: ${e.message}`); }
    try { stats = await api("/api/rio/hermes/stats"); }
    catch (e) { stats = null; errs.push(`stats: ${e.message}`); }
    try { recent = await api("/api/rio/hermes/recent?limit=20"); }
    catch (e) { recent = null; errs.push(`recent: ${e.message}`); }
    try {
      const ev = await api("/api/rio/hermes/events?limit=40");
      events = ev.events || [];
    } catch (e) { errs.push(`events: ${e.message}`); }
    say(errs.length ? errs.join(" · ") : null, errs.length ? "err" : null);
    busy = false; paint();
  }

  // ── live events (existing /ws) ────────────────────────────────────────
  function connectWS() {
    try {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(token())}`);
    } catch (_) { return; }
    ws.onmessage = (ev) => {
      let m = null;
      try { m = JSON.parse(ev.data); } catch (_) { return; }
      if (m && m.type === "hermes_event" && m.event && m.event.stage) {
        events.unshift(m.event);
        events = events.slice(0, 80);
        paint();
      }
    };
    ws.onclose = () => { ws = null; };
  }

  // ── shell ─────────────────────────────────────────────────────────────
  function build() {
    const frag = document.createDocumentFragment();
    const root = document.createElement("div");
    root.className = "hm-wrap";
    root.innerHTML = `
      <h1>Hermes</h1>
      <p class="sub">The Telegram / WhatsApp bridge — real connection state,
        real message counts and the live message pipeline. Every number is
        measured from the gateway's own log, database or probes; a value the
        system cannot know shows <b>N/A</b>, never a guess.</p>

      <div class="hm-statusbar" id="hm-statusbar"></div>
      <div class="mem-flash" id="hm-flash" hidden></div>

      <h2>Messages today</h2>
      <div class="hm-stats" id="hm-stats"></div>

      <h2>Live pipeline</h2>
      <p class="hm-legend">Telegram → Hermes → RIO → Agent → Tool → Response
        → Hermes → Telegram &nbsp;·&nbsp; WhatsApp → Hermes → RIO → Agent →
        Response → WhatsApp</p>
      <div class="hm-pipes" id="hm-pipes"></div>
      <div class="hm-feed" id="hm-feed"></div>

      <h2>Recent messages</h2>
      <div class="hm-bar">
        <button class="btn" id="hm-refresh">↻ Refresh</button>
        <span class="hm-checked" id="hm-checked"></span>
      </div>
      <div class="hm-recent" id="hm-recent"></div>`;
    frag.append(root);

    queueMicrotask(() => {
      document.getElementById("hm-refresh")?.addEventListener("click", refresh);
      connectWS();
      refresh();
      poll = setInterval(() => {
        if (!document.querySelector(".hm-wrap")) { clearInterval(poll); poll = null; return; }
        refresh();
      }, 30000);
    });
    return frag;
  }

  // ── paint ─────────────────────────────────────────────────────────────
  function paint() {
    const root = document.querySelector(".hm-wrap");
    if (!root) return;
    paintStatus();
    paintFlash();
    paintStats();
    paintPipeline();
    paintFeed();
    paintRecent();
  }

  function pillClass(st) {
    return st === "CONNECTED" ? "hm-pill hm-ok"
         : st === "ERROR" ? "hm-pill hm-err"
         : st === "DISCONNECTED" ? "hm-pill hm-off"
         : "hm-pill hm-unknown";
  }

  function paintStatus() {
    const bar = document.getElementById("hm-statusbar");
    if (!bar) return;
    if (busy && !status) { bar.innerHTML = `<div class="empty">Checking…</div>`; return; }
    const cards = [
      ["HERMES", status && status.hermes],
      ["Telegram", status && status.telegram],
      ["WhatsApp", status && status.whatsapp],
    ];
    bar.replaceChildren(...cards.map(([label, s]) => {
      const c = document.createElement("div");
      c.className = "hm-card";
      const st = s ? s.status : "N/A";
      c.innerHTML = `
        <div class="hm-card-head">
          <span class="hm-card-label">${esc(label)}</span>
          <span class="${s ? pillClass(st) : "hm-pill hm-unknown"}">${
            s ? esc(st) : "N/A"}</span>
        </div>
        <div class="hm-card-detail">${s ? esc(s.detail || "") : "backend unavailable"}</div>`;
      return c;
    }));
    const chk = document.getElementById("hm-checked");
    if (chk) chk.textContent = status && status.checked_at
      ? `checked ${status.checked_at.replace("T", " ")}` : "";
  }

  function paintFlash() {
    const fb = document.getElementById("hm-flash");
    if (!fb) return;
    if (!flash) { fb.hidden = true; return; }
    fb.hidden = false;
    fb.textContent = flash.text;
    fb.className = "mem-flash fb-" + flash.kind;
  }

  function paintStats() {
    const box = document.getElementById("hm-stats");
    if (!box) return;
    if (busy && !stats) { box.innerHTML = `<div class="empty">Counting…</div>`; return; }
    const s = stats || {};
    const cells = [
      ["Messages today", s.messages],
      ["Incoming", s.incoming],
      ["Outgoing", s.outgoing],
      ["Failed", s.failed],
      ["Via RIO intake", s.rio_intake],
    ];
    box.replaceChildren(...cells.map(([label, val]) => {
      const c = document.createElement("div");
      c.className = "hm-stat";
      const v = val == null || val === "" ? "N/A" : String(val);
      const isNa = v === "N/A";
      c.innerHTML = `<span class="hm-stat-val${isNa ? " is-na" : ""}">${esc(v)}</span>
                     <span class="hm-stat-label">${esc(label)}</span>`;
      return c;
    }));
    // per-channel breakdown (real counts from the gateway log)
    const ch = s.channels || {};
    const keys = Object.keys(ch);
    if (keys.length) {
      const chip = document.createElement("div");
      chip.className = "hm-chan";
      chip.innerHTML = keys.map(k =>
        `<span class="mem-tag">${esc(k)} · ${ch[k].incoming} in / ${ch[k].outgoing} out</span>`
      ).join(" ");
      box.append(chip);
    }
    if (s.error) {
      const e = document.createElement("div");
      e.className = "hm-chan";
      e.innerHTML = `<span class="mem-tag">sources: ${esc(s.error)}</span>`;
      box.append(e);
    }
  }

  // which stages have fired recently (from the event feed)
  function activeStages() {
    const set = new Set();
    for (const e of events.slice(0, 30)) set.add(e.stage);
    return set;
  }

  function paintPipeline() {
    const box = document.getElementById("hm-pipes");
    if (!box) return;
    const live = activeStages();
    box.replaceChildren(...PIPELINES.map(p => {
      const row = document.createElement("div");
      row.className = "hm-pipe";
      const nodes = p.nodes.map((n, i) => {
        const lit = [...live].some(st =>
          (STAGE_NODES[st] || []).includes(n));
        const arrow = i < p.nodes.length - 1
          ? `<span class="hm-arrow">→</span>` : "";
        return `<span class="hm-node${lit ? " lit" : ""}">${esc(n)}</span>${arrow}`;
      }).join("");
      row.innerHTML = `<span class="hm-pipe-label">${p.id === "telegram"
        ? "Telegram flow" : "WhatsApp flow"}</span>
        <span class="hm-chain">${nodes}</span>`;
      return row;
    }));
  }

  function paintFeed() {
    const box = document.getElementById("hm-feed");
    if (!box) return;
    const recent5 = events.slice(0, 8);
    if (!recent5.length) {
      box.innerHTML = `<div class="empty">No pipeline events yet — they appear
        live when a Telegram/WhatsApp message reaches RIO.</div>`;
      return;
    }
    box.replaceChildren(...recent5.map(e => {
      const row = document.createElement("div");
      row.className = "hm-ev";
      const okMark = e.ok === false ? `<span class="hm-fail">failed</span>` : "";
      row.innerHTML = `
        <span class="hm-ev-stage st-${esc(e.stage)}">${
          esc(STAGE_LABELS[e.stage] || e.stage)}</span>
        <span class="hm-ev-meta">${esc((e.ts || "").replace("T", " ").slice(0, 19))}
          · ${esc(e.channel || e.platform || "")}${
            e.agent ? ` · agent ${esc(e.agent)}` : ""}${
            e.text ? ` · “${esc(e.text)}”` : ""}${
            e.detail ? ` · ${esc(e.detail)}` : ""} ${okMark}</span>`;
      return row;
    }));
  }

  function paintRecent() {
    const box = document.getElementById("hm-recent");
    if (!box) return;
    if (busy && !recent) { box.innerHTML = `<div class="empty">Loading…</div>`; return; }
    const msgs = (recent && recent.messages) || [];
    if (!msgs.length) {
      box.innerHTML = `<div class="empty">${recent && recent.available === false
        ? "Message stores unavailable — N/A."
        : "No messages recorded yet."}</div>`;
      return;
    }
    box.replaceChildren(...msgs.map(m => {
      const row = document.createElement("div");
      row.className = "mem-row hm-msg " + (m.direction === "incoming"
        ? "hm-in" : "hm-out");
      const arrow = m.direction === "incoming" ? "↓ in" : "↑ out";
      row.innerHTML = `
        <div class="mem-main">
          <span class="hm-dir">${esc(arrow)}</span>
          <span class="mem-key">${esc(m.who || "")}</span>
          <span class="mem-tag">${esc(m.platform || "")}${m.chat ? " · " + esc(m.chat) : ""}</span>
          <span class="mem-tag">${esc(m.status || "")}</span>
        </div>
        <div class="mem-val">${esc(m.text || "")}</div>
        <div class="mem-meta"><span>${esc(m.ts || "")}</span></div>`;
      return row;
    }));
  }

  return { build };
})();

window.RIOHermes = RIOHermes;
