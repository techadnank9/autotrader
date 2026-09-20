(function () {
  var $ = function (id) { return document.getElementById(id); };
  var state = { decision: null, busy: false };

  function money(v) {
    var n = Number(v);
    if (!isFinite(n)) n = 0;
    return "$" + n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function clockOf(iso) { return (iso && iso.length >= 16) ? iso.slice(11, 16) + " UTC" : "--"; }
  function dayOf(iso) { return (iso && iso.length >= 10) ? iso.slice(0, 10) : "--"; }

  async function api(url, options) {
    var res = await fetch(url, options);
    var data = await res.json().catch(function () { return {}; });
    if (!res.ok) throw new Error(data.detail || ("Request failed (" + res.status + ")"));
    return data;
  }

  /* ---------- today's call ---------- */

  function renderCall(decision) {
    state.decision = decision;
    var body = $("call-body");
    if (!decision) {
      body.innerHTML =
        '<p class="empty">No call is waiting. Your agents propose at most one at a time — ' +
        'run research to produce today\'s.</p>';
      return;
    }

    var pct = Math.max(0, Math.min(1, Number(decision.confidence) || 0));
    var answered = decision.status !== "pending";
    var html =
      '<div class="pending">' +
        '<div class="pending-main">' +
          '<p class="sym-row"><span class="sym mono">' + esc(decision.symbol) + '</span>' +
          '<span class="side">' + esc(decision.side) + '</span></p>' +
          '<p class="amount">Order <b class="mono">' + money(decision.amount_usd) + '</b></p>' +
          '<p class="reason">' + esc(decision.reason) + '</p>' +
          '<p class="expiry">Expires ' + esc(clockOf(decision.expires_at)) + '. No answer means no trade.</p>' +
        '</div>' +
        '<div class="conf">' +
          '<p class="conf-head"><span>CONFIDENCE</span><span class="mono">' + pct.toFixed(2) + '</span></p>' +
          '<span class="conf-track"><i class="conf-fill" style="width:' + (pct * 100).toFixed(0) + '%"></i></span>' +
        '</div>' +
      '</div>';

    if (answered) {
      var exec = decision.execution || {};
      var neg = decision.status !== "approved";
      html += '<p class="state' + (neg ? " neg" : "") + '"><i></i>' + esc(decision.status) +
        (exec.status ? " · execution " + esc(exec.status) : "") + '</p>';
      if (exec.message) html += '<p class="expiry">' + esc(exec.message) + '</p>';
    } else {
      html +=
        '<div class="answer">' +
          '<button class="btn btn-ghost" id="skip-btn" type="button">Skip</button>' +
          '<button class="btn btn-primary" id="approve-btn" type="button">Approve this order</button>' +
        '</div>';
    }
    body.innerHTML = html;

    if (!answered) {
      $("approve-btn").onclick = function () { answer(decision.decision_id, true); };
      $("skip-btn").onclick = function () { answer(decision.decision_id, false); };
    }
  }

  async function answer(decisionId, approved) {
    if (state.busy) return;
    state.busy = true;
    var btn = approved ? $("approve-btn") : $("skip-btn");
    if (btn) { btn.disabled = true; btn.textContent = approved ? "Placing…" : "Skipping…"; }
    try {
      var data = await api("/api/decisions/answer", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ decision_id: decisionId, approved: approved })
      });
      renderCall(data.decision);
      loadHistory();
    } catch (err) {
      $("call-body").insertAdjacentHTML("beforeend",
        '<p class="expiry" style="color:var(--down)">' + esc(err.message) + '</p>');
      if (btn) { btn.disabled = false; btn.textContent = approved ? "Approve this order" : "Skip"; }
    } finally {
      state.busy = false;
    }
  }

  async function runResearch() {
    var btn = $("run");
    btn.disabled = true; btn.textContent = "Researching…";
    $("call-body").innerHTML = '<p class="skel">Agents are reading the market. This can take a moment…</p>';
    try {
      var data = await api("/api/decisions/propose", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ pool_size: 5 })
      });
      if (data.status === "no_trade") {
        $("call-body").innerHTML = '<p class="empty">No candidate cleared the bar today. ' +
          esc(data.reason || "") + '</p>';
        renderTrace((data.analysis || {}).reasoning_trace);
      } else {
        renderCall(data.decision);
        if (data.delivery && data.delivery.sent) {
          $("call-body").insertAdjacentHTML("beforeend",
            '<p class="expiry">Also sent to your Telegram.</p>');
        }
        loadTrace();
      }
      loadHistory();
    } catch (err) {
      $("call-body").innerHTML = '<p class="empty" style="color:var(--down)">' + esc(err.message) + '</p>';
    } finally {
      btn.disabled = false; btn.textContent = "Run research";
    }
  }

  /* ---------- reasoning ---------- */

  function renderTrace(trace) {
    if (!Array.isArray(trace) || !trace.length) return;
    $("why").innerHTML = '<div class="trace">' + trace.map(function (t) {
      return '<article class="trace-row">' +
        '<p class="trace-stage">' + esc(t.stage || "step") + '</p>' +
        '<p class="trace-title">' + esc(t.title || "") + '</p>' +
        '<p class="trace-detail">' + esc(t.detail || "") + '</p>' +
      '</article>';
    }).join("") + '</div>';
  }

  async function loadTrace() {
    try {
      var data = await api("/api/analyze", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ budget: "5", pool_size: 5 })
      });
      renderTrace(data.reasoning_trace);
    } catch (err) { /* the call itself still stands without its trace */ }
  }

  /* ---------- account ---------- */

  async function loadAccount() {
    try {
      var data = await api("/api/portfolio-snapshot");
      var snap = data.account_snapshot || {};
      var pf = snap.portfolio || {};
      var positions = Array.isArray(snap.positions) ? snap.positions : [];

      $("stats").innerHTML =
        stat("Total value", money(pf.total_value)) +
        stat("Cash available", money(pf.cash_available)) +
        stat("Buying power", money(pf.buying_power)) +
        stat("Open positions", String(positions.length));

      $("holdings").innerHTML = positions.length
        ? positions.map(function (p) {
            return '<div class="hold-row"><span>' + esc(p.symbol || "--") + '</span>' +
              '<span>' + Number(p.quantity || 0).toFixed(4) + '</span>' +
              '<span>' + money(p.market_value) + '</span></div>';
          }).join("")
        : '<p class="empty">No positions. Connect a brokerage to see holdings here.</p>';
    } catch (err) {
      $("stats").innerHTML = "";
      $("holdings").innerHTML = '<p class="empty">Could not read the account snapshot.</p>';
    }
  }

  function stat(label, value, isText) {
    return '<div><dt>' + esc(label) + '</dt><dd' + (isText ? ' class="txt"' : '') + '>' + esc(value) + '</dd></div>';
  }

  /* ---------- history ---------- */

  async function loadHistory() {
    try {
      var data = await api("/api/decisions");
      var open = data.open || [];
      var recent = data.recent || [];
      if (open.length && !state.decision) renderCall(open[0]);
      else if (!open.length && !state.decision) renderCall(null);

      var answered = recent.filter(function (d) { return d.status !== "pending"; });
      $("history").innerHTML = answered.length
        ? '<div class="hist">' + answered.map(function (d) {
            var exec = d.execution || {};
            var cls = d.status === "approved" ? "ok" : (d.status === "skipped" ? "" : "no");
            return '<div class="hist-row">' +
              '<span class="hist-when">' + esc(dayOf(d.created_at)) + '</span>' +
              '<span class="hist-what"><b>' + esc(d.symbol) + '</b> <span>' + esc(d.side) + ' ' + money(d.amount_usd) + '</span></span>' +
              '<span class="tag ' + cls + '">' + esc(d.status) + '</span>' +
              '<span class="hist-out">' + esc(exec.status || "—") + '</span>' +
            '</div>';
          }).join("") + '</div>'
        : '<p class="empty">Nothing answered yet. Decisions you approve or skip are recorded here with what happened after.</p>';
    } catch (err) {
      $("history").innerHTML = '<p class="empty">Could not load decision history.</p>';
    }
  }

  /* ---------- limits + delivery ---------- */

  async function loadLimits() {
    try {
      var cfg = await api("/api/config");
      var policy = cfg.portfolio_policy || {};
      $("rails").innerHTML =
        stat("Max order", money(cfg.max_budget_usd)) +
        stat("Cash reserve", money(policy.cash_reserve_usd)) +
        stat("Max positions", String(policy.max_positions)) +
        stat("Instruments", "Long-only US equities", true);
    } catch (err) { $("rails").innerHTML = ""; }

    try {
      var tg = await api("/api/telegram/status");
      $("delivery").textContent = tg.configured
        ? "Decision cards are delivered to Telegram" + (tg.bot ? " via @" + tg.bot : "") + "."
        : "Telegram delivery is not configured, so calls appear here only.";
    } catch (err) {
      $("delivery").textContent = "Telegram delivery status unavailable.";
    }
  }

  /* ---------- session ---------- */

  async function boot() {
    try {
      var me = await api("/api/auth/me");
      if (!me.authenticated) { window.location.href = "/login"; return; }
      $("who").textContent = me.user.email;
      if (me.user.is_demo) $("demo-note").hidden = false;
    } catch (err) { window.location.href = "/login"; return; }

    $("signout").onclick = async function () {
      try { await api("/api/auth/logout", { method: "POST" }); } catch (e) {}
      window.location.href = "/login";
    };
    $("run").onclick = runResearch;

    loadHistory();
    loadAccount();
    loadLimits();
  }

  boot();
})();
