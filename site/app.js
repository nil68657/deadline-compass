import { BRAND } from "./brand.js";
import {
  activeGate,
  deadlineLabel,
  deadlineStatus,
  matchesFilters,
  sortEvents,
  uniqueValues,
} from "./logic.js";

const PAGE_SIZE = 48;
const EVENT_TYPES = [
  "Academic conference",
  "Summit",
  "Industry conference",
  "Hackathon",
  "Open-source meetup",
];
const state = {
  phase: "loading",
  events: [],
  filtered: [],
  visible: PAGE_SIZE,
  sourceCheckedAt: "",
  coverageNotice: "",
};

const form = document.querySelector("#filters");
const results = document.querySelector("#results");
const resultSummary = document.querySelector("#result-summary");
const loadMore = document.querySelector("#load-more");
const clearFilters = document.querySelector("#clear-filters");
const freshness = document.querySelector("#freshness");

document.querySelectorAll("[data-brand]").forEach((node) => {
  node.textContent = BRAND.name;
});
document.querySelectorAll("[data-tagline]").forEach((node) => {
  node.textContent = BRAND.tagline;
});
document.title = `${BRAND.name} — opportunity deadlines`;
document.querySelector('meta[name="description"]').content = BRAND.description;

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function safeSourceUrl(value) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : "#";
  } catch {
    return "#";
  }
}

function formatDate(iso, options = {}) {
  if (!iso) return "Not announced";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeZone: "UTC",
    ...options,
  }).format(new Date(`${iso}T12:00:00Z`));
}

function fillSelect(id, values, label) {
  const select = document.querySelector(`#${id}`);
  select.replaceChildren(new Option(label, ""));
  values.forEach((value) => select.add(new Option(value, value)));
}

function filtersFromForm() {
  const data = new FormData(form);
  return {
    query: String(data.get("query") || ""),
    organization: String(data.get("organization") || ""),
    eventType: String(data.get("eventType") || ""),
    topic: String(data.get("topic") || ""),
    mode: String(data.get("mode") || ""),
    status: String(data.get("status") || ""),
    sort: String(data.get("sort") || "deadline"),
  };
}

function syncUrl(filters) {
  const params = new URLSearchParams();
  Object.entries(filters).forEach(([key, value]) => {
    if (value && !(key === "sort" && value === "deadline")) params.set(key, value);
  });
  const query = params.toString();
  history.replaceState(null, "", `${location.pathname}${query ? `?${query}` : ""}${location.hash}`);
}

function restoreUrl() {
  const params = new URLSearchParams(location.search);
  for (const element of form.elements) {
    if (element.name && params.has(element.name)) element.value = params.get(element.name);
  }
}

function statusTitle(status) {
  return {
    urgent: "Due within 14 days",
    open: "Due within 45 days",
    upcoming: "Upcoming",
    archived: "Archived",
    unannounced: "Date not announced",
  }[status];
}

function deadlineRows(event) {
  const entries = [
    ["Abstract", event.deadlines.abstract],
    ["Paper / proposal", event.deadlines.paper],
    ["Notification", event.deadlines.notification],
    ["Camera-ready", event.deadlines.camera_ready],
  ].filter(([, date]) => date);
  if (!entries.length) return '<p class="muted">No dates have been published.</p>';
  return `<dl class="date-grid">${entries.map(([label, date]) => `
    <div><dt>${label}</dt><dd>${formatDate(date)}</dd></div>
  `).join("")}</dl>`;
}

function forwardedNote(event) {
  const previous = event.previous_cycle;
  if (!previous) return "";
  return `<p class="forwarded-note">
      <strong>${escapeHtml(previous.edition)} cycle archived</strong> — its call closed
      ${formatDate(previous.closed)}. Forwarded to the ${escapeHtml(event.edition)} cycle;
      these dates are projected until its call is published.
    </p>`;
}

function card(event) {
  const status = deadlineStatus(event);
  const gate = activeGate(event);
  const deadline = gate?.date || "";
  const confidence = ["verified", "announced", "projected"].includes(event.confidence)
    ? event.confidence
    : "projected";
  const topics = event.topics.map((topic) => `<span class="chip">${escapeHtml(topic)}</span>`).join("");
  const timezone = event.deadlines.timezone === "See source"
    ? "Time not normalized — check source"
    : `Deadline timezone: ${event.deadlines.timezone}`;
  return `
    <article class="event-card" data-status="${status}">
      <div class="card-topline">
        <span class="status-pill status-${status}">${statusTitle(status)}</span>
        <span class="confidence confidence-${confidence}">${escapeHtml(confidence)}</span>
      </div>
      <div class="card-heading">
        <div>
          <p class="eyebrow">${escapeHtml(event.acronym)} · ${escapeHtml(event.edition)}</p>
          <h2>${escapeHtml(event.name)}</h2>
        </div>
      </div>
      <p class="organization">${escapeHtml(event.organization)}</p>
      <div class="deadline-block">
        <div>
          <span class="deadline-kind">${gate ? `${escapeHtml(gate.kind)} deadline` : "Deadline not announced"}</span>
          <strong>${formatDate(deadline)}</strong>
          <small>${escapeHtml(timezone)}</small>
        </div>
        <span class="countdown">${deadlineLabel(event)}</span>
      </div>
      ${forwardedNote(event)}
      <ul class="event-facts" aria-label="Event details">
        <li><span aria-hidden="true">◉</span>${escapeHtml(event.event_type)}</li>
        <li><span aria-hidden="true">⌖</span>${escapeHtml(event.location)}</li>
        <li><span aria-hidden="true">◫</span>${event.event_start ? formatDate(event.event_start) : "Event date TBA"}</li>
      </ul>
      <div class="chips" aria-label="Topics">${topics}</div>
      <details>
        <summary>Dates and provenance</summary>
        ${deadlineRows(event)}
        <p class="provenance">
          ${escapeHtml(event.source_basis)} · confirm details on the linked source
        </p>
      </details>
      <a class="source-link" href="${escapeHtml(safeSourceUrl(event.source_url))}" target="_blank" rel="noopener noreferrer"
         aria-label="Check the official source for ${escapeHtml(event.name)} (opens in a new tab)">
        Check the official source <span aria-hidden="true">↗</span>
      </a>
    </article>`;
}

function render() {
  if (state.phase !== "ready") return;
  const filters = filtersFromForm();
  state.filtered = sortEvents(
    state.events.filter((event) => matchesFilters(event, filters)),
    filters.sort,
  );
  const shown = state.filtered.slice(0, state.visible);
  results.innerHTML = shown.length
    ? shown.map(card).join("")
    : `<div class="empty-state">
        <span aria-hidden="true">⌁</span>
        <h2>No matching opportunities</h2>
        <p>Broaden a filter or clear them to return to the full index.</p>
        <button class="button secondary" type="button" data-clear>Clear filters</button>
      </div>`;
  const summary = `Showing ${shown.length.toLocaleString()} of ${state.filtered.length.toLocaleString()} matching opportunities`;
  const archived = state.events.filter((event) => deadlineStatus(event) === "archived").length;
  // Only worth saying while they are hidden: once the filter is on Archived,
  // the count above is the archived count.
  resultSummary.innerHTML = !filters.status && archived
    ? `${summary} · <button type="button" class="link-button" data-show-archived>${archived.toLocaleString()} archived hidden</button>`
    : summary;
  loadMore.hidden = shown.length >= state.filtered.length;
  syncUrl(filters);
}

function renderStats() {
  const counts = state.events.reduce((acc, event) => {
    const status = deadlineStatus(event);
    acc[status] = (acc[status] || 0) + 1;
    return acc;
  }, {});
  document.querySelector("#stat-total").textContent = state.events.length.toLocaleString();
  document.querySelector("#stat-urgent").textContent = (counts.urgent || 0).toLocaleString();
  document.querySelector("#stat-open").textContent = (
    (counts.urgent || 0) + (counts.open || 0) + (counts.upcoming || 0)
  ).toLocaleString();
  document.querySelector("#stat-unannounced").textContent = (counts.unannounced || 0).toLocaleString();
  document.querySelector("#stat-archived").textContent = (counts.archived || 0).toLocaleString();
}

function initializeFilters() {
  fillSelect("organization", uniqueValues(state.events, (event) => [event.organization_group]), "All organizations");
  fillSelect("eventType", EVENT_TYPES, "All event types");
  fillSelect("topic", uniqueValues(state.events, (event) => event.categories), "All categories");
  fillSelect("mode", ["In person", "Online", "Hybrid", "TBA"], "All locations");
  restoreUrl();
}

async function loadData() {
  state.phase = "loading";
  results.setAttribute("aria-busy", "true");
  resultSummary.textContent = "Loading opportunities…";
  freshness.textContent = "Loading source freshness…";
  results.innerHTML = `<div class="loading-state" role="status">
    <span class="loader" aria-hidden="true"></span>
    <p>Plotting the latest opportunities…</p>
  </div>`;
  try {
    const response = await fetch("./data/events.json");
    if (!response.ok) throw new Error(`Data request failed (${response.status})`);
    const payload = await response.json();
    if (!Array.isArray(payload.events)) throw new Error("Data response has no events array");
    state.events = payload.events;
    state.sourceCheckedAt = payload.source_checked_at;
    state.coverageNotice = payload.coverage_notice;
    initializeFilters();
    renderStats();
    freshness.innerHTML = `
      <strong>Source review:</strong> ${formatDate(state.sourceCheckedAt)}
      <span aria-hidden="true">·</span> ${escapeHtml(state.coverageNotice)}
    `;
    // Written by the deploy, not committed, so its absence is normal: a
    // local checkout, a preview, or the first deploy before this shipped.
    // Non-fatal by design - the source-review date above stands on its own.
    fetch("./data/build-info.json")
      .then((response) => (response.ok ? response.json() : null))
      .then((info) => {
        if (!info || typeof info.built_at !== "string") return;
        freshness.insertAdjacentHTML(
          "beforeend",
          ` <span aria-hidden="true">\u00b7</span> <strong>Site rebuilt:</strong> ${formatDate(info.built_at.slice(0, 10))}`,
        );
      })
      .catch(() => {});
    results.setAttribute("aria-busy", "false");
    if (!state.events.length) {
      state.phase = "empty";
      resultSummary.textContent = "No opportunities are currently published.";
      results.innerHTML = `<div class="empty-state">
        <h2>The index is empty</h2>
        <p>Source data loaded successfully, but it contains no opportunities.</p>
      </div>`;
      return;
    }
    state.phase = "ready";
    render();
  } catch (error) {
    state.phase = "error";
    console.error(error);
    results.setAttribute("aria-busy", "false");
    freshness.textContent = "Source freshness unavailable.";
    resultSummary.textContent = "The opportunity index could not be loaded.";
    results.innerHTML = `<div class="error-state" role="alert">
      <h2>Data unavailable</h2>
      <p>${escapeHtml(error.message)}. Check your connection, then try again.</p>
      <button class="button" type="button" data-retry>Retry</button>
    </div>`;
  }
}

form.addEventListener("input", () => {
  state.visible = PAGE_SIZE;
  render();
});
form.addEventListener("change", () => {
  state.visible = PAGE_SIZE;
  render();
});
clearFilters.addEventListener("click", () => {
  form.reset();
  state.visible = PAGE_SIZE;
  render();
  document.querySelector("#query").focus();
});
loadMore.addEventListener("click", () => {
  state.visible += PAGE_SIZE;
  render();
});
results.addEventListener("click", (event) => {
  if (event.target.closest("[data-clear]")) clearFilters.click();
  if (event.target.closest("[data-retry]")) loadData();
});

resultSummary.addEventListener("click", (fromClick) => {
  if (!fromClick.target.closest("[data-show-archived]")) return;
  form.elements.status.value = "archived";
  state.visible = PAGE_SIZE;
  render();
});

function scheduleMidnightRefresh() {
  const now = new Date();
  const next = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1, 0, 0, 1);
  setTimeout(() => {
    renderStats();
    render();
    scheduleMidnightRefresh();
  }, next.getTime() - now.getTime());
}

loadData();
scheduleMidnightRefresh();
