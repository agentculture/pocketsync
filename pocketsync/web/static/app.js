// pocketchat: talks only to the same-origin JSON API (docs/web-api.md).
// All text from recordings or the model goes into the DOM via textContent, never as HTML.

const $ = (sel) => document.querySelector(sel);

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

// ---------- API ----------

class ApiError extends Error {
  constructor(status, body) {
    const err = (body && body.error) || {};
    super(err.message || `request failed (${status})`);
    this.status = status;
    this.code = err.code || "error";
    this.hint = err.hint || "";
    this.body = body || {};
  }
}

async function api(path, { method = "GET", body } = {}) {
  const init = { method, credentials: "same-origin", headers: { Accept: "application/json" } };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch {
    throw new ApiError(0, { error: { code: "offline", message: "Can't reach pocketchat.", hint: "Check your connection and try again." } });
  }
  let data = null;
  try { data = await res.json(); } catch { data = null; }
  if (!res.ok) throw new ApiError(res.status, data);
  return data;
}

function qs(params) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") q.set(k, v);
  const s = q.toString();
  return s ? `?${s}` : "";
}

// ---------- formatting ----------

const dayFmt = new Intl.DateTimeFormat(undefined, { weekday: "long", month: "long", day: "numeric" });
const dayYearFmt = new Intl.DateTimeFormat(undefined, { weekday: "long", month: "long", day: "numeric", year: "numeric" });
const timeFmt = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });
const shortFmt = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });

function parseDate(iso) {
  const d = iso ? new Date(iso) : null;
  return d && !Number.isNaN(d.getTime()) ? d : null;
}

function dayLabel(d) {
  const today = new Date();
  const start = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((start(today) - start(d)) / 86400000);
  if (diff === 0) return "Today";
  if (diff === 1) return "Yesterday";
  return (d.getFullYear() === today.getFullYear() ? dayFmt : dayYearFmt).format(d);
}

function when(iso) {
  const d = parseDate(iso);
  if (!d) return "";
  const label = dayLabel(d);
  return `${label === "Today" || label === "Yesterday" ? label : shortFmt.format(d)}, ${timeFmt.format(d)}`;
}

function clock(sec) {
  if (typeof sec !== "number" || !Number.isFinite(sec)) return "";
  const s = Math.max(0, Math.floor(sec));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${r}` : `${m}:${r}`;
}

function span(start, end) {
  if (typeof start !== "number") return "";
  return typeof end === "number" && end - start >= 1 ? `${clock(start)}–${clock(end)}` : clock(start);
}

function length(sec) {
  if (typeof sec !== "number" || !Number.isFinite(sec) || sec <= 0) return "";
  const m = Math.round(sec / 60);
  if (m < 1) return "under a minute";
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}

// ---------- the tape ----------

function tape(duration, start, end, { large = false } = {}) {
  const node = el("div", { class: large ? "tape tape-large" : "tape", "aria-hidden": "true" });
  paintTape(node, duration, start, end);
  return node;
}

function paintTape(node, duration, start, end) {
  node.replaceChildren();
  const total = duration && duration > 0 ? duration : Math.max(end || 0, start || 0) * 1.25 || 0;
  if (!total || typeof start !== "number") return;
  const left = Math.min(100, (start / total) * 100);
  const right = Math.min(100, ((typeof end === "number" ? end : start) / total) * 100);
  const span = el("div", { class: "tape-span" });
  span.style.left = `${left}%`;
  span.style.width = `${Math.max(right - left, 0.8)}%`;
  node.append(span);
}

// ---------- state ----------

const state = {
  history: [],            // [{role, content}] for this browser session only
  recordings: new Map(),  // id -> list row (duration, labels, title)
  labels: [],
  recLabel: "",
  reader: null,           // {id, start, end, duration}
  busy: false,
};

// ---------- health ----------

async function loadHealth() {
  const box = $("#health");
  try {
    const h = await api("/api/health");
    box.dataset.state = "ok";
    box.querySelector(".health-text").textContent = shortModel(h.chat_model);
    box.title = `Answers by ${h.chat_model}`;
  } catch (err) {
    box.dataset.state = "down";
    box.querySelector(".health-text").textContent = err.status === 403 ? "Signed out" : "Offline";
  }
}

function shortModel(id) {
  if (!id) return "Ready";
  const tail = id.split("/").pop();
  return tail.replace(/-NVFP4|-FP8|-AWQ|-GGUF/gi, "");
}

// ---------- recordings index (labels + durations) ----------

async function loadIndex() {
  try {
    const data = await api(`/api/recordings${qs({ limit: 500 })}`);
    state.recordings = new Map(data.recordings.map((r) => [r.id, r]));
    const labels = new Set();
    data.recordings.forEach((r) => (r.labels || []).forEach((l) => labels.add(l)));
    state.labels = [...labels].sort();
    const select = $("#scope-label");
    select.replaceChildren(el("option", { value: "", text: "All kinds" }),
      ...state.labels.map((l) => el("option", { value: l, text: labelName(l) })));
    renderRecent(data.recordings);
    return data;
  } catch (err) {
    return { recordings: [], error: err };
  }
}

// The latest recordings as tapes drawn to scale: a long meeting is a long tape.
function renderRecent(recordings) {
  const rows = [...recordings]
    .sort((a, b) => String(b.recording_at || "").localeCompare(String(a.recording_at || "")))
    .slice(0, 4);
  if (!rows.length) return;
  const longest = Math.max(...rows.map((r) => r.duration || 0)) || 1;
  $("#recent-list").replaceChildren(...rows.map((r) => {
    return el("li", {}, el("button", { type: "button", class: "recent-item", onclick: () => openReader(r.id) },
      el("span", { class: "recent-title", text: r.title || "Untitled recording" }),
      el("span", { class: "recent-when", text: [when(r.recording_at), length(r.duration)].filter(Boolean).join(", ") }),
      scaleTape(r.duration, longest)));
  }));
  $("#recent").hidden = false;
}

function scaleTape(duration, longest) {
  const bar = el("span", { class: "recent-tape" });
  bar.style.width = `${Math.max(((duration || 0) / longest) * 100, 4)}%`;
  return el("span", { class: "recent-track", "aria-hidden": "true" }, bar);
}

function labelName(l) {
  const words = l.replace(/[-_]+/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

// ---------- ask ----------

function scope() {
  const days = Number($("#scope-range").value);
  const since = days ? new Date(Date.now() - days * 86400000).toISOString().slice(0, 10) : null;
  const label = $("#scope-label").value;
  return {
    since,
    labels: label ? [label] : [],
    exclude_sensitive: $("#scope-sensitive").checked,
  };
}

async function ask(question) {
  question = question.trim();
  if (!question || state.busy) return;
  state.busy = true;
  $("#ask-send").disabled = true;
  $("#ask-empty").hidden = true;

  const answerNode = el("p", { class: "answer pending", text: "Listening back through your recordings" });
  const turn = el("li", { class: "turn" }, el("p", { class: "question", text: question }), answerNode);
  $("#thread").append(turn);
  turn.scrollIntoView({ block: "end", behavior: "smooth" });

  const body = { question, history: state.history.slice(-6), ...scope() };
  try {
    const res = await api("/api/chat", { method: "POST", body });
    renderAnswer(turn, answerNode, res);
    if (res.answer) {
      state.history.push({ role: "user", content: question }, { role: "assistant", content: res.answer });
    }
  } catch (err) {
    renderFailure(turn, answerNode, err);
  } finally {
    state.busy = false;
    $("#ask-send").disabled = false;
    $("#question").focus();
  }
}

function renderAnswer(turn, answerNode, res) {
  answerNode.classList.remove("pending");
  const citations = res.citations || [];
  const byN = new Map(citations.map((c) => [c.n, c]));

  if (!res.answer && citations.length === 0) {
    answerNode.textContent = "Nothing in your recordings matches that. Try other words, or widen the time range.";
    answerNode.classList.add("answer-note");
    return;
  }

  answerNode.replaceChildren();
  const parts = (res.answer || "").split(/(\[\d+\])/g);
  for (const part of parts) {
    const m = /^\[(\d+)\]$/.exec(part);
    const c = m && byN.get(Number(m[1]));
    if (c) {
      answerNode.append(el("button", {
        type: "button", class: "cite", text: String(c.n),
        "aria-label": `Open source ${c.n}: ${c.title || "recording"} at ${clock(c.start)}`,
        onclick: () => openCitation(turn, c),
      }));
    } else if (part) {
      answerNode.append(document.createTextNode(part));
    }
  }

  if (res.retrieval && res.retrieval.mode === "keyword") {
    turn.append(el("p", { class: "notice", text: "Matched on keywords only: semantic search is unavailable right now, so some relevant moments may be missing." }));
  }
  if (citations.length) turn.append(sourceList(turn, citations));
}

function renderFailure(turn, answerNode, err) {
  answerNode.classList.remove("pending");
  if (err.code === "chat_unavailable") {
    answerNode.remove();
    turn.append(el("p", { class: "notice", text: "The answer model is unavailable, so here are the moments that matched. Open one to read it." }));
    const found = (err.body.citations || []).map((c, i) => ({ ...c, n: c.n || i + 1 }));
    if (found.length) turn.append(sourceList(turn, found));
    return;
  }
  answerNode.textContent = err.hint ? `${err.message} ${err.hint}` : err.message;
  answerNode.classList.add("answer-note");
}

function sourceList(turn, citations) {
  return el("ol", { class: "sources", "aria-label": "Sources" },
    citations.map((c) => {
      const rec = state.recordings.get(c.id) || {};
      return el("li", {},
        el("button", {
          type: "button", class: "source", dataset: { n: String(c.n), id: c.id },
          onclick: () => openCitation(turn, c),
        },
          el("span", { class: "source-n", text: String(c.n) }),
          el("span", { class: "source-title", text: c.title || rec.title || "Untitled recording" }),
          el("span", { class: "source-when", text: when(c.recording_at) }),
          c.text ? el("span", { class: "source-quote", text: c.text }) : null,
          el("span", { class: "tape-row" },
            tape(rec.duration, c.start, c.end),
            el("span", { class: "tape-at", text: span(c.start, c.end) })),
        ));
    }));
}

function openCitation(turn, c) {
  turn.querySelectorAll(".cite").forEach((b) => b.setAttribute("aria-pressed", b.textContent === String(c.n) ? "true" : "false"));
  turn.querySelectorAll(".source").forEach((b) => b.setAttribute("aria-current", b.dataset.n === String(c.n) ? "true" : "false"));
  openReader(c.id, c.start, c.end);
}

// ---------- reader ----------

async function openReader(id, start = null, end = null) {
  const sameRec = state.reader && state.reader.id === id;
  state.reader = { id, start, end, duration: (state.recordings.get(id) || {}).duration };
  $(".shell").classList.add("reading");
  const reader = $("#reader");
  reader.hidden = false;
  $("#enrich-panel").hidden = true;
  $("#enrich-open").setAttribute("aria-expanded", "false");
  markCurrent(id);

  if (sameRec && $("#transcript").childElementCount) {
    highlight(start, end);
    return;
  }

  $("#reader-title").textContent = (state.recordings.get(id) || {}).title || "Loading";
  $("#reader-meta").textContent = "";
  $("#reader-labels").replaceChildren();
  $("#reader-summary").replaceChildren();
  $("#reader-todos").replaceChildren();
  $("#transcript").replaceChildren(el("li", { class: "loading", text: "Loading transcript" }));

  try {
    const [rec, tr] = await Promise.all([
      api(`/api/recordings/${encodeURIComponent(id)}`),
      api(`/api/recordings/${encodeURIComponent(id)}/transcript`),
    ]);
    if (!state.reader || state.reader.id !== id) return;
    renderReader(rec, tr);
    highlight(start, end);
  } catch (err) {
    $("#transcript").replaceChildren(el("li", { class: "error-line", text: err.message }));
  }
}

function renderReader(rec, tr) {
  const segs = tr.segments || [];
  const lastEnd = segs.length ? segs[segs.length - 1].end || segs[segs.length - 1].start : 0;
  state.reader.duration = rec.duration || lastEnd || state.reader.duration;

  $("#reader-title").textContent = rec.title || "Untitled recording";
  $("#reader-meta").textContent = [when(rec.recording_at), length(state.reader.duration),
    rec.speakers && rec.speakers.length ? `${rec.speakers.length} speaker${rec.speakers.length === 1 ? "" : "s"}` : ""]
    .filter(Boolean).join(", ");
  $("#reader-labels").replaceChildren(...(rec.labels || []).map((l) => el("span", { class: "chip", text: labelName(l) })));

  renderSummary(rec.summary);

  const todos = rec.action_items || [];
  $("#reader-todos").replaceChildren(...(todos.length ? [
    el("h3", { class: "reader-section", text: "Action items" }),
    el("ul", { class: "todo-items" }, todos.map((t) => todoRow(t))),
  ] : []));

  buildSpeechMap(segs);

  let prevSpeaker = null;
  $("#transcript").replaceChildren(...(segs.length ? segs.map((s) => {
    const who = s.speaker_name || s.speaker || "";
    const continues = who && who === prevSpeaker;
    prevSpeaker = who;
    return el("li", { class: continues ? "seg continues" : "seg", dataset: { start: String(s.start ?? ""), end: String(s.end ?? "") } },
      el("button", { type: "button", class: "seg-time", text: clock(s.start), "aria-label": `Mark ${clock(s.start)}`, onclick: () => highlight(s.start, s.end) }),
      el("div", {},
        who ? el("span", { class: "seg-speaker", text: who }) : null,
        el("p", { class: "seg-text", text: s.text || "" })));
  }) : [el("li", { class: "none", text: "This recording has no transcript yet." })]));
}

function renderSummary(md) {
  const box = $("#reader-summary");
  if (!md) { box.replaceChildren(); return; }
  const out = [el("h3", { class: "reader-section", text: "Summary" })];
  let list = null;
  let para = [];
  const flush = () => {
    if (para.length) out.push(el("p", { text: para.join(" ") }));
    para = [];
  };
  for (const raw of md.split(/\r?\n/)) {
    const line = raw.trim().replace(/\*\*(.+?)\*\*/g, "$1").replace(/__(.+?)__/g, "$1");
    if (!line) { flush(); list = null; continue; }
    const h = /^#{1,6}\s+(.*)$/.exec(line);
    const li = /^(?:[-*•]|\d+[.)])\s+(.*)$/.exec(line);
    if (h) { flush(); list = null; out.push(el("h4", { text: h[1] })); }
    else if (li) { flush(); if (!list) { list = el("ul"); out.push(list); } list.append(el("li", { text: li[1] })); }
    else { list = null; para.push(line); }
  }
  flush();
  box.replaceChildren(...out);
}

// The reader's tape is a speech map: every turn drawn where it falls, tinted by speaker,
// with the cited turns in highlighter and a frame around the cited span.
function buildSpeechMap(segs) {
  const t = $("#reader-tape");
  const legend = $("#reader-legend");
  t.replaceChildren();
  legend.replaceChildren();
  const total = state.reader.duration;
  if (!total || !segs.length) return;
  const voices = new Map();
  for (const s of segs) {
    const who = s.speaker_name || s.speaker || "";
    if (!voices.has(who)) voices.set(who, voices.size % 4);
    if (typeof s.start !== "number") continue;
    const end = typeof s.end === "number" ? s.end : s.start;
    const mark = el("div", { class: `turnmark v${voices.get(who)}`, dataset: { start: String(s.start), end: String(end) } });
    mark.style.left = `${(s.start / total) * 100}%`;
    mark.style.width = `${Math.max(((end - s.start) / total) * 100, 0.25)}%`;
    t.append(mark);
  }
  for (const [who, v] of voices) if (who) legend.append(el("span", { class: `v${v}`, text: who }));
  legend.append(el("span", { class: "tape-legend-end", text: clock(total) }));
}

function seekTape(event) {
  if (!state.reader || !state.reader.duration) return;
  const box = event.currentTarget.getBoundingClientRect();
  const at = ((event.clientX - box.left) / box.width) * state.reader.duration;
  const segs = [...document.querySelectorAll("#transcript .seg")];
  let best = null;
  for (const node of segs) {
    if (Number(node.dataset.start) <= at) best = node;
  }
  best = best || segs[0];
  if (best) highlight(Number(best.dataset.start), Number(best.dataset.end));
}

function highlight(start, end) {
  if (!state.reader) return;
  state.reader.start = start;
  state.reader.end = end;
  const t = $("#reader-tape");
  t.querySelectorAll(".tape-span").forEach((n) => n.remove());
  const total = state.reader.duration;
  const stop = typeof end === "number" ? end : start;
  t.querySelectorAll(".turnmark").forEach((m) => {
    const s = Number(m.dataset.start);
    const e = Number(m.dataset.end);
    m.classList.toggle("hit", typeof start === "number" && e >= start && s <= stop);
  });
  if (typeof start === "number" && total) {
    const frame = el("div", { class: "tape-span" });
    frame.style.left = `${Math.min(100, (start / total) * 100)}%`;
    frame.style.width = `${Math.max(((stop - start) / total) * 100, 0.6)}%`;
    t.append(frame);
  }
  let first = null;
  document.querySelectorAll("#transcript .seg").forEach((node) => {
    const s = Number(node.dataset.start);
    const e = node.dataset.end === "" ? s : Number(node.dataset.end);
    const hit = typeof start === "number" && e >= start && s <= (typeof end === "number" ? end : start);
    node.classList.toggle("cited", hit);
    if (hit && !first) first = node;
  });
  if (first) first.scrollIntoView({ block: "center", behavior: "smooth" });
}

function closeReader() {
  state.reader = null;
  $("#reader").hidden = true;
  $(".shell").classList.remove("reading");
  markCurrent(null);
  document.querySelectorAll(".cite[aria-pressed='true']").forEach((b) => b.setAttribute("aria-pressed", "false"));
  document.querySelectorAll(".source[aria-current='true']").forEach((b) => b.setAttribute("aria-current", "false"));
}

function markCurrent(id) {
  document.querySelectorAll(".rec").forEach((b) => b.setAttribute("aria-current", b.dataset.id === id ? "true" : "false"));
}

// ---------- re-enrich ----------

function enrichBody(apply) {
  const only = document.querySelector("input[name='enrich-only']:checked").value;
  return { ids: [state.reader.id], only: only === "all" ? null : only, apply };
}

async function enrichPreview() {
  const out = $("#enrich-preview");
  $("#enrich-apply").disabled = true;
  out.textContent = "Checking";
  try {
    const res = await api("/api/enrich", { method: "POST", body: enrichBody(false) });
    if (!res.count) {
      out.textContent = "Nothing to re-enrich: this recording can't be queued right now.";
      return;
    }
    out.textContent = `This queues ${res.count} recording for ${modeName(res.mode)}, about ${res.estimated_gateway_calls} gateway call${res.estimated_gateway_calls === 1 ? "" : "s"}. The worker picks it up within a minute.`;
    $("#enrich-apply").disabled = false;
  } catch (err) {
    out.textContent = err.message;
  }
}

async function enrichApply() {
  const out = $("#enrich-preview");
  $("#enrich-apply").disabled = true;
  try {
    const res = await api("/api/enrich", { method: "POST", body: enrichBody(true) });
    out.textContent = res.updated ? `Queued. ${labelName(modeName(res.mode))} will refresh in the background.` : "Nothing was queued.";
  } catch (err) {
    out.textContent = err.message;
  }
}

function modeName(mode) {
  return mode === "classify" ? "new labels" : mode === "embed" ? "new search vectors" : "new labels and search vectors";
}

// ---------- recordings view ----------

async function showRecordings() {
  const list = $("#rec-list");
  list.replaceChildren(el("p", { class: "loading", text: "Loading recordings" }));
  const data = state.recordings.size ? { recordings: [...state.recordings.values()] } : await loadIndex();
  if (data.error) { list.replaceChildren(el("p", { class: "error-line", text: data.error.message })); return; }

  $("#rec-labels").replaceChildren(
    el("button", { type: "button", class: "chip", "aria-pressed": state.recLabel === "" ? "true" : "false", text: "All", onclick: () => { state.recLabel = ""; showRecordings(); } }),
    ...state.labels.map((l) => el("button", {
      type: "button", class: "chip", "aria-pressed": state.recLabel === l ? "true" : "false", text: labelName(l),
      onclick: () => { state.recLabel = state.recLabel === l ? "" : l; showRecordings(); },
    })));

  const rows = data.recordings
    .filter((r) => !state.recLabel || (r.labels || []).includes(state.recLabel))
    .sort((a, b) => String(b.recording_at || "").localeCompare(String(a.recording_at || "")));
  if (!rows.length) {
    list.replaceChildren(el("p", { class: "none", text: state.recLabel ? `No recordings labelled ${labelName(state.recLabel).toLowerCase()} yet.` : "No recordings yet. New ones appear here a minute or two after Pocket finishes them." }));
    return;
  }

  const longest = Math.max(...rows.map((r) => r.duration || 0)) || 1;
  const days = new Map();
  for (const r of rows) {
    const d = parseDate(r.recording_at);
    const key = d ? d.toDateString() : "Undated";
    if (!days.has(key)) days.set(key, { label: d ? dayLabel(d) : "Undated", rows: [] });
    days.get(key).rows.push(r);
  }

  list.replaceChildren(...[...days.values()].map((day) => el("section", { class: "day" },
    el("h2", { class: "day-head", text: day.label }),
    el("ul", { class: "rec-list" }, day.rows.map((r) => {
      const d = parseDate(r.recording_at);
      return el("li", {}, el("button", {
        type: "button", class: "rec", dataset: { id: r.id },
        "aria-current": state.reader && state.reader.id === r.id ? "true" : "false",
        onclick: () => openReader(r.id),
      },
        el("span", { class: "rec-time", text: d ? timeFmt.format(d) : "" }),
        el("span", { class: "rec-title", text: r.title || "Untitled recording" }),
        el("span", { class: "rec-side", text: [length(r.duration), r.action_items ? `${r.action_items} to-do${r.action_items === 1 ? "" : "s"}` : ""].filter(Boolean).join(", ") }),
        scaleTape(r.duration, longest),
        (r.labels || []).length ? el("span", { class: "rec-labels" }, r.labels.map((l) => el("span", { class: "chip", text: labelName(l) }))) : null,
      ));
    })))));
}

// ---------- to-dos view ----------

function todoRow(t) {
  return el("li", { class: t.done ? "todo done" : "todo" },
    el("span", { class: "todo-mark", "aria-hidden": "true" }),
    el("span", { class: "todo-text" }, t.text || "", t.owner ? el("span", { class: "todo-owner", text: t.owner }) : null));
}

async function showTodos() {
  const list = $("#todo-list");
  list.replaceChildren(el("p", { class: "loading", text: "Loading to-dos" }));
  try {
    const data = await api(`/api/actions${qs({ open_only: $("#todo-done").checked ? "false" : "true" })}`);
    if (!data.action_items.length) {
      list.replaceChildren(el("p", { class: "none", text: $("#todo-done").checked ? "No action items in your recordings yet." : "No open to-dos. Pocket adds them when a recording mentions one." }));
      return;
    }
    const groups = new Map();
    for (const item of data.action_items) {
      if (!groups.has(item.recording_id)) groups.set(item.recording_id, { item, todos: [] });
      groups.get(item.recording_id).todos.push(item);
    }
    list.replaceChildren(...[...groups.values()].map(({ item, todos }) => el("section", { class: "todo-group" },
      el("button", { type: "button", class: "todo-source", onclick: () => openReader(item.recording_id) },
        el("strong", { text: item.title || "Untitled recording" }),
        el("span", { text: when(item.recording_at) })),
      el("ul", { class: "todo-items" }, todos.map(todoRow)))));
  } catch (err) {
    list.replaceChildren(el("p", { class: "error-line", text: err.message }));
  }
}

// ---------- routing ----------

const VIEWS = ["ask", "recordings", "todos"];

function route() {
  const view = VIEWS.includes(location.hash.slice(1)) ? location.hash.slice(1) : "ask";
  for (const v of VIEWS) $(`#view-${v}`).hidden = v !== view;
  document.querySelectorAll(".views a").forEach((a) => {
    if (a.dataset.view === view) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  if (view === "recordings") showRecordings();
  if (view === "todos") showTodos();
  if (view === "ask") $("#question").focus({ preventScroll: true });
}

// ---------- wiring ----------

function autosize(t) {
  t.style.height = "auto";
  t.style.height = `${Math.min(t.scrollHeight, 144)}px`;
}

function wire() {
  const q = $("#question");
  $("#composer").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = q.value;
    q.value = "";
    autosize(q);
    ask(text);
  });
  q.addEventListener("input", () => autosize(q));
  q.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      $("#composer").requestSubmit();
    }
  });
  document.querySelectorAll(".suggestion").forEach((b) => b.addEventListener("click", () => ask(b.textContent)));

  $("#reader-close").addEventListener("click", closeReader);
  $("#reader-tape").addEventListener("click", seekTape);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && state.reader) closeReader(); });

  $("#enrich-open").addEventListener("click", () => {
    const panel = $("#enrich-panel");
    panel.hidden = !panel.hidden;
    $("#enrich-open").setAttribute("aria-expanded", String(!panel.hidden));
    $("#enrich-preview").textContent = "";
    $("#enrich-apply").disabled = true;
  });
  document.querySelectorAll("input[name='enrich-only']").forEach((r) => r.addEventListener("change", () => {
    $("#enrich-apply").disabled = true;
    $("#enrich-preview").textContent = "";
  }));
  $("#enrich-preview-btn").addEventListener("click", enrichPreview);
  $("#enrich-apply").addEventListener("click", enrichApply);
  $("#todo-done").addEventListener("change", showTodos);

  window.addEventListener("hashchange", route);
}

wire();
route();
loadHealth();
loadIndex();
