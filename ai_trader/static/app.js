const state = {
  recommendation: null,
};

const $ = (id) => document.getElementById(id);

function syncLabels() {
  $("reddit-label").textContent = $("reddit").value;
  $("x-label").textContent = $("x").value;
  $("realtime-label").textContent = $("realtime").value;
}

function setStatus(text) {
  $("status").textContent = text;
}

function renderPool(pool = []) {
  $("pool").innerHTML = pool
    .map(
      (item) => `
        <div class="row">
          <span>${item.symbol ?? "--"}</span>
          <span>${Number(item.score ?? 0).toFixed(2)}</span>
          <span>$${Number(item.allocation_usd ?? 0).toFixed(2)}</span>
          <span>${item.reason ?? ""}</span>
        </div>
      `,
    )
    .join("");
}

function renderAnalysis(data) {
  const recommendation = data.recommendation ?? {};
  state.recommendation = recommendation;

  $("symbol").textContent = recommendation.symbol ?? "--";
  $("decision").textContent = `${recommendation.decision ?? "blocked"} · $${Number(
    recommendation.dollar_amount ?? 0,
  ).toFixed(2)} · confidence ${Number(recommendation.confidence ?? 0).toFixed(2)}`;
  $("rationale").textContent = recommendation.rationale ?? "No rationale returned.";

  const summary = data.source_summary ?? {};
  $("reddit-summary").textContent = summary.reddit ?? "--";
  $("x-summary").textContent = summary.x ?? "--";
  $("realtime-summary").textContent = summary.realtime ?? "--";
  renderPool(data.pool ?? []);

  $("trade").disabled = recommendation.decision !== "buy";
  $("trade-output").textContent = JSON.stringify({ status: "no_op" }, null, 2);
}

async function analyze() {
  document.body.classList.add("loading");
  setStatus("Analyzing");
  $("analyze").disabled = true;

  try {
    const response = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        budget: $("budget").value || "5",
        pool_size: Number($("pool-size").value || 5),
        weights: {
          reddit: Number($("reddit").value) / 100,
          x: Number($("x").value) / 100,
          realtime: Number($("realtime").value) / 100,
        },
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail ?? "Analysis failed");
    renderAnalysis(data);
    setStatus("Ready");
  } catch (error) {
    setStatus("Error");
    $("trade-output").textContent = JSON.stringify({ error: error.message }, null, 2);
  } finally {
    document.body.classList.remove("loading");
    $("analyze").disabled = false;
  }
}

async function trade() {
  const response = await fetch("/api/trade", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      recommendation: state.recommendation ?? {},
      execute: $("execute").checked,
      confirm_phrase: $("confirm").value,
    }),
  });
  const data = await response.json();
  $("trade-output").textContent = JSON.stringify(data, null, 2);
}

["reddit", "x", "realtime"].forEach((id) => $(id).addEventListener("input", syncLabels));
$("analyze").addEventListener("click", analyze);
$("trade").addEventListener("click", trade);
syncLabels();
