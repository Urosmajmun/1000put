"use strict";

// Vanilla JS front-end for the Stake promotion knowledge base.

const form = document.getElementById("search-form");
const queryInput = document.getElementById("query");
const categorySelect = document.getElementById("category");
const sourceSelect = document.getElementById("source");
const resultsEl = document.getElementById("results");
const emptyEl = document.getElementById("empty");
const statusEl = document.getElementById("status");
const refreshBtn = document.getElementById("refresh-btn");
const totalCountEl = document.getElementById("total-count");
const statusButtons = Array.from(document.querySelectorAll(".status-btn"));

const categoriesByGroup = window.CATEGORIES_BY_GROUP || { site: [], forum: [] };

let refreshPollTimer = null;
let currentStatus = "active"; // default view: active promotions

/** Reflect the selected status on the segmented toggle. */
function updateStatusButtons() {
  for (const b of statusButtons) {
    const selected = b.dataset.status === currentStatus;
    b.classList.toggle("bg-stake-green", selected);
    b.classList.toggle("text-stake-bg", selected);
    b.classList.toggle("text-stake-muted", !selected);
    b.classList.toggle("hover:text-white", !selected);
  }
}

/** Fill the category dropdown with the categories of the selected group.
 *  With no group selected, show the union of every group's categories. */
function populateCategories() {
  const group = sourceSelect.value;
  let cats;
  if (group && categoriesByGroup[group]) {
    cats = categoriesByGroup[group].slice();
  } else {
    cats = Array.from(
      new Set([...(categoriesByGroup.site || []), ...(categoriesByGroup.forum || [])])
    ).sort();
  }

  categorySelect.replaceChildren();
  const all = document.createElement("option");
  all.value = "";
  all.textContent = "All categories";
  categorySelect.appendChild(all);
  for (const c of cats) {
    const opt = document.createElement("option");
    opt.value = c;
    opt.textContent = c.charAt(0).toUpperCase() + c.slice(1);
    categorySelect.appendChild(opt);
  }
}

/** Build a single result card using textContent to avoid any HTML injection. */
function renderResult(item) {
  const card = document.createElement("a");
  card.href = `/promotion/${item.id}`;
  card.className =
    "block bg-stake-panel rounded-xl border border-stake-border p-4 transition-colors " +
    "hover:bg-stake-panel2 hover:border-stake-blue/60";

  const header = document.createElement("div");
  header.className = "flex items-start justify-between gap-3";

  const title = document.createElement("h3");
  title.className = "font-semibold text-white";
  title.textContent = item.title;

  const badges = document.createElement("div");
  badges.className = "flex items-center gap-2 shrink-0";

  // Group badge (Site / Forum) so agents can tell the two sources apart.
  if (item.source) {
    const group = document.createElement("span");
    const isForum = item.source === "forum";
    group.className =
      "text-xs font-bold uppercase tracking-wide px-2 py-1 rounded " +
      (isForum ? "bg-stake-green/15 text-stake-green" : "bg-stake-blue/15 text-stake-blue");
    group.textContent = isForum ? "Forum" : "Site";
    badges.appendChild(group);
  }

  const badge = document.createElement("span");
  badge.className =
    "bg-stake-panel2 text-stake-muted text-xs font-semibold uppercase tracking-wide px-2 py-1 rounded border border-stake-border";
  badge.textContent = item.category;
  badges.appendChild(badge);

  // Finished / active status badge, shown next to every promotion.
  const statusBadge = document.createElement("span");
  statusBadge.className =
    "text-xs font-bold uppercase tracking-wide px-2 py-1 rounded " +
    (item.finished ? "bg-red-500/15 text-red-400" : "bg-stake-green/15 text-stake-green");
  statusBadge.textContent = item.finished ? "Finished" : "Active";
  badges.appendChild(statusBadge);

  header.appendChild(title);
  header.appendChild(badges);
  card.appendChild(header);

  // Promotion duration (date range), visible before opening the promotion.
  if (item.duration) {
    const duration = document.createElement("p");
    duration.className = "text-xs font-semibold text-stake-green mt-1.5";
    duration.textContent = "🗓 " + item.duration;
    card.appendChild(duration);
  }

  const preview = document.createElement("p");
  preview.className = "text-sm text-stake-muted mt-2 leading-relaxed";
  preview.textContent = item.preview || "No preview available.";
  card.appendChild(preview);

  return card;
}

/** Run a search against the API and render the results. */
async function runSearch() {
  const params = new URLSearchParams();
  params.set("q", queryInput.value.trim());
  if (categorySelect.value) {
    params.set("category", categorySelect.value);
  }
  if (sourceSelect.value) {
    params.set("source", sourceSelect.value);
  }
  if (currentStatus) {
    params.set("status", currentStatus);
  }

  statusEl.textContent = "Searching…";
  try {
    const resp = await fetch(`/api/search?${params.toString()}`);
    if (!resp.ok) {
      throw new Error(`Search failed (HTTP ${resp.status})`);
    }
    const data = await resp.json();
    resultsEl.replaceChildren();

    if (!data.results.length) {
      emptyEl.classList.remove("hidden");
      statusEl.textContent = "";
      return;
    }
    emptyEl.classList.add("hidden");
    for (const item of data.results) {
      resultsEl.appendChild(renderResult(item));
    }
    statusEl.textContent = `${data.count} result${data.count === 1 ? "" : "s"}.`;
  } catch (err) {
    statusEl.textContent = err.message;
  }
}

/** Poll the refresh status endpoint until the background scrape finishes. */
async function pollRefreshStatus() {
  try {
    const resp = await fetch("/api/refresh/status");
    const state = await resp.json();
    totalCountEl.textContent = state.total_in_db;

    if (state.running) {
      statusEl.textContent = "Refreshing promotions from the Stake site and forum… this can take a minute.";
      return;
    }

    // Finished — stop polling and report the outcome.
    clearInterval(refreshPollTimer);
    refreshPollTimer = null;
    refreshBtn.disabled = false;

    if (state.last_error) {
      statusEl.textContent = `Refresh failed: ${state.last_error}`;
    } else if (state.last_summary) {
      const s = state.last_summary;
      statusEl.textContent =
        `Refresh complete: ${s.inserted} new, ${s.updated} updated, ` +
        `${s.failed} failed (${s.total_in_db} total).`;
      runSearch();
    } else {
      statusEl.textContent = "Refresh complete.";
      runSearch();
    }
  } catch (err) {
    statusEl.textContent = `Could not read refresh status: ${err.message}`;
  }
}

/** Kick off a background refresh of the data. */
async function triggerRefresh() {
  refreshBtn.disabled = true;
  statusEl.textContent = "Starting refresh…";
  try {
    const resp = await fetch("/api/refresh", { method: "POST" });
    if (resp.status === 409) {
      statusEl.textContent = "A refresh is already running.";
    } else if (!resp.ok) {
      throw new Error(`HTTP ${resp.status}`);
    }
    if (!refreshPollTimer) {
      refreshPollTimer = setInterval(pollRefreshStatus, 2000);
    }
    pollRefreshStatus();
  } catch (err) {
    refreshBtn.disabled = false;
    statusEl.textContent = `Could not start refresh: ${err.message}`;
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  runSearch();
});

categorySelect.addEventListener("change", runSearch);
// Changing the group rebuilds the category list (only that group's categories),
// resets the category choice, then searches.
sourceSelect.addEventListener("change", () => {
  populateCategories();
  runSearch();
});
refreshBtn.addEventListener("click", triggerRefresh);

// Status toggle: Active / Finished / All.
for (const b of statusButtons) {
  b.addEventListener("click", () => {
    currentStatus = b.dataset.status;
    updateStatusButtons();
    runSearch();
  });
}

// Build the initial category list, set the default status, and show results.
populateCategories();
updateStatusButtons();
runSearch();
