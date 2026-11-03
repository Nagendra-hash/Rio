/* RIO Hermes assistant dock — shared by the News, Trading and Backtest pages.
 *
 * One contextual drawer, mounted by each page module. It shows the REAL
 * bridge state (GET /api/rio/hermes), sends a page-aware question through
 * the existing Hermes task bridge (POST /api/rio/hermes/ask, async 202),
 * and polls the answer (GET /api/rio/hermes/ask/{id}). Recent asks come
 * from GET /api/rio/hermes/asks so a reload can pick the thread back up.
 *
 * Rules:
 *   • status and answers are measured — a failed bridge is shown failed
 *   • the page context is display data, quoted to Hermes as untrusted
 *   • research only: this module has no order, broker or execution path
 *   • polling stops when the drawer closes or the page is replaced
 */
"use strict";

const RIOHermesDock = (() => {

  const STATUS_TTL_MS = 30_000;
  const ASK_POLL_MS = 1_500;
  const MAX_PROMPT = 4_000;
  const MAX_CONTEXT = 5_000;
  const ACTIVE = new Set(["QUEUED", "RUNNING"]);

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function token() {
    try { return sessionStorage.getItem("jarvis_token") || ""; } catch (_) { return ""; }
  }

  async function api(path, { method = "GET", body = null } = {}) {
    const headers = { "Authorization": `Bearer ${token()}` };
    const opts = { method, headers, cache: "no-store" };
    if (body != null) {
      headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(path, opts);
    let data = null;
    try { data = await res.json(); } catch (_) { data = null; }
    if (!res.ok) {
      const msg = (data && (data.error || data.detail)) ||
        (res.status === 401 ? "session expired — sign in again"
                            : `request failed (${res.status})`);
      const err = new Error(String(msg));
      err.status = res.status;
      throw err;
    }
    return data || {};
  }

  function isPending(status) { return ACTIVE.has(String(status || "").toUpperCase()); }

  function fmtWhen(epoch) {
    const n = Number(epoch);
    if (!Number.isFinite(n) || n <= 0) return "";
    const d = new Date(n * 1000);
    return d.toLocaleString([], { month: "short", day: "numeric",
                                  hour: "2-digit", minute: "2-digit" });
  }

  function fmtElapsed(ask) {
    const start = Number(ask && ask.started_at);
    if (!Number.isFinite(start) || start <= 0) return "";
    const secs = Math.max(0, Math.round(Date.now() / 1000 - start));
    return secs < 60 ? `${secs}s` : `${Math.floor(secs / 60)}m ${secs % 60}s`;
  }

  function pillClass(st) {
    return st === "CONNECTED" ? "hd-pill ok"
         : st === "ERROR" ? "hd-pill err"
         : st === "DISCONNECTED" ? "hd-pill off"
         : "hd-pill unknown";
  }

  function trimContext(text) {
    const s = String(text == null ? "" : text).trim();
    if (s.length <= MAX_CONTEXT) return s;
    return s.slice(0, MAX_CONTEXT - 80).trimEnd() +
      "\n… (page context truncated)";
  }

  function getStored(key) {
    try { return sessionStorage.getItem(key) || ""; } catch (_) { return ""; }
  }
  function setStored(key, value) {
    try { value ? sessionStorage.setItem(key, value)
                : sessionStorage.removeItem(key); } catch (_) { /* ignore */ }
  }

  function attach(host, opts = {}) {
    if (!host || !opts.page) return null;
    if (host.querySelector(":scope > .hd-root")) return null;

    const page = String(opts.page);
    const label = String(opts.label || page);
    const getContext = typeof opts.context === "function"
      ? opts.context : () => "";
    const storageKey = `rio_hermes_ask_${page}`;
    const drawerId = `hd-drawer-${page}`;

    const state = {
      open: false,
      status: null,
      statusAt: 0,
      statusError: "",
      asks: [],
      submitting: false,
      statusTimer: null,
      polling: new Set(),
      askTimers: new Map(),
      threadSig: null,
    };

    const root = document.createElement("div");
    root.className = "hd-root";
    root.innerHTML = `
      <button class="hd-fab" type="button" aria-expanded="false"
              aria-controls="${drawerId}" aria-label="Ask Hermes about this page"
              title="Ask Hermes about this page">
        <span class="hd-fab-ico" aria-hidden="true">${
          window.RIOIcons ? RIOIcons.get("sparkles") : "H"}</span>
        <span class="hd-fab-label">Hermes</span>
        <span class="hd-fab-dot unknown" data-dot></span>
      </button>
      <section class="hd-drawer" id="${drawerId}" role="dialog"
               aria-modal="false" aria-label="Hermes assistant" hidden>
        <header class="hd-head">
          <div class="hd-title">
            <span class="hd-orb" aria-hidden="true">H</span>
            <div>
              <b>Hermes</b>
              <span>local research assistant · ${esc(label)}</span>
            </div>
          </div>
          <div class="hd-head-actions">
            <button class="hd-icon-btn" type="button" data-refresh
                    title="Refresh bridge status" aria-label="Refresh bridge status">↻</button>
            <button class="hd-icon-btn" type="button" data-close
                    title="Close Hermes" aria-label="Close Hermes">✕</button>
          </div>
        </header>

        <div class="hd-status" data-status>
          <span class="skel">checking bridge…</span>
        </div>

        <details class="hd-context">
          <summary>Page context Hermes will see</summary>
          <pre data-context>collecting context…</pre>
        </details>

        <div class="hd-thread" data-thread role="log"
             aria-live="polite" aria-label="Hermes conversation"></div>

        <form class="hd-ask" data-form>
          <textarea data-input rows="2" maxlength="${MAX_PROMPT}"
                    aria-label="Question for Hermes"
                    placeholder="Ask Hermes about this page…"></textarea>
          <div class="hd-askrow">
            <span class="hd-hint">⌘/Ctrl + Enter · research only, no orders</span>
            <button class="btn hd-send" type="submit" data-send>Ask Hermes</button>
          </div>
          <div class="hd-error" data-error role="alert" hidden></div>
        </form>

        <footer class="hd-foot">
          <span>Runs the local Hermes bridge on this machine.</span>
          <a class="hd-link" href="/hermes">Full control center →</a>
        </footer>
      </section>`;

    host.append(root);

    const els = {
      fab: root.querySelector(".hd-fab"),
      dot: root.querySelector("[data-dot]"),
      drawer: root.querySelector(".hd-drawer"),
      status: root.querySelector("[data-status]"),
      context: root.querySelector("[data-context]"),
      thread: root.querySelector("[data-thread]"),
      form: root.querySelector("[data-form]"),
      input: root.querySelector("[data-input]"),
      send: root.querySelector("[data-send]"),
      error: root.querySelector("[data-error]"),
      close: root.querySelector("[data-close]"),
      refresh: root.querySelector("[data-refresh]"),
    };

    // ── context ────────────────────────────────────────────────────────
    function contextText() {
      let text = "";
      try { text = getContext() || ""; } catch (err) {
        text = `Page: ${label}\n(context unavailable: ${err && err.message})`;
      }
      return trimContext(text);
    }

    function paintContext() {
      const text = contextText();
      els.context.textContent = text || "(no page context available yet)";
      return text;
    }

    // ── bridge status ──────────────────────────────────────────────────
    async function loadStatus(force = false) {
      if (!root.isConnected) { close(); return; }
      if (!force && state.status &&
          Date.now() - state.statusAt < STATUS_TTL_MS) {
        paintStatus();
        return;
      }
      if (force) els.status.innerHTML = `<span class="skel">checking bridge…</span>`;
      try {
        const data = await api("/api/rio/hermes");
        if (data && data.ok === false) {
          throw new Error(data.error || "bridge status unavailable");
        }
        state.status = data;
        state.statusAt = Date.now();
        state.statusError = "";
      } catch (err) {
        state.statusError = err && err.message ? err.message : "unavailable";
      }
      paintStatus();
      paintDot();
    }

    function paintStatus() {
      if (state.statusError) {
        els.status.innerHTML = `<div class="hd-status-err">Status unavailable
          — ${esc(state.statusError)}</div>`;
        return;
      }
      if (!state.status) {
        els.status.innerHTML = `<span class="skel">checking bridge…</span>`;
        return;
      }
      const s = state.status;
      const rows = [
        ["Hermes", s.hermes],
        ["Telegram", s.telegram],
        ["WhatsApp", s.whatsapp],
      ];
      const pills = rows.map(([name, item]) => {
        const st = item && item.status ? String(item.status) : "";
        const detail = item && item.detail ? String(item.detail) : "";
        return `<span class="hd-stat" title="${esc(detail || name)}">
          <span class="hd-stat-name">${esc(name)}</span>
          <span class="${item ? pillClass(st) : "hd-pill unknown"}">${
            esc(st || "N/A")}</span></span>`;
      }).join("");
      const checked = s.checked_at
        ? `<span class="hd-checked">checked ${esc(String(s.checked_at)
            .replace("T", " "))}</span>` : "";
      els.status.innerHTML = `<div class="hd-pills">${pills}</div>${checked}`;
    }

    function paintDot() {
      const st = state.status && state.status.hermes
        ? String(state.status.hermes.status) : "";
      const cls = st === "CONNECTED" ? "ok"
                : st === "ERROR" ? "err"
                : st === "DISCONNECTED" ? "off" : "unknown";
      els.dot.className = `hd-fab-dot ${cls}`;
      const detail = state.status && state.status.hermes
        ? String(state.status.hermes.detail || st) : "status unknown";
      els.fab.title = `Hermes · ${st || "status unknown"} — ${detail}`;
    }

    // ── thread ─────────────────────────────────────────────────────────
    function sortAsks() {
      state.asks.sort((a, b) =>
        (Number(a.created_at) || 0) - (Number(b.created_at) || 0));
    }

    function updateAsk(ask) {
      if (!ask || !ask.ask_id) return;
      const i = state.asks.findIndex(a => a.ask_id === ask.ask_id);
      if (i >= 0) state.asks[i] = ask;
      else state.asks.push(ask);
      sortAsks();
      if (state.asks.length > 12) state.asks = state.asks.slice(-12);
    }

    function paintThread() {
      const rows = state.asks.slice(-8);
      // Replacing innerHTML every poll makes screen readers repeat the whole
      // thread. Paint only when the content (or the elapsed-time bucket)
      // actually changed.
      const sig = rows.map(a => [
        a.ask_id, a.status, String(a.answer || "").length,
        String(a.error || "").length,
        a.started_at ? Math.floor(Date.now() / 15_000) : 0,
      ].join(":")).join("|");
      if (sig === state.threadSig) return;
      state.threadSig = sig;
      if (!rows.length) {
        els.thread.innerHTML = `<div class="hd-empty">No questions yet.
          Hermes sees the page context above — ask about a headline, a
          symbol, or a backtest result.</div>`;
        return;
      }
      els.thread.innerHTML = rows.map(ask => {
        const pending = isPending(ask.status);
        const failed = String(ask.status || "").toUpperCase() === "FAILED";
        const when = fmtWhen(ask.created_at);
        const elapsed = pending ? fmtElapsed(ask) : "";
        return `<div class="hd-turn" data-ask="${esc(ask.ask_id)}">
          <div class="hd-user">
            <div class="hd-meta"><span>You</span>${when
              ? `<time>${esc(when)}</time>` : ""}</div>
            <p class="hd-prompt">${esc(ask.prompt)}</p>
          </div>
          <div class="hd-reply${pending ? " is-pending" : ""}${
            failed ? " is-failed" : ""}">
            <div class="hd-meta"><span>Hermes${pending
              ? ` · ${esc(String(ask.status).toLowerCase())}` : ""}</span>${
              elapsed ? `<time>${esc(elapsed)}</time>` : ""}</div>
            ${pending ? `<div class="hd-work"><span class="hd-spin"
              aria-hidden="true"></span>${ask.status === "QUEUED"
                ? "queued — the bridge is busy" : "working through the task"}…</div>` : ""}
            ${ask.answer ? `<pre class="hd-answer">${esc(ask.answer)}</pre>
              <button class="hd-copy" type="button"
                      data-copy="${esc(ask.ask_id)}">Copy</button>` : ""}
            ${ask.error ? `<div class="hd-err"><b>Bridge error</b>
              ${esc(ask.error)}</div>` : ""}
          </div>
        </div>`;
      }).join("");

      els.thread.querySelectorAll("[data-copy]").forEach(btn => {
        btn.addEventListener("click", async () => {
          const ask = state.asks.find(a => a.ask_id === btn.dataset.copy);
          if (!ask || !ask.answer) return;
          try {
            await navigator.clipboard.writeText(ask.answer);
            const old = btn.textContent;
            btn.textContent = "Copied";
            setTimeout(() => { btn.textContent = old; }, 1200);
          } catch (_) { btn.textContent = "Copy failed"; }
        });
      });
      els.thread.scrollTop = els.thread.scrollHeight;
    }

    async function loadAsks() {
      try {
        const data = await api(
          `/api/rio/hermes/asks?page=${encodeURIComponent(page)}&limit=12`);
        const server = Array.isArray(data.asks) ? data.asks : [];
        const byId = new Map(server.map(a => [a.ask_id, a]));
        // Keep a just-submitted local ask if the server has not listed it yet.
        for (const local of state.asks) {
          if (!byId.has(local.ask_id)) byId.set(local.ask_id, local);
        }
        state.asks = [...byId.values()];
        sortAsks();
      } catch (err) {
        showError(`Could not load recent asks: ${err.message}`);
      }
      paintThread();
      resumePendingAsks();
    }

    async function resumePendingAsks() {
      const stored = getStored(storageKey);
      if (stored && !state.asks.some(a => a.ask_id === stored)) {
        // The stored ask may be older than the 12-ask list (or the list
        // request may have failed); fetch it directly when it still exists.
        try {
          const data = await api(
            `/api/rio/hermes/ask/${encodeURIComponent(stored)}`);
          if (data.ask) updateAsk(data.ask);
        } catch (err) {
          if (err.status === 404) setStored(storageKey, "");
        }
      }
      // Resume EVERY queued/running ask for this page, not just the last one.
      // The backend runs one at a time, so a burst can legitimately be queued.
      for (const ask of state.asks) {
        if (isPending(ask.status)) pollAsk(ask.ask_id);
      }
      if (stored && !state.asks.some(a => a.ask_id === stored)) {
        setStored(storageKey, "");
      }
      paintThread();
    }

    function stopPoll(askId) {
      if (askId == null) {
        for (const timer of state.askTimers.values()) clearTimeout(timer);
        state.askTimers.clear();
        state.polling.clear();
        return;
      }
      const timer = state.askTimers.get(askId);
      if (timer) clearTimeout(timer);
      state.askTimers.delete(askId);
      state.polling.delete(askId);
    }

    function pollAsk(askId) {
      if (state.polling.has(askId)) return;
      state.polling.add(askId);

      const tick = async () => {
        if (!root.isConnected || !state.open) { stopPoll(askId); return; }
        let ask = null;
        try {
          const data = await api(
            `/api/rio/hermes/ask/${encodeURIComponent(askId)}`);
          ask = data.ask || null;
        } catch (err) {
          if (err.status === 404) {
            stopPoll(askId);
            if (getStored(storageKey) === askId) setStored(storageKey, "");
            showError("That ask is no longer available on the server.");
            return;
          }
          // Transient network failure: keep the pending turn visible and
          // retry, instead of declaring the bridge dead.
          state.askTimers.set(askId, setTimeout(tick, 3_000));
          return;
        }
        if (!ask) { stopPoll(askId); return; }
        updateAsk(ask);
        paintThread();
        if (isPending(ask.status)) {
          state.askTimers.set(askId, setTimeout(tick, ASK_POLL_MS));
        } else {
          stopPoll(askId);
          if (getStored(storageKey) === askId) setStored(storageKey, "");
          loadStatus(false);
        }
      };
      tick();
    }

    // ── ask form ───────────────────────────────────────────────────────
    function showError(text) {
      els.error.hidden = false;
      els.error.textContent = String(text || "");
    }
    function clearError() {
      els.error.hidden = true;
      els.error.textContent = "";
    }

    async function submit(event) {
      event.preventDefault();
      if (state.submitting) return;
      const prompt = (els.input.value || "").trim();
      if (!prompt) { showError("Type a question for Hermes first."); return; }
      if (prompt.length > MAX_PROMPT) {
        showError(`Keep the question under ${MAX_PROMPT} characters.`);
        return;
      }
      const context = paintContext();
      clearError();
      state.submitting = true;
      els.send.disabled = true;
      els.send.textContent = "Sending…";
      try {
        const data = await api("/api/rio/hermes/ask", {
          method: "POST",
          body: { prompt, page, context },
        });
        if (!data.ask) throw new Error("the bridge did not return an ask");
        updateAsk(data.ask);
        els.input.value = "";
        setStored(storageKey, data.ask.ask_id);
        paintThread();
        pollAsk(data.ask.ask_id);
      } catch (err) {
        showError(err.status === 429
          ? `${err.message} Try again in a few seconds.`
          : `Ask failed: ${err.message}`);
      } finally {
        state.submitting = false;
        els.send.disabled = false;
        els.send.textContent = "Ask Hermes";
      }
    }

    // ── open / close ───────────────────────────────────────────────────
    function onDocKey(event) {
      if (event.key === "Escape" && state.open) {
        event.stopPropagation();
        close();
      }
    }

    function open() {
      if (state.open) return;
      if (!root.isConnected) return;
      state.open = true;
      els.drawer.hidden = false;
      els.fab.setAttribute("aria-expanded", "true");
      root.classList.add("is-open");
      clearError();
      paintContext();
      paintThread();
      loadStatus(false);
      loadAsks();
      if (state.statusTimer) clearInterval(state.statusTimer);
      state.statusTimer = setInterval(() => {
        if (!root.isConnected || !state.open) { close(); return; }
        loadStatus(true);
      }, 30_000);
      document.addEventListener("keydown", onDocKey);
      setTimeout(() => els.input.focus(), 0);
    }

    function close() {
      if (!state.open) return;
      state.open = false;
      els.drawer.hidden = true;
      els.fab.setAttribute("aria-expanded", "false");
      root.classList.remove("is-open");
      stopPoll();
      if (state.statusTimer) {
        clearInterval(state.statusTimer);
        state.statusTimer = null;
      }
      document.removeEventListener("keydown", onDocKey);
      els.fab.focus({ preventScroll: true });
    }

    els.fab.addEventListener("click", open);
    els.close.addEventListener("click", close);
    els.refresh.addEventListener("click", () => loadStatus(true));
    els.form.addEventListener("submit", submit);
    els.input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        els.form.requestSubmit();
      }
    });

    return { root, open, close, refresh: () => loadStatus(true) };
  }

  return { attach };
})();

window.RIOHermesDock = RIOHermesDock;
