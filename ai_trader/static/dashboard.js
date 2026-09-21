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

      renderTiles(recent);
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
      var c2 = (await api("/api/config")).capabilities || {};
      var parts = [
        "Research: " + (c2.research_providers && c2.research_providers.length ? c2.research_providers.join(" + ") : "not connected"),
        "Ranking: " + (c2.ranking_model || "not connected"),
      ];
      try {
        var bs = await api("/api/broker/status");
        parts.push("Broker: " + (bs.connected ? "Alpaca (" + bs.mode + ")" : "not connected"));
      } catch (e) { parts.push("Broker: unknown"); }
      $("delivery").insertAdjacentHTML("beforebegin", '<p class="delivery">' + esc(parts.join(" · ")) + '</p>');
    } catch (e) {}
    try {
      var tg = await api("/api/telegram/status");
      $("delivery").textContent = tg.configured
        ? "Decision cards are delivered to Telegram" + (tg.bot ? " via @" + tg.bot : "") + "."
        : "Telegram delivery is not configured, so calls appear here only.";
    } catch (err) {
      $("delivery").textContent = "Telegram delivery status unavailable.";
    }
  }


  /* ---------- account value chart ---------- */

  var period = "1M";
  var NS = "http://www.w3.org/2000/svg";
  function el(tag, attrs) {
    var n = document.createElementNS(NS, tag);
    for (var k in attrs) n.setAttribute(k, attrs[k]);
    return n;
  }
  function fmtDay(ts) {
    var d = new Date(ts * 1000);
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  }

  function renderEquity(data) {
    var wrap = $("chart");
    var pts = (data.points || []).filter(function (p) { return p.equity != null; });
    if (pts.length < 2) {
      wrap.innerHTML = '<p class="empty">Not enough history yet. The curve fills in as your account trades.</p>';
      $("chart-table").innerHTML = "";
      return;
    }

    var first = pts[0].equity, last = pts[pts.length - 1].equity;
    var chg = last - first, pct = first ? chg / first : 0;
    var sign = chg >= 0 ? "+" : "−";

    wrap.innerHTML =
      '<div class="headline"><span class="big">' + money(last) + '</span>' +
      '<span class="chg">' + sign + money(Math.abs(chg)).slice(1) + " (" + sign + Math.abs(pct * 100).toFixed(2) + '%) this period</span></div>';

    var W = Math.max(wrap.clientWidth, 320), H = 260;
    var ys = pts.map(function (p) { return p.equity; });
    var lo = Math.min.apply(null, ys), hi = Math.max.apply(null, ys);
    var pad = (hi - lo) * 0.12 || Math.max(hi * 0.01, 1);
    lo -= pad; hi += pad;

    // Size the left gutter from the widest tick label, so large balances never clip.
    var widest = Math.max(money(lo).length, money(hi).length);
    var M = { t: 16, r: 18, b: 26, l: Math.max(56, Math.round(widest * 6.9) + 16) };
    var iw = W - M.l - M.r, ih = H - M.t - M.b;

    var x = function (i) { return M.l + (i / (pts.length - 1)) * iw; };
    var y = function (v) { return M.t + (1 - (v - lo) / (hi - lo)) * ih; };

    var svg = el("svg", { viewBox: "0 0 " + W + " " + H, role: "img",
      "aria-label": "Account value over " + period + ", from " + money(first) + " to " + money(last) });

    for (var g = 0; g <= 3; g++) {
      var gv = lo + (hi - lo) * (g / 3), gy = y(gv);
      svg.appendChild(el("line", { x1: M.l, x2: W - M.r, y1: gy, y2: gy, stroke: "#242A33", "stroke-width": 1 }));
      var t = el("text", { x: M.l - 10, y: gy + 4, "text-anchor": "end", class: "axis" });
      t.textContent = money(gv);
      svg.appendChild(t);
    }
    [0, Math.floor((pts.length - 1) / 2), pts.length - 1].forEach(function (i, k) {
      var t = el("text", { x: x(i), y: H - 6, class: "axis",
        "text-anchor": k === 0 ? "start" : (k === 2 ? "end" : "middle") });
      t.textContent = fmtDay(pts[i].t);
      svg.appendChild(t);
    });

    var d = pts.map(function (p, i) { return (i ? "L" : "M") + x(i).toFixed(1) + " " + y(p.equity).toFixed(1); }).join(" ");
    svg.appendChild(el("path", { d: d, fill: "none", stroke: "#C8F24C", "stroke-width": 2,
      "stroke-linejoin": "round", "stroke-linecap": "round" }));
    svg.appendChild(el("circle", { cx: x(pts.length - 1), cy: y(last), r: 4.5, fill: "#C8F24C",
      stroke: "#101317", "stroke-width": 2 }));

    var cross = el("line", { y1: M.t, y2: M.t + ih, stroke: "#8B95A3", "stroke-width": 1, opacity: 0 });
    var dot = el("circle", { r: 5, fill: "#C8F24C", stroke: "#101317", "stroke-width": 2, opacity: 0 });
    svg.appendChild(cross); svg.appendChild(dot);
    var hit = el("rect", { x: M.l, y: M.t, width: iw, height: ih, fill: "transparent" });
    svg.appendChild(hit);
    wrap.appendChild(svg);

    var tip = document.createElement("div");
    tip.className = "tip"; tip.hidden = true;
    wrap.appendChild(tip);

    function show(evt) {
      var r = svg.getBoundingClientRect();
      var px = (evt.clientX - r.left) * (W / r.width);
      var i = Math.round(((px - M.l) / iw) * (pts.length - 1));
      i = Math.max(0, Math.min(pts.length - 1, i));
      var p = pts[i], cx = x(i), cy = y(p.equity);
      cross.setAttribute("x1", cx); cross.setAttribute("x2", cx); cross.setAttribute("opacity", 1);
      dot.setAttribute("cx", cx); dot.setAttribute("cy", cy); dot.setAttribute("opacity", 1);
      var delta = p.equity - first;
      tip.innerHTML = '<b>' + money(p.equity) + '</b><span>' + fmtDay(p.t) + ' · ' +
        (delta >= 0 ? "+" : "−") + money(Math.abs(delta)).slice(1) + '</span>';
      tip.style.left = (cx * r.width / W) + "px";
      tip.style.top = (svg.offsetTop + cy * r.height / H) + "px";
      tip.hidden = false;
    }
    function hide() { cross.setAttribute("opacity", 0); dot.setAttribute("opacity", 0); tip.hidden = true; }
    hit.addEventListener("mousemove", show);
    hit.addEventListener("mouseleave", hide);
    hit.addEventListener("touchstart", function (e) { show(e.touches[0]); }, { passive: true });
    hit.addEventListener("touchmove", function (e) { show(e.touches[0]); }, { passive: true });

    var step = Math.max(1, Math.floor(pts.length / 30));
    var rows = pts.filter(function (_, i) { return i % step === 0 || i === pts.length - 1; });
    $("chart-table").innerHTML = '<table><thead><tr><th>Date</th><th>Value</th></tr></thead><tbody>' +
      rows.map(function (p) { return '<tr><td>' + fmtDay(p.t) + '</td><td>' + money(p.equity) + '</td></tr>'; }).join("") +
      '</tbody></table>';
  }

  async function loadEquity() {
    $("chart").innerHTML = '<p class="empty">Loading…</p>';
    try {
      var b = await api("/api/broker/status");
      if (!b.configured || !b.connected) {
        $("chart").innerHTML = '<p class="empty">No broker connected. <a href="/profile">Connect your Alpaca account</a> to chart your account value.</p>';
        $("perf-sub").textContent = "Your own brokerage account, once connected.";
        return;
      }
      $("perf-sub").textContent = "From your Alpaca " + b.mode + " account.";
      renderEquity(await api("/api/portfolio/history?period=" + period));
    } catch (err) {
      $("chart").innerHTML = '<p class="empty">Could not load account history: ' + esc(err.message) + '</p>';
    }
  }

  function bindPeriods() {
    Array.prototype.forEach.call(document.querySelectorAll("#periods button"), function (b) {
      b.onclick = function () {
        period = b.dataset.p;
        Array.prototype.forEach.call(document.querySelectorAll("#periods button"), function (o) {
          o.setAttribute("aria-pressed", String(o === b));
        });
        loadEquity();
      };
    });
    var t; window.addEventListener("resize", function () { clearTimeout(t); t = setTimeout(loadEquity, 200); });
  }

  /* ---------- calls + trades ---------- */

  function renderTiles(decisions) {
    var count = function (st) { return decisions.filter(function (d) { return d.status === st; }).length; };
    $("tiles").innerHTML =
      tile("Calls proposed", decisions.length) + tile("Approved", count("approved")) +
      tile("Skipped", count("skipped")) + tile("Expired", count("expired"));
  }
  function tile(label, n) { return '<div><dt>' + esc(label) + '</dt><dd>' + n + '</dd></div>'; }

  async function loadTrades() {
    try {
      var data = await api("/api/orders");
      var orders = data.orders || [];
      if (data.broker === "none") {
        $("trades").innerHTML = '<p class="empty">No broker connected yet. <a href="/profile">Connect Alpaca</a> and your approved orders appear here.</p>';
        return;
      }
      $("trades").innerHTML = orders.length
        ? '<div class="trade-list">' + orders.map(function (o) {
            var price = o.filled_avg_price ? money(o.filled_avg_price) : "—";
            return '<div class="trade-row">' +
              '<span class="d">' + esc((o.submitted_at || "").slice(0, 10)) + '</span>' +
              '<span class="m"><b>' + esc(o.symbol) + '</b></span>' +
              '<span class="m">' + esc(o.side) + ' ' + (o.notional ? money(o.notional) : esc(o.filled_qty || "")) + '</span>' +
              '<span class="tag ' + (o.status === "filled" ? "ok" : (/cancel|reject|expire/.test(o.status) ? "no" : "")) + '">' + esc(o.status) + '</span>' +
              '<span class="m" style="text-align:right">' + price + '</span></div>';
          }).join("") + '</div>'
        : '<p class="empty">No orders yet. Approve a call to place your first one.</p>';
    } catch (err) {
      $("trades").innerHTML = '<p class="empty">Could not load orders: ' + esc(err.message) + '</p>';
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

    bindPeriods();
    loadHistory();
    loadAccount();
    loadLimits();
    loadEquity();
    loadTrades();
  }

  boot();
})();
