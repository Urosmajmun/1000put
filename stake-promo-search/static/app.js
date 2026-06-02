"use strict";

// Vanilla JS front-end for the Stake promotion knowledge base.

const form = document.getElementById("search-form");
const queryInput = document.getElementById("query");
const categorySelect = document.getElementById("category");
const resultsEl = document.getElementById("results");
const emptyEl = document.getElementById("empty");
const statusEl = document.getElementById("status");
const refreshBtn = document.getElementById("refresh-btn");
const totalCountEl = document.getElementById("total-count");

let refreshPollTimer = null;

/** Build a single result card using textContent to avoid any HTML injection. */
function renderResult(item) {
  const card = document.createElement("a");
  card.href = `/promotion/${item.id}`;
  card.className =
    "block bg-white rounded-xl shadow-sm p-4 hover:shadow-md transition-shadow border border-transparent hover:border-indigo-200";

  const header = document.createElement("div");
  header.className = "flex items-center justify-between gap-3";

  const title = document.createElement("h3");
  title.className = "font-semibold text-slate-900";
  title.textContent = item.title;

  const badge = document.createElement("span");
  badge.className =
    "shrink-0 bg-indigo-100 text-indigo-700 text-xs font-semibold uppercase tracking-wide px-2 py-1 rounded";
  badge.textContent = item.category;

  header.appendChild(title);
  header.appendChild(badge);

  const preview = document.createElement("p");
  preview.className = "text-sm text-slate-500 mt-2";
  preview.textContent = item.preview || "No preview available.";

  card.appendChild(header);
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
      statusEl.textContent = "Refreshing promotions from stake.com… this can take a minute.";
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
refreshBtn.addEventListener("click", triggerRefresh);

// Show the full catalogue on first load.
runSearch();
