/* RIO Chat (Phase 3) — real chat over the EXISTING backend protocol.
 *
 * Transport:  WS /ws?token=<bearer>  (auth + 50-entry history replay +
 *             {"type":"command","text"} send + broadcast fan-out) — the same
 *             channel dashboard/server.py and the phone app already use.
 * Fallback:   POST /api/command when the socket is down (same queue).
 * Attach:     POST /api/upload.
 * Metadata:   GET /api/rio/home (session model, journal for agent match).
 * History:    GET /api/rio/conversations (+ /{id}) — the REAL append-only
 *             conversation journal (memory/conversations/*.jsonl). New turns
 *             are stamped server-side with the conversation id sent along
 *             each command, and persist across restarts. Old (unstamped)
 *             history is grouped by the documented ≥30-min silence rule.
 *
 * Replies arrive ONLY when the live assistant session consumes the queue
 * (main.py:3056). When it doesn't, this UI says so honestly — timeouts and
 * disconnects produce useful messages + Retry, never fabricated answers.
 */
"use strict";

const RIOChat = (() => {

  // ── state ────────────────────────────────────────────────────────────
  const TURN_TIMEOUT_MS = () => Number(window.__RIO_TEST_TIMEOUT) || 90000;

  let convs = [];                   // client view: {id,title,messages[],loaded?}
  let activeId = null;
  let ws = null;
  let wsState = "closed";           // connecting | open | closed
  let lastCloseCode = 0;
  let turn = null;                  // {userId, startedAt, delivered, timer}
  let rootEl = null;
  let homeMeta = null;              // cached /api/rio/home (model + journal)
  let flashMsg = null;

  // ── conversation storage (journal-backed) ───────────────────────────
  // Only the ACTIVE choice + the current conversation's id live in
  // localStorage; messages come from the server journal. `draft` holds the
  // messages of the just-created conversation until its first journal rows
  // appear (the turn completes), so the thread is never blank meanwhile.
  const LS_ACTIVE = "rio_active_conv";

  let serverConvs = [];             // [{id,title,started,last,turns,stamped}]
  let localConvs = [];              // client view: {id,title,messages[]} (draft+loaded)

  function loadActiveId() {
    try { return localStorage.getItem(LS_ACTIVE) || ""; } catch (_) { return ""; }
  }
  function saveActiveId(id) {
    try { localStorage.setItem(LS_ACTIVE, id || ""); } catch (_) {}
  }

  const active = () => convs.find(c => c.id === activeId) || convs[0];

  function newConvObj(title) {
    // Server journal-friendly id — stamped into every turn via /ws + /api/command.
    return { id: "c" + Date.now().toString(36) + Math.random().toString(36).slice(2, 7),
             title, created: Date.now(), messages: [] };
  }

  // Seed the list from the REAL journal (newest first) + keep any drafts.
  async function refreshConvList() {
    const tok = token();
    if (!tok) return;
    try {
      const r = await fetch("/api/rio/conversations",
        { headers: { "Authorization": `Bearer ${tok}` }, cache: "no-store" });
      if (r.status !== 200) return;
      const j = await r.json();
      if (!j || !j.ok || !Array.isArray(j.conversations)) return;
      serverConvs = j.conversations;
      // Keep only drafts (client conversations with local messages that are
      // newer than the journal); journal-backed cards come from the server.
      const drafted = convs.filter(c => c.messages.length && c.draft);
      convs = serverConvs.map(s => ({ id: s.id, title: s.title, messages: [],
                                      loaded: false, draft: false }));
      for (const d of drafted) convs.unshift(d);
      if (!convs.length) convs = [newConvObj("New conversation")];
      if (!convs.some(c => c.id === activeId)) activeId = convs[0].id;
      saveActiveId(activeId);
    } catch (_) { /* backend unreachable — keep current view */ }
  }

  // Load one conversation's verbatim messages from the journal (once).
  async function loadConvMessages(id) {
    const conv = convs.find(c => c.id === id);
    if (!conv || conv.loaded || conv.draft) return;
    const tok = token();
    if (!tok) return;
    try {
      const r = await fetch(`/api/rio/conversations/${encodeURIComponent(id)}`,
        { headers: { "Authorization": `Bearer ${tok}` }, cache: "no-store" });
      if (r.status !== 200) return;
      const j = await r.json();
      if (!j || !j.ok || !Array.isArray(j.messages)) return;
      conv.messages = j.messages.map(m => ({
        id: "m" + Math.random().toString(36).slice(2),
        role: m.speaker === "user" ? "user" : "assistant",
        text: m.text || "", ts: m.ts || "", state: "done", journal: true,
      }));
      conv.loaded = true;
      if (id === activeId) render();
    } catch (_) { /* keep empty view; journal row count is shown in the list */ }
  }

  function newConversation() {
    const c = newConvObj("New conversation");
    c.draft = true;                 // journal rows appear on first completed turn
    convs.unshift(c);
    activeId = c.id;
    saveActiveId(activeId);
    render();
  }

  function selectConv(id) {
    activeId = id;
    saveActiveId(id);
    loadConvMessages(id);
    render();
  }

  /* Old records are append-only history; the journal has no edit/delete.
     Draft-only conversations (no turn sent yet) can be discarded locally. */
  function deleteConv(id) {
    const c = convs.find(x => x.id === id);
    if (c && !c.draft) {
      feedback("The conversation journal is append-only — past conversations " +
               "cannot be deleted from here.", "warn");
      return;
    }
    convs = convs.filter(x => x.id !== id);
    if (!convs.length) convs = [newConvObj("New conversation")];
    if (activeId === id) activeId = convs[0].id;
    if (turn) stop(true);
    saveActiveId(activeId);
    render();
  }

  function token() {
    try { return sessionStorage.getItem("jarvis_token") || ""; } catch (_) { return ""; }
  }

  // ── WebSocket (the REAL /ws contract) ────────────────────────────────
  function connect() {
    if (ws && (wsState === "open" || wsState === "connecting")) return;
    const tok = token();
    if (!tok) { wsState = "closed"; lastCloseCode = 4001; paintConn(); return; }
    wsState = "connecting";
    paintConn();
    const proto = location.protocol === "https:" ? "wss" : "ws";
    try {
      ws = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(tok)}`);
    } catch (e) {
      wsState = "closed";
      paintConn();
      return;
    }
    ws.onopen = () => { wsState = "open"; lastCloseCode = 0; paintConn(); };
    ws.onmessage = (ev) => {
      let m;
      try { m = JSON.parse(ev.data); } catch (_) { return; }
      handleEntry(m);
    };
    ws.onclose = (ev) => {
      wsState = "closed";
      lastCloseCode = ev.code;
      paintConn();
      if (turn && !turn.delivered) failTurn(
        "Live channel closed before delivery (code " + ev.code + ").");
    };
    ws.onerror = () => { /* onclose follows — state handled there */ };
  }

  // Server replay: last 50 entries on connect. Replay history now comes
  // from the journal API (loadConvMessages); live WS entries render into the
  // open thread only when that thread is actually loaded.
  function handleEntry(m) {
    if (m && m.type === "log" && (m.speaker === "user" || m.speaker === "jarvis")) {
      if (m.speaker === "user") {
        if (turn) { onUserEcho(m); return; }
        // no pending turn → live stream entry from another surface (voice,
        // phone): it is journaled server-side, show it in the open thread.
        const conv = active();
        if (conv && conv.loaded) {
          conv.messages.push(replayMsg(m));
          render();
        }
        return;
      }
      onAssistantReply(m);
      return;
    }
    // status / sys entries: ignored for the thread (the Home page owns them)
  }

  function replayMsg(m) {
    return { id: "m" + Math.random().toString(36).slice(2),
             role: m.speaker === "user" ? "user" : "assistant",
             text: m.text || "", ts: m.ts || "",
             state: "done",
             meta: m.speaker === "assistant"
               ? { agent: "Session (direct)", model: "(from session history)",
                   status: "replayed", summary: [] }
               : undefined };
  }

  // ── helpers + markdown (escape-first: raw text never reaches innerHTML) ──
  function esc(s) {
    return String(s).replace(/[&<>"']/g, c =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function escRe(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }

  /* Minimal safe markdown: fenced code (w/ copy), tables, headings, lists,
     blockquotes, hr, inline code/bold/italic, http(s) links. Output is built
     from ESCAPED text only — no raw HTML passes through. */
  function md(src) {
    const text = String(src == null ? "" : src);
    const blocks = [];
    // 1) pull fenced code blocks out first (placeholders are inert text)
    let t = text.replace(/```([\w+-]*)\n?([\s\S]*?)(?:\n```|$)/g,
      (m, lang, code) => {
        blocks.push(`<pre class="md-pre" data-lang="${esc(lang || "")}">` +
          `<button class="btn btn-mini md-copy" title="Copy code">${RIOIcons.get("copy")}</button>` +
          `<code>${esc(code.replace(/\n$/, ""))}</code></pre>`);
        return `\u0000BLOCK${blocks.length - 1}\u0000`;
      });

    const lines = t.split("\n");
    const out = [];
    let i = 0, listType = null, para = [];
    const flushPara = () => {
      if (para.length) { out.push(`<p>${inline(para.join("\n"))}</p>`); para = []; }
    };
    const closeList = () => {
      if (listType) { out.push(`</${listType}>`); listType = null; }
    };

    while (i < lines.length) {
      const line = lines[i];
      const blockRef = line.match(/^\u0000BLOCK(\d+)\u0000$/);
      if (blockRef) { flushPara(); closeList(); out.push(blocks[+blockRef[1]]); i++; continue; }

      // table: header row + separator row
      if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length &&
          /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
        flushPara(); closeList();
        const head = line.split("|").map(s => s.trim()).filter((_, x, a) => x && x < a.length - 1);
        const sep = lines[i + 1];
        i += 2;
        const rows = [];
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i]) &&
               !/^\s*\u0000/.test(lines[i])) {
          rows.push(lines[i].split("|").map(s => s.trim())
            .filter((_, x, a) => x && x < a.length - 1));
          i++;
        }
        out.push(`<table class="md-table"><thead><tr>` +
          head.map(h => `<th>${inline(h)}</th>`).join("") + `</tr></thead><tbody>` +
          rows.map(r => `<tr>${r.map(c => `<td>${inline(c)}</td>`).join("")}</tr>`).join("") +
          `</tbody></table>`);
        continue;
      }

      const h = line.match(/^(#{1,4})\s+(.*)$/);
      if (h) { flushPara(); closeList();
        out.push(`<h${h[1].length} class="md-h">${inline(h[2])}</h${h[1].length}>`); i++; continue; }
      if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) { flushPara(); closeList(); out.push("<hr>"); i++; continue; }
      const ul = line.match(/^\s*[-*]\s+(.*)$/);
      const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
      if (ul || ol) {
        flushPara();
        const want = ul ? "ul" : "ol";
        if (listType !== want) { closeList(); out.push(`<${want}>`); listType = want; }
        out.push(`<li>${inline((ul || ol)[1])}</li>`);
        i++; continue;
      }
      const bq = line.match(/^>\s?(.*)$/);
      if (bq) { flushPara(); closeList(); out.push(`<blockquote>${inline(bq[1])}</blockquote>`); i++; continue; }
      if (line.trim() === "") { flushPara(); closeList(); i++; continue; }
      para.push(line); i++;
    }
    flushPara(); closeList();

    let html = out.join("\n");
    html = html.replace(/\u0000BLOCK(\d+)\u0000/g, (m, n) => blocks[+n]);
    return html;
  }

  function inline(s) {
    let h = esc(s);
    h = h.replace(/`([^`\n]+)`/g, "<code class=\"md-inline\">$1</code>");
    h = h.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    h = h.replace(/(?<!\*)\*([^*\n]+)\*(?!\*)/g, "<em>$1</em>");
    h = h.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    h = h.replace(/(^|[^\(])(https?:\/\/[^\s<)]+)/g,
      '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
    h = h.replace(/\n/g, "<br>");
    return h;
  }

  // ── metadata (REAL: /api/rio/home session model + journal match) ────
  async function fetchHomeMeta() {
    const tok = token();
    if (!tok) return null;
    try {
      const r = await fetch("/api/rio/home", {
        headers: { "Authorization": `Bearer ${tok}` }, cache: "no-store" });
      if (r.status !== 200) return null;
      const j = await r.json();
      if (j && j.ok) { homeMeta = j; return j; }
    } catch (_) {}
    return homeMeta;
  }

  /* Agent match: dashboard turns are dispatched straight to the live session
     (the orchestrator is in shadow mode — audit B2), so the honest default
     is "Session (direct)". If the orchestration journal recorded an agent
     result inside this turn's window, that agent name is shown instead. */
  function agentForTurn(startedAt, endedAt) {
    const fallback = "Session (direct)";
    if (!homeMeta || !homeMeta.tasks || !Array.isArray(homeMeta.tasks.recent))
      return fallback;
    const now = Date.now();
    let best = null;
    for (const e of homeMeta.tasks.recent) {
      if (e.event !== "agent_result" || e.age_s == null) continue;
      const eTs = now - e.age_s * 1000;
      if (eTs >= startedAt - 3000 && eTs <= endedAt + 3000 && e.label)
        best = e;
    }
    return best ? best.label : fallback;
  }

  function sessionModel() {
    return (homeMeta && homeMeta.session_model) || "N/A";
  }

  // ── turn lifecycle ─────────────────────────────────────────────────────
  function send(text) {
    text = (text || "").trim();
    if (!text) { feedback("Type a message first.", "warn"); return; }
    if (turn) { feedback("A reply is still pending — Stop it first.", "warn"); return; }
    const conv = active();
    const userMsg = { id: "m" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
                      role: "user", text, ts: new Date().toISOString(),
                      state: "sending" };
    conv.messages.push(userMsg);
    // assistant placeholder (state machine makes the wait visible & honest)
    const asstMsg = { id: userMsg.id + "-r", role: "assistant", text: "",
                      ts: "", state: "waiting",
                      meta: { model: sessionModel(), startedAt: Date.now() } };
    conv.messages.push(asstMsg);
    turn = { userId: userMsg.id, asstId: asstMsg.id, convId: conv.id,
             startedAt: Date.now(),
             delivered: false, stopped: false, timer: null };
    turn.timer = setTimeout(() => failTurn(
      "No reply within " + Math.round(TURN_TIMEOUT_MS() / 1000) +
      "s — the assistant session may be offline (check Home). Retry?"),
      TURN_TIMEOUT_MS());
    window.RIOBus?.emit("chat:busy");   // real event: a turn is now pending
    window.__rioLateTurn = null;      // a fresh attempt supersedes any late-watch
    const rb = document.getElementById("chat-retry");
    if (rb) rb.hidden = true;          // fresh attempt supersedes a stale Retry
    saveActiveId(activeId);
    render();
    dispatch(text);
    fetchHomeMeta();      // warm metadata while we wait
  }

  function dispatch(text) {
    // The turn's conversation id rides along so the server journals these
    // turns under this conversation (tuple tag on WS, `conv` on HTTP).
    // `mid` is the message id — the server dedupes a replay of the SAME
    // message (WS/HTTP fallback race, double-send) by it.
    const convId = (turn && turn.convId) || (active() && active().id) || "";
    const mid    = (turn && turn.userId) || "";
    if (ws && wsState === "open" && ws.readyState === 1) {
      try {
        ws.send(JSON.stringify({ type: "command", text, conv: convId, mid }));
        return;
      } catch (_) { /* fall through to HTTP */ }
    }
    // honest fallback: same queue, HTTP surface
    fetch("/api/command", {
      method: "POST",
      headers: { "Content-Type": "application/json",
                 "Authorization": `Bearer ${token()}` },
      body: JSON.stringify({ text, conv: convId, mid })
    }).then(async r => {
      if (r.status === 401) failTurn("command: pairing required — pair this browser first.");
      else if (!r.ok) failTurn("command failed: HTTP " + r.status);
      else {
        const u = msgById(turn && turn.userId);
        if (u) u.state = "sent";
        render();
      }
    }).catch(() => failTurn("backend unreachable"));
  }

  function msgById(id) {
    if (!id) return null;
    // resolve in the turn's conversation (the user may switch mid-turn)
    const conv = (turn && turn.convId)
      ? (convs.find(c => c.id === turn.convId) || active())
      : active();
    return conv.messages.find(m => m.id === id) || null;
  }

  function onUserEcho(m) {
    if (!turn) return;                       // replay/history handled elsewhere
    const u = msgById(turn.userId);
    if (!u) return;
    // dedupe: echo must match the pending text
    if ((m.text || "").trim() !== u.text.trim()) return;
    u.state = "delivered";
    turn.delivered = true;
    saveActiveId(activeId);
    render();
  }

  function onAssistantReply(m) {
    if (!turn) {
      // late reply for a turn the user STOPPED — update its placeholder,
      // honestly flagged as late (never silently dropped, never faked)
      const late = window.__rioLateTurn;
      if (late) {
        window.__rioLateTurn = null;
        const lconv = convs.find(c => c.id === late.convId) || active();
        const a = lconv.messages.find(x => x.id === late.asstId);
        const u = lconv.messages.find(x => x.id === late.userId);
        if (a && a.state === "error") {
          a.text = m.text || "";
          a.state = "done";
          a.ts = m.ts || new Date().toISOString();
          a.meta = Object.assign({}, a.meta, {
            model: sessionModel(),
            status: "stopped (reply arrived later)",
            lateAfterStop: true,
            summary: ["Request received", "Sent to session", "Stopped by you",
                      "Result arrived late"] });
          if (u && u.state === "stopped") u.state = "delivered";
          saveActiveId(activeId);
          render();
          return;
        }
      }
      // unsolicited reply (generated by another surface) — append it
      active().messages.push({
        id: "m" + Math.random().toString(36).slice(2), role: "assistant",
        text: m.text || "", ts: m.ts || "", state: "done",
        meta: { agent: "Session (direct)", model: sessionModel(),
                status: "unsolicited", summary: [] } });
      saveActiveId(activeId); render(); return;
    }
    clearTimeout(turn.timer);
    const a = msgById(turn.asstId);
    const u = msgById(turn.userId);
    const elapsed = ((Date.now() - turn.startedAt) / 1000).toFixed(1);
    const stopped = turn.stopped;
    if (u && u.state !== "delivered" && u.state !== "stopped") u.state = "done";
    if (a) {
      a.text = m.text || "";
      a.state = "done";
      a.ts = m.ts || new Date().toISOString();
      a.meta = {
        agent: "Session (direct)",
        model: sessionModel(),
        status: stopped ? "stopped (reply arrived later)" : "completed",
        elapsed_s: elapsed,
        lateAfterStop: stopped,
        summary: ["Request received", "Sent to session"]
      };
      if (!stopped) a.meta.summary.push("Result generated");
    }
    turn = null;
    window.RIOBus?.emit("chat:reply");  // real event: assistant reply arrived
    fetchHomeMeta().then(() => {
      if (!a) return;
      a.meta.agent = agentForTurn(
        (a.meta.startedAt || Date.now()) - Number(elapsed) * 1000, Date.now());
      if (a.meta.agent !== "Session (direct)" &&
          !a.meta.summary.includes("Agent: " + a.meta.agent))
        a.meta.summary.splice(2, 0, "Agent: " + a.meta.agent);
      saveActiveId(activeId); render();
    });
    saveActiveId(activeId);
    render();
  }

  function failTurn(reason) {
    if (!turn) return;
    clearTimeout(turn.timer);
    const u = msgById(turn.userId);
    const a = msgById(turn.asstId);
    if (u && u.state === "sending") u.state = "failed";
    if (a) {
      a.state = "error";
      a.text = "";
      a.meta = Object.assign({}, a.meta, {
        status: "failed", error: reason,
        summary: ["Request received", "Not delivered"] });
    }
    // The backend may still answer later (queue was already accepted).
    // Keep listening so the late reply reattaches to THIS pair instead of
    // appearing as a second, "unsolicited" bubble. (stop() does the same.)
    const t = turn;
    turn = null;
    window.RIOBus?.emit("chat:fail");   // real event: turn failed / stopped
    saveActiveId(activeId);
    render();
    feedback(reason, "err");
    showRetry(reason);
    if (!t.lateWatch) t.lateWatch = true;
    window.__rioLateTurn = t;
  }

  function showRetry(reason) {
    const box = document.getElementById("chat-retry");
    if (!box) return;
    box.innerHTML = `<span class="retry-text">${esc(reason)}</span>` +
      `<button class="btn btn-primary btn-mini" id="chat-retry-btn">Retry</button>`;
    box.hidden = false;
    document.getElementById("chat-retry-btn")?.addEventListener("click", () => {
      box.hidden = true;
      const conv = active();
      // retry = resend the last USER text (the failed assistant stub is removed)
      let idx = -1;
      for (let i = conv.messages.length - 1; i >= 0; i--)
        if (conv.messages[i].role === "user") { idx = i; break; }
      if (idx < 0) return;
      const text = conv.messages[idx].text;
      // drop the failed pair, then resend fresh
      conv.messages.splice(idx, 2);
      saveActiveId(activeId);
      send(text);
    });
  }

  function stop() {
    if (!turn) return;
    clearTimeout(turn.timer);
    turn.stopped = true;
    const u = msgById(turn.userId);
    const a = msgById(turn.asstId);
    if (u && u.state !== "delivered") u.state = "stopped";
    if (a && a.state === "waiting") {
      a.state = "error";
      a.meta = Object.assign({}, a.meta, {
        status: "stopped by user",
        summary: ["Request received", "Sent to session", "Stopped by you"] });
    }
    // the queue entry cannot be recalled (backend has no cancel API) — if a
    // reply still arrives it is appended, honestly flagged as late.
    const t = turn;
    turn = null;
    window.RIOBus?.emit("chat:fail");   // real event: user stopped the turn
    saveActiveId(activeId);
    render();
    feedback("Stopped. The backend has no cancel API — if the session still " +
             "answers, the reply will be appended.", "warn");
    // keep listening for a late reply of this turn:
    t.lateWatch = true;
    window.__rioLateTurn = t;
  }

  function regenerate() {
    if (turn) return;
    const conv = active();
    let idx = -1;
    for (let i = conv.messages.length - 1; i >= 0; i--)
      if (conv.messages[i].role === "user") { idx = i; break; }
    if (idx < 0) { feedback("Nothing to regenerate.", "warn"); return; }
    // mark the previous assistant reply (if any) superseded
    for (let i = idx + 1; i < conv.messages.length; i++) {
      if (conv.messages[i].role === "assistant") {
        conv.messages[i].meta = Object.assign({}, conv.messages[i].meta,
          { superseded: true });
        break;
      }
      if (conv.messages[i].role === "user") break;
    }
    const text = conv.messages[idx].text;
    saveActiveId(activeId);
    send(text);
  }

  async function copyMessage(m) {
    const plain = m.text || "";
    try {
      await navigator.clipboard.writeText(plain);
      flash("Copied to clipboard.");
    } catch (_) {
      // clipboard permission denied — honest fallback
      const ta = document.createElement("textarea");
      ta.value = plain;
      document.body.append(ta);
      ta.select();
      try { document.execCommand("copy"); flash("Copied to clipboard."); }
      catch (e) { feedback("Copy failed: clipboard unavailable.", "err"); }
      ta.remove();
    }
  }

  function flash(text) {
    flashMsg = { text, until: Date.now() + 2000 };
    feedback(text, "ok");
    setTimeout(() => { if (flashMsg && flashMsg.until <= Date.now()) {
      flashMsg = null; feedback(""); } }, 2100);
  }

  // ── UI ─────────────────────────────────────────────────────────────────
  function feedback(text, kind) {
    const el = document.getElementById("chat-feedback");
    if (!el) return;
    el.textContent = text || "";
    el.className = "chat-feedback" + (kind ? " fb-" + kind : "");
  }

  function paintConn() {
    const el = document.getElementById("chat-conn");
    if (!el) return;
    if (wsState === "open")
      el.innerHTML = `<i class="dot dot-ok"></i> live channel connected`;
    else if (wsState === "connecting")
      el.innerHTML = `<i class="dot dot-unknown"></i> connecting…`;
    else {
      const why = lastCloseCode === 4001 ? " (not paired)" : "";
      el.innerHTML = `<i class="dot dot-err"></i> live channel down${why} ` +
        `<button class="btn btn-mini" id="chat-reconnect">Reconnect</button>`;
      el.querySelector("#chat-reconnect")?.addEventListener("click", connect);
    }
  }

  function build() {
    // Journal is the source of truth: seed the sidebar from the server, then
    // load whichever conversation was last open (fast local empty view first;
    // messages arrive when the journal fetch resolves).
    activeId = loadActiveId();
    refreshConvList().then(() => {
      if (activeId) loadConvMessages(activeId);
      render();
    });
    if (!convs.length) convs = [newConvObj("New conversation")];
    if (!activeId || !convs.some(c => c.id === activeId)) activeId = convs[0].id;
    const frag = document.createDocumentFragment();
    rootEl = document.createElement("div");
    rootEl.className = "chat-wrap";
    rootEl.innerHTML = `
      <aside class="conv-side">
        <div class="conv-side-head">
          <button class="btn btn-primary" id="conv-new">${RIOIcons.get("plus")} New conversation</button>
        </div>
        <div id="conv-list"></div>
      </aside>
      <section class="conv-main">
        <header class="conv-head">
          <h1 id="conv-title">…</h1>
          <span class="chat-conn" id="chat-conn"></span>
        </header>
        <div class="thread" id="thread"></div>
        <div class="chat-retry" id="chat-retry" hidden></div>
        <div class="chat-feedback" id="chat-feedback" role="status"></div>
        <div class="composer">
          <textarea id="chat-input" rows="1" placeholder="Ask RIO anything…"
                    aria-label="Message"></textarea>
          <div class="composer-actions">
            <button class="btn btn-primary" id="chat-send">Send</button>
            <button class="btn" id="chat-stop" disabled>${RIOIcons.get("square")} Stop</button>
            <button class="btn" id="chat-regen" disabled>${RIOIcons.get("rotate-ccw")} Regenerate</button>
            <button class="btn" id="chat-attach" title="Attach a file">${RIOIcons.get("paperclip")}</button>
            <button class="btn btn-mute" id="chat-mute" title="Mute the assistant's microphone">${RIOIcons.get("mic")}</button>
            <button class="btn" id="chat-mic" title="Voice input">${RIOIcons.get("mic")}</button>
            <input type="file" id="chat-file" hidden>
            <span class="attach-name dim" id="attach-name"></span>
          </div>
        </div>
      </section>`;
    frag.append(rootEl);

    rootEl.querySelector("#conv-new").addEventListener("click", newConversation);
    rootEl.querySelector("#chat-send").addEventListener("click", () => {
      const inp = rootEl.querySelector("#chat-input");
      const att = rootEl.querySelector("#attach-name").textContent;
      const text = (inp.value || "") + (att ? `\n📎 ${att}` : "");
      inp.value = "";
      rootEl.querySelector("#attach-name").textContent = "";
      send(text);
    });
    rootEl.querySelector("#chat-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        rootEl.querySelector("#chat-send").click();
      }
    });
    rootEl.querySelector("#chat-stop").addEventListener("click", () => stop());
    rootEl.querySelector("#chat-regen").addEventListener("click", regenerate);

    // attach → real POST /api/upload
    const file = rootEl.querySelector("#chat-file");
    rootEl.querySelector("#chat-attach").addEventListener("click", () => file.click());
    file.addEventListener("change", async () => {
      const f = file.files && file.files[0];
      if (!f) return;
      feedback(`Uploading ${f.name}…`);
      const fd = new FormData();
      fd.append("file", f);
      try {
        const r = await fetch("/api/upload", { method: "POST", body: fd,
          headers: { "Authorization": `Bearer ${token()}` } });
        const j = await r.json().catch(() => null);
        if (r.status === 200 && j && (j.ok || j.name)) {
          rootEl.querySelector("#attach-name").textContent = j.name || f.name;
          feedback(`Attached: ${j.name || f.name} — add text and press Send.`, "ok");
        } else if (r.status === 401) feedback("attach: pairing required (/login).", "err");
        else feedback("attach: " + ((j && j.error) || ("HTTP " + r.status)), "err");
      } catch (_) { feedback("attach: backend unreachable", "err"); }
      file.value = "";
    });

    // voice input — real Web Speech API, feature-detected (never faked)
    // Gate: while the assistant's mic is muted (shared state with Home's
    // mute button) the local voice input is off too — never record into a
    // muted microphone.
    const mic = rootEl.querySelector("#chat-mic");
    const chatMute = rootEl.querySelector("#chat-mute");
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) {
      mic.dataset.srOff = "1";      // paintMicMute must not re-enable it
      mic.disabled = true;
      mic.title = "Voice input not supported in this browser";
    } else {
      const rec = new SR();
      rec.lang = navigator.language || "en-US";
      rec.interimResults = false;
      let listening = false;
      rec.onresult = (ev) => {
        const t = ev.results[0][0].transcript;
        const inp = rootEl.querySelector("#chat-input");
        inp.value = (inp.value ? inp.value + " " : "") + t;
        feedback("Voice captured.", "ok");
      };
      rec.onend = () => { listening = false; mic.classList.remove("live"); };
      rec.onerror = () => { listening = false; mic.classList.remove("live");
        feedback("Voice input failed — type instead.", "err"); };
      mic.addEventListener("click", () => {
        if (micMuted === true) { feedback("Mic is muted — unmute first.", "warn"); return; }
        if (listening) { rec.stop(); return; }
        listening = true;
        mic.classList.add("live");
        rec.start();
      });
      // Mute button (same real state as Home's): click → mic off; stop any
      // in-flight capture so it can't keep recording into a muted mic.
      chatMute.addEventListener("click", async () => {
        const now = await toggleMicMute();
        if (now === true && listening) {
          try { rec.stop(); } catch (_) { /* already stopped */ }
          listening = false;
          mic.classList.remove("live");
          feedback("Microphone muted.", "warn");
        } else if (now === false) {
          feedback("Microphone live again.", "ok");
        }
      });
    }

    render();
    connect();
    fetchHomeMeta();
    fetchMicMute();          // shared real mic-mute state (Home ↔ Chat)
    // the caller mounts the fragment later in THIS task — repaint once mounted
    // (initial render/paint above no-op while detached)
    setTimeout(() => {
      if (rootEl && document.body.contains(rootEl)) { render(); paintConn(); }
    }, 0);
    return frag;
  }

  function render() {
    if (!rootEl || !document.body.contains(rootEl)) return;

    const list = document.getElementById("conv-list");
    if (list) {
      list.replaceChildren(...convs.map(c => {
        const row = document.createElement("div");
        row.className = "conv-item" + (c.id === activeId ? " active" : "");
        row.innerHTML = `<span class="conv-name">${esc(c.title)}</span>
          <span class="conv-acts">
            <button class="conv-btn" data-act="rename" title="Rename">${RIOIcons.get("pen", "ico-sm")}</button>
            <button class="conv-btn" data-act="del" title="Delete">${RIOIcons.get("trash", "ico-sm")}</button>
          </span>`;
        row.querySelector(".conv-name").addEventListener("click", () => selectConv(c.id));
        row.querySelector('[data-act="rename"]').addEventListener("click", () => {
          const t = prompt("Rename conversation", c.title);
          if (t != null) renameConv(c.id, t);
        });
        row.querySelector('[data-act="del"]').addEventListener("click", () => {
          if (confirm(`Delete "${c.title}"?`)) deleteConv(c.id);
        });
        return row;
      }));
    }
    const title = document.getElementById("conv-title");
    if (title) title.textContent = active().title;

    paintConn();

    const thread = document.getElementById("thread");
    if (thread) {
      const conv = active();
      if (!conv.messages.length) {
        thread.innerHTML = `<div class="empty">No messages yet — ask RIO anything. ` +
          `Messages travel over the live WebSocket channel the phone app uses.</div>`;
      } else {
        thread.replaceChildren(...conv.messages.map(bubble));
      }
      thread.scrollTop = thread.scrollHeight;
    }

    const sending = !!turn;
    const stopBtn = document.getElementById("chat-stop");
    const regenBtn = document.getElementById("chat-regen");
    if (stopBtn) stopBtn.disabled = !sending;
    if (regenBtn) regenBtn.disabled = sending ||
      !active().messages.some(m => m.role === "user");

    if (flashMsg && flashMsg.until > Date.now()) feedback(flashMsg.text, "ok");
  }

  function bubble(m) {
    const el = document.createElement("div");
    el.className = "msg " + (m.role === "user" ? "msg-user" : "msg-rio") +
      (m.state === "error" ? " msg-failed" : "");
    el.dataset.msgId = m.id;
    const isAsst = m.role === "assistant";
    const stateChip = m.role === "user" && m.state && m.state !== "done"
      ? `<span class="st st-${m.state}">${esc(m.state)}</span>` : "";
    const body = isAsst
      ? md(m.text)
      : `<div class="msg-text">${esc(m.text).replace(/\n/g, "<br>")}</div>`;

    const meta = m.meta || {};
    let metaHtml = "";
    if (isAsst && m.state === "done") {
      const steps = (meta.summary && meta.summary.length)
        ? meta.summary.map(s => `<span>${esc(s)}</span>`)
            .join(`<span class="step-arrow">→</span>`)
        : "";
      metaHtml = `
        <div class="msg-meta">
          <span>Agent: <b>${esc(meta.agent || "Session (direct)")}</b></span>
          <span>Model: <b>${esc(meta.model || "N/A")}</b></span>
          <span>Status: <b>${esc(meta.status || "—")}</b>${
            meta.elapsed_s != null ? ` (${esc(String(meta.elapsed_s))}s)` : ""}</span>
        </div>
        ${steps ? `<div class="msg-steps">${steps}</div>` : ""}
        ${meta.superseded ? `<div class="msg-note">superseded by a regenerated reply</div>` : ""}
        ${meta.lateAfterStop ? `<div class="msg-note warn">arrived after you pressed Stop</div>` : ""}`;
    } else if (isAsst && m.state === "error") {
      metaHtml = `<div class="msg-meta"><span>Status: <b class="err-txt">${
        esc(meta.status || "failed")}</b></span></div>` +
        (meta.error ? `<div class="msg-note err-txt">${esc(meta.error)}</div>` : "");
    } else if (isAsst && m.state === "waiting") {
      metaHtml = `<div class="msg-wait">waiting for RIO…</div>`;
    }
    const copyBtn = isAsst && m.state === "done" && m.text
      ? `<button class="btn btn-mini msg-copy" title="Copy">${RIOIcons.get("copy")} Copy</button>` : "";

    el.innerHTML = `
      <div class="msg-head">${isAsst ? "RIO" : "You"} ${stateChip}</div>
      <div class="msg-body">${body}</div>
      ${metaHtml}${copyBtn}`;
    el.querySelector(".msg-copy")?.addEventListener("click", () => copyMessage(m));
    el.querySelectorAll(".md-copy").forEach(btn =>
      btn.addEventListener("click", () => {
        const code = btn.parentElement.querySelector("code");
        if (code) navigator.clipboard?.writeText(code.textContent);
        btn.innerHTML = RIOIcons.get("check", "ico-sm");
        setTimeout(() => { btn.innerHTML = RIOIcons.get("copy", "ico-sm"); }, 1200);
      }));
    return el;
  }

  return { build, send, stop, regenerate,
           _state: () => ({ wsState, turn: !!turn }) };
})();
window.RIOChat = RIOChat;
