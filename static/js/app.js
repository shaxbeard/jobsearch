"use strict";

// ---- Map setup -------------------------------------------------------------
// Centered on the continental US at a zoom that still shows Toronto up top.
const map = L.map("map", {
  center: [41.0, -96.0],
  zoom: 4,
  scrollWheelZoom: true,
});

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

function formatDate(iso) {
  if (!iso) return "unknown date";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString();
}

// ---- Overall stats ---------------------------------------------------------
async function loadStats() {
  const res = await fetch("/api/stats");
  const data = await res.json();

  const summary = document.getElementById("stats-summary");
  const bySource = (data.summary_by_source || [])
    .map((s) => `<strong>${s.postings}</strong> postings across <strong>${s.cities}</strong> cities (${s.source})`)
    .join("<br>");
  summary.innerHTML = bySource || "No data yet.";

  renderLanguageList(document.getElementById("overall-languages"), data.top_languages_overall || []);

  const titles = document.getElementById("overall-titles");
  titles.innerHTML = "";
  for (const t of data.top_titles_overall || []) {
    const li = el("li");
    li.appendChild(el("span", null, `${t.title} `));
    li.appendChild(el("span", "muted", `(${t.count})`));
    titles.appendChild(li);
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
  "san francisco": "left",
  "san diego": "left",
};

// Display-only adjustments for crowded areas. The stored city coordinates stay
// geographically accurate; Phoenix is shifted south enough to clear Los Angeles
// at the map's initial zoom.
const DISPLAY_COORD_OVERRIDES = {
  phoenix: [31.4, -112.074],
};

async function loadCities() {
  const res = await fetch("/api/cities");
  const cities = await res.json();

  for (const c of cities) {
    if (c.lat == null || c.lng == null) continue;
    const position = DISPLAY_COORD_OVERRIDES[c.city] || [c.lat, c.lng];
    const marker = L.marker(position).addTo(map);
    const dir = LABEL_DIR_OVERRIDES[c.city] || DEFAULT_LABEL_DIR;
    marker.bindTooltip(
      `${c.label} <span class="pin-count">${c.total_matched}</span>`,
      {
        permanent: true,
        interactive: true,
        direction: dir,
        className: "city-pin-label",
        offset: LABEL_OFFSETS[dir],
      }
    );
    const openCity = () => openCityModal(c.city);
    marker.on("click", openCity);
    marker.getTooltip().on("click", openCity);
  }
}

// ---- City modal ------------------------------------------------------------
const modal = document.getElementById("city-modal");

async function openCityModal(cityKey) {
  const res = await fetch(`/api/city/${encodeURIComponent(cityKey)}`);
  if (!res.ok) return;
  const data = await res.json();

  document.getElementById("modal-title").textContent = data.label;
  document.getElementById("modal-meta").textContent =
    `${data.total_matched} matched postings · last updated ${formatDate(data.updated_at)}`;

  renderLanguageList(document.getElementById("modal-languages"), data.languages || []);

  const count = document.getElementById("modal-postings-count");
  count.textContent = `(${data.postings.length})`;

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
}

document.getElementById("modal-close").addEventListener("click", closeModal);
modal.addEventListener("click", (e) => {
  if (e.target === modal) closeModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeModal();
});

// ---- Init ------------------------------------------------------------------
loadStats();
loadCities();
