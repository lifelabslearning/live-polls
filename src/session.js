/* ---------- session app: ?s=<code> ----------
   Phones never sign in. A random device token identifies this browser; the database
   stores only its hash. Every screen is drawn from one session_state() answer, and a
   Realtime broadcast on llpoll:<code> is only a hint to refetch. */
const SUPABASE_JS = "https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.45.4/+esm"; // loaded only once a session is joined

const SUPABASE_URL = "__SUPABASE_URL__";
const SUPABASE_PUBLISHABLE_KEY = "__SUPABASE_KEY__";
const REST = SUPABASE_URL + "/rest/v1/rpc/";
const SAVE_DELAY = 500;        // batch window after the last change
const HEARTBEAT = 30000;       // refetch anyway
const FALLBACK = 10000;        // refetch while the socket is down
const MAX_ENTRY = 280;

const app = document.getElementById("s-app");
const view = document.getElementById("s-view");
const live = document.getElementById("s-live");
const banner = document.getElementById("s-banner");
const codeEl = document.getElementById("s-code");

// ---------- helpers ----------
const el = (tag, attrs, ...kids) => {
  const n = document.createElement(tag);
  for (const k in attrs || {}) {
    if (k === "class") n.className = attrs[k];
    else if (k === "text") n.textContent = attrs[k];
    else if (k === "html") n.innerHTML = attrs[k];
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), attrs[k]);
    else if (attrs[k] === false || attrs[k] == null) continue;
    else n.setAttribute(k, attrs[k] === true ? "" : attrs[k]);
  }
  for (const kid of kids) if (kid != null) n.append(kid);
  return n;
};
// replaceChildren renders a null child as the text "null"; this drops nulls first.
const put = (node, ...kids) => node.replaceChildren(...kids.filter(k => k != null));
const esc = s => String(s == null ? "" : s);
const nbsp = s => { // keep the last two words together, as the decks do
  const t = esc(s).trim(); const i = t.lastIndexOf(" ");
  return i > 0 && t.split(" ").length >= 4 ? t.slice(0, i) + " " + t.slice(i + 1) : t;
};
const announce = msg => { live.textContent = ""; setTimeout(() => { live.textContent = msg; }, 30); };

function getToken() {
  const re = /^[0-9a-f]{64}$/;
  let t = null;
  try { t = localStorage.getItem("llpoll-device"); } catch (e) {}
  if (!re.test(t || "")) { try { t = sessionStorage.getItem("llpoll-device"); } catch (e) {} }
  if (!re.test(t || "")) {
    t = Array.from(crypto.getRandomValues(new Uint8Array(32))).map(b => b.toString(16).padStart(2, "0")).join("");
    try { localStorage.setItem("llpoll-device", t); } catch (e) { try { sessionStorage.setItem("llpoll-device", t); } catch (e2) {} }
  }
  return t;
}
const TOKEN = getToken();

class RpcError extends Error { constructor(msg, status) { super(msg); this.status = status; } }
async function rpc(fn, params, keepalive) {
  const r = await fetch(REST + fn, {
    method: "POST", keepalive: !!keepalive,
    headers: { apikey: SUPABASE_PUBLISHABLE_KEY, Authorization: "Bearer " + SUPABASE_PUBLISHABLE_KEY, "Content-Type": "application/json" },
    body: JSON.stringify(params)
  });
  const text = await r.text();
  if (!r.ok) { let m = text; try { m = JSON.parse(text).message || text; } catch (e) {} throw new RpcError(m, r.status); }
  return text ? JSON.parse(text) : null;
}

// ---------- state ----------
const S = {
  code: null, sessionId: null, expiresAt: null,
  state: "joining", poll: null, mine: null, results: null,
  answers: {}, dirty: false, saveTimer: null, saving: false, rankTouched: {}, drafts: {},
  renderedPollId: null, renderedStatus: null, renderedRevealed: "",
  channel: null, subscribed: false, fallbackTimer: null, heartbeatTimer: null, endTimer: null, refetchTimer: null,
  client: null
};

// ---------- join screen (bare URL) ----------
function renderJoin() {
  codeEl.textContent = "";
  view.replaceChildren(
    el("section", { class: "s-card s-hero s-waiting s-join" },
      el("h1", { text: "Join a live poll" }),
      el("p", { text: "Enter the 6-character code on the screen, or scan the QR code with your phone camera." }),
      el("form", { onsubmit: e => { e.preventDefault(); const c = e.target.code.value.trim().toUpperCase(); if (c) location.search = "?s=" + encodeURIComponent(c); } },
        el("label", { for: "s-codein", text: "Session code" }),
        el("input", { id: "s-codein", name: "code", autocomplete: "off", autocapitalize: "characters", spellcheck: "false", maxlength: "6", inputmode: "text", required: true }),
        el("button", { class: "s-btn s-primary", type: "submit", text: "Join" }))));
}

// ---------- hero states ----------
function renderHero(kind, title, lines) {
  view.replaceChildren(el("section", { class: "s-card s-hero s-" + kind }, el("h1", { text: title }), ...lines.map(t => el("p", { text: t }))));
  S.renderedPollId = null;
}
const renderWaiting = () => renderHero("waiting", "You’re in.", ["The next poll appears here on its own. Keep this page open."]);
const renderEnded = (title, line) => renderHero("ended", title || "This session has ended.", [line || "A new link comes from the facilitator."]);

// ---------- open poll ----------
function renderOpen() {
  const p = S.poll;
  const wrap = el("section", { class: "s-card" },
    el("h1", { class: "s-title", text: p.questions.length === 1 ? "Vote" : "Vote on " + p.questions.length + " questions" }),
    el("p", { class: "s-lead", text: "Your answers save as soon as you tap. You can change them until the poll closes." }));
  p.questions.forEach((q, i) => wrap.append(renderQuestion(q, i, p.questions.length)));
  wrap.append(el("p", { class: "s-status s-sticky", id: "s-status" }));
  view.replaceChildren(wrap);
  paintStatus("");
}

function qHead(q, i, n) {
  return el("p", { class: "s-prompt" }, n > 1 ? el("span", { class: "s-num", "aria-hidden": "true", text: String(i + 1) }) : null,
    n > 1 ? el("span", { class: "sr-only", text: "Question " + (i + 1) + ". " }) : null, nbsp(q.prompt));
}

function renderQuestion(q, i, n) {
  const box = el("div", { class: "s-q", "data-q": i });
  box.append(qHead(q, i, n));
  const mine = (S.answers || {})[i];
  if (q.type === "choice") {
    const grp = el("div", { class: "s-opts", role: "radiogroup", "aria-label": q.prompt });
    q.options.forEach((opt, k) => {
      const b = el("button", { type: "button", class: "s-opt", role: "radio", "aria-checked": mine && mine.choice === k ? "true" : "false",
        onclick: () => { setAnswer(i, { choice: k }); grp.querySelectorAll(".s-opt").forEach((x, j) => x.setAttribute("aria-checked", j === k ? "true" : "false")); } },
        el("span", { class: "s-dot", "aria-hidden": "true" }), el("span", { text: opt }));
      grp.append(b);
    });
    box.append(grp);
  } else if (q.type === "rating") {
    const grp = el("div", { class: "s-scale", role: "radiogroup", "aria-label": q.prompt });
    for (let v = q.min; v <= q.max; v++) {
      grp.append(el("button", { type: "button", role: "radio", "aria-checked": mine && mine.value === v ? "true" : "false", "aria-label": String(v) + ratingWord(q, v), text: String(v),
        onclick: e => { setAnswer(i, { value: v }); grp.querySelectorAll("button").forEach(x => x.setAttribute("aria-checked", x === e.currentTarget ? "true" : "false")); } }));
    }
    box.append(grp);
    if (q.labels && (q.labels.min || q.labels.max)) box.append(el("div", { class: "s-ends", "aria-hidden": "true" }, el("span", { text: q.min + " = " + (q.labels.min || "") }), el("span", { text: q.max + " = " + (q.labels.max || "") })));
  } else if (q.type === "ranking") {
    const order = (mine && Array.isArray(mine.order) && mine.order.length === q.options.length) ? mine.order.slice() : q.options.map((_, k) => k);
    const touched = !!(mine && mine.order) || !!S.rankTouched[i];
    const list = el("ol", { class: "s-rank", "aria-label": q.prompt });
    const hint = el("div", { class: "s-rankhint" });
    const paintList = () => {
      list.replaceChildren(...order.map((k, pos) => el("li", { class: S.rankTouched[i] ? "s-moved" : "" },
        el("span", { class: "s-lbl", text: q.options[k] }),
        el("div", { class: "s-mv" },
          el("button", { type: "button", "aria-label": "Move “" + q.options[k] + "” up", disabled: pos === 0, text: "▲", onclick: () => move(pos, -1) }),
          el("button", { type: "button", "aria-label": "Move “" + q.options[k] + "” down", disabled: pos === order.length - 1, text: "▼", onclick: () => move(pos, 1) })))));
      put(hint,
        el("span", { text: S.rankTouched[i] || (mine && mine.order) ? "1 = " + rankTop(q) + ". Your order is saved; keep moving items if you like." : "1 = " + rankTop(q) + ". Move items with the arrows, or keep this order." }),
        (!S.rankTouched[i] && !(mine && mine.order)) ? el("button", { type: "button", class: "s-btn s-small", text: "This order is right", onclick: () => { S.rankTouched[i] = true; setAnswer(i, { order: order.slice() }); paintList(); } }) : null);
    };
    const move = (pos, d) => {
      const j = pos + d; if (j < 0 || j >= order.length) return;
      [order[pos], order[j]] = [order[j], order[pos]];
      S.rankTouched[i] = true; setAnswer(i, { order: order.slice() }); paintList();
      const btns = list.querySelectorAll("li")[j].querySelectorAll("button"); (d < 0 ? btns[0] : btns[1]).focus();
    };
    paintList();
    box.append(list, hint);
  } else if (q.type === "text") {
    const max = q.maxEntries || 5;
    const entries = () => (S.mine && S.mine.entries ? S.mine.entries.filter(e => e.question === i) : []);
    const list = el("ul", { class: "s-entries", "aria-label": "Your entries" });
    const count = el("div", { class: "s-count" });
    const ta = el("textarea", { maxlength: String(MAX_ENTRY), rows: "2", placeholder: "Type an answer…", "aria-label": q.prompt, oninput: () => { S.drafts[i] = ta.value; paintCount(); } });
    if (S.drafts[i]) ta.value = S.drafts[i];
    const addBtn = el("button", { type: "button", class: "s-btn s-primary", text: "Add", onclick: () => add() });
    const paintCount = () => {
      const n = entries().length;
      count.replaceChildren(el("span", { text: n + " of " + max + (max === 1 ? " entry" : " entries") }), el("span", { text: ta.value.length + "/" + MAX_ENTRY }));
      addBtn.disabled = n >= max || !ta.value.trim();
      ta.disabled = n >= max;
      if (n >= max) ta.placeholder = "Entry limit reached. Remove one to add another.";
    };
    const paintEntries = () => {
      list.replaceChildren(...entries().map(e => el("li", { class: e.pending ? "s-pending" : "" },
        el("span", { class: "s-lbl" }, e.body, e.masked ? el("span", { class: "s-filtered", text: "Filtered: the word filter keeps this entry off the screen." }) : null),
        el("button", { type: "button", class: "s-btn s-x", text: "Remove", "aria-label": "Remove entry: " + e.body, disabled: !!e.pending, onclick: () => remove(e) }))));
      paintCount();
    };
    const add = async () => {
      const body = ta.value.trim(); if (!body) return;
      const tmp = { id: "tmp-" + Date.now(), question: i, body, pending: true };
      S.mine.entries.push(tmp); ta.value = ""; S.drafts[i] = ""; paintEntries(); paintStatus("saving");
      try {
        const id = await rpc("add_text_entry", { p_poll: S.poll.id, p_q: i, p_body: body, p_token: TOKEN });
        tmp.id = id; delete tmp.pending; paintEntries(); paintStatus("saved"); refetch();
      } catch (e) {
        S.mine.entries = S.mine.entries.filter(x => x !== tmp); ta.value = body; S.drafts[i] = body; paintEntries();
        if (/not open/i.test(e.message)) { paintStatus("closed"); refetch(); }
        else if (/limit/i.test(e.message)) paintStatus("error", "Entry limit reached.");
        else paintStatus("error", "Not saved, tap Add again.");
      }
    };
    const remove = async e => {
      e.pending = true; paintEntries(); paintStatus("saving");
      try {
        await rpc("remove_text_entry", { p_entry: e.id, p_token: TOKEN });
        S.mine.entries = S.mine.entries.filter(x => x !== e); paintEntries(); paintStatus("saved", "Removed.");
      } catch (err) {
        delete e.pending; paintEntries();
        if (/not open/i.test(err.message)) { paintStatus("closed"); refetch(); } else paintStatus("error", "Not removed, tap again.");
      }
    };
    paintEntries();
    box.append(list, el("div", { class: "s-add" }, ta, addBtn), count);
    box._paintEntries = paintEntries;
  }
  return box;
}
const ratingWord = (q, v) => q.labels ? (v === q.min && q.labels.min ? " \u00b7 " + q.labels.min : v === q.max && q.labels.max ? " \u00b7 " + q.labels.max : "") : "";
const rankTop = q => q.rankLabel || "top";

// ---------- batched autosave for choice, rating, ranking ----------
function setAnswer(i, value) {
  S.answers[i] = value; S.dirty = true;
  clearTimeout(S.saveTimer); S.saveTimer = setTimeout(() => flush(false), SAVE_DELAY);
  paintStatus("pending");
}
async function flush(keepalive, isRetry) {
  clearTimeout(S.saveTimer); S.saveTimer = null;
  if (!S.dirty || !S.poll || S.poll.status !== "open") return;
  const batch = {}; for (const k in S.answers) batch[k] = S.answers[k];
  S.dirty = false; S.saving = true; paintStatus("saving");
  try {
    await rpc("save_answers", { p_poll: S.poll.id, p_answers: batch, p_token: TOKEN }, keepalive);
    S.saving = false;
    if (S.dirty) { S.saveTimer = setTimeout(() => flush(false), SAVE_DELAY); } else paintStatus("saved");
  } catch (e) {
    S.saving = false;
    if (e instanceof RpcError && /not open/i.test(e.message)) { paintStatus("closed"); refetch(); return; }
    if (e instanceof RpcError && e.status >= 400 && e.status < 500) { paintStatus("error", "That answer was rejected. Tap again."); return; }
    if (!isRetry) { S.dirty = true; S.saveTimer = setTimeout(() => flush(false, true), 1500); paintStatus("saving"); return; }
    S.dirty = true; paintStatus("error", "Not saved, tap again.");
  }
}
function paintStatus(kind, custom) {
  const st = document.getElementById("s-status"); if (!st) return;
  const base = "You can change your answer until the poll closes.";
  const map = {
    "": [null, ""],
    pending: [null, "Saving…"],
    saving: [null, "Saving…"],
    saved: [el("span", { class: "s-ok", text: "Saved" }), custom || base],
    closed: [el("span", { class: "s-err", text: "Poll closed" }), "This poll closed before your last change saved."],
    error: [el("span", { class: "s-err", text: "Not saved" }), custom || "Tap again."]
  };
  const [tag, text] = map[kind] || map[""];
  st.replaceChildren(...[tag, el("span", { text: text })].filter(Boolean));
  if (kind === "saved") announce("Saved. " + (custom || base));
  else if (kind === "closed" || kind === "error") announce(text);
}

// ---------- closed: look up, with own answers ----------
function renderClosed() {
  const p = S.poll;
  const wrap = el("section", { class: "s-card s-hero s-closed" }, el("h1", { text: "Poll closed." }), el("p", { text: "Look up at the screen. Results appear here as the facilitator reveals them." }));
  const own = ownAnswers(p);
  put(view, wrap, own ? el("section", { class: "s-card" }, el("h2", { class: "s-title", text: "Your answers" }), own) : null);
}
function ownAnswers(p) {
  const mine = S.mine || { answers: {}, entries: [] };
  const items = p.questions.map((q, i) => {
    const a = mine.answers && mine.answers[i];
    let val = null;
    if (q.type === "choice" && a) val = q.options[a.choice];
    else if (q.type === "rating" && a) val = String(a.value) + ratingWord(q, a.value);
    else if (q.type === "ranking" && a && a.order) val = a.order.map((k, pos) => (pos + 1) + ". " + q.options[k]).join("  ·  ");
    else if (q.type === "text") { const es = (mine.entries || []).filter(e => e.question === i); if (es.length) val = es.map(e => "“" + e.body + "”").join("  ·  "); }
    return val == null ? null : el("div", {}, el("p", { class: "s-prompt", text: nbsp(q.prompt) }), el("p", { class: "s-ans", text: val }));
  }).filter(Boolean);
  return items.length ? el("div", { class: "s-own" }, ...items) : null;
}

// ---------- results ----------
function renderResults() {
  const p = S.poll, r = S.results;
  const head = el("section", { class: "s-card s-hero s-closed" }, el("h1", { text: "Results" }),
    el("p", { text: (r && r.respondents ? r.respondents + (r.respondents === 1 ? " response" : " responses") + ". " : "") + "Each question appears as the facilitator reveals it." }));
  const cards = p.questions.map((q, i) => {
    const rq = r && r.questions ? r.questions[i] : null;
    const revealed = !!(rq && rq.revealed);
    const card = el("section", { class: "s-card s-q" + (revealed ? "" : " s-pendingq") });
    card.append(qHead(q, i, p.questions.length));
    if (!revealed) { card.append(el("p", { text: "Look up at the screen. Results for this question appear when the facilitator reveals them." })); return card; }
    const mine = S.mine && S.mine.answers ? S.mine.answers[i] : null;
    const total = rq.answered || 0;
    if (q.type === "choice" || q.type === "rating") {
      const labels = q.type === "choice" ? q.options : Array.from({ length: q.max - q.min + 1 }, (_, k) => String(q.min + k) + ratingWord(q, q.min + k));
      const bars = el("div", { class: "s-bars" });
      labels.forEach((lbl, k) => {
        const n = (rq.counts || [])[k] || 0, pct = total ? Math.round(n / total * 100) : 0;
        const isMine = mine && (q.type === "choice" ? mine.choice === k : mine.value === q.min + k);
        bars.append(el("div", { class: "s-bar" + (isMine ? " s-mine" : "") },
          el("div", { class: "s-row" }, el("span", {}, lbl, isMine ? el("span", { class: "s-you", text: "Your answer" }) : null), el("b", { text: pct + "% (" + n + ")" })),
          el("div", { class: "s-track", role: "img", "aria-label": lbl + ": " + pct + " percent, " + n + (n === 1 ? " vote" : " votes") }, el("div", { class: "s-fill", style: "width:" + pct + "%" }))));
      });
      card.append(bars);
      if (q.type === "rating" && rq.mean != null) card.append(el("p", { class: "s-meta", text: "Average " + Number(rq.mean).toFixed(1) + " of " + q.max + " · " + total + (total === 1 ? " response" : " responses") }));
      else card.append(el("p", { class: "s-meta", text: total + (total === 1 ? " response" : " responses") }));
    } else if (q.type === "ranking") {
      const avg = rq.average_rank || [];
      const ordered = q.options.map((o, k) => ({ o, k, a: avg[k] == null ? Infinity : Number(avg[k]) })).sort((x, y) => x.a - y.a);
      const list = el("ol", { class: "s-rlist" });
      ordered.forEach((it, pos) => {
        const myPos = mine && mine.order ? mine.order.indexOf(it.k) + 1 : 0;
        list.append(el("li", {}, el("span", { class: "s-pos", "aria-hidden": "true", text: String(pos + 1) }),
          el("span", { class: "s-lbl" }, it.o, myPos ? el("span", { class: "s-you", text: "You: " + ordinal(myPos) }) : null),
          el("span", { class: "s-avg", text: isFinite(it.a) ? "avg " + it.a.toFixed(1) : "—" })));
      });
      card.append(list, el("p", { class: "s-meta", text: "Ordered by the room’s average position (1 = " + rankTop(q) + ") · " + total + (total === 1 ? " response" : " responses") }));
    } else if (q.type === "text") {
      const myBodies = new Set((S.mine && S.mine.entries || []).filter(e => e.question === i).map(e => e.body));
      const list = el("ul", { class: "s-tlist" });
      (rq.entries || []).forEach(e => list.append(el("li", { class: myBodies.has(e.body) ? "s-mine" : "" }, e.body, myBodies.has(e.body) ? el("span", { class: "s-you", text: "Yours" }) : null)));
      if (!(rq.entries || []).length) list.append(el("li", { text: "No entries yet." }));
      card.append(list, el("p", { class: "s-meta", text: (rq.answered || 0) + (rq.answered === 1 ? " person" : " people") + " added entries" }));
    }
    return card;
  });
  view.replaceChildren(head, ...cards);
}
const ordinal = n => n + (n % 10 === 1 && n % 100 !== 11 ? "st" : n % 10 === 2 && n % 100 !== 12 ? "nd" : n % 10 === 3 && n % 100 !== 13 ? "rd" : "th");

// ---------- state fetch and render ----------
let refetching = false, refetchAgain = false;
async function refetch() {
  if (!S.sessionId) return;
  if (refetching) { refetchAgain = true; return; }
  refetching = true;
  try {
    const st = await rpc("session_state", { p_session: S.sessionId, p_token: TOKEN });
    applyState(st);
  } catch (e) {
    // network trouble: keep the current screen; the timers try again
  } finally {
    refetching = false;
    if (refetchAgain) { refetchAgain = false; refetch(); }
  }
}
const scheduleRefetch = () => { clearTimeout(S.refetchTimer); S.refetchTimer = setTimeout(refetch, 150); };

async function applyState(st) {
  if (!st || st.state === "ended") {
    stopTimers(); S.state = "ended"; renderEnded(); paintBanner();
    if (S.channel) { try { S.client.removeChannel(S.channel); } catch (e) {} S.channel = null; }
    return;
  }
  S.expiresAt = st.expires_at ? new Date(st.expires_at).getTime() - (st.server_time ? new Date(st.server_time).getTime() - Date.now() : 0) : null;
  armEndTimer();
  const p = st.poll;
  const samePoll = p && S.poll && p.id === S.poll.id;
  if (!samePoll) { S.answers = {}; S.dirty = false; clearTimeout(S.saveTimer); S.rankTouched = {}; S.drafts = {}; }
  S.poll = p; S.state = st.state;
  // adopt the server's copy of this phone's answers unless a save is in flight or pending
  const mine = st.mine || { answers: {}, entries: [] };
  if (!S.dirty && !S.saving) S.answers = Object.assign({}, mine.answers || {});
  S.mine = S.mine && samePoll && S.mine.entries.some(e => e.pending) ? S.mine : { answers: mine.answers || {}, entries: (mine.entries || []).slice() };

  if (st.state === "waiting" || !p) { S.results = null; if (S.renderedStatus !== "waiting") renderWaiting(); S.renderedStatus = "waiting"; S.renderedPollId = null; return; }
  const revealedKey = JSON.stringify(p.revealed || []);
  if (p.status === "open") {
    if (S.renderedPollId !== p.id || S.renderedStatus !== "open") renderOpen();
    else { const boxes = view.querySelectorAll(".s-q"); boxes.forEach(b => b._paintEntries && b._paintEntries()); }
    S.renderedPollId = p.id; S.renderedStatus = "open"; S.renderedRevealed = ""; S.results = null;
    return;
  }
  // closed
  if (p.revealed && p.revealed.length) {
    try { S.results = await rpc("poll_results", { p_poll: p.id, p_token: TOKEN }); } catch (e) { S.results = null; }
    renderResults();
  } else {
    S.results = null;
    if (S.renderedPollId !== p.id || S.renderedStatus !== "closed" || S.renderedRevealed !== "[]") renderClosed();
  }
  S.renderedPollId = p.id; S.renderedStatus = "closed"; S.renderedRevealed = revealedKey;
}

function armEndTimer() {
  clearTimeout(S.endTimer);
  if (!S.expiresAt) return;
  const ms = S.expiresAt - Date.now();
  if (ms <= 0) { refetch(); return; }
  S.endTimer = setTimeout(refetch, Math.min(ms + 500, 2147000000));
}

// ---------- realtime ----------
let connecting = false;
async function connectRealtime() {
  if (connecting) return; connecting = true;
  try {
    if (!S.client) {
      const { createClient } = await import(SUPABASE_JS);
      S.client = createClient(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY, { auth: { persistSession: false, autoRefreshToken: false, detectSessionInUrl: false } });
    }
  } catch (e) { connecting = false; paintBanner(); return; } // the timed refetch keeps the page alive without a socket
  connecting = false;
  if (S.state === "ended" || !S.sessionId) return;
  if (S.channel) { try { S.client.removeChannel(S.channel); } catch (e) {} S.channel = null; }
  S.subscribed = false;
  S.channel = S.client.channel("llpoll:" + S.code, { config: { broadcast: { self: false }, private: false } })
    .on("broadcast", { event: "poll-changed" }, () => scheduleRefetch())
    .subscribe(status => {
      const was = S.subscribed;
      S.subscribed = status === "SUBSCRIBED";
      paintBanner();
      if (S.subscribed && !was) scheduleRefetch();
    });
}
function paintBanner() { banner.hidden = S.subscribed || S.state === "ended" || S.state === "joining"; }
function startTimers() {
  stopTimers();
  S.heartbeatTimer = setInterval(() => { if (document.visibilityState === "visible") refetch(); }, HEARTBEAT);
  S.fallbackTimer = setInterval(() => { if (!S.subscribed && document.visibilityState === "visible") refetch(); }, FALLBACK);
}
function stopTimers() {
  clearInterval(S.heartbeatTimer); clearInterval(S.fallbackTimer); clearTimeout(S.endTimer); clearTimeout(S.refetchTimer);
  if (S.channel && S.client) { try { S.client.removeChannel(S.channel); } catch (e) {} S.channel = null; }
  S.subscribed = false; paintBanner();
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") { if (S.dirty) flush(true); return; }
  if (S.state === "ended" || !S.sessionId) return;
  refetch();
  if (!S.subscribed) connectRealtime();
});
window.addEventListener("pagehide", () => { if (S.dirty) flush(true); });
window.addEventListener("online", () => { if (S.state !== "ended" && S.sessionId) { refetch(); if (!S.subscribed) connectRealtime(); } });

// ---------- boot ----------
async function startSession(code) {
  document.documentElement.classList.add("s-mode"); document.body.classList.add("s-mode");
  app.hidden = false;
  if (!code) { renderJoin(); return; }
  S.code = code.toUpperCase();
  codeEl.replaceChildren("Session ", el("b", { text: S.code }));
  renderHero("waiting", "Joining…", [""]);
  try {
    const rows = await rpc("join_session", { p_code: S.code, p_token: TOKEN });
    S.sessionId = rows[0].session_id;
  } catch (e) {
    S.state = "ended";
    if (e instanceof RpcError && /full/i.test(e.message)) renderEnded("This session is full.", "Ask the facilitator what to do next.");
    else if (e instanceof RpcError && /not found|ended/i.test(e.message)) renderEnded("This session has ended.", "Check the code with the facilitator, or ask for a new link.");
    else renderEnded("Can’t reach the poll service.", "Check your connection, then reload this page.");
    return;
  }
  await refetch();
  if (S.state !== "ended") { connectRealtime(); startTimers(); }
}

const params = new URLSearchParams(location.search);
if (!params.has("poll")) startSession((params.get("s") || "").trim());
