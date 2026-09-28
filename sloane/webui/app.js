// Sloane's control room. No framework and nothing loaded from outside: the
// page is served with CSP 'self'. Everything shown that came from outside
// (assignment titles, calendar text, her replies) goes in as text, or through
// `markdown()`, which escapes first and only then adds a few safe tags.
"use strict";

const $ = (sel) => document.querySelector(sel);
const HEAD = { "X-Sloane": "1", "Content-Type": "application/json" };
const RAIL_START = 6 * 60;   // 6 AM
const RAIL_END = 24 * 60;    // midnight: an 11:59 PM deadline is on it
let state = { tz: undefined, minutes: 0, fetchedAt: Date.now() };
let latest = null;           // the last /api/overview
let shop = null;             // the last /api/workshop

// -- small helpers ------------------------------------------------------------------

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "style") {
      // CSSOM, not a style attribute: the page's CSP has no 'unsafe-inline'.
      for (const decl of value.split(";")) {
        const at = decl.indexOf(":");
        if (at > 0) node.style.setProperty(decl.slice(0, at).trim(), decl.slice(at + 1).trim());
      }
    }
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children) if (child != null && child !== false) node.append(child);
  return node;
}

function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Her detail is Markdown. Escaped first; then bold, code, links (http/https
// only), headings and bullets. Nothing she writes can become a tag.
function markdown(text) {
  const blocks = escapeHtml(text).split(/\n{2,}/);
  const inline = (s) => s
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  const item = /^\s*([-*•]|\d+\.)\s+/;
  return blocks.map((block) => {
    const lines = block.split("\n");
    if (/^\|.*\|$/.test(lines[0].trim())) {
      const rows = lines.filter((l) => !/^\|?\s*-{3,}/.test(l.trim()));
      return "<ul>" + rows.map((r) => "<li>" + inline(r.split("|").map((c) => c.trim()).filter(Boolean).join(" · ")) + "</li>").join("") + "</ul>";
    }
    // Runs of list lines become a list; the lines around them stay text
    // ("Tonight, in order:" then the items).
    let out = "", text = [], list = [], ordered = false;
    const flushText = () => { if (text.length) out += "<p>" + text.join("<br>") + "</p>"; text = []; };
    const flushList = () => {
      if (list.length) out += (ordered ? "<ol>" : "<ul>") + list.map((l) => "<li>" + l + "</li>").join("") + (ordered ? "</ol>" : "</ul>");
      list = [];
    };
    for (const raw of lines) {
      const hit = raw.match(item);
      if (hit) {
        if (!list.length) { flushText(); ordered = /\d/.test(hit[1]); }
        list.push(inline(raw.replace(item, "")));
      } else {
        flushList();
        const heading = /^#{1,4}\s+/.test(raw);
        text.push(heading ? "<strong>" + inline(raw.replace(/^#{1,4}\s+/, "")) + "</strong>" : inline(raw));
      }
    }
    flushList();
    flushText();
    return out;
  }).join("");
}

function toast(message) {
  const box = $("#toast");
  box.textContent = message;
  box.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => box.classList.remove("show"), 4200);
}

function remember(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } }
function recall(key, fallback) { try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; } }

async function api(path, options = {}) {
  const response = await fetch(path, { credentials: "same-origin", ...options });
  if (response.status === 401) {
    location.href = "/app/login";
    throw new Error("signed out");
  }
  return response;
}

async function post(path, body) {
  const response = await api(path, { method: "POST", headers: HEAD, body: JSON.stringify(body || {}) });
  let data = {};
  try { data = await response.json(); } catch { /* an empty body is fine */ }
  toast(data.message || (response.ok ? "Done." : "That didn't work."));
  refresh();
  return data;
}

// -- time, said plainly ----------------------------------------------------------------

function nowMinutes() {
  return state.minutes + Math.floor((Date.now() - state.fetchedAt) / 60000);
}

// 900 → "3 PM", 435 → "7:15 AM", 1440 → "midnight".
function clockOf(minutes) {
  if (minutes >= 1440) return "midnight";
  const h = Math.floor(minutes / 60), m = minutes % 60;
  return `${h % 12 || 12}${m ? ":" + String(m).padStart(2, "0") : ""} ${h < 12 ? "AM" : "PM"}`;
}

// A span of minutes, the way you'd say it: "25 min", "5 h 28 min", "2 days".
function span(minutes) {
  minutes = Math.max(0, Math.round(minutes));
  if (minutes < 60) return `${minutes} min`;
  if (minutes < 48 * 60) {
    const h = Math.floor(minutes / 60), m = minutes % 60;
    return m && h < 10 ? `${h} h ${m} min` : `${h} h`;
  }
  return `${Math.round(minutes / 1440)} days`;
}

function fromNow(iso) {
  if (!iso) return "";
  const minutes = (new Date(iso).getTime() - Date.now()) / 60000;
  if (Number.isNaN(minutes)) return "";
  if (minutes < -1) return `${span(-minutes)} ago`;
  if (minutes < 1) return "now";
  return `in ${span(minutes)}`;
}

// -- views ---------------------------------------------------------------------------

const wide = window.matchMedia("(min-width: 900px)");

function show(view) {
  if (wide.matches && view === "talk") view = "today";
  document.querySelectorAll(".tab").forEach((tab) => {
    if (tab.dataset.view === view) tab.setAttribute("aria-current", "page");
    else tab.removeAttribute("aria-current");
  });
  for (const id of ["today", "memory", "workshop", "engine"]) $("#" + id).hidden = id !== view;
  if (view === "workshop") loadWorkshop();
  // The rail is laid out in pixels, so it's drawn once it can be measured.
  if (view === "today" && latest) drawRail(latest.agenda?.today?.items || []);
  $("#talk").hidden = !wide.matches && view !== "talk";
  if (view === "talk") scrollDown();
  remember("sloane.view", view);
}

document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => show(tab.dataset.view)));
wide.addEventListener("change", () => show(document.querySelector(".tab[aria-current]")?.dataset.view || "today"));

// -- the conversation ------------------------------------------------------------------

const thread = $("#thread");
const input = $("#composer-input");
const sendButton = $("#send");
const readAloud = $("#read-aloud");

function scrollDown() { thread.scrollTop = thread.scrollHeight; }

function hisMessage(text, { when = "", forwarded = false, spoken = false, pending = false } = {}) {
  const item = el("li", { class: `msg him${spoken ? " spoken" : ""}${pending ? " pending" : ""}` },
    when ? el("span", { class: "when", text: when }) : null,
    el("span", { class: "body", text }),
    forwarded ? el("span", { class: "tag", text: "forwarded" }) : null);
  thread.append(item);
  scrollDown();
  return item;
}

function herMessage({ speech = "", detail = "", when = "", outside = false, cls = "" } = {}) {
  const item = el("li", { class: "msg her " + cls });
  if (when) item.append(el("span", { class: "when", text: when }));
  const said = el("p", { class: "speech", text: speech });
  if (outside) said.append(el("span", { class: "tag", text: "from outside text" }));
  item.append(said);
  if (detail) {
    const more = el("div", { class: "detail" });
    more.innerHTML = markdown(detail);
    item.append(more);
  }
  thread.append(item);
  scrollDown();
  return item;
}

function splitLogged(text) {
  // The log keeps speech, a blank line, then detail (when it adds something).
  const at = text.indexOf("\n\n");
  return at < 0 ? { speech: text, detail: "" } : { speech: text.slice(0, at), detail: text.slice(at + 2) };
}

async function loadHistory() {
  const response = await api("/api/history");
  if (!response.ok) return;
  const rows = await response.json();
  thread.replaceChildren();
  for (const row of rows) {
    if (row.from === "him") hisMessage(row.text, { when: row.when, forwarded: row.forwarded, spoken: row.voice });
    else herMessage({ ...splitLogged(row.text), when: row.when, outside: row.outside });
  }
  if (!rows.length) {
    herMessage({ speech: "Ask me anything, or tell me what needs doing.", cls: "hello" });
  }
}

function grow() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, window.innerHeight * 0.4) + "px";
}
input.addEventListener("input", grow);
input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    $("#composer").requestSubmit();
  }
});

// Her reply, read out by the browser's own voice (never leaves the device).
function speakAloud(text) {
  if (readAloud.getAttribute("aria-pressed") !== "true" || !("speechSynthesis" in window) || !text) return;
  const words = new SpeechSynthesisUtterance(text);
  words.lang = latest?.talk?.lang || "en-US";
  const voice = speechSynthesis.getVoices().find((v) => v.lang.replace("_", "-") === words.lang);
  if (voice) words.voice = voice;
  speechSynthesis.speak(words);
}

if ("speechSynthesis" in window) {
  readAloud.setAttribute("aria-pressed", recall("sloane.readAloud", "false"));
  readAloud.addEventListener("click", () => {
    const on = readAloud.getAttribute("aria-pressed") !== "true";
    readAloud.setAttribute("aria-pressed", String(on));
    remember("sloane.readAloud", String(on));
    if (!on) speechSynthesis.cancel();
    toast(on ? "She'll read her replies aloud." : "Reading aloud is off.");
  });
} else {
  readAloud.hidden = true;
}

// One exchange: his message (typed, or a voice note), her reply streamed in.
async function exchange(request, mine) {
  if (sendButton.disabled) return;
  sendButton.disabled = true;
  if ("speechSynthesis" in window) speechSynthesis.cancel();
  let draft = null;
  const place = () => draft || (draft = herMessage({ speech: "Thinking", cls: "thinking" }));
  try {
    const response = await request();
    if (!response.ok || !response.body) {
      const data = await response.json().catch(() => ({}));
      if (mine.classList.contains("pending")) mine.remove();
      place().replaceWith(herMessage({ speech: data.error || "That didn't go through.", cls: "error" }));
      draft = null;
      return;
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let newline;
      while ((newline = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, newline).trim();
        buffer = buffer.slice(newline + 1);
        if (!line) continue;
        const event = JSON.parse(line);
        if (event.t === "heard") {
          mine.classList.remove("pending");
          mine.querySelector(".body").textContent = event.text;
          place();
        } else if (event.t === "typing") {
          place();
        } else if (event.t === "partial") {
          const node = place();
          node.className = "msg her draft";
          node.querySelector(".speech").textContent = event.text;
          scrollDown();
        } else if (event.t === "reply") {
          const finished = herMessage({ speech: event.speech, detail: event.detail, outside: event.outside });
          if (draft) { draft.replaceWith(finished); draft = null; }
          speakAloud(event.speech);
        } else if (event.t === "error") {
          const failed = herMessage({ speech: event.text, cls: "error" });
          if (draft) { draft.replaceWith(failed); draft = null; }
        }
      }
    }
    if (draft) draft.remove();
  } catch (error) {
    if (draft) draft.remove();
    if (error.message !== "signed out") herMessage({ speech: "Lost the connection. Try that again.", cls: "error" });
  } finally {
    sendButton.disabled = false;
    refresh();
  }
}

async function say(text) {
  text = text.trim();
  if (!text || sendButton.disabled) return;
  input.value = "";
  grow();
  const mine = hisMessage(text);
  await exchange(() => api("/api/chat", { method: "POST", headers: HEAD, body: JSON.stringify({ text }) }), mine);
  if (wide.matches || !$("#talk").hidden) input.focus();
}

$("#composer").addEventListener("submit", (event) => { event.preventDefault(); say(input.value); });

// -- the microphone: hold a thought, tap to send --------------------------------------------

const mic = $("#mic");
const MAX_RECORDING = 110 * 1000;
let recording = null;

function canRecord() {
  return window.isSecureContext && !!navigator.mediaDevices?.getUserMedia && "MediaRecorder" in window;
}

function recordingType() {
  for (const type of ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"]) {
    if (MediaRecorder.isTypeSupported(type)) return type;
  }
  return "";
}

function recordingState(on) {
  mic.setAttribute("aria-pressed", String(on));
  mic.setAttribute("aria-label", on ? "Stop and send the voice message" : "Record a voice message");
  $("#composer").classList.toggle("recording", on);
  input.placeholder = on ? "Listening… tap the mic to send, Esc to cancel" : "Message Sloane";
  input.disabled = on;
}

async function startRecording() {
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    toast("The microphone is blocked. Allow it for this page in the browser's settings.");
    return;
  }
  const type = recordingType();
  const recorder = new MediaRecorder(stream, type ? { mimeType: type } : {});
  const chunks = [];
  recording = { recorder, stream, cancelled: false, timer: setTimeout(() => stopRecording(), MAX_RECORDING) };
  recorder.addEventListener("dataavailable", (event) => { if (event.data.size) chunks.push(event.data); });
  recorder.addEventListener("stop", () => {
    const done = recording;
    recording = null;
    clearTimeout(done.timer);
    stream.getTracks().forEach((track) => track.stop());
    recordingState(false);
    if (done.cancelled) { toast("Voice message cancelled."); return; }
    const kind = (recorder.mimeType || type || "audio/webm").split(";")[0];
    const blob = new Blob(chunks, { type: kind });
    if (blob.size < 800) { toast("That was too short to hear."); return; }
    const mine = hisMessage("Listening back…", { spoken: true, pending: true });
    exchange(() => api("/api/voice", { method: "POST", headers: { "X-Sloane": "1", "Content-Type": kind }, body: blob }), mine);
  });
  recorder.start();
  recordingState(true);
}

function stopRecording(cancel = false) {
  if (!recording) return;
  recording.cancelled = cancel;
  recording.recorder.stop();
}

mic.addEventListener("click", () => (recording ? stopRecording() : startRecording()));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && recording) { event.preventDefault(); stopRecording(true); }
});

// "/" goes to the message box from anywhere that isn't already a field.
document.addEventListener("keydown", (event) => {
  if (event.key !== "/" || event.ctrlKey || event.metaKey || event.altKey) return;
  const tag = document.activeElement?.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || document.activeElement?.isContentEditable) return;
  event.preventDefault();
  if (!wide.matches) show("talk");
  input.focus();
});

// What to ask, from what's actually on today.
function renderStarters(data) {
  const now = nowMinutes();
  const items = data.agenda?.today?.items || [];
  const asks = [];
  if (items.some((i) => i.kind === "due" && i.start > now)) asks.push("What should I do first tonight?");
  else asks.push("What's due tomorrow?");
  if (items.some((i) => i.kind === "shift" && i.end > now)) asks.push("Plan my evening around work");
  else asks.push("Help me plan tonight");
  if ((data.overdue || []).length) asks.push("How do I catch up on what's overdue?");
  asks.push("/reminders", "What do you remember about me?");
  const box = $("#starters");
  const key = asks.join("|");
  if (box.dataset.key === key) return;
  box.dataset.key = key;
  box.replaceChildren(...asks.map((text) => el("button", { type: "button", class: "chip", text, onclick: () => say(text) })));
}

// -- today -----------------------------------------------------------------------------

function pct(minutes) {
  const clamped = Math.max(RAIL_START, Math.min(RAIL_END, minutes));
  return ((clamped - RAIL_START) / (RAIL_END - RAIL_START)) * 100;
}

function hourLabel(h) { return (h % 12 || 12) + (h < 12 ? "a" : "p"); }

function drawRail(items) {
  const rail = $("#rail");
  rail.replaceChildren();
  const now = nowMinutes();
  const width = rail.clientWidth;
  if (!width) return;
  rail.append(el("div", { class: "past", style: `width:${pct(now)}%` }));
  const nowX = (pct(now) / 100) * width;
  const showsNow = now >= RAIL_START && now <= RAIL_END;
  for (let h = 6; h < 24; h += width < 520 ? 3 : 2) {
    const x = (pct(h * 60) / 100) * width;
    const hidden = showsNow && x > nowX - 34 && x < nowX + 74;
    rail.append(el("div", { class: "hour", style: `left:${pct(h * 60)}%` }, hidden ? null : el("span", { text: hourLabel(h) })));
  }
  let lastRight = -Infinity;
  for (const item of items) {
    if (item.all_day) continue;
    if (item.kind === "shift" || item.kind === "event") {
      const left = pct(item.start);
      const size = Math.max(pct(item.end) - left, 0.9);
      const room = (size / 100) * width;
      rail.append(el("div", { class: `block ${item.kind}${item.end <= now ? " done" : ""}`, style: `left:${left}%;width:${size}%`, title: `${item.time} ${item.title}` },
        room > Math.min(item.title.length * 7 + 18, 76) ? el("span", { text: item.title }) : null));
    } else {
      const x = Math.max(8, Math.min(width - 8, (pct(item.start) / 100) * width));
      const flip = pct(item.start) > 72;
      const label = item.title.length > 22 ? item.title.slice(0, 21) + "…" : item.title;
      const guess = label.length * 6.3 + 22;
      const from = flip ? x - guess : x - 5;
      const to = flip ? x + 5 : x + guess;
      const fits = from > lastRight + 6 && from > 0 && to < width;
      if (fits) lastRight = to;
      rail.append(el("div", { class: `pin${flip ? " flip" : ""}`, style: flip ? `right:${width - x - 5}px` : `left:${x - 5}px`, title: `${item.time} ${item.title}` },
        el("span", { class: `k ${item.kind}` }), fits ? el("span", { text: label }) : null));
    }
  }
  if (showsNow) {
    rail.append(el("div", { class: now > RAIL_END - 150 ? "now late" : "now", style: `left:${pct(now)}%` },
      el("span", { text: $("#clock").textContent })));
  }
}

function sayTitle(item) {
  return item.kind === "shift" ? "work" : item.title;
}

// Her line on the day: what's happening, what's next, what's due. By rule, from
// the same rows as the agenda below it; no model, so it's never wrong about a time.
function herLine(data) {
  const now = nowMinutes();
  const name = data.name || "Landen";
  const hour = Math.floor(now / 60);
  const hello = hour < 5 ? `Still up, ${name}?` : hour < 12 ? `Morning, ${name}.` : hour < 17 ? `Afternoon, ${name}.`
    : `Evening, ${name}.`;
  const items = (data.agenda?.today?.items || []).filter((i) => !i.all_day);
  const blocks = items.filter((i) => i.kind === "shift" || i.kind === "event");
  const current = blocks.find((i) => i.start <= now && now < i.end);
  const next = blocks.find((i) => i.start > now);
  const due = items.filter((i) => i.kind === "due" && i.start > now);
  const parts = [hello];
  if (current) {
    parts.push(current.kind === "shift" ? `You're at work until ${clockOf(current.end)}.` : `${current.title} until ${clockOf(current.end)}.`);
  }
  if (next) {
    const until = next.end > next.start ? `, until ${clockOf(next.end)}` : "";
    parts.push(next.kind === "shift" ? `Work at ${clockOf(next.start)}${until}.` : `${current ? "Then" : "Next"}: ${next.title} at ${clockOf(next.start)}.`);
  }
  if (due.length === 1) parts.push(`${due[0].title} is due at ${clockOf(due[0].start)}.`);
  else if (due.length > 1) parts.push(`${due.length} things are due today, the first at ${clockOf(due[0].start)}.`);
  if (!current && !next && !due.length) {
    const first = (data.agenda?.tomorrow?.items || []).find((i) => !i.all_day);
    parts.push(first ? `Nothing else today. Tomorrow starts with ${sayTitle(first)} at ${clockOf(first.start)}.` : "Nothing else on today.");
  }
  return parts.join(" ");
}

function nextChip(data) {
  const chip = $("#next-chip");
  const now = nowMinutes();
  const items = (data.agenda?.today?.items || []).filter((i) => !i.all_day);
  const current = items.find((i) => (i.kind === "shift" || i.kind === "event") && i.start <= now && now < i.end);
  const next = items.find((i) => i.start > now);
  const pick = current || next;
  if (!pick) { chip.hidden = true; return; }
  chip.hidden = false;
  chip.replaceChildren(
    el("span", { class: `k ${pick.kind}`, "aria-hidden": "true" }),
    el("span", { text: `${current ? "Now" : "Next"}: ${pick.kind === "shift" ? "Work" : pick.title}` }),
    el("span", { class: "in", text: current ? `until ${clockOf(pick.end)}` : `in ${span(pick.start - now)}` }));
}

function timeOf(item) {
  if (item.all_day) return "All day";
  if (item.end <= item.start) return clockOf(item.start);
  const a = clockOf(item.start), b = clockOf(item.end);
  const am = (m) => m < 720;
  return item.end < 1440 && am(item.start) === am(item.end) ? `${a.replace(/ [AP]M$/, "")}–${b}` : `${a}–${b}`;
}

function renderAgenda(list, day, { live = false, empty = "Nothing on the calendar." } = {}) {
  list.replaceChildren();
  const items = day?.items || [];
  if (!items.length) {
    list.append(el("li", { class: "empty", text: empty }));
    return;
  }
  const now = nowMinutes();
  const nextIndex = live ? items.findIndex((i) => !i.all_day && i.start > now) : -1;
  items.forEach((item, index) => {
    const block = item.kind === "shift" || item.kind === "event";
    let when = "", cls = item.kind;
    if (live && !item.all_day) {
      if (block && item.start <= now && now < item.end) { cls += " now"; when = "now"; }
      else if ((block ? item.end : item.start) <= now) cls += " past";
      else if (index === nextIndex) { cls += " next"; when = `in ${span(item.start - now)}`; }
    }
    list.append(el("li", { class: cls },
      el("span", { class: "t", text: timeOf(item) }),
      el("span", { class: `k ${item.kind}`, "aria-hidden": "true" }),
      el("span", { class: "what" }, item.kind === "due" ? "Due: " : null, item.kind === "reminder" ? "Reminder: " : null,
        item.title, item.sub ? el("span", { class: "sub", text: item.sub }) : null),
      live ? el("span", { class: "state", text: when }) : null));
  });
  for (const c of day.clashes || []) {
    list.append(el("li", { class: "clash", text: `⚠ ${c.what} ${c.hard ? "clashes with" : "is tight against"} ${c.against}.` }));
  }
}

function fill(id, rows, render, empty) {
  const list = $(id);
  list.replaceChildren();
  if (!rows || !rows.length) {
    list.append(el("li", {}, el("span", { class: "empty", text: empty })));
    return;
  }
  for (const row of rows) list.append(render(row));
}

function line(text, meta = "", ...actions) {
  return el("li", {}, el("span", { class: "text", text }), meta ? el("span", { class: "meta", text: meta }) : null,
    actions.length ? el("span", { class: "acts" }, ...actions) : null);
}

function button(label, onclick, cls = "act", extra = {}) {
  return el("button", { type: "button", class: cls, text: label, onclick, ...extra });
}

const JOBS = {
  entity_sync: "Canvas and calendar sync", morning_brief: "Morning brief", pre_shift: "Before your shift",
  post_shift: "After your shift", wrap: "Night wrap", reflection: "Reflection", reminders: "Reminders",
  heartbeat: "Heartbeat", inbox: "Inbox triage", learn: "Nightly learning", backup: "Backup", watchdog: "Watchdog",
  weekly_review: "Weekly review", think: "Thinking", workshop: "Workshop night shift",
};
function jobName(name) { return JOBS[name] || name.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()); }

// Everything waiting on him, in one list: approvals, what's broken, overdue
// work, and builds ready for his OK.
function renderNeeds() {
  const data = latest;
  if (!data) return;
  const list = $("#needs-list");
  list.replaceChildren();
  const row = (kind, text, sub, actions = [], cls = "") => el("li", {},
    el("span", { class: "text" }, el("span", { class: `kind ${cls}`, text: kind }), text,
      sub ? el("span", { class: "sub", text: sub }) : null),
    actions.length ? el("span", { class: "acts" }, ...actions) : null);
  let count = 0;
  for (const p of data.proposals || []) {
    count++;
    list.append(row(p.status === "editing" ? "Being edited" : "Needs your OK", p.preview, "", [
      button("Approve", () => post(`/api/proposals/${p.id}/approve`), "act primary"),
      button("Deny", () => post(`/api/proposals/${p.id}/deny`), "act danger")]));
  }
  for (const a of data.alerts || []) { count++; list.append(row("Broken", a.message, "", [], "bad")); }
  for (const name of data.unreadable || []) { count++; list.append(row("Couldn't read", name, "Part of this page didn't load.", [], "bad")); }
  for (const job of (data.jobs || []).filter((j) => j.status === "failed")) {
    count++;
    list.append(row("Job failed", jobName(job.name), job.error,
      [button("Run again", () => post(`/api/jobs/${job.name}/run`), "act", { "aria-label": `Run ${jobName(job.name)} again` })], "bad"));
  }
  const overdue = data.overdue || [];
  for (const a of overdue.slice(0, 3)) {
    count++;
    list.append(row("Overdue", a.title, [a.course, a.when].filter(Boolean).join(" · "), [], "bad"));
  }
  if (overdue.length > 3) { count += overdue.length - 3; list.append(line(`And ${overdue.length - 3} more overdue.`)); }
  const ready = (shop?.items || []).filter((i) => i.status === "ready");
  if (ready.length) {
    count++;
    list.append(row("Workshop", `${ready.length} build${ready.length === 1 ? "" : "s"} ready for you`,
      ready.map((i) => i.title).slice(0, 3).join(", "), [button("Review", () => show("workshop"), "act primary")], "her"));
  }
  $("#needs").classList.toggle("clear", !count);
  if (!count) list.append(el("li", {}, el("span", { class: "empty", text: "Nothing needs you." })));
  const chip = $("#needs-chip");
  chip.hidden = !count;
  chip.textContent = count === 1 ? "1 thing needs you" : `${count} things need you`;
  const badge = $("#today-badge");
  badge.hidden = !count;
  badge.textContent = String(count);
  badge.setAttribute("aria-label", `${count} need you`);
}

$("#needs-chip").addEventListener("click", (event) => {
  event.preventDefault();
  $("#needs").focus();
  $("#needs").scrollIntoView({ behavior: "smooth", block: "start" });
});

function renderToday(data) {
  $("#her-line").textContent = herLine(data);
  nextChip(data);
  const today = data.agenda?.today || { items: [], clashes: [] };
  drawRail(today.items);
  renderAgenda($("#agenda-today"), today, { live: true, empty: "Nothing on the calendar today." });
  renderAgenda($("#agenda-tomorrow"), data.agenda?.tomorrow, { empty: "A clear day." });
  renderNeeds();

  fill("#due", data.due, (a) => el("li", {},
    el("span", { class: "text" }, a.title, a.course ? el("span", { class: "sub", text: a.course }) : null),
    el("span", { class: "meta" }, el("b", { text: fromNow(a.at) }), a.when)), "Nothing due in the next three days.");
  fill("#reminders", data.reminders, (r) => el("li", {},
    el("span", { class: "text" }, r.text, r.repeats ? el("span", { class: "sub", text: `↻ ${r.repeats}` }) : null),
    el("span", { class: "meta" }, el("b", { text: fromNow(r.at) }), r.when),
    el("span", { class: "acts" }, button(r.repeats ? "Stop" : "Cancel", () => post(`/api/reminders/${r.id}/cancel`), "act danger",
      { "aria-label": `${r.repeats ? "Stop" : "Cancel"} reminder: ${r.text}` }))),
  "No reminders. Tell her: remind me at 5 to call Keegan.");
  fill("#grades", data.grades, (g) => {
    const score = Number(g.score) || 0;
    const level = score >= 90 ? "" : score >= 80 ? "mid" : score >= 70 ? "low" : "bad";
    return el("li", {},
      el("span", { class: "text" }, g.course, el("span", { class: "meter", "aria-hidden": "true" },
        el("i", { class: level, style: `width:${Math.max(0, Math.min(100, score))}%` }))),
      el("span", { class: "meta" }, el("b", { text: `${score.toFixed(1).replace(/\.0$/, "")}%` }), g.grade || ""));
  }, "No grades from Canvas yet.");
  fill("#promises", data.promises, (c) => line(c.what + (c.to ? ` (to ${c.to})` : ""), c.when), "No open promises.");

  const panels = $("#panels");
  panels.replaceChildren();
  for (const panel of data.panels || []) {
    const list = el("ul", { class: "lines" });
    for (const text of panel.lines) list.append(line(text));
    panels.append(el("article", { class: "card" }, el("h2", { text: panel.title }), list));
  }
  renderStarters(data);
}

// -- memory --------------------------------------------------------------------------------

function renderLearned() {
  const facts = latest?.learned || [];
  const words = $("#learned-filter").value.trim().toLowerCase();
  const shown = words ? facts.filter((f) => `${f.value} ${f.key} ${f.source}`.toLowerCase().includes(words)) : facts;
  $("#learned-count").textContent = facts.length ? String(facts.length) : "";
  fill("#learned", shown, (f) => el("li", {},
    el("span", { class: "text" }, f.value, el("span", { class: "sub", text: f.key.replace(/^learned\./, "").replace(/\./g, " · ") + (f.source ? ` · ${f.source}` : "") })),
    el("span", { class: "acts" }, button("Forget", () => post("/api/memory/forget", { key: f.key }), "act danger", { "aria-label": `Forget: ${f.value}` }))),
  words ? "Nothing she knows matches that." : "Nothing yet. Tell her: remember that I'm vegetarian now.");
}
$("#learned-filter").addEventListener("input", renderLearned);

function renderMemory(data) {
  renderLearned();
  $("#loose-count").textContent = (data.loose || []).length ? String(data.loose.length) : "";
  fill("#loose", data.loose, (r) => line(r.summary, r.when, button("Done", () => post(`/api/followups/${r.id}/close`), "act",
    { "aria-label": `Done: ${r.summary}` })), "No loose ends.");
  const diary = $("#diary");
  diary.replaceChildren();
  if (!data.diary || !data.diary.length) {
    diary.append(el("li", {}, el("span", { class: "text", text: "Her first entry comes tonight, after you've talked." })));
  }
  for (const d of data.diary || []) diary.append(el("li", {}, el("span", { class: "when", text: d.when }), el("span", { class: "text", text: d.text })));
}

// -- engine -----------------------------------------------------------------------------------

const PROVIDERS = { claude_code: "Claude, on your plan", anthropic: "Anthropic API", groq: "Groq", local: "Local model",
  piper: "Piper, on the box" };
function provider(name) { return PROVIDERS[name] || name; }

function renderEngine(data) {
  const sys = data.system;
  const tiles = $("#system");
  tiles.replaceChildren();
  const rows = [
    ["Database", sys.database ? "Connected" : "Unreachable", sys.database],
    ["Telegram", sys.telegram ? "Polling" : "Off", sys.telegram],
    ["Main model", provider(sys.main)],
    ["Bulk model", provider(sys.bulk)],
    ["Local model", sys.local || "None set up"],
    ["Her voice", sys.voice.replace(/^(\w+)/, (m) => provider(m))],
    ["Quiet hours", sys.quiet],
    ["Web lookups", sys.web_lookup ? "On" : "Off", sys.web_lookup ? true : undefined],
    ["Microphone here", data.talk?.hears ? "On" : "Needs a Groq key", data.talk?.hears ? true : undefined],
  ];
  for (const [label, value, good] of rows) {
    tiles.append(el("div", { class: `tile ${good === true ? "ok" : good === false ? "bad" : ""}`, role: "listitem" },
      el("span", { class: "label", text: label }), el("span", { class: "value", text: value })));
  }
  const skills = $("#skills");
  skills.replaceChildren(...(sys.skills || []).map((name) => el("li", { text: name })));
  if (!skills.children.length) skills.append(el("li", { text: "None loaded" }));

  const body = $("#jobs tbody");
  body.replaceChildren();
  const jobs = [...(data.jobs || [])].sort((a, b) => (b.status === "failed") - (a.status === "failed"));
  for (const job of jobs) {
    const status = job.status === "ok" ? "ok" : job.status === "failed" ? "failed" : "";
    body.append(el("tr", { class: status === "failed" ? "failed" : "" },
      el("td", {}, el("span", { class: "job-name", text: jobName(job.name) }), el("span", { class: `pill ${status}`, text: job.status }),
        el("span", { class: "job-id", text: job.name }),
        job.error ? el("span", { class: "job-error", text: job.error }) : null),
      el("td", { class: "mono", text: job.last || "—" }),
      el("td", { class: "mono", text: job.next || "—" }),
      el("td", {}, button("Run now", () => post(`/api/jobs/${job.name}/run`), "act", { "aria-label": `Run ${jobName(job.name)} now` }))));
  }

  fill("#trust", data.trust, (t) => line(`${t.action} → ${t.target}`,
    t.hard ? "hard line" : `${t.state}${t.state === "trusted" ? "" : ` · ${t.streak}/10`}`,
    ...(t.hard || t.state !== "trusted" ? [] : [button("Take back", () => post("/api/trust/revoke", { action: t.action, target: t.target }), "act danger")])),
  "She asks before everything.");
  const most = Math.max(1, ...(data.usage || []).map((u) => u.calls));
  fill("#usage", data.usage, (u) => el("li", {},
    el("span", { class: "text", text: `${u.lane} · ${provider(u.provider)}` }),
    el("span", { class: "meta", text: `${u.calls} call${u.calls === 1 ? "" : "s"}${u.failures ? `, ${u.failures} failed` : ""}` }),
    el("span", { class: "bar", "aria-hidden": "true" }, el("i", { class: u.failures ? "failed" : "", style: `width:${(u.calls / most) * 100}%` }))),
  "No model calls in the last day.");
}

$("#sync-now").addEventListener("click", () => post("/api/jobs/entity_sync/run"));

// -- the workshop ------------------------------------------------------------------------------

const ORIGIN = { him: "your idea", her: "her idea" };
const MOVING = { queued: "queued", building: "building now…", deploying: "going live…", accepted: "merged; waiting for the box" };

async function shopPost(id, verb, body) {
  await post(`/api/workshop/${id}/${verb}`, body);
  loadWorkshop();
}

function fileKind(path) {
  if (path.startsWith("sloane/skills/")) return ["skill", "skill"];
  if (path.startsWith("tests/")) return ["test", "test"];
  if (path.startsWith("sql/")) return ["data", ""];
  if (path.endsWith(".md")) return ["docs", ""];
  return ["code", ""];
}

function shopCard(item, actions = [], opts = {}) {
  const card = el("article", { class: `shop-card ${item.status}` });
  card.append(el("div", { class: "shop-card-head" },
    el("h4", { text: item.title }),
    el("span", { class: `tag ${item.origin}`, text: ORIGIN[item.origin] || item.origin })));
  const meta = [MOVING[item.status] || (opts.showStatus ? item.status.replace("_", " ") : ""), item.when].filter(Boolean).join(" · ");
  if (meta) card.append(el("p", { class: "shop-meta", text: meta }));
  const text = opts.full ? item.summary || item.plan || item.request : item.plan || item.request;
  if (text) {
    const body = el("div", { class: "shop-body" });
    body.innerHTML = markdown(text);
    card.append(body);
  }
  if (item.files && item.files.length && opts.full) {
    const list = el("ul", { class: "shop-files", "aria-label": "Files changed" });
    for (const f of item.files) {
      const [label, cls] = fileKind(f);
      list.append(el("li", {}, el("span", { class: `what ${cls}`, text: label }), el("span", { text: f })));
    }
    if (item.status === "ready") card.append(list);
    else card.append(el("details", { class: "shop-more" }, el("summary", { text: `${item.files.length} file${item.files.length === 1 ? "" : "s"} changed` }), list));
  }
  if (item.checks && opts.full) card.append(el("p", { class: "shop-checks", text: "✓ " + item.checks }));
  if (opts.full && item.request && item.summary) {
    card.append(el("details", { class: "shop-more" }, el("summary", { text: "What was asked" }), el("p", { text: item.request })));
  }
  if (opts.full && item.plan && item.summary) {
    const plan = el("div", { class: "shop-body" });
    plan.innerHTML = markdown(item.plan);
    card.append(el("details", { class: "shop-more" }, el("summary", { text: "The plan" }), plan));
  }
  if (item.error) card.append(el("p", { class: "shop-error", text: item.error }));
  if (item.feedback) card.append(el("p", { class: "shop-meta", text: `You said: ${item.feedback}` }));
  if (item.pr_url && opts.full && /^https:\/\//.test(item.pr_url)) {
    card.append(el("p", {}, el("a", { href: item.pr_url, target: "_blank", rel: "noopener noreferrer", text: "See every change on GitHub" })));
  }
  if (actions.length) card.append(el("div", { class: "acts shop-acts" }, ...actions));
  return card;
}

function denyButton(item) {
  return button("Deny", () => {
    const card = document.getElementById(`card-${item.id}`);
    if (!card || card.querySelector(".why")) return;
    const why = el("input", { class: "why", type: "text", maxlength: "500", placeholder: "Why not? (optional; she learns from it)", "aria-label": "Why not" });
    const confirm = button("Deny it", () => shopPost(item.id, "deny", { why: why.value }), "act danger");
    card.append(el("div", { class: "acts shop-acts" }, why, confirm));
    why.focus();
  }, "act danger", { "aria-label": `Deny ${item.title}` });
}

function lane(id, items, render, empty) {
  const box = $(id);
  box.replaceChildren();
  if (!items.length) { box.append(el("p", { class: "empty", text: empty })); return; }
  for (const item of items) {
    const card = render(item);
    card.id = `card-${item.id}`;
    box.append(card);
  }
}

async function loadWorkshop() {
  let response;
  try { response = await api("/api/workshop"); } catch { return; }
  if (!response.ok) return;
  const data = await response.json();
  shop = data;
  const items = data.items || [];
  const by = (...statuses) => items.filter((i) => statuses.includes(i.status));
  const notice = $("#shop-notice");
  if (!data.building_on) {
    notice.hidden = false;
    notice.textContent = "Building is off until the GitHub token is on the box (DEPLOY 7h). Ideas and plans still work.";
  } else if (!data.upgrader) {
    notice.hidden = false;
    notice.textContent = "Accepted changes will merge, but go live only after you rerun the installer (it sets up the automatic upgrade).";
  } else {
    notice.hidden = true;
  }
  const ready = by("ready");
  const stages = [["Ideas", by("idea").length, ""], ["Planned", by("planned").length, ""],
    ["Building", by("queued", "building", "deploying", "accepted").length, ""], ["Ready for you", ready.length, "ready"],
    ["Live", by("live").length, ""]];
  $("#stages").replaceChildren(...stages.flatMap(([label, n, cls], i) => [
    i ? el("li", { class: "arrow", "aria-hidden": "true", text: "→" }) : null,
    el("li", { class: n && cls ? cls : "" }, el("b", { text: String(n) }), label)].filter(Boolean)));
  lane("#shop-ready", ready, (i) => shopCard(i, [
    button("Accept", () => shopPost(i.id, "accept"), "act primary", { "aria-label": `Accept ${i.title}` }), denyButton(i)], { full: true }),
  "Nothing waiting on you.");
  lane("#shop-moving", by("queued", "building", "deploying", "accepted"), (i) => shopCard(i), "Nothing building.");
  lane("#shop-planned", by("planned", "idea"), (i) => shopCard(i, [
    button("Build tonight", () => shopPost(i.id, "build", { now: false }), "act primary"),
    button("Build now", () => shopPost(i.id, "build", { now: true })),
    ...(i.status === "idea" ? [button("Plan it", () => shopPost(i.id, "plan"))] : []),
    button("Drop", () => shopPost(i.id, "drop"), "act danger", { "aria-label": `Drop ${i.title}` })]),
  "No ideas yet. Add one above.");
  lane("#shop-live", by("live"), (i) => shopCard(i, [button("Undo", () => shopPost(i.id, "undo"), "act danger", { "aria-label": `Undo ${i.title}` })], { full: true }),
    "Nothing from the workshop is live yet.");
  lane("#shop-history", by("denied", "failed", "rolled_back", "undone", "dropped"), (i) => shopCard(i,
    i.status === "failed" || i.status === "rolled_back" ? [button("Try again", () => shopPost(i.id, "build", { now: false }))] : [],
    { showStatus: true }), "Nothing here.");
  const badge = $("#workshop-badge");
  badge.hidden = !ready.length;
  badge.textContent = ready.length ? String(ready.length) : "";
  badge.setAttribute("aria-label", `${ready.length} ready`);
  renderNeeds();
}

$("#idea-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = $("#idea-input").value.trim();
  if (!text) return;
  $("#idea-input").value = "";
  await post("/api/workshop/ideas", { text });
  loadWorkshop();
});

// -- the clock, the pulse, the refresh ------------------------------------------------------

function tick() {
  const options = { hour: "numeric", minute: "2-digit" };
  try { $("#clock").textContent = new Intl.DateTimeFormat("en-US", { ...options, timeZone: state.tz }).format(new Date()); }
  catch { $("#clock").textContent = new Date().toLocaleTimeString([], options); }
}

function pulse(data) {
  const box = $("#pulse");
  const broken = data.health?.broken || 0;
  const failed = (data.health?.failed_jobs || []).length;
  if (broken) { box.textContent = `${broken} to look at`; box.className = "pulse bad"; }
  else if (failed) { box.textContent = failed === 1 ? "1 job failed" : `${failed} jobs failed`; box.className = "pulse warn"; }
  else { box.textContent = "All systems normal"; box.className = "pulse ok"; }
  box.setAttribute("aria-label", `${box.textContent}. ${broken || failed ? "Show what needs you" : "Show the engine"}`);
  box.dataset.go = broken || failed ? "needs" : "engine";
}

$("#pulse").addEventListener("click", () => {
  if ($("#pulse").dataset.go === "needs") {
    show("today");
    $("#needs").focus();
    $("#needs").scrollIntoView({ behavior: "smooth", block: "start" });
  } else {
    show("engine");
  }
});

async function refresh() {
  const box = $("#pulse");
  try {
    const response = await api("/api/overview");
    if (!response.ok) throw new Error(String(response.status));
    const data = await response.json();
    latest = data;
    state = { tz: data.now.tz, minutes: data.now.minutes, fetchedAt: Date.now() };
    $("#date").textContent = data.now.date;
    $("#today-title").textContent = data.now.date;
    tick();
    renderToday(data);
    renderMemory(data);
    renderEngine(data);
    pulse(data);
    mic.hidden = !(data.talk?.hears && canRecord());
    loadWorkshop();
  } catch (error) {
    if (error.message === "signed out") return;
    box.textContent = "Can't reach her";
    box.className = "pulse bad";
  }
}

$("#signout").addEventListener("click", async () => {
  await fetch("/app/logout", { method: "POST", headers: HEAD, credentials: "same-origin" });
  location.href = "/app/login";
});

// Once a minute is enough for the day's shape; the clock and the rail's "now" move every second.
let lastMinute = -1;
function everySecond() {
  tick();
  const minute = nowMinutes();
  if (minute === lastMinute || !latest) return;
  lastMinute = minute;
  $("#her-line").textContent = herLine(latest);
  nextChip(latest);
  drawRail(latest.agenda?.today?.items || []);
  renderAgenda($("#agenda-today"), latest.agenda?.today, { live: true, empty: "Nothing on the calendar today." });
}

let resizeTimer = 0;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => latest && drawRail(latest.agenda?.today?.items || []), 150);
});

show(recall("sloane.view", wide.matches ? "today" : "talk"));
loadHistory();
refresh();
setInterval(refresh, 60000);
setInterval(everySecond, 1000);
