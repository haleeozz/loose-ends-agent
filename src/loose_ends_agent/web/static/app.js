const state = { run: null, filter: "all", pollTimer: null };

const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;").replaceAll("'", "&#039;");

async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error?.message || "Request failed");
  return payload;
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.hidden = false;
  window.setTimeout(() => { toast.hidden = true; }, 4500);
}

function category(item) {
  if (item.state === "human_decision_required") return "paused";
  if (item.state === "ready_action") return "ready";
  if (item.state === "completed") return "completed";
  return "unresolved";
}

function stateLabel(item) {
  const value = category(item);
  return value === "paused" ? "Decision required" : value === "ready" ? "Ready action" : value === "completed" ? "Completed" : "Unresolved";
}

function renderStatus(run) {
  const band = $("#status-band");
  band.className = `status-band ${run?.status || ""}`;
  $("#status-title").textContent = run?.status_label || "No active run";
  const details = {
    running: "The local Strands agent is processing source evidence.",
    paused: "The agent has stopped at a real Strands interrupt. Reconciliation is not complete.",
    completed: "Every observed obligation has reached a terminal action state.",
    incomplete: "The model stopped while reconciliation requirements remained.",
    failed: run?.error || "The run could not continue.",
    historical: "This report is read-only. Its live Strands session is no longer available.",
  };
  $("#status-detail").textContent = details[run?.status] || "Start a local run to inspect obligations.";
  const continuity = run?.continuity;
  $("#run-meta").innerHTML = run ? [
    `<div>${escapeHtml(run.model?.id)}</div>`,
    continuity ? `<div>Invocation ${continuity.invocation_count} · ${continuity.resume_count} resumes</div>` : "",
    run.stop_reason ? `<div>Stop reason: ${escapeHtml(run.stop_reason)}</div>` : "",
  ].join("") : "";
  $("#start-run").disabled = Boolean(run && ["running", "paused"].includes(run.status));
}

function renderMetrics(run) {
  const summary = run?.summary || {};
  $("#metric-evidence").textContent = summary.observed_evidence || 0;
  $("#metric-loose-ends").textContent = summary.loose_ends || 0;
  $("#metric-ready").textContent = summary.ready_actions || 0;
  $("#metric-decisions").textContent = summary.human_decisions_required || 0;
  const inspected = run?.reconciliation?.inspected_source_types?.length || 0;
  const expected = run?.reconciliation?.expected_source_types?.length || 0;
  $("#metric-coverage").textContent = `${inspected} / ${expected}`;
}

function renderWarning(run) {
  const notice = $("#reconciliation-warning");
  const unassigned = run?.unassigned_evidence?.length || 0;
  const issues = run?.reconciliation?.issues || [];
  if (!run || run.reconciliation?.complete) {
    notice.hidden = true;
    return;
  }
  const fragments = [];
  if (run.status === "paused") fragments.push("This run paused before full reconciliation.");
  if (unassigned) fragments.push(`${unassigned} evidence record${unassigned === 1 ? " is" : "s are"} not yet assigned to a loose end.`);
  if (run.status === "historical") fragments.push("Start a new run to create a resumable session.");
  if (!fragments.length && issues.length) fragments.push(issues[0]);
  notice.textContent = fragments.join(" ");
  notice.hidden = !notice.textContent;
}

function evidenceCard(record) {
  return `<article class="evidence-record">
    <div class="evidence-source">
      <span class="source-type">${escapeHtml(record.source_type)}</span>
      <strong>${escapeHtml(record.source_path)}${record.line_number ? `:${record.line_number}` : ""}</strong>
      <span class="date-chip">${escapeHtml(record.due_date || "No date")}</span>
    </div>
    <p class="evidence-copy">${escapeHtml(record.evidence)}</p>
    <div class="evidence-confidence">${Math.round((record.confidence || 0) * 100)}% extraction confidence</div>
  </article>`;
}

function renderDecision(run) {
  const section = $("#decision-section");
  const interrupt = run?.interrupts?.[0];
  if (!interrupt || run.status !== "paused") {
    section.hidden = true;
    return;
  }
  const reason = interrupt.reason || {};
  section.hidden = false;
  $("#decision-question").textContent = reason.question || "Human decision required";
  $("#decision-rationale").textContent = reason.rationale || "The agent cannot safely continue without judgment.";
  $("#decision-options").innerHTML = (reason.options || []).map((option, index) => `
    <label class="option-label">
      <input type="radio" name="decision" value="${escapeHtml(option)}" ${index === 0 ? "checked" : ""}>
      ${escapeHtml(option)}
    </label>`).join("");
  $("#decision-evidence").innerHTML = (reason.provenance || []).map(evidenceCard).join("");
  $("#decision-form").dataset.interruptId = interrupt.id;
  $("#resume-run").disabled = !run.resumable;
}

function provenanceRow(record) {
  return `<div class="provenance-row">
    <div class="provenance-header">
      <span class="source-type">${escapeHtml(record.source_type)}</span>
      <strong>${escapeHtml(record.source_path)}${record.line_number ? `:${record.line_number}` : ""}</strong>
      ${record.due_date ? `<span class="date-chip">${escapeHtml(record.due_date)}</span>` : ""}
    </div>
    <p class="provenance-copy">${escapeHtml(record.evidence)}</p>
  </div>`;
}

function looseEndRow(item) {
  const itemCategory = category(item);
  const date = item.due_date || item.provenance?.map((entry) => entry.due_date).filter(Boolean)[0];
  return `<details class="loose-end" data-category="${itemCategory}">
    <summary>
      <span class="loose-end-title">${escapeHtml(item.title)}<span class="loose-end-subtitle">${escapeHtml(item.id)}</span></span>
      <span class="due-date">${escapeHtml(date || "No deadline")}</span>
      <span class="state-tag state-${itemCategory}">${stateLabel(item)}</span>
    </summary>
    <div class="loose-end-detail">
      <div class="decision-summary">
        <h3>${itemCategory === "ready" ? "Next action" : "Decision"}</h3>
        <p class="action-text">${escapeHtml(item.next_action || item.human_decision?.question || "No action recorded yet.")}</p>
        <p class="muted">${escapeHtml(item.decision_rationale || item.grouping_rationale)}</p>
      </div>
      <div>
        <h3>Provenance</h3>
        <div class="provenance-list">${(item.provenance || []).map(provenanceRow).join("")}</div>
      </div>
    </div>
  </details>`;
}

function renderLooseEnds(run) {
  const items = run?.loose_ends || [];
  const counts = { all: items.length, paused: 0, unresolved: 0, ready: 0, completed: 0 };
  items.forEach((item) => { counts[category(item)] += 1; });
  Object.entries(counts).forEach(([name, count]) => { $(`#count-${name}`).textContent = count; });
  const visible = state.filter === "all" ? items : items.filter((item) => category(item) === state.filter);
  $("#loose-ends-list").innerHTML = visible.length ? visible.map(looseEndRow).join("") : `<div class="empty-state">No ${state.filter === "all" ? "loose ends" : state.filter + " items"} in this run.</div>`;
}

function renderActivity(run) {
  const events = run?.events || [];
  $("#activity-count").textContent = `${events.length} events`;
  $("#activity-list").innerHTML = events.slice(-14).reverse().map((event) => `
    <li class="activity-item">
      <span class="activity-phase">${escapeHtml(event.phase)}</span>
      <span class="activity-detail">${escapeHtml(event.detail)}</span>
      <time class="activity-time">${new Date(event.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</time>
    </li>`).join("") || `<li class="empty-state">No agent activity yet.</li>`;
}

function render(run) {
  state.run = run;
  renderStatus(run);
  renderMetrics(run);
  renderWarning(run);
  renderDecision(run);
  renderLooseEnds(run);
  renderActivity(run);
  schedulePoll();
}

function schedulePoll() {
  window.clearTimeout(state.pollTimer);
  if (state.run?.status === "running") {
    state.pollTimer = window.setTimeout(loadRun, 1500);
  }
}

async function loadRun() {
  try {
    const payload = state.run?.id && state.run.id !== "historical-latest"
      ? await request(`/api/runs/${state.run.id}`)
      : await request("/api/runs/current");
    render(payload.run);
  } catch (error) {
    showToast(error.message);
    schedulePoll();
  }
}

$("#start-run").addEventListener("click", async () => {
  try {
    $("#start-run").disabled = true;
    const payload = await request("/api/runs", { method: "POST", body: JSON.stringify({ as_of: "2026-09-12" }) });
    state.filter = "all";
    render(payload.run);
  } catch (error) {
    showToast(error.message);
    $("#start-run").disabled = false;
  }
});

$("#decision-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const selected = new FormData(event.currentTarget).get("decision");
  if (!selected) return showToast("Select a decision before resuming.");
  try {
    $("#resume-run").disabled = true;
    const payload = await request(`/api/runs/${state.run.id}/resume`, {
      method: "POST",
      body: JSON.stringify({ responses: [{ interrupt_id: event.currentTarget.dataset.interruptId, response: selected }] }),
    });
    render(payload.run);
  } catch (error) {
    showToast(error.message);
    $("#resume-run").disabled = false;
  }
});

$("#tabs").addEventListener("click", (event) => {
  const tab = event.target.closest(".tab");
  if (!tab) return;
  state.filter = tab.dataset.filter;
  document.querySelectorAll(".tab").forEach((item) => item.classList.toggle("active", item === tab));
  renderLooseEnds(state.run);
});

loadRun();
