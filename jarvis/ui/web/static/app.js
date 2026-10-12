/* JARVIS Dashboard – Single-Page-App ohne Build-Schritt.
 * Seiten: Übersicht, Chat, Projekte, Selbstverbesserung, Bilder, Erinnerungen,
 * Gedächtnis, Protokoll, Modelle, Einrichtung, Einstellungen.
 * Live-Ereignisse kommen über den WebSocket /ws.
 */
"use strict";

const TOKEN = window.JARVIS.token;
const state = {
  page: "overview",
  params: [],
  conversation: null,
  streaming: {},
  speakReplies: false,
  status: null,
  pendingConfirmations: [],
  improvementLog: {},
  ws: null,
  timers: [],
};

/* ------------------------------------------------------------------ Hilfen */

async function api(path, options = {}) {
  const headers = Object.assign({ "X-Jarvis-Token": TOKEN }, options.headers || {});
  let body = options.body;
  if (body && !(body instanceof FormData) && typeof body !== "string") {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(body);
  }
  const response = await fetch(path, { method: options.method || (body ? "POST" : "GET"), headers, body });
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch (e) { /* kein JSON */ }
    throw new Error(detail);
  }
  const type = response.headers.get("content-type") || "";
  if (options.raw) return response;
  return type.includes("application/json") ? response.json() : response.text();
}

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "html") el.innerHTML = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "style" && typeof value === "object") Object.assign(el.style, value);
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

/* replaceChildren() fügt null/false als Text "null"/"false" ein – daher vorher aussortieren. */
function show(el, ...children) {
  el.replaceChildren(...children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false));
}

function esc(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function imgUrl(url) {
  if (!url) return "";
  return url.startsWith("/api/") ? `${url}${url.includes("?") ? "&" : "?"}t=${encodeURIComponent(TOKEN)}` : url;
}

function fmtTime(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  if (isNaN(date)) return iso;
  const today = new Date();
  const sameDay = date.toDateString() === today.toDateString();
  return sameDay
    ? date.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" })
    : date.toLocaleString("de-DE", { weekday: "short", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

/* Kleiner, sicherer Markdown-Renderer: erst escapen, dann formatieren. */
function md(source) {
  const blocks = [];
  let text = esc(source || "").replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    blocks.push(`<pre><code>${code.replace(/\n$/, "")}</code></pre>`);
    return `\u0000${blocks.length - 1}\u0000`;
  });
  const inline = (s) => s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/!\[([^\]]*)\]\((\/api\/images\/file\/[^)\s]+|https?:\/\/[^)\s]+)\)/g, (_, alt, url) => `<img alt="${alt}" src="${imgUrl(url)}">`)
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
  const out = [];
  let list = null;
  let table = null;
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  const closeTable = () => {
    if (!table) return;
    const [head, ...rows] = table.filter((r) => !/^\s*\|?\s*:?-{2,}/.test(r));
    const cells = (r) => r.replace(/^\s*\||\|\s*$/g, "").split("|").map((c) => inline(c.trim()));
    out.push(`<table><thead><tr>${cells(head).map((c) => `<th>${c}</th>`).join("")}</tr></thead><tbody>${rows.map((r) => `<tr>${cells(r).map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table>`);
    table = null;
  };
  for (const line of text.split("\n")) {
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    const bullet = line.match(/^\s*[-*•]\s+(.*)$/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (/^\s*\|.*\|\s*$/.test(line)) { closeList(); (table = table || []).push(line); continue; }
    closeTable();
    if (heading) { closeList(); out.push(`<h4>${inline(heading[2])}</h4>`); }
    else if (bullet || numbered) {
      const kind = bullet ? "ul" : "ol";
      if (list !== kind) { closeList(); out.push(`<${kind}>`); list = kind; }
      out.push(`<li>${inline((bullet || numbered)[1])}</li>`);
    } else if (/^\u0000\d+\u0000$/.test(line.trim())) { closeList(); out.push(line.trim()); }
    else if (!line.trim()) { closeList(); out.push(""); }
    else { closeList(); out.push(`<p>${inline(line)}</p>`); }
  }
  closeList();
  closeTable();
  return out.join("\n").replace(/\u0000(\d+)\u0000/g, (_, i) => blocks[Number(i)]);
}

function toast(title, message, level = "info", timeout = 6000) {
  const el = h("div", { class: `toast ${level === "warning" ? "warn" : level === "error" ? "bad" : ""}` },
    h("strong", {}, title), h("div", { class: "muted" }, message));
  document.getElementById("toasts").append(el);
  setTimeout(() => el.remove(), timeout);
}

function modal(content, { onClose } = {}) {
  const root = document.getElementById("modal-root");
  const close = () => { backdrop.remove(); if (onClose) onClose(); };
  const backdrop = h("div", { class: "modal-backdrop", onclick: (e) => { if (e.target === backdrop) close(); } }, content);
  root.append(backdrop);
  return close;
}

function lightbox(url) {
  const close = modal(h("div", { class: "lightbox", onclick: () => close() }, h("img", { src: imgUrl(url) })));
}

function badgeFor(status) {
  const map = {
    queued: ["wartet", "info"], running: ["läuft", "info"], testing: ["testet", "info"], ready: ["bereit zur Freigabe", "warn"],
    merging: ["übernimmt", "info"], merged: ["übernommen", "ok"], rejected: ["verworfen", ""], failed: ["fehlgeschlagen", "bad"],
    cancelled: ["abgebrochen", ""], rolled_back: ["zurückgerollt", "bad"],
    planning: ["plant", "info"], planning_running: ["plant", "info"], active: ["aktiv", "ok"], paused: ["pausiert", ""],
    waiting_user: ["wartet auf dich", "warn"], done: ["fertig", "ok"], pending: ["offen", ""], blocked: ["blockiert", "bad"],
    skipped: ["übersprungen", ""],
  };
  const [label, cls] = map[status] || [status, ""];
  return h("span", { class: `badge ${cls}` }, label);
}

const ICONS = {
  overview: '<path d="M3 13h8V3H3zm0 8h8v-6H3zm10 0h8V11h-8zm0-18v6h8V3z"/>',
  chat: '<path d="M4 4h16v12H7l-3 3z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>',
  projects: '<path d="M3 6h7l2 2h9v11H3z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>',
  improve: '<path d="M12 2l2.4 5.6L20 10l-5.6 2.4L12 18l-2.4-5.6L4 10l5.6-2.4z"/>',
  images: '<rect x="3" y="4" width="18" height="16" rx="2" fill="none" stroke="currentColor" stroke-width="2"/><path d="M5 18l5-6 4 4 2-2 3 4z"/>',
  reminders: '<circle cx="12" cy="13" r="8" fill="none" stroke="currentColor" stroke-width="2"/><path d="M12 9v4l3 2" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  memory: '<path d="M9 3a4 4 0 0 0-4 4v1a4 4 0 0 0 0 8v1a4 4 0 0 0 7 2.6A4 4 0 0 0 19 17v-1a4 4 0 0 0 0-8V7a4 4 0 0 0-7-2.6A4 4 0 0 0 9 3z" fill="none" stroke="currentColor" stroke-width="2"/>',
  log: '<path d="M5 4h14v16H5zM8 8h8M8 12h8M8 16h5" fill="none" stroke="currentColor" stroke-width="2"/>',
  models: '<circle cx="12" cy="12" r="3"/><path d="M12 2v4M12 18v4M2 12h4M18 12h4M4.9 4.9l2.8 2.8M16.3 16.3l2.8 2.8M4.9 19.1l2.8-2.8M16.3 7.7l2.8-2.8" stroke="currentColor" stroke-width="2"/>',
  setup: '<path d="M4 12l5 5L20 6" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>',
  settings: '<circle cx="12" cy="12" r="3.2" fill="none" stroke="currentColor" stroke-width="2"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M5 19l2-2M17 7l2-2" stroke="currentColor" stroke-width="2"/>',
  mic: '<rect x="9" y="3" width="6" height="11" rx="3" fill="none" stroke="currentColor" stroke-width="2"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3" fill="none" stroke="currentColor" stroke-width="2"/>',
  send: '<path d="M3 11l18-8-8 18-2-7z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>',
  speaker: '<path d="M4 9h4l5-4v14l-5-4H4z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/><path d="M16 9a4 4 0 0 1 0 6" fill="none" stroke="currentColor" stroke-width="2"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
};
const icon = (name) => {
  const span = document.createElement("span");
  span.innerHTML = `<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">${ICONS[name] || ""}</svg>`;
  return span.firstChild;
};

/* ------------------------------------------------------------------ Navigation */

const PAGES = [
  ["overview", "Übersicht"], ["chat", "Chat"], ["projects", "Projekte"], ["improve", "Selbstverbesserung"],
  ["images", "Bilder"], ["reminders", "Erinnerungen"], ["memory", "Gedächtnis"], ["log", "Protokoll"],
  ["models", "Modelle"], ["setup", "Einrichtung"], ["settings", "Einstellungen"],
];

function renderNav() {
  const nav = document.getElementById("nav");
  nav.replaceChildren(...PAGES.map(([key, label]) => h("a", {
    href: `#/${key}`, class: state.page === key ? "active" : "",
  }, icon(key), label, h("span", { class: "badge hidden", id: `nav-badge-${key}` }))));
  updateBadges();
}

function updateBadges() {
  const confirmations = state.pendingConfirmations.length;
  const badge = document.getElementById("nav-badge-overview");
  if (badge) { badge.textContent = confirmations; badge.className = `badge warn ${confirmations ? "" : "hidden"}`; }
}

function route() {
  const parts = (location.hash.replace(/^#\/?/, "") || "overview").split("/");
  state.page = PAGES.some(([key]) => key === parts[0]) ? parts[0] : "overview";
  state.params = parts.slice(1);
  state.timers.forEach(clearInterval);
  state.timers = [];
  document.getElementById("page-title").textContent = PAGES.find(([key]) => key === state.page)[1];
  document.getElementById("sidebar").classList.remove("open");
  renderNav();
  const view = document.getElementById("view");
  show(view, h("div", { class: "faint" }, "Lädt …"));
  const renderer = RENDER[state.page];
  Promise.resolve(renderer(view)).catch((error) => {
    show(view, h("div", { class: "panel" }, h("h2", {}, "Fehler"), h("p", { class: "muted" }, error.message)));
  });
}

/* ------------------------------------------------------------------ Übersicht */

function greeting() {
  const hour = new Date().getHours();
  return hour < 5 ? "Gute Nacht" : hour < 11 ? "Guten Morgen" : hour < 18 ? "Guten Tag" : "Guten Abend";
}

async function renderOverview(view) {
  const [data, status, weather] = await Promise.all([
    api("/api/overview"), api("/api/status"), api("/api/weather").catch(() => null),
  ]);
  const name = status.user ? `, ${status.user}` : "";
  const clock = h("div", { class: "clock" });
  const tick = () => { clock.textContent = new Date().toLocaleString("de-DE", { weekday: "long", day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" }); };
  tick();
  state.timers.push(setInterval(tick, 15000));
  const askInput = h("input", { type: "text", placeholder: "Frag JARVIS etwas … (z. B. „Zeig mir ein Bild vom Mars“)" });
  const ask = () => { if (askInput.value.trim()) { sessionStorage.setItem("jarvis-prefill", askInput.value.trim()); location.hash = "#/chat/new"; } };
  askInput.addEventListener("keydown", (e) => { if (e.key === "Enter") ask(); });

  const sys = data.system;
  const statTile = (label, value, percent, extra) => h("div", { class: "panel stat" },
    h("div", { class: "label" }, label), h("div", { class: "value" }, value),
    percent !== null ? h("div", { class: "progress" }, h("span", { style: { width: `${Math.min(100, percent)}%` } })) : null,
    extra ? h("div", { class: "faint" }, extra) : null);
  const stats = h("div", { class: "grid cols-4", id: "stats" },
    statTile("CPU", `${Math.round(sys.cpu_percent)} %`, sys.cpu_percent, `${sys.cpu_count} Threads`),
    statTile("Arbeitsspeicher", `${sys.ram_used_gb} / ${sys.ram_total_gb} GB`, sys.ram_percent, null),
    statTile("Laufwerk C:", sys.disk.free_gb !== undefined ? `${sys.disk.free_gb} GB frei` : "–", sys.disk.percent ?? null, null),
    statTile(sys.battery ? "Akku" : "Laufzeit", sys.battery ? `${sys.battery.percent} %` : `${sys.uptime_h} h`, sys.battery ? sys.battery.percent : null,
      sys.battery ? (sys.battery.plugged ? "lädt" : "Akkubetrieb") : "seit dem Start"),
  );
  state.timers.push(setInterval(async () => {
    if (state.page !== "overview") return;
    try {
      const fresh = await api("/api/system");
      const tiles = document.querySelectorAll("#stats .stat");
      if (tiles.length < 2) return;
      tiles[0].querySelector(".value").textContent = `${Math.round(fresh.cpu_percent)} %`;
      tiles[0].querySelector(".progress span").style.width = `${fresh.cpu_percent}%`;
      tiles[1].querySelector(".value").textContent = `${fresh.ram_used_gb} / ${fresh.ram_total_gb} GB`;
      tiles[1].querySelector(".progress span").style.width = `${fresh.ram_percent}%`;
    } catch (e) { /* ignorieren */ }
  }, 5000));

  const confirmations = data.confirmations.length ? h("div", { class: "panel", style: { borderColor: "var(--warn)", marginBottom: "18px" } },
    h("h2", {}, "⚠️ JARVIS wartet auf deine Bestätigung"),
    h("div", { class: "list" }, data.confirmations.map((c) => h("div", { class: "item" },
      h("div", { class: "grow" }, h("div", { class: "title" }, c.tool), h("div", { class: "faint" }, c.summary.slice(0, 200))),
      h("button", { class: "btn small primary", onclick: () => answerConfirmation(c.id, true) }, "Erlauben"),
      h("button", { class: "btn small danger", onclick: () => answerConfirmation(c.id, false) }, "Ablehnen"))))) : null;

  const cards = data.cards.map((card) => h("div", { class: "panel" },
    h("h2", {}, card.title, h("span", { class: "actions" },
      h("button", { class: "btn small ghost", title: "Entfernen", onclick: async () => { await api(`/api/cards/${card.id}`, { method: "DELETE" }); route(); } }, "×"))),
    card.image ? h("img", { src: imgUrl(card.image), style: { width: "100%", borderRadius: "10px", marginBottom: "8px" } }) : null,
    h("div", { class: "card-md", html: md(card.content) }),
    h("div", { class: "faint" }, `aktualisiert ${fmtTime(card.updated)}`)));

  const mail = h("div", { class: "panel" }, h("h2", {}, "📬 E-Mail"),
    data.mail.length ? h("div", { class: "list" }, data.mail.map((m) => h("div", { class: "item" },
      h("div", { class: "grow" }, m.account), m.unread === null ? h("span", { class: "badge bad" }, "Fehler") : h("span", { class: `badge ${m.unread ? "info" : ""}` }, `${m.unread} ungelesen`))))
      : h("div", { class: "empty" }, "Noch kein Gmail-Konto verbunden – siehe Einrichtung."));

  const calendar = h("div", { class: "panel" }, h("h2", {}, "📅 Termine (7 Tage)"),
    data.calendar.length && !data.calendar[0].error ? h("div", { class: "list" }, data.calendar.map((e) => h("div", { class: "item" },
      h("div", { class: "faint", style: { minWidth: "92px" } }, e.all_day ? new Date(e.start).toLocaleDateString("de-DE", { weekday: "short", day: "2-digit", month: "2-digit" }) : fmtTime(e.start)),
      h("div", { class: "grow" }, e.title)))) : h("div", { class: "empty" }, data.calendar[0]?.error || "Keine Termine oder Kalender nicht verbunden."));

  const reminders = h("div", { class: "panel" }, h("h2", {}, "⏰ Erinnerungen", h("span", { class: "actions" }, h("a", { href: "#/reminders", class: "btn small ghost" }, "Alle"))),
    data.reminders.length ? h("div", { class: "list" }, data.reminders.map((r) => h("div", { class: "item" },
      h("div", { class: "faint", style: { minWidth: "92px" } }, fmtTime(r.due)), h("div", { class: "grow" }, r.text)))) : h("div", { class: "empty" }, "Keine offenen Erinnerungen."));

  const projects = h("div", { class: "panel" }, h("h2", {}, "🗂️ Projekte", h("span", { class: "actions" }, h("a", { href: "#/projects", class: "btn small ghost" }, "Alle"))),
    data.projects.length ? h("div", { class: "list" }, data.projects.map((p) => h("div", { class: "item clickable", onclick: () => { location.hash = `#/projects/${p.id}`; } },
      h("div", { class: "grow" }, h("div", { class: "title" }, p.title),
        h("div", { class: "progress", style: { marginTop: "6px" } }, h("span", { style: { width: `${p.total ? (p.done / p.total) * 100 : 0}%` } }))),
      p.open_questions ? h("span", { class: "badge warn" }, `${p.open_questions} Frage${p.open_questions > 1 ? "n" : ""}`) : null,
      badgeFor(p.status)))) : h("div", { class: "empty" }, "Noch keine Projekte. Sag z. B. „Starte ein Projekt: …“"));

  const improvements = h("div", { class: "panel" }, h("h2", {}, "✨ Selbstverbesserung", h("span", { class: "actions" }, h("a", { href: "#/improve", class: "btn small ghost" }, "Öffnen"))),
    data.suggestions.length ? h("div", { class: "list", style: { marginBottom: "10px" } }, data.suggestions.slice(0, 3).map((s) => h("div", { class: "item" },
      h("div", { class: "grow" }, h("div", { class: "title" }, `💡 ${s.title}`), h("div", { class: "faint" }, s.description.slice(0, 140))),
      h("button", { class: "btn small primary", onclick: async () => { await api(`/api/suggestions/${s.id}/accept`, { method: "POST" }); toast("Wird umgesetzt", s.title); route(); } }, "Umsetzen")))) : null,
    data.improvements.length ? h("div", { class: "list" }, data.improvements.map((j) => h("div", { class: "item clickable", onclick: () => { location.hash = `#/improve/${j.id}`; } },
      h("div", { class: "grow" }, h("div", { class: "title" }, `#${j.id} ${j.request.slice(0, 70)}`)), badgeFor(j.status))))
      : h("div", { class: "empty" }, "Sag z. B. „Bau dir eine Druckfunktion“ – JARVIS programmiert sich das selbst."));

  const images = h("div", { class: "panel" }, h("h2", {}, "🖼️ Neueste Bilder", h("span", { class: "actions" }, h("a", { href: "#/images", class: "btn small ghost" }, "Galerie"))),
    data.images.length ? h("div", { class: "thumbs" }, data.images.map((img) => h("img", { src: imgUrl(img.url), title: img.prompt || "", onclick: () => lightbox(img.url) })))
      : h("div", { class: "empty" }, "Noch keine Bilder."));

  const temperature = weather?.current?.temperature_2m;
  const weatherBox = weather && weather.place ? h("div", { class: "weather" },
    temperature !== undefined && temperature !== null ? h("div", { class: "temp" }, `${Math.round(temperature)} °C`) : null,
    h("div", { class: "faint" }, [weather.place, weather.text].filter(Boolean).join(" · ")))
    : h("a", { class: "weather faint", href: "#/settings", title: "Ohne Wohnort (oder ohne Internet) gibt es kein Wetter" }, "🌤️ Wohnort in den Einstellungen setzen");

  show(view, 
    h("div", { class: "hero" }, h("div", {}, h("h2", {}, `${greeting()}${name}.`), clock), weatherBox),
    h("div", { class: "ask-bar" }, askInput, h("button", { class: "btn primary", onclick: ask }, icon("send"), "Fragen")),
    confirmations,
    stats,
    h("div", { style: { height: "18px" } }),
    h("div", { class: "grid cols-3" }, ...cards, projects, improvements, calendar, mail, reminders, images),
    h("p", { class: "faint", style: { marginTop: "18px" } }, data.costs),
  );
}

/* ------------------------------------------------------------------ Chat */

let mediaRecorder = null;

async function renderChat(view) {
  const [list, models] = await Promise.all([api("/api/conversations"), api("/api/models")]);
  let id = state.params[0] && state.params[0] !== "new" ? state.params[0] : null;
  if (!id && state.params[0] !== "new" && list.length) id = list[0].id;
  state.conversation = id;

  const convList = h("div", { class: "panel conv-list" },
    h("button", { class: "btn primary", onclick: () => { location.hash = "#/chat/new"; } }, "+ Neues Gespräch"),
    list.map((c) => h("div", { class: `conv ${c.id === id ? "active" : ""}`, onclick: () => { location.hash = `#/chat/${c.id}`; } },
      h("div", {}, c.title || "Gespräch"), h("div", { class: "when" }, `${c.channel === "voice" ? "🎙️ " : ""}${fmtTime(c.updated)}`))));

  const messages = h("div", { class: "messages", id: "messages" });
  const input = h("textarea", { rows: 1, placeholder: "Nachricht an JARVIS … (Enter senden, Umschalt+Enter neue Zeile)" });
  const modelSelect = h("select", { title: "Modell" }, h("option", { value: "" }, "Automatisch"),
    models.models.filter((m) => m.status === "bereit").map((m) => h("option", { value: m.id }, m.label)));
  const taskSelect = h("select", { title: "Art der Aufgabe" },
    [["", "Chat"], ["code", "Programmieren"], ["planning", "Planen"], ["simple", "Schnell & einfach"]].map(([v, l]) => h("option", { value: v }, l)));
  const micBtn = h("button", { class: "icon-btn", title: "Spracheingabe (Aufnahme im Browser)", onclick: () => toggleRecording(micBtn, input, send) }, icon("mic"));
  const speakBtn = h("button", { class: `icon-btn ${state.speakReplies ? "on" : ""}`, title: "Antworten vorlesen",
    onclick: () => { state.speakReplies = !state.speakReplies; speakBtn.classList.toggle("on", state.speakReplies); } }, icon("speaker"));
  const sendBtn = h("button", { class: "btn primary", onclick: () => send() }, icon("send"), "Senden");
  const stopBtn = h("button", { class: "btn danger hidden", onclick: () => id && api(`/api/chat/${id}/cancel`, { method: "POST" }) }, icon("stop"), "Stopp");

  async function send(textOverride) {
    const text = (textOverride ?? input.value).trim();
    if (!text) return;
    input.value = "";
    autoSize();
    appendMessage(messages, { role: "user", content: text });
    const pending = appendMessage(messages, { role: "assistant", content: "", pending: true });
    try {
      const result = await api("/api/chat", { body: { text, conversation_id: id, model: modelSelect.value || null, task: taskSelect.value || null } });
      if (!id) { id = result.conversation_id; state.conversation = id; history.replaceState(null, "", `#/chat/${id}`); }
      state.streaming[id] = pending;
      sendBtn.classList.add("hidden");
      stopBtn.classList.remove("hidden");
    } catch (error) {
      pending.remove();
      appendMessage(messages, { role: "error", content: error.message });
    }
  }
  const autoSize = () => { input.style.height = "auto"; input.style.height = `${Math.min(input.scrollHeight, 220)}px`; };
  input.addEventListener("input", autoSize);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });
  state.chatControls = { sendBtn, stopBtn };

  show(view, h("div", { class: "chat-layout" }, convList,
    h("div", { class: "panel chat-main" }, messages,
      h("div", { class: "composer" }, micBtn, input, speakBtn, sendBtn, stopBtn),
      h("div", { class: "composer opts" }, h("span", { class: "faint" }, "Modell"), modelSelect, h("span", { class: "faint" }, "Modus"), taskSelect))));

  if (id) {
    const conversation = await api(`/api/conversations/${id}`);
    conversation.messages.forEach((m) => appendMessage(messages, m));
    if (conversation.busy) {
      state.streaming[id] = appendMessage(messages, { role: "assistant", content: "", pending: true });
      sendBtn.classList.add("hidden");
      stopBtn.classList.remove("hidden");
    }
  } else {
    messages.append(h("div", { class: "empty", style: { textAlign: "center", marginTop: "18vh" } },
      "Wie kann ich helfen? Du kannst schreiben oder auf das Mikrofon klicken."));
  }
  const prefill = sessionStorage.getItem("jarvis-prefill");
  if (prefill) { sessionStorage.removeItem("jarvis-prefill"); send(prefill); }
  input.focus();
}

function appendMessage(container, message) {
  const role = message.role === "user" ? "user" : message.role === "error" ? "error" : "assistant";
  const meta = message.meta || {};
  const bubble = h("div", { class: "bubble" });
  if (message.pending) bubble.append(h("div", { class: "chips" }), h("div", { class: "content" }, h("span", { class: "typing" }, h("span"), h("span"), h("span"))));
  else {
    const chips = (meta.tools || []).map((t) => h("span", { class: `chip ${t.ok ? "ok" : "bad"}` }, `🔧 ${t.name}`));
    if (chips.length) bubble.append(h("div", { class: "chips" }, chips));
    bubble.append(h("div", { class: "content", html: role === "user" ? esc(message.content).replace(/\n/g, "<br>") : md(message.content) }));
    if ((meta.images || []).length) bubble.append(h("div", { class: "msg-images" }, meta.images.map((img) => h("img", { src: imgUrl(img.url), title: img.caption || "", onclick: () => lightbox(img.url) }))));
    if (role === "assistant" && meta.model) bubble.append(metaLine(meta));
  }
  const el = h("div", { class: `msg ${role}` }, h("div", { class: "avatar" }, role === "user" ? "Du" : role === "error" ? "!" : "J"), bubble);
  container.querySelector(".empty")?.remove();
  container.append(el);
  container.scrollTop = container.scrollHeight;
  return el;
}

function metaLine(meta) {
  const cost = meta.subscription ? "Abo (kostenlos)" : meta.cost_usd ? `$${meta.cost_usd.toFixed(4)}` : "kostenlos";
  return h("div", { class: "meta" }, h("span", {}, meta.model), h("span", {}, `${meta.tokens_in || 0} → ${meta.tokens_out || 0} Tokens`), h("span", {}, cost),
    (meta.fallbacks || []).length ? h("span", {}, `↪ Ausweichmodell (statt ${meta.fallbacks.join(", ")})`) : null);
}

async function toggleRecording(button, input, send) {
  if (mediaRecorder && mediaRecorder.state === "recording") { mediaRecorder.stop(); return; }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const chunks = [];
    mediaRecorder = new MediaRecorder(stream);
    mediaRecorder.ondataavailable = (e) => chunks.push(e.data);
    mediaRecorder.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      button.classList.remove("rec");
      const form = new FormData();
      form.append("file", new Blob(chunks, { type: mediaRecorder.mimeType || "audio/webm" }), "aufnahme.webm");
      input.placeholder = "Erkenne Sprache …";
      try {
        const result = await api("/api/voice/transcribe", { body: form });
        if (result.text) send(result.text);
        else toast("Nichts verstanden", "Bitte noch einmal versuchen.", "warning");
      } catch (error) { toast("Spracherkennung", error.message, "error"); }
      input.placeholder = "Nachricht an JARVIS …";
    };
    mediaRecorder.start();
    button.classList.add("rec");
  } catch (error) { toast("Mikrofon", `Kein Zugriff: ${error.message}`, "error"); }
}

async function speak(text) {
  try {
    const response = await api("/api/voice/speak", { body: { text: text.slice(0, 2000) }, raw: true });
    const audio = new Audio(URL.createObjectURL(await response.blob()));
    audio.play();
  } catch (error) { /* Sprachausgabe optional */ }
}

function onChatEvent(event) {
  const target = state.streaming[event.session_id];
  if (!target) {
    if (event.type === "chat.final" && state.page === "chat" && event.session_id === state.conversation) route();
    return;
  }
  const bubble = target.querySelector(".bubble");
  const content = bubble.querySelector(".content");
  const chips = bubble.querySelector(".chips");
  if (event.type === "chat.delta") {
    target.dataset.text = (target.dataset.text || "") + event.data.text;
    content.innerHTML = md(target.dataset.text);
  } else if (event.type === "tool.started") {
    chips.append(h("span", { class: "chip run", "data-tool": event.data.name }, `⏳ ${event.data.name}`));
  } else if (event.type === "tool.finished") {
    const chip = [...chips.querySelectorAll(".chip.run")].find((c) => c.dataset.tool === event.data.name);
    if (chip) { chip.className = `chip ${event.data.ok ? "ok" : "bad"}`; chip.textContent = `🔧 ${event.data.name}`; }
  } else if (event.type === "image.new") {
    let grid = bubble.querySelector(".msg-images");
    if (!grid) { grid = h("div", { class: "msg-images" }); bubble.append(grid); }
    grid.append(h("img", { src: imgUrl(event.data.url), title: event.data.caption || "", onclick: () => lightbox(event.data.url) }));
  } else if (["chat.final", "chat.error", "chat.cancelled"].includes(event.type)) {
    delete state.streaming[event.session_id];
    if (state.chatControls) { state.chatControls.sendBtn.classList.remove("hidden"); state.chatControls.stopBtn.classList.add("hidden"); }
    if (event.type === "chat.final") {
      content.innerHTML = md(event.data.text || target.dataset.text || "");
      if (!event.data.text && !(event.data.images || []).length) content.innerHTML = '<span class="faint">(keine Antwort)</span>';
      bubble.append(metaLine(event.data));
      if (state.speakReplies && event.data.text) speak(event.data.text);
    } else if (event.type === "chat.error") {
      target.classList.add("error");
      content.textContent = event.data.error;
    } else {
      content.innerHTML += '<div class="faint">⏹ abgebrochen</div>';
    }
  }
  const messages = document.getElementById("messages");
  if (messages) messages.scrollTop = messages.scrollHeight;
}

/* ------------------------------------------------------------------ Projekte */

async function renderProjects(view) {
  if (state.params[0]) return renderProject(view, state.params[0]);
  const projects = await api("/api/projects");
  const goal = h("textarea", { rows: 3, placeholder: "Projektziel, z. B. „Plane meinen Umzug nach Hamburg im März: Checkliste, Kosten, Termine“" });
  const priority = h("select", {}, [1, 2, 3, 4, 5].map((p) => h("option", { value: p, selected: p === 3 }, `Priorität ${p}${p === 1 ? " (höchste)" : ""}`)));
  const create = async () => {
    if (!goal.value.trim()) return;
    const project = await api("/api/projects", { body: { goal: goal.value, priority: Number(priority.value) } });
    toast("Projekt angelegt", "JARVIS erstellt jetzt den Plan.");
    location.hash = `#/projects/${project.id}`;
  };
  show(view, 
    h("div", { class: "panel", style: { marginBottom: "18px" } }, h("h2", {}, "Neues Langzeit-Projekt"),
      h("p", { class: "faint" }, "JARVIS plant, arbeitet selbstständig im Hintergrund (auch über Tage), prüft seine Ergebnisse und fragt dich, wenn er feststeckt."),
      goal, h("div", { class: "row", style: { marginTop: "10px" } }, h("div", { style: { width: "220px" } }, priority), h("button", { class: "btn primary", onclick: create }, "Projekt starten"))),
    projects.length ? h("div", { class: "grid cols-2" }, projects.map((p) => h("div", { class: "panel item clickable", style: { display: "block" }, onclick: () => { location.hash = `#/projects/${p.id}`; } },
      h("div", { class: "row" }, h("div", { class: "title grow" }, p.title), badgeFor(p.status)),
      h("div", { class: "faint", style: { margin: "6px 0" } }, p.goal.slice(0, 160)),
      h("div", { class: "progress" }, h("span", { style: { width: `${p.total ? (p.done / p.total) * 100 : 0}%` } })),
      h("div", { class: "row faint", style: { marginTop: "6px" } }, `${p.done}/${p.total} Aufgaben`, `Priorität ${p.priority}`,
        p.open_questions ? h("span", { class: "badge warn" }, `${p.open_questions} offene Frage(n)`) : null))))
      : h("div", { class: "empty" }, "Noch keine Projekte."));
}

async function renderProject(view, projectId) {
  const p = await api(`/api/projects/${projectId}`);
  const control = async (action) => { await api(`/api/projects/${projectId}/${action}`, { method: "POST" }); route(); };
  const questions = p.questions.filter((q) => q.status === "open");
  show(view, 
    h("div", { class: "row", style: { marginBottom: "16px" } }, h("a", { href: "#/projects", class: "btn small ghost" }, "← Alle Projekte"),
      h("h2", { class: "grow", style: { margin: 0 } }, p.title), badgeFor(p.status),
      p.status === "paused" ? h("button", { class: "btn small", onclick: () => control("resume") }, "Fortsetzen") : h("button", { class: "btn small", onclick: () => control("pause") }, "Pausieren"),
      h("button", { class: "btn small danger", onclick: () => confirm("Projekt wirklich abbrechen?") && control("cancel") }, "Abbrechen")),
    questions.length ? h("div", { class: "panel", style: { borderColor: "var(--warn)", marginBottom: "18px" } }, h("h2", {}, "❓ JARVIS braucht deine Antwort"),
      questions.map((q) => {
        const answer = h("input", { type: "text", placeholder: "Deine Antwort (oder „überspringen“)" });
        return h("div", { style: { marginBottom: "12px" } }, h("p", {}, q.question),
          h("div", { class: "row" }, answer, h("button", { class: "btn primary", onclick: async () => { await api(`/api/project-questions/${q.id}`, { body: { answer: answer.value } }); toast("Danke", "JARVIS arbeitet weiter."); route(); } }, "Antworten")));
      })) : null,
    h("div", { class: "grid cols-2" },
      h("div", { class: "panel" }, h("h2", {}, "Ziel & Plan"), h("p", {}, p.goal), p.summary ? h("p", { class: "muted" }, p.summary) : null,
        h("div", { class: "progress", style: { margin: "10px 0" } }, h("span", { style: { width: `${p.total ? (p.done / p.total) * 100 : 0}%` } })),
        h("div", { class: "faint" }, `${p.done} von ${p.total} Aufgaben erledigt · Kosten $${(p.cost_usd || 0).toFixed(2)}`)),
      h("div", { class: "panel" }, h("h2", {}, "Verlauf"), h("div", { class: "log", style: { height: "220px" } },
        p.events.map((e) => `[${fmtTime(e.ts)}] ${e.text}`).join("\n")))),
    h("div", { class: "panel", style: { marginTop: "18px" } }, h("h2", {}, "Aufgaben"),
      p.tasks.length ? h("div", { class: "list" }, p.tasks.map((t) => h("details", { class: "item", style: { display: "block" } },
        h("summary", { class: "row", style: { cursor: "pointer" } }, h("span", { class: "faint" }, `${t.position}.`), h("span", { class: "grow title" }, t.title), t.milestone ? h("span", { class: "faint" }, t.milestone) : null, badgeFor(t.status)),
        t.description ? h("p", { class: "muted" }, t.description) : null,
        t.feedback ? h("p", { class: "faint" }, `Prüfer: ${t.feedback}`) : null,
        t.result ? h("div", { class: "card-md", html: md(t.result) }) : null)))
        : h("div", { class: "empty" }, "Der Plan wird gerade erstellt …")));
}

/* ------------------------------------------------------------------ Selbstverbesserung */

async function renderImprove(view) {
  if (state.params[0]) return renderImprovement(view, state.params[0]);
  const data = await api("/api/improvements");
  const request = h("textarea", { rows: 3, placeholder: "Was soll JARVIS können? z. B. „Füge eine Druckoption hinzu: Dateien und Chat-Antworten drucken“" });
  const start = async () => {
    if (!request.value.trim()) return;
    const job = await api("/api/improvements", { body: { request: request.value } });
    location.hash = `#/improve/${job.id}`;
  };
  show(view, 
    data.unavailable ? h("div", { class: "panel", style: { borderColor: "var(--warn)", marginBottom: "18px" } }, h("h2", {}, "⚠️ Noch nicht bereit"), h("p", {}, data.unavailable), h("a", { href: "#/setup", class: "btn small" }, "Zur Einrichtung")) : null,
    h("div", { class: "panel", style: { marginBottom: "18px" } }, h("h2", {}, "JARVIS etwas Neues beibringen"),
      h("p", { class: "faint" }, "JARVIS programmiert die Änderung mit Claude Code (über dein Pro-Abo) in einer eigenen Kopie, testet sie und legt sie dir zur Freigabe vor. Sicherheitscode bleibt immer tabu."),
      request, h("div", { class: "row", style: { marginTop: "10px" } }, h("button", { class: "btn primary", onclick: start }, "✨ Umsetzen lassen"),
        h("button", { class: "btn", onclick: async () => { await api("/api/improvements-self-check", { method: "POST" }); toast("Selbst-Check gestartet", "JARVIS sucht nach Verbesserungen – Vorschläge erscheinen hier."); } }, "🔍 Selbst-Check jetzt"),
        h("span", { class: "faint" }, `Automatisch übernehmen: ${({ never: "nie (immer fragen)", plugins_only: "nur Plugins", always: "immer" })[data.auto_merge]}`))),
    data.suggestions.length ? h("div", { class: "panel", style: { marginBottom: "18px" } }, h("h2", {}, "💡 Vorschläge von JARVIS"),
      h("div", { class: "list" }, data.suggestions.map((s) => h("div", { class: "item" },
        h("div", { class: "grow" }, h("div", { class: "title" }, s.title), h("div", { class: "faint" }, s.description)),
        h("button", { class: "btn small primary", onclick: async () => { const job = await api(`/api/suggestions/${s.id}/accept`, { method: "POST" }); location.hash = `#/improve/${job.id}`; } }, "Umsetzen"),
        h("button", { class: "btn small ghost", onclick: async () => { await api(`/api/suggestions/${s.id}/dismiss`, { method: "POST" }); route(); } }, "Verwerfen"))))) : null,
    h("div", { class: "panel" }, h("h2", {}, "Verlauf"),
      data.jobs.length ? h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "#"), h("th", {}, "Auftrag"), h("th", {}, "Status"), h("th", {}, "Aktualisiert"))),
        h("tbody", {}, data.jobs.map((j) => h("tr", { style: { cursor: "pointer" }, onclick: () => { location.hash = `#/improve/${j.id}`; } },
          h("td", {}, j.id), h("td", {}, j.request.slice(0, 110)), h("td", {}, badgeFor(j.status)), h("td", { class: "faint" }, fmtTime(j.updated))))))
        : h("div", { class: "empty" }, "Noch keine Verbesserungen.")));
}

const IMPROVE_STEPS = [["queued", "Warteschlange"], ["running", "Programmiert"], ["testing", "Testet"], ["ready", "Freigabe"], ["merged", "Übernommen"]];

async function renderImprovement(view, jobId) {
  const job = await api(`/api/improvements/${jobId}`);
  const order = IMPROVE_STEPS.map(([k]) => k);
  const current = order.indexOf(job.status);
  const steps = h("div", { class: "steps" }, IMPROVE_STEPS.map(([key, label], index) => h("span", { class: index < current || job.status === "merged" ? "done" : index === current ? "now" : "" }, label)));
  const logBox = h("div", { class: "log", id: "improve-log" }, job.log.join("\n"));
  state.improvementLog = { id: Number(jobId), el: logBox };
  const action = async (name) => {
    try { await api(`/api/improvements/${jobId}/${name}`, { method: "POST" }); route(); }
    catch (error) { toast("Nicht möglich", error.message, "error", 10000); }
  };
  const diffBox = h("div", { class: "diff hidden" });
  const showDiff = async () => {
    if (!diffBox.classList.contains("hidden")) { diffBox.classList.add("hidden"); return; }
    const text = await api(`/api/improvements/${jobId}/diff`);
    diffBox.innerHTML = esc(text || "(kein Diff)").split("\n").map((line) => {
      const cls = line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff --git") ? "file" : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : line.startsWith("@@") ? "hunk" : "";
      return cls ? `<span class="${cls}">${line}</span>` : line;
    }).join("\n");
    diffBox.classList.remove("hidden");
  };
  show(view, 
    h("div", { class: "row", style: { marginBottom: "12px" } }, h("a", { href: "#/improve", class: "btn small ghost" }, "← Übersicht"),
      h("h2", { class: "grow", style: { margin: 0 } }, `Verbesserung #${job.id}`), badgeFor(job.status)),
    steps,
    h("div", { class: "panel", style: { marginBottom: "18px" } }, h("h2", {}, "Auftrag"), h("p", {}, job.request),
      job.error ? h("p", { style: { color: "var(--bad)" } }, job.error) : null,
      h("div", { class: "row" },
        job.status === "ready" ? h("button", { class: "btn primary", onclick: () => action("approve") }, "✅ Übernehmen & neu starten") : null,
        ["ready", "failed"].includes(job.status) ? h("button", { class: "btn danger", onclick: () => action("reject") }, "Verwerfen") : null,
        ["queued", "running", "testing"].includes(job.status) ? h("button", { class: "btn danger", onclick: () => action("cancel") }, "Abbrechen") : null,
        ["failed", "rejected", "cancelled", "rolled_back"].includes(job.status) ? h("button", { class: "btn", onclick: async () => { const j = await api(`/api/improvements/${jobId}/retry`, { method: "POST" }); location.hash = `#/improve/${j.id}`; } }, "Erneut versuchen") : null,
        job.branch ? h("button", { class: "btn", onclick: showDiff }, "Änderungen (Diff) anzeigen") : null)),
    job.summary ? h("div", { class: "panel", style: { marginBottom: "18px" } }, h("h2", {}, "Zusammenfassung von Claude Code"), h("div", { class: "card-md", html: md(job.summary) }),
      job.changed_files.length ? h("p", { class: "faint" }, `Dateien: ${job.changed_files.join(", ")}`) : null,
      job.diff_stat ? h("pre", { class: "mono faint" }, job.diff_stat) : null) : null,
    diffBox,
    h("div", { class: "panel", style: { marginTop: "18px" } }, h("h2", {}, "Live-Protokoll"), logBox),
    job.test_output ? h("details", { class: "panel", style: { marginTop: "18px" } }, h("summary", {}, "Testausgabe"), h("pre", { class: "log", style: { height: "260px" } }, job.test_output)) : null,
  );
  logBox.scrollTop = logBox.scrollHeight;
}

/* ------------------------------------------------------------------ Bilder, Erinnerungen, Gedächtnis */

async function renderImages(view) {
  const images = await api("/api/images");
  show(view, images.length ? h("div", { class: "gallery" }, images.map((img) => h("figure", {},
    h("img", { src: imgUrl(img.url), loading: "lazy", onclick: () => lightbox(img.url) }),
    h("figcaption", {}, h("span", { class: `badge ${img.kind === "generated" ? "purple" : "info"}` }, img.kind === "generated" ? "erzeugt" : "gefunden"),
      h("span", { class: "grow", title: img.attribution || img.prompt || "" }, (img.prompt || "").slice(0, 40)),
      img.source_url && img.kind === "search" ? h("a", { href: img.source_url, target: "_blank", rel: "noopener noreferrer", title: img.attribution || "Quelle" }, "↗") : null,
      h("button", { class: "btn small ghost", onclick: async () => { await api(`/api/images/${img.id}`, { method: "DELETE" }); route(); } }, "×")))))
    : h("div", { class: "empty" }, "Noch keine Bilder. Sag im Chat z. B. „Erstelle ein Bild von einem Leuchtturm im Sturm“."));
}

async function renderReminders(view) {
  const reminders = await api("/api/reminders");
  const text = h("input", { type: "text", placeholder: "Woran soll ich dich erinnern?" });
  const due = h("input", { type: "datetime-local" });
  const repeat = h("select", {}, [["", "einmalig"], ["daily", "täglich"], ["weekdays", "werktags"], ["weekly", "wöchentlich"], ["monthly", "monatlich"]].map(([v, l]) => h("option", { value: v }, l)));
  const add = async () => {
    if (!text.value.trim() || !due.value) return;
    await api("/api/reminders", { body: { text: text.value, due: due.value, repeat: repeat.value || null } });
    route();
  };
  show(view, 
    h("div", { class: "panel", style: { marginBottom: "18px" } }, h("h2", {}, "Neue Erinnerung"),
      h("div", { class: "row" }, h("div", { class: "grow" }, text), h("div", { style: { width: "220px" } }, due), h("div", { style: { width: "150px" } }, repeat), h("button", { class: "btn primary", onclick: add }, "Hinzufügen")),
      h("p", { class: "faint" }, "Tipp: Du kannst JARVIS auch einfach sagen „Erinnere mich in 20 Minuten an den Tee“.")),
    h("div", { class: "panel" }, h("h2", {}, "Anstehend"),
      reminders.length ? h("div", { class: "list" }, reminders.map((r) => h("div", { class: "item" },
        h("div", { class: "faint", style: { minWidth: "120px" } }, fmtTime(r.due)), h("div", { class: "grow" }, r.text), r.repeat ? h("span", { class: "badge" }, r.repeat) : null,
        h("button", { class: "btn small ghost", onclick: async () => { await api(`/api/reminders/${r.id}`, { method: "DELETE" }); route(); } }, "×"))))
        : h("div", { class: "empty" }, "Keine Erinnerungen.")));
}

async function renderMemory(view) {
  const memories = await api("/api/memory");
  show(view, h("div", { class: "panel" }, h("h2", {}, `Was JARVIS sich gemerkt hat (${memories.length})`),
    h("p", { class: "faint" }, "Sag z. B. „Merk dir, dass ich Tee ohne Zucker trinke“. Einträge kannst du hier löschen."),
    memories.length ? h("div", { class: "list" }, memories.map((m) => h("div", { class: "item" },
      h("span", { class: "badge" }, m.category), h("div", { class: "grow" }, m.text), h("span", { class: "faint" }, fmtTime(m.created)),
      h("button", { class: "btn small ghost", onclick: async () => { await api(`/api/memory/${m.id}`, { method: "DELETE" }); route(); } }, "×"))))
      : h("div", { class: "empty" }, "Noch nichts gespeichert.")));
}

/* ------------------------------------------------------------------ Protokoll, Modelle */

async function renderLog(view) {
  const tab = state.params[0] || "actions";
  const tabs = h("div", { class: "tabs" }, [["actions", "Aktionen"], ["logs", "Logdatei"], ["costs", "Kosten"]].map(([key, label]) =>
    h("button", { class: tab === key ? "active" : "", onclick: () => { location.hash = `#/log/${key}`; } }, label)));
  let body;
  if (tab === "logs") {
    const data = await api("/api/logs?lines=500");
    body = h("div", { class: "panel" }, h("p", { class: "faint" }, data.file), h("div", { class: "log", style: { height: "65vh" } }, data.lines.join("\n")));
  } else if (tab === "costs") {
    const data = await api("/api/costs");
    const table = (rows) => rows.length ? h("table", {}, h("thead", {}, h("tr", {}, ["Modell", "Aufrufe", "Tokens ein", "Tokens aus", "Kosten"].map((c) => h("th", {}, c)))),
      h("tbody", {}, rows.map((r) => h("tr", {}, h("td", {}, r.model), h("td", {}, r.calls), h("td", {}, r.input_tokens), h("td", {}, r.output_tokens), h("td", {}, `$${r.cost_usd.toFixed(4)}`))))) : h("div", { class: "empty" }, "Keine Aufrufe.");
    body = h("div", { class: "grid" }, h("div", { class: "panel" }, h("p", {}, data.status)),
      h("div", { class: "panel" }, h("h2", {}, "Heute"), table(data.day)), h("div", { class: "panel" }, h("h2", {}, "Dieser Monat"), table(data.month)));
  } else {
    const rows = await api("/api/audit?limit=300");
    body = h("div", { class: "panel" }, rows.length ? h("table", {}, h("thead", {}, h("tr", {}, ["Zeit", "Werkzeug", "Quelle", "Stufe", "Entscheidung", "Ergebnis"].map((c) => h("th", {}, c)))),
      h("tbody", {}, rows.map((r) => h("tr", {}, h("td", { class: "faint" }, fmtTime(r.ts)), h("td", { class: "mono" }, r.tool), h("td", {}, r.source), h("td", {}, r.level),
        h("td", {}, h("span", { class: `badge ${r.decision === "approved" || r.decision === "auto" ? "ok" : r.decision === "denied" || r.decision === "forbidden" ? "bad" : "warn"}` }, r.decision)),
        h("td", { class: "faint", title: r.arguments }, `${r.ok ? "✔" : "✘"} ${(r.result || "").slice(0, 120)}`))))) : h("div", { class: "empty" }, "Noch keine Aktionen."));
  }
  show(view, tabs, body);
}

async function renderModels(view) {
  const data = await api("/api/models");
  show(view, 
    h("div", { class: "panel", style: { marginBottom: "18px" } }, h("h2", {}, "KI-Modelle"),
      h("table", {}, h("thead", {}, h("tr", {}, ["Modell", "Anbieter", "Eigenschaften", "Status"].map((c) => h("th", {}, c)))),
        h("tbody", {}, data.models.map((m) => h("tr", {}, h("td", {}, h("div", { class: "title" }, m.label), h("div", { class: "faint mono" }, m.target)), h("td", {}, m.provider),
          h("td", {}, h("div", { class: "row" }, m.local ? h("span", { class: "badge ok" }, "lokal") : h("span", { class: "badge info" }, "Cloud"),
            m.subscription ? h("span", { class: "badge purple" }, "Abo") : m.paid ? h("span", { class: "badge bad" }, "kostet Geld") : h("span", { class: "badge ok" }, "kostenlos"),
            m.trusted ? h("span", { class: "badge" }, "darf private Daten") : null, m.tools ? h("span", { class: "badge" }, "Werkzeuge") : null)),
          h("td", {}, m.status === "bereit" ? h("span", { class: "badge ok" }, "bereit") : h("span", { class: "faint" }, m.status))))))),
    h("div", { class: "panel" }, h("h2", {}, "Routing – welches Modell wofür (in Reihenfolge, mit Ausweichmodellen)"),
      h("table", {}, h("tbody", {}, Object.entries(data.routes).map(([task, chain]) => h("tr", {}, h("td", { class: "title" }, task), h("td", {}, chain.join("  →  "))))))));
}

/* ------------------------------------------------------------------ Einrichtung */

async function renderSetup(view) {
  const data = await api("/api/setup");
  const settings = await api("/api/settings");
  const geminiKey = h("input", { type: "password", placeholder: "Gemini-API-Key einfügen (beginnt meist mit AIza…)" });
  const saveKey = async () => {
    if (!geminiKey.value.trim()) return;
    try { await api("/api/secrets", { body: { name: "GEMINI_API_KEY", value: geminiKey.value.trim() } }); toast("Gespeichert", "Der Key liegt sicher im Windows Credential Manager."); route(); }
    catch (error) { toast("Fehler", error.message, "error"); }
  };
  const pullBox = h("div", { class: "faint", id: "pull-status" });
  const extra = {
    gemini: h("div", { class: "row", style: { marginTop: "8px" } }, h("div", { class: "grow" }, geminiKey), h("button", { class: "btn primary", onclick: saveKey }, "Speichern"),
      h("a", { class: "btn", href: "https://aistudio.google.com/apikey", target: "_blank", rel: "noopener noreferrer" }, "Key holen ↗")),
    ollama: data.missing_models.length ? h("div", { style: { marginTop: "8px" } }, h("button", { class: "btn primary", onclick: async () => { await api("/api/setup/ollama-pull", { method: "POST" }); pullBox.textContent = "Download gestartet …"; } }, `Fehlende Modelle laden (${data.missing_models.join(", ")})`), pullBox) : null,
  };
  show(view, 
    h("div", { class: "panel", style: { marginBottom: "18px" } }, h("h2", {}, "Einrichtung – alles kostenlos"),
      h("p", { class: "faint" }, "JARVIS braucht keine kostenpflichtigen API-Keys. Claude läuft über dein Pro-Abo (Claude Code), Gemini über den kostenlosen Key, lokale Modelle über Ollama."),
      data.checks.map((c) => h("div", { class: "check" }, h("div", { class: "icon" }, c.ok ? "✅" : c.optional ? "⚪" : "⚠️"),
        h("div", { class: "grow" }, h("div", { class: "title" }, c.title, c.optional ? h("span", { class: "faint" }, "  (optional)") : null), h("div", { class: "muted" }, c.detail),
          !c.ok && c.hint ? h("div", { class: "faint" }, c.hint) : null, !c.ok ? extra[c.key] || null : null)))),
    h("div", { class: "panel" }, h("h2", {}, "Gespeicherte Schlüssel"),
      h("table", {}, h("tbody", {}, Object.entries(settings.secrets).map(([name, source]) => h("tr", {}, h("td", { class: "mono" }, name),
        h("td", {}, source === "keyring" ? h("span", { class: "badge ok" }, "Credential Manager") : source === "env" ? h("span", { class: "badge info" }, ".env / Umgebung") : h("span", { class: "faint" }, "nicht gesetzt"))))))));
}

/* ------------------------------------------------------------------ Einstellungen */

async function renderSettings(view) {
  const s = await api("/api/settings");
  const field = (label, input, hint) => h("label", { class: "field" }, label, input, hint ? h("span", { class: "faint" }, hint) : null);
  const text = (value) => h("input", { type: "text", value: value ?? "" });
  const select = (value, options) => h("select", {}, options.map(([v, l]) => h("option", { value: v, selected: String(v) === String(value) }, l)));
  const check = (value) => h("input", { type: "checkbox", checked: !!value, style: { width: "auto" } });
  const f = {
    user_name: text(s.assistant.user_name), home_location: text(s.assistant.home_location),
    voice_enabled: check(s.voice.enabled), wake_threshold: h("input", { type: "number", step: "0.05", min: "0.1", max: "0.95", value: s.voice.wake_threshold }),
    tts_engine: select(s.voice.tts_engine, [["edge", "Edge-Stimmen (natürlich, kostenlos, online)"], ["sapi", "Windows-Stimme (offline)"], ["off", "aus"]]),
    tts_voice: select(s.voice.tts_voice, [["de-DE-ConradNeural", "Conrad (männlich)"], ["de-DE-FlorianMultilingualNeural", "Florian (männlich)"], ["de-DE-KillianNeural", "Killian (männlich)"], ["de-DE-KatjaNeural", "Katja (weiblich)"], ["de-DE-SeraphinaMultilingualNeural", "Seraphina (weiblich)"]]),
    follow_up: h("input", { type: "number", min: "0", max: "30", value: s.voice.follow_up_seconds }),
    auto_merge: select(s.self_improve.auto_merge, [["never", "Nie – immer mich fragen"], ["plugins_only", "Nur reine Plugin-Änderungen automatisch"], ["always", "Immer automatisch (nicht empfohlen)"]]),
    self_check: select(s.self_improve.self_check, [["off", "aus"], ["daily", "täglich"], ["weekly", "wöchentlich"]]),
    claude_model: select(s.self_improve.claude_model, [["sonnet", "Sonnet (schnell, schont das Abo-Limit)"], ["opus", "Opus (stärker, verbraucht mehr Limit)"]]),
    report_time: h("input", { type: "text", value: s.projects.daily_report_time }), max_tasks: h("input", { type: "number", min: "1", value: s.projects.max_tasks_per_day }),
    tray: check(s.desktop.tray), hotkeys: check(s.desktop.hotkeys), notifications: check(s.desktop.notifications),
  };
  const theme = select(document.documentElement.dataset.theme, [["dark", "Dunkel"], ["light", "Hell"]]);
  theme.addEventListener("change", () => { document.documentElement.dataset.theme = theme.value; try { localStorage.setItem("jarvis-theme", theme.value); } catch (e) { /* egal */ } });
  const save = async () => {
    const changes = {
      assistant: { user_name: f.user_name.value, home_location: f.home_location.value },
      voice: { enabled: f.voice_enabled.checked, wake_threshold: Number(f.wake_threshold.value), tts_engine: f.tts_engine.value, tts_voice: f.tts_voice.value, follow_up_seconds: Number(f.follow_up.value) },
      self_improve: { auto_merge: f.auto_merge.value, self_check: f.self_check.value, claude_model: f.claude_model.value },
      projects: { daily_report_time: f.report_time.value, max_tasks_per_day: Number(f.max_tasks.value) },
      desktop: { tray: f.tray.checked, hotkeys: f.hotkeys.checked, notifications: f.notifications.checked },
    };
    try {
      await api("/api/settings", { body: { changes } });
      const close = modal(h("div", { class: "modal" }, h("h3", {}, "Gespeichert"), h("p", {}, "Die Änderungen gelten nach einem Neustart von JARVIS."),
        h("div", { class: "row" }, h("button", { class: "btn", onclick: () => close() }, "Später"),
          h("button", { class: "btn primary", onclick: async () => { await api("/api/system/restart", { method: "POST" }); close(); toast("Neustart", "JARVIS startet neu …"); } }, "Jetzt neu starten"))));
    } catch (error) { toast("Fehler", error.message, "error", 10000); }
  };
  show(view, h("div", { class: "grid cols-2" },
    h("div", { class: "panel grid" }, h("h2", {}, "Allgemein"), field("Wie soll JARVIS dich nennen?", f.user_name), field("Wohnort (für Wetter)", f.home_location, "z. B. Berlin"), field("Design", theme)),
    h("div", { class: "panel grid" }, h("h2", {}, "Sprache"), h("label", { class: "row" }, f.voice_enabled, "„Hey Jarvis“ dauerhaft zuhören"),
      field("Empfindlichkeit des Wake-Words (niedriger = empfindlicher)", f.wake_threshold), field("Sprachausgabe", f.tts_engine), field("Stimme", f.tts_voice), field("Nachfragen ohne Wake-Word (Sekunden)", f.follow_up)),
    h("div", { class: "panel grid" }, h("h2", {}, "Selbstverbesserung"), field("Änderungen übernehmen", f.auto_merge), field("Selbstständig nach Verbesserungen suchen", f.self_check), field("Claude-Modell zum Programmieren", f.claude_model)),
    h("div", { class: "panel grid" }, h("h2", {}, "Projekte & Desktop"), field("Tagesbericht um (HH:MM)", f.report_time), field("Max. Arbeitsschritte pro Tag", f.max_tasks, "schützt deine kostenlosen Kontingente"),
      h("label", { class: "row" }, f.tray, "Tray-Icon"), h("label", { class: "row" }, f.hotkeys, "Globale Tastenkürzel"), h("label", { class: "row" }, f.notifications, "Windows-Benachrichtigungen")),
  ), h("div", { class: "row", style: { marginTop: "18px" } },
    h("button", { class: "btn primary", onclick: save }, "Speichern"),
    h("button", { class: "btn", onclick: async () => { await api("/api/system/restart", { method: "POST" }); toast("Neustart", "JARVIS startet neu …"); } }, "JARVIS neu starten"),
    h("span", { class: "faint" }, `Gespeichert wird in ${s.local_file}`)));
}

const RENDER = {
  overview: renderOverview, chat: renderChat, projects: renderProjects, improve: renderImprove, images: renderImages,
  reminders: renderReminders, memory: renderMemory, log: renderLog, models: renderModels, setup: renderSetup, settings: renderSettings,
};

/* ------------------------------------------------------------------ Bestätigungen, Not-Aus, Status */

const confirmationModals = {};

function showConfirmation(item) {
  if (confirmationModals[item.id]) return;
  const close = modal(h("div", { class: "modal" },
    h("h3", {}, "🔐 JARVIS möchte etwas tun"),
    h("p", { class: "muted" }, `Werkzeug: ${item.tool} · ausgelöst von: ${item.source}`),
    h("pre", {}, item.summary),
    h("div", { class: "row" },
      h("button", { class: "btn danger", onclick: () => answerConfirmation(item.id, false) }, "Ablehnen"),
      h("button", { class: "btn primary", onclick: () => answerConfirmation(item.id, true) }, "Erlauben"))),
  { onClose: () => { delete confirmationModals[item.id]; } });
  confirmationModals[item.id] = close;
}

async function answerConfirmation(id, approved) {
  try { await api(`/api/confirmations/${id}`, { body: { approved } }); } catch (e) { /* schon erledigt */ }
  confirmationModals[id]?.();
  delete confirmationModals[id];
}

async function refreshStatus() {
  try {
    const status = await api("/api/status");
    state.status = status;
    state.pendingConfirmations = status.confirmations;
    status.confirmations.forEach(showConfirmation);
    document.getElementById("kill-banner").classList.toggle("hidden", !status.killswitch.engaged);
    document.getElementById("models-label").textContent = `${status.models_ready.length} Modelle bereit`;
    document.querySelector("#models-pill .dot").className = `dot ${status.models_ready.length ? "ok" : "bad"}`;
    setVoice(status.voice);
    document.getElementById("sidebar-foot").textContent = status.hardware || "";
    updateBadges();
  } catch (error) { /* Dienst evtl. im Neustart */ }
}

const VOICE_BUSY = ["hört zu", "denkt nach", "spricht"];

function setVoice(voice) {
  const pill = document.getElementById("voice-pill");
  const label = document.getElementById("voice-label");
  const dot = document.querySelector("#voice-pill .dot");
  const stateName = voice?.state || "aus";
  document.querySelector(".reactor").classList.toggle("busy", !!voice?.enabled && VOICE_BUSY.includes(stateName));
  if (!voice || !voice.enabled || stateName === "aus") {
    label.textContent = "Sprache aus";
    dot.className = "dot";
    pill.title = "Sprachsteuerung ist aus – in den Einstellungen aktivieren";
  } else if (stateName === "fehler") {
    label.textContent = "Sprache: Fehler";
    dot.className = "dot bad";
    pill.title = voice.error ? `Fehler: ${voice.error}` : "Sprachsteuerung ist fehlgeschlagen";
  } else if (!voice.listening || stateName === "pausiert") {
    label.textContent = "Sprache pausiert";
    dot.className = "dot";
    pill.title = "Klicken, um wieder zuzuhören";
  } else {
    label.textContent = `🎙️ ${stateName}`;
    dot.className = `dot ${VOICE_BUSY.includes(stateName) ? "live" : stateName === "lädt" ? "warn" : "ok"}`;
    pill.title = "Sprachsteuerung an/aus (klicken zum Pausieren)";
  }
}

/* ------------------------------------------------------------------ Live-Verbindung */

function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws?t=${encodeURIComponent(TOKEN)}`);
  state.ws = ws;
  ws.onmessage = (message) => {
    const event = JSON.parse(message.data);
    const type = event.type;
    if (type.startsWith("chat.") || type.startsWith("tool.") || type === "image.new") onChatEvent(event);
    if (type === "confirmation.requested") { state.pendingConfirmations.push(event.data); showConfirmation(event.data); updateBadges(); }
    if (type === "confirmation.resolved") {
      state.pendingConfirmations = state.pendingConfirmations.filter((c) => c.id !== event.data.id);
      confirmationModals[event.data.id]?.();
      updateBadges();
    }
    if (type === "notification") toast(event.data.title, event.data.message, event.data.level);
    if (type === "killswitch.engaged" || type === "killswitch.released") {
      document.getElementById("kill-banner").classList.toggle("hidden", type === "killswitch.released");
      if (type === "killswitch.engaged") toast("Not-Aus", "Alle Aufgaben wurden gestoppt.", "error");
    }
    if (type === "voice.state" && state.status) { state.status.voice = Object.assign(state.status.voice || {}, event.data); setVoice(state.status.voice); }
    if (type === "voice.heard") toast("🎙️ Gehört", event.data.text, "info", 4000);
    if (type === "improvement.log" && state.improvementLog.id === event.data.id && state.improvementLog.el?.isConnected) {
      const el = state.improvementLog.el;
      el.textContent += `\n${event.data.line}`;
      el.scrollTop = el.scrollHeight;
    }
    if (type === "improvement.updated" && state.page === "improve") {
      if (!state.params[0] || Number(state.params[0]) === event.data.id) setTimeout(route, 300);
    }
    if (type === "project.updated" && state.page === "projects") setTimeout(route, 300);
    if ((type === "cards.changed" || type === "reminders.changed") && state.page === "overview") setTimeout(route, 300);
    if (type === "setup.pull") {
      const box = document.getElementById("pull-status");
      if (box) box.textContent = `${event.data.model} ${event.data.status || ""} ${event.data.percent != null ? `${event.data.percent} %` : ""}`;
      if (event.data.status === "fertig" && state.page === "setup") setTimeout(route, 500);
    }
  };
  ws.onclose = () => setTimeout(connect, 2000);
}

/* ------------------------------------------------------------------ Start */

document.getElementById("kill-btn").addEventListener("click", async () => {
  await api("/api/killswitch", { body: { engage: true } });
  refreshStatus();
});
document.getElementById("kill-release").addEventListener("click", async () => {
  await api("/api/killswitch", { body: { engage: false } });
  refreshStatus();
});
document.getElementById("voice-pill").addEventListener("click", async () => {
  if (!state.status?.voice?.enabled) { location.hash = "#/settings"; return; }
  try { const voice = await api("/api/voice/listening", { body: { enabled: !state.status.voice.listening } }); state.status.voice = voice; setVoice(voice); }
  catch (error) { toast("Sprache", error.message, "error"); }
});
document.getElementById("models-pill").addEventListener("click", () => { location.hash = "#/models"; });
document.getElementById("menu-btn").addEventListener("click", () => document.getElementById("sidebar").classList.toggle("open"));
window.addEventListener("hashchange", route);
route();
refreshStatus();
setInterval(refreshStatus, 15000);
connect();
