"use strict";

// ---- Map setup -------------------------------------------------------------
// Centered on the continental US at a zoom that still shows Toronto up top.
const map = L.map("map", {
  center: [41.0, -96.0],
  zoom: 4,
  scrollWheelZoom: true,
});
const cityMarkers = L.layerGroup().addTo(map);
const cityMarkerByKey = new Map();

const rangeState = {
  key: "6m",
  start: "",
  end: "",
};
let activeCityKey = null;

function rangeQuery() {
  const params = new URLSearchParams({ range: rangeState.key });
  if (rangeState.key === "custom") {
    params.set("start", rangeState.start);
    params.set("end", rangeState.end);
  }
  return params.toString();
}

L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png", {
  maxZoom: 12,
  attribution: "&copy; OpenStreetMap contributors &copy; CARTO",
}).addTo(map);

// ---- Helpers ---------------------------------------------------------------
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function rankListItem(language, count, percent, maxPercent) {
  const li = document.createElement("li");
  const row = el("div", "bar-row");
  row.appendChild(el("span", "lang-name", language));
  const bar = el("div", "bar");
  const fill = document.createElement("span");
  const width = maxPercent > 0 ? (percent / maxPercent) * 100 : 0;
  fill.style.width = `${width}%`;
  bar.appendChild(fill);
  row.appendChild(bar);
  row.appendChild(el("span", "count", `${count} (${percent}%)`));
  li.appendChild(row);
  return li;
}

function renderLanguageList(container, languages) {
  container.innerHTML = "";
  if (!languages.length) {
    container.appendChild(el("li", null, "No languages detected."));
    return;
  }
  const maxPercent = Math.max(...languages.map((l) => l.percent));
  for (const lang of languages) {
    container.appendChild(rankListItem(lang.language, lang.count, lang.percent, maxPercent));
  }
}

function renderToolList(container, tools) {
  container.innerHTML = "";
  if (!tools.length) {
    container.appendChild(el("li", null, "No tools/frameworks detected."));
    return;
  }
  const maxPercent = Math.max(...tools.map((t) => t.percent));
  for (const tool of tools) {
    container.appendChild(rankListItem(tool.tool, tool.count, tool.percent, maxPercent));
  }
}

function formatDate(iso) {
  if (!iso) return "unknown date";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString();
}

function formatCalendarDate(iso) {
  if (!iso) return "";
  const date = new Date(`${iso.slice(0, 10)}T00:00:00`);
  return Number.isNaN(date.getTime())
    ? iso
    : date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function formatMonthLabel(month) {
  if (!month) return "";
  const [year, mon] = month.split("-").map(Number);
  const date = new Date(Date.UTC(year, (mon || 1) - 1, 1));
  return Number.isNaN(date.getTime())
    ? month
    : date.toLocaleDateString(undefined, { month: "short", year: "numeric", timeZone: "UTC" });
}

function renderPostingActivity(chartId, startId, endId, activity) {
  const chart = document.getElementById(chartId);
  const recent = (activity || []).slice(-30);
  chart.innerHTML = "";

  if (!recent.length) {
    chart.textContent = "No posting dates available.";
    chart.classList.add("activity-chart-empty");
    return;
  }

  chart.classList.remove("activity-chart-empty");
  chart.style.gridTemplateColumns = `repeat(${recent.length}, minmax(2px, 1fr))`;
  const maxCount = Math.max(...recent.map((day) => day.count), 1);
  const total = recent.reduce((sum, day) => sum + day.count, 0);
  chart.setAttribute(
    "aria-label",
    `${total} postings across ${recent.length} selected ${recent.length === 1 ? "day" : "days"}. Hover over a bar for its date and count.`
  );

  for (const day of recent) {
    const bar = el("span", "activity-bar");
    const height = day.count ? Math.max((day.count / maxCount) * 100, 8) : 2;
    bar.style.height = `${height}%`;
    bar.title = `${formatCalendarDate(day.date)}: ${day.count} posting${day.count === 1 ? "" : "s"}`;
    bar.setAttribute("aria-label", bar.title);
    chart.appendChild(bar);
  }

  document.getElementById(startId).textContent = formatCalendarDate(recent[0].date);
  document.getElementById(endId).textContent = formatCalendarDate(recent.at(-1).date);
}

function renderWeekdayAverages(chartId, activity) {
  const chart = document.getElementById(chartId);
  const weekdayNames = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
  const totals = Array(7).fill(0);
  const samples = Array(7).fill(0);
  chart.innerHTML = "";

  for (const day of activity || []) {
    const parsed = new Date(`${day.date}T00:00:00Z`);
    if (Number.isNaN(parsed.getTime())) continue;
    const weekday = (parsed.getUTCDay() + 6) % 7;
    totals[weekday] += day.count;
    samples[weekday] += 1;
  }

  const averages = totals.map((total, index) =>
    samples[index] ? total / samples[index] : 0
  );
  const maxAverage = Math.max(...averages, 1);
  chart.style.gridTemplateColumns = "repeat(7, minmax(12px, 1fr))";
  chart.setAttribute(
    "aria-label",
    `Average postings by weekday: ${averages
      .map((average, index) => `${weekdayNames[index]} ${average.toFixed(1)}`)
      .join(", ")}.`
  );

  averages.forEach((average, index) => {
    const bar = el("span", "activity-bar weekday-average-bar");
    const height = average ? Math.max((average / maxAverage) * 100, 8) : 2;
    bar.style.height = `${height}%`;
    bar.title = `${weekdayNames[index]}: ${average.toFixed(1)} average postings per day`;
    bar.setAttribute("aria-label", bar.title);
    chart.appendChild(bar);
  });
}

function renderMonthlyActivity(chartId, startId, endId, activity) {
  const chart = document.getElementById(chartId);
  const months = (activity || []).slice(-24);
  chart.innerHTML = "";

  if (!months.length) {
    chart.textContent = "No posting dates available.";
    chart.classList.add("activity-chart-empty");
    return;
  }

  chart.classList.remove("activity-chart-empty");
  chart.style.gridTemplateColumns = `repeat(${months.length}, minmax(6px, 1fr))`;
  const maxCount = Math.max(...months.map((month) => month.count), 1);
  const total = months.reduce((sum, month) => sum + month.count, 0);
  chart.setAttribute(
    "aria-label",
    `${total} postings across ${months.length} selected ${months.length === 1 ? "month" : "months"}. Hover over a bar for its month and count.`
  );

  for (const month of months) {
    const bar = el("span", "activity-bar");
    const height = month.count ? Math.max((month.count / maxCount) * 100, 8) : 2;
    bar.style.height = `${height}%`;
    bar.title = `${formatMonthLabel(month.month)}: ${month.count} posting${month.count === 1 ? "" : "s"}`;
    bar.setAttribute("aria-label", bar.title);
    chart.appendChild(bar);
  }

  document.getElementById(startId).textContent = formatMonthLabel(months[0].month);
  document.getElementById(endId).textContent = formatMonthLabel(months.at(-1).month);
}


// ---- Overall stats ---------------------------------------------------------
async function loadStats() {
  const query = rangeQuery();
  const res = await fetch(`/api/stats?${query}`);
  const data = await res.json();
  if (!res.ok || query !== rangeQuery()) return;

  const summary = document.getElementById("stats-summary");
  const bySource = (data.summary_by_source || [])
    .map((s) => `<strong>${s.postings}</strong> postings across <strong>${s.cities}</strong> ${s.cities === 1 ? "city" : "cities"} (${s.source})`)
    .join("<br>");
  summary.innerHTML = bySource || "No data yet.";

  renderPostingActivity(
    "overall-activity",
    "overall-activity-start",
    "overall-activity-end",
    data.posting_activity_daily
  );
  renderWeekdayAverages("overall-weekday-average", data.posting_activity_daily);
  renderMonthlyActivity(
    "overall-activity-monthly",
    "overall-activity-monthly-start",
    "overall-activity-monthly-end",
    data.posting_activity_monthly
  );

  renderLanguageList(document.getElementById("overall-languages"), data.top_languages_overall || []);
  renderToolList(document.getElementById("overall-tools"), data.top_tools_overall || []);

  const titles = document.getElementById("overall-titles");
  titles.innerHTML = "";
  for (const t of data.top_titles_overall || []) {
    const li = el("li");
    li.appendChild(el("span", null, `${t.title} `));
    li.appendChild(el("span", "muted", `(${t.count})`));
    titles.appendChild(li);
  }

  const titlesNote = document.getElementById("overall-titles-note");
  if (titlesNote) {
    const unique = data.unique_titles ?? 0;
    const total = data.total_postings ?? 0;
    titlesNote.textContent =
      `${unique.toLocaleString()} distinct titles across ${total.toLocaleString()} postings — ` +
      `titles rarely repeat exactly (e.g. "Senior Backend Engineer" vs. "Senior Software Engineer, Backend"), ` +
      `so this top list won't add up to the total.`;
  }
}

// ---- City pins -------------------------------------------------------------
// Each city's name sits to the RIGHT of its pin, vertically level with the base
// (tip) of the pin. Offsets are tuned for the default Leaflet marker (tip at the
// pin's bottom; tooltipAnchor [16, -28]) so the label's vertical center lines up
// with the pin's baseline. A few packed cities flip to the LEFT to avoid overlap.
const LABEL_OFFSETS = {
  right: [6, 28],
  left: [-26, 28],
};
const DEFAULT_LABEL_DIR = "right";

// Cities whose right-side label would collide with a neighbor are flipped left.
const LABEL_DIR_OVERRIDES = {
  austin: "left",
  memphis: "left",
  "salt lake city": "left",
  "san francisco": "left",
  "san diego": "left",
};

const LABEL_OFFSET_OVERRIDES = {
  boston: [6, 26],
  memphis: [-22, 26],
  "new york": [6, 24],
  philadelphia: [6, 35],
  "washington dc": [1, 42],
};

// Display-only adjustments for crowded areas. The stored city coordinates stay
// geographically accurate; Phoenix is shifted south enough to clear Los Angeles
// at the map's initial zoom.
const DISPLAY_COORD_OVERRIDES = {
  phoenix: [31.4, -112.074],
};

async function loadCities() {
  const query = rangeQuery();
  const res = await fetch(`/api/cities?${query}`);
  const cities = await res.json();
  if (!res.ok || query !== rangeQuery()) return;

  for (const c of cities) {
    if (c.lat == null || c.lng == null) continue;
    const tooltipContent = `${c.label} <span class="pin-count">${c.total_matched}</span>`;
    const existingMarker = cityMarkerByKey.get(c.city);
    if (existingMarker) {
      existingMarker.setTooltipContent(tooltipContent);
      continue;
    }
    const position = DISPLAY_COORD_OVERRIDES[c.city] || [c.lat, c.lng];
    const marker = L.marker(position).addTo(cityMarkers);
    const dir = LABEL_DIR_OVERRIDES[c.city] || DEFAULT_LABEL_DIR;
    marker.bindTooltip(
      tooltipContent,
      {
        permanent: true,
        interactive: true,
        direction: dir,
        className: "city-pin-label",
        offset: LABEL_OFFSET_OVERRIDES[c.city] || LABEL_OFFSETS[dir],
      }
    );
    const openCity = () => openCityModal(c.city);
    marker.on("click", openCity);
    marker.getTooltip().on("click", openCity);
    cityMarkerByKey.set(c.city, marker);
  }
}

// ---- City modal ------------------------------------------------------------
const modal = document.getElementById("city-modal");

async function openCityModal(cityKey) {
  activeCityKey = cityKey;
  const query = rangeQuery();
  const res = await fetch(`/api/city/${encodeURIComponent(cityKey)}?${query}`);
  if (!res.ok) return;
  const data = await res.json();
  if (query !== rangeQuery() || activeCityKey !== cityKey) return;

  document.getElementById("modal-title").textContent = data.label;
  document.getElementById("modal-meta").textContent =
    `${data.total_matched} matched postings in window · last updated ${formatDate(data.updated_at)}`;

  renderPostingActivity(
    "city-activity",
    "city-activity-start",
    "city-activity-end",
    data.posting_activity_daily
  );
  renderWeekdayAverages("city-weekday-average", data.posting_activity_daily);
  renderMonthlyActivity(
    "city-activity-monthly",
    "city-activity-monthly-start",
    "city-activity-monthly-end",
    data.posting_activity_monthly
  );

  renderLanguageList(document.getElementById("modal-languages"), data.languages || []);
  renderToolList(document.getElementById("modal-tools"), data.tools || []);

  const count = document.getElementById("modal-postings-count");
  count.textContent = `(${data.postings.length})`;
  document.getElementById("modal-postings-title").textContent = "Job postings";

  const download = document.getElementById("download-postings");
  download.href = `/api/city/${encodeURIComponent(data.city)}/postings.csv?${rangeQuery()}`;
  download.download = `${data.city.replaceAll(" ", "-")}-job-postings-${data.range_key}.csv`;

  const list = document.getElementById("modal-postings");
  list.innerHTML = "";
  for (const p of data.postings) {
    const li = el("li");
    const title = el("div", "p-title");
    if (p.url) {
      const a = el("a", null, p.title);
      a.href = p.url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      title.appendChild(a);
    } else {
      title.textContent = p.title;
    }
    li.appendChild(title);

    li.appendChild(
      el("div", "p-meta", `${p.company} · ${p.location || "location n/a"} · posted ${formatDate(p.posted_at)}`)
    );

    if (p.matched_languages && p.matched_languages.length) {
      const langs = el("div", "p-langs");
      for (const lang of p.matched_languages) {
        langs.appendChild(el("span", "tag", lang));
      }
      li.appendChild(langs);
    }
    list.appendChild(li);
  }

  modal.classList.remove("hidden");
}

function closeModal() {
  modal.classList.add("hidden");
  activeCityKey = null;
}

document.getElementById("modal-close").addEventListener("click", closeModal);
modal.addEventListener("click", (e) => {
  if (e.target === modal) closeModal();
});

const collectionMethodOpen = document.getElementById("collection-method-open");
const collectionMethodModal = document.getElementById("collection-method-modal");
const collectionMethodClose = document.getElementById("collection-method-close");

function openCollectionMethod() {
  collectionMethodModal.classList.remove("hidden");
  collectionMethodClose.focus();
}

function closeCollectionMethod() {
  collectionMethodModal.classList.add("hidden");
  collectionMethodOpen.focus();
}

collectionMethodOpen.addEventListener("click", openCollectionMethod);
collectionMethodClose.addEventListener("click", closeCollectionMethod);
collectionMethodModal.addEventListener("click", (event) => {
  if (event.target === collectionMethodModal) closeCollectionMethod();
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!collectionMethodModal.classList.contains("hidden")) {
    closeCollectionMethod();
  } else if (!modal.classList.contains("hidden")) {
    closeModal();
  }
});

async function applyDateRange(key, start = "", end = "") {
  rangeState.key = key;
  rangeState.start = start;
  rangeState.end = end;
  document.querySelectorAll(".range-option").forEach((button) => {
    const active = button.dataset.range === key;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  await Promise.all([loadStats(), loadCities()]);
  if (activeCityKey) await openCityModal(activeCityKey);
}

const customRange = document.getElementById("custom-range");
const customStart = document.getElementById("custom-start");
const customEnd = document.getElementById("custom-end");
const today = new Date().toISOString().slice(0, 10);
customStart.max = today;
customEnd.max = today;
customEnd.value = today;

document.querySelectorAll(".range-option").forEach((button) => {
  button.addEventListener("click", () => {
    const key = button.dataset.range;
    const isCustom = key === "custom";
    customRange.classList.toggle("hidden", !isCustom);
    if (isCustom) {
      customStart.focus();
      return;
    }
    applyDateRange(key);
  });
});

customRange.addEventListener("submit", (event) => {
  event.preventDefault();
  customStart.setCustomValidity(
    customStart.value > customEnd.value ? "Start date must not be after end date." : ""
  );
  if (!customRange.reportValidity()) return;
  applyDateRange("custom", customStart.value, customEnd.value);
});

// ---- Init ------------------------------------------------------------------
loadStats();
loadCities();
