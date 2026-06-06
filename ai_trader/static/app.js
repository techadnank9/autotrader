const state = {
  config: null,
  snapshot: null,
  manageResult: null,
  agents: null,
  sia: null,
  activeTab: "portfolio",
  snapshotLoading: false,
  snapshotLoaded: false,
};

const $ = (id) => document.getElementById(id);

function money(value) {
  return `$${Number(value ?? 0).toFixed(2)}`;
}

function setStatus(text) {
  $("status").textContent = text;
}

function setMode(text, muted = false) {
  $("mode").textContent = text;
  $("mode").classList.toggle("muted", muted);
}

function uniqueAgents(payload) {
  const map = new Map();
  const eligible = Array.isArray(payload?.eligible) ? payload.eligible : [];
  eligible.forEach((agent) => {
    if (agent?.version_id) {
      map.set(agent.version_id, agent);
    }
  });
  return Array.from(map.values());
}

function syncPortfolioAgentControls() {
  const payload = state.agents;
  if (!payload) {
    $("portfolio-agent-select").innerHTML = "";
    $("portfolio-activate-agent").disabled = true;
    return;
  }
  const current = payload.current ?? "builtin-default-v1";
  const options = uniqueAgents(payload);
  const selectedValue = $("portfolio-agent-select").value || current;
  $("portfolio-agent-select").innerHTML = options
    .map(
      (agent) => `
        <option value="${agent.version_id}" ${agent.version_id === selectedValue ? "selected" : ""}>
          ${agent.label ?? agent.version_id}
        </option>
      `,
    )
    .join("");
  if (!options.some((agent) => agent.version_id === $("portfolio-agent-select").value)) {
    $("portfolio-agent-select").value = current;
  }
  const selected = options.find((agent) => agent.version_id === $("portfolio-agent-select").value);
  const sourceRun = selected?.source_run ? ` · ${selected.source_run}` : "";
  const metric = selected?.metrics?.top1_accuracy != null ? `Top-1 ${Number(selected.metrics.top1_accuracy).toFixed(2)}` : "";
  $("portfolio-agent-meta").textContent = selected
    ? `${selected.label ?? selected.version_id}${sourceRun}${metric ? ` · ${metric}` : ""}`
    : "Built-in and SIA-generated portfolio agents can both be activated here for live execution.";
  $("portfolio-activate-agent").disabled = !selected || selected.version_id === current;
}

function setActiveTab(tab) {
  state.activeTab = tab;
  document.querySelectorAll("[data-tab]").forEach((button) => {
    const active = button.dataset.tab === tab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  });
  document.querySelectorAll("[data-tab-panel]").forEach((panel) => {
    const active = panel.dataset.tabPanel === tab;
    panel.classList.toggle("active", active);
    panel.hidden = !active;
  });
  if (tab === "portfolio" && !state.snapshotLoading) {
    loadPortfolioSnapshot({ background: true });
  }
}

function renderTrace(targetId, trace = []) {
  const items = Array.isArray(trace) ? trace : [];
  $(targetId).innerHTML =
    items
      .map(
        (item) => `
          <article class="trace-card">
            <p class="trace-stage">${item.stage ?? "trace"}</p>
            <h3 class="trace-title">${item.title ?? "--"}</h3>
            <p class="trace-detail">${item.detail ?? ""}</p>
          </article>
        `,
      )
      .join("") || `<p class="agent-line">No trace recorded yet.</p>`;
}

function renderSnapshot(payload) {
  const snapshot = payload?.account_snapshot ?? payload ?? {};
  const portfolio = snapshot.portfolio ?? {};
  const positions = Array.isArray(snapshot.positions) ? snapshot.positions : [];
  const warnings = Array.isArray(snapshot.warnings) ? snapshot.warnings : [];
  const account = snapshot.agentic_account ?? {};

  state.snapshot = snapshot;

  $("snapshot-total-equity").textContent = money(portfolio.total_value ?? portfolio.equity ?? 0);
  $("snapshot-buying-power").textContent = money(portfolio.buying_power ?? 0);
  $("snapshot-cash").textContent = money(portfolio.cash_available ?? 0);
  $("snapshot-account").textContent = account.account_number_masked || account.account_id || "--";
  $("poster-summary").textContent = positions.length
    ? `Loaded ${positions.length} live position${positions.length === 1 ? "" : "s"} from the latest Robinhood snapshot.`
    : "No live positions were returned in the latest snapshot. The portfolio manager can still return no_trade.";

  $("holdings").innerHTML =
    positions
      .map(
        (position) => `
          <div class="row positions-row">
            <span>${position.symbol ?? "--"}</span>
            <span>${Number(position.quantity ?? 0).toFixed(4)}</span>
            <span>${money(position.market_value ?? 0)}</span>
            <span>${money(position.current_price ?? 0)}</span>
          </div>
        `,
      )
      .join("") || `<p class="empty-state">No positions returned.</p>`;

  $("snapshot-warnings").innerHTML =
    warnings.map((warning) => `<p class="warning-line">${warning}</p>`).join("") ||
    `<p class="agent-line">Snapshot completed without portfolio warnings.</p>`;
}

function renderManageResult(data) {
  state.manageResult = data;
  const plan = data.management_plan ?? {};
  const execution = data.execution ?? {};
  const actions = Array.isArray(plan.actions) ? plan.actions : [];
  const ordered = Array.isArray(plan.ordered_actions) ? plan.ordered_actions : [];
  const risks = Array.isArray(plan.warnings) ? plan.warnings : [];
  const agentMode = data.meta?.use_active_agent ? "Active agent" : "Built-in manager";

  $("latest-decision").textContent = plan.decision ?? "no_trade";
  $("latest-execution").textContent = execution.status ?? "unknown";
  $("latest-risk").textContent = risks[0] ?? "No major execution warning returned.";
  $("latest-summary").textContent = plan.summary ?? "No management summary returned.";

  $("latest-actions").innerHTML =
    actions
      .map(
        (action) => `
          <article class="action-card">
            <div class="action-head">
              <span class="action-type">${action.action ?? "hold"}</span>
              <span class="action-symbol">${action.symbol ?? "--"}</span>
            </div>
            <p class="action-copy">${action.reason ?? "No action rationale returned."}</p>
            <p class="action-meta">
              Current ${money(action.current_dollar ?? action.current_value ?? 0)} · Target ${money(
                action.target_dollar ?? action.target_value ?? 0,
              )} · Order ${money(action.order_dollar ?? action.order_value ?? 0)}
            </p>
          </article>
        `,
      )
      .join("") || `<p class="agent-line">The active portfolio manager returned no actions.</p>`;

  $("manage-output").textContent = JSON.stringify(data, null, 2);
  renderTrace("manage-trace", data.reasoning_trace ?? []);

  if (data.resulting_snapshot) {
    renderSnapshot(data);
  }

  const actionCount = ordered.length;
  setStatus(actionCount ? "Managed" : "No-op");
  setMode(`${agentMode} · ${data.meta?.active_agent ?? "Portfolio pass"}`, false);
}

function renderAgents(payload) {
  state.agents = payload;
  const current = payload.current ?? "--";
  const previous = payload.previous ?? "--";
  $("agent-current").textContent = current;
  $("agent-previous").textContent = previous;
  $("active-agent").textContent = current;
  $("sia-active-agent").textContent = current;
  $("rollback-agent").disabled = !payload.previous;
  syncPortfolioAgentControls();

  const eligible = Array.isArray(payload.eligible) ? payload.eligible : [];
  $("agent-list").innerHTML =
    eligible
      .map(
        (agent) => `
          <article class="agent-card">
            <div class="agent-copy">
              <p class="section-label">${agent.label ?? agent.version_id}</p>
              <p class="agent-line">${agent.version_id}</p>
              <p class="agent-line">Top-1 ${Number(agent.metrics?.top1_accuracy ?? 0).toFixed(2)} · Regret ${Number(agent.metrics?.avg_regret ?? 0).toFixed(2)}</p>
            </div>
            <button class="secondary activate-agent" data-version-id="${agent.version_id}" ${agent.is_active ? "disabled" : ""}>
              ${agent.is_active ? "Active" : "Activate"}
            </button>
          </article>
        `,
      )
      .join("") || `<p class="agent-line">No passed SIA-generated portfolio managers are registered yet.</p>`;
}

function renderSiaStatus(payload) {
  state.sia = payload;
  if (!payload) {
    $("sia-status").textContent = "Unavailable";
    $("sia-run").disabled = true;
    return;
  }
  $("sia-status").textContent = payload.installed ? "Ready" : "Unavailable";
  $("sia-run").disabled = !payload.installed;
}

function renderSiaSummaryCard(summary) {
  if (!summary) {
    $("sia-summary-card").innerHTML = `<p class="agent-line">Run SIA to generate a new portfolio-agent debrief.</p>`;
    return;
  }
  const metrics = summary.metrics ?? {};
  const mistakes = Array.isArray(summary.mistakes) ? summary.mistakes : [];
  const improvements = Array.isArray(summary.improvements) ? summary.improvements : [];
  const badgeClass = summary.status === "completed" ? "sia-summary-badge" : "sia-summary-badge failed";
  const comparison = summary.comparison_run_id
    ? `Compared with run ${summary.comparison_run_id}.`
    : "This is the first comparable run in the current workspace.";
  $("sia-summary-card").innerHTML = `
    <div class="sia-summary-hero">
      <div>
        <p class="sia-summary-title">${summary.headline ?? "SIA run debrief"}</p>
        <p class="sia-summary-copy">${comparison} ${
          summary.version_id ? `Live version: ${summary.version_id}.` : "No live version was registered."
        }</p>
      </div>
      <span class="${badgeClass}">${summary.status ?? "unknown"}</span>
    </div>
    <div class="sia-summary-metrics">
      <article class="sia-metric">
        <p class="sia-metric-label">Score</p>
        <p class="sia-metric-value">${Number(metrics.score ?? 0).toFixed(2)}</p>
      </article>
      <article class="sia-metric">
        <p class="sia-metric-label">Valid Output</p>
        <p class="sia-metric-value">${Number(metrics.valid_output_rate ?? 0).toFixed(2)}</p>
      </article>
      <article class="sia-metric">
        <p class="sia-metric-label">Scored Cases</p>
        <p class="sia-metric-value">${Number(metrics.scored_count ?? 0)}/${Number(metrics.sample_count ?? 0)}</p>
      </article>
      <article class="sia-metric">
        <p class="sia-metric-label">Pending Labels</p>
        <p class="sia-metric-value">${Number(metrics.pending_labels ?? 0)}</p>
      </article>
    </div>
    <div class="sia-summary-columns">
      <section class="sia-summary-panel">
        <p class="section-label">Mistakes Found</p>
        <ul class="sia-summary-list">
          ${mistakes.map((item) => `<li>${item}</li>`).join("")}
        </ul>
      </section>
      <section class="sia-summary-panel">
        <p class="section-label">What Improved</p>
        <ul class="sia-summary-list">
          ${improvements.map((item) => `<li>${item}</li>`).join("")}
        </ul>
      </section>
    </div>
  `;
}

async function loadConfig() {
  const response = await fetch("/api/config");
  const config = await response.json();
  state.config = config;
  setMode(config.bright_data_configured ? "Bright Data ready" : "Demo mode", !config.bright_data_configured);
  renderSiaStatus(config.sia ?? null);
}

async function loadAgents() {
  const response = await fetch("/api/portfolio-agents");
  const data = await response.json();
  renderAgents(data);
}

async function loadSiaStatus() {
  const response = await fetch("/api/sia/status");
  const data = await response.json();
  renderSiaStatus(data);
}

function syncManageButtons() {
  $("refresh-portfolio").disabled = state.snapshotLoading;
  $("manage-portfolio").disabled = state.snapshotLoading || !state.snapshotLoaded;
}

async function loadPortfolioSnapshot({ background = false } = {}) {
  state.snapshotLoading = true;
  syncManageButtons();
  if (!background) {
    setStatus("Loading");
    setMode("Pulling live Robinhood snapshot", true);
  }
  try {
    const response = await fetch("/api/portfolio-snapshot");
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail ?? "Snapshot load failed");
    renderSnapshot(data);
    state.snapshotLoaded = true;
    if (data.meta?.active_agent) {
      $("active-agent").textContent = data.meta.active_agent;
      $("sia-active-agent").textContent = data.meta.active_agent;
    }
    setMode($("use-active-agent").checked ? "Active agent armed" : "Built-in manager armed", true);
    setStatus("Ready");
  } catch (error) {
    state.snapshotLoaded = false;
    $("snapshot-warnings").innerHTML = `<p class="warning-line">${error.message}</p>`;
    setStatus("Error");
    setMode("Snapshot failed", true);
  } finally {
    state.snapshotLoading = false;
    syncManageButtons();
  }
}

async function managePortfolio() {
  $("manage-portfolio").disabled = true;
  $("manage-output").textContent = JSON.stringify({ status: "managing_portfolio" }, null, 2);
  setStatus("Managing");
  setMode("Agent pass in progress", true);
  try {
    const response = await fetch("/api/manage-portfolio", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        use_active_agent: $("use-active-agent").checked,
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail ?? "Portfolio management failed");
    renderManageResult(data);
    await loadAgents();
  } catch (error) {
    $("manage-output").textContent = JSON.stringify({ error: error.message }, null, 2);
    renderTrace("manage-trace", []);
    $("latest-decision").textContent = "error";
    $("latest-execution").textContent = "failed";
    $("latest-summary").textContent = error.message;
    $("latest-risk").textContent = "Autonomous pass failed before a valid decision returned.";
    setStatus("Error");
    setMode("Manage failed", true);
  } finally {
    $("manage-portfolio").disabled = false;
  }
}

function writeAgentOutputError(message, scope = "sia") {
  const outputId = scope === "portfolio" ? "manage-output" : "sia-output";
  $(outputId).textContent = JSON.stringify({ error: message }, null, 2);
}

async function activateAgent(versionId, scope = "sia") {
  const response = await fetch("/api/portfolio-agents/activate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ version_id: versionId }),
  });
  const data = await response.json();
  if (!response.ok) {
    writeAgentOutputError(data.detail ?? "Activation failed", scope);
    return;
  }
  renderAgents(data);
  setStatus("Ready");
  setMode(`Active agent switched to ${data.current}`, true);
  if (state.activeTab === "portfolio") {
    loadPortfolioSnapshot({ background: true });
  }
}

async function rollbackAgent() {
  const response = await fetch("/api/portfolio-agents/rollback", { method: "POST" });
  const data = await response.json();
  if (!response.ok) {
    $("sia-output").textContent = JSON.stringify({ error: data.detail ?? "Rollback failed" }, null, 2);
    return;
  }
  renderAgents(data);
  if (state.activeTab === "portfolio") {
    loadPortfolioSnapshot({ background: true });
  }
}

async function buildReplayDataset() {
  $("sia-build").disabled = true;
  $("sia-output").textContent = JSON.stringify({ status: "building_replay" }, null, 2);
  try {
    const response = await fetch("/api/sia/replay-build", { method: "POST" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail ?? "Replay build failed");
    $("sia-output").textContent = JSON.stringify(data, null, 2);
    renderTrace("sia-trace", data.reasoning_trace ?? []);
  } catch (error) {
    $("sia-output").textContent = JSON.stringify({ error: error.message }, null, 2);
    renderTrace("sia-trace", []);
  } finally {
    $("sia-build").disabled = false;
  }
}

async function runSia() {
  $("sia-run").disabled = true;
  $("sia-output").textContent = JSON.stringify({ status: "running_sia" }, null, 2);
  try {
    const response = await fetch("/api/sia/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        max_generations: Number($("sia-max-gen").value || 1),
        build_replay_first: true,
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail ?? "SIA run failed");
    $("sia-output").textContent = JSON.stringify(data, null, 2);
    renderTrace("sia-trace", data.reasoning_trace ?? []);
    renderSiaSummaryCard(data.summary_card ?? null);
    await loadAgents();
  } catch (error) {
    $("sia-output").textContent = JSON.stringify({ error: error.message }, null, 2);
    renderTrace("sia-trace", []);
    renderSiaSummaryCard(null);
  } finally {
    $("sia-run").disabled = !(state.sia?.installed);
  }
}

document.querySelectorAll("[data-tab]").forEach((button) => {
  button.addEventListener("click", () => setActiveTab(button.dataset.tab));
});

$("manage-portfolio").addEventListener("click", managePortfolio);
$("refresh-portfolio").addEventListener("click", loadPortfolioSnapshot);
$("use-active-agent").addEventListener("change", () => {
  setMode($("use-active-agent").checked ? "Active agent armed" : "Built-in manager armed", true);
});
$("portfolio-agent-select").addEventListener("change", syncPortfolioAgentControls);
$("portfolio-activate-agent").addEventListener("click", () => {
  activateAgent($("portfolio-agent-select").value, "portfolio");
});
$("rollback-agent").addEventListener("click", rollbackAgent);
$("agent-list").addEventListener("click", (event) => {
  const button = event.target.closest(".activate-agent");
  if (!button) return;
  activateAgent(button.dataset.versionId);
});
$("sia-build").addEventListener("click", buildReplayDataset);
$("sia-run").addEventListener("click", runSia);

renderTrace("manage-trace", []);
renderTrace("sia-trace", []);
renderSiaSummaryCard(null);
setStatus("Loading");
setMode("Pulling live Robinhood snapshot", true);
syncManageButtons();
setActiveTab("portfolio");
loadConfig();
loadAgents();
loadSiaStatus();
