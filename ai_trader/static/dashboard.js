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

  /* ---------- today's picks ---------- */

  var view = null;
  var LABEL = { buy: "Buy", watch: "Watch", avoid: "Avoid" };

  function timeOf(epoch) {
    return new Date(epoch * 1000).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
  }

  function pickRow(p, v) {
    var bought = v.bought.indexOf(p.symbol) !== -1;
    var action = "";
    if (p.verdict === "buy") {
      if (bought) action = '<span class="tag ok">Bought</span>';
      else if (v.is_demo) action = '<a class="btn btn-ghost btn-sm" href="/login">Create an account to buy</a>';
      else if (!v.has_broker) action = '<a class="btn btn-ghost btn-sm" href="/connect">Connect a broker to buy</a>';
      else action = '<button class="btn btn-primary btn-sm" data-buy="' + esc(p.symbol) + '" type="button">Buy ' + money(v.order_usd) + '</button>';
    }
    var sources = (p.sources || []).map(function (s) {
      return '<li><a href="' + esc(s.url) + '" target="_blank" rel="noopener">' + esc(s.title || s.url) + '</a>' +
        (s.outlet ? ' <span>' + esc(s.outlet) + '</span>' : '') + '</li>';
    }).join("");
    var risks = (p.risks || []).map(function (r) { return '<li>' + esc(r) + '</li>'; }).join("");
    var more = (sources || risks)
      ? '<details class="pick-more"><summary>Why</summary>' +
          (sources ? '<p class="pick-h">In the news</p><ul class="pick-src">' + sources + '</ul>' : '') +
          (risks ? '<p class="pick-h">Risks</p><ul class="pick-risk">' + risks + '</ul>' : '') +
        '</details>'
      : '';
    return '<article class="pick v-' + esc(p.verdict) + '">' +
      '<div class="pick-id"><span class="pick-sym mono">' + esc(p.symbol) + '</span>' +
        '<span class="verdict v-' + esc(p.verdict) + '">' + (LABEL[p.verdict] || "Watch") + '</span></div>' +
      '<div class="pick-body"><p class="pick-sum">' + esc(p.summary) + '</p>' + more +
        '<p class="pick-msg" data-msg="' + esc(p.symbol) + '" role="status"></p></div>' +
      '<div class="pick-act" data-act="' + esc(p.symbol) + '">' + action + '</div>' +
    '</article>';
  }

  function renderPicks(v) {
    view = v;
    $("picks-sub").textContent = "Updated " + timeOf(v.updated_at) +
      (v.articles_read ? " · from " + v.articles_read + " news articles" : "");
    $("refresh").hidden = false;

    if (v.status !== "ok" || !v.picks.length) {
      $("picks").innerHTML = '<p class="empty">' + esc(v.message || "Today's picks aren't ready yet. Check back shortly.") + '</p>';
      return;
    }
    var buys = v.picks.filter(function (p) { return p.verdict === "buy"; });
    var lead = buys.length
      ? '<p class="picks-lead">' + buys.length + (buys.length === 1 ? " stock looks" : " stocks look") + " worth buying today.</p>"
      : '<p class="picks-lead">No strong buys today. Nothing in today’s news clears the bar, so here is what we’re watching.</p>';
    $("picks").innerHTML = lead + '<div class="pick-list">' + v.picks.map(function (p) { return pickRow(p, v); }).join("") + '</div>';

    Array.prototype.forEach.call(document.querySelectorAll("[data-buy]"), function (btn) {
      btn.onclick = function () { confirmBuy(btn.dataset.buy); };
    });
  }

  function confirmBuy(symbol) {
    var slot = document.querySelector('[data-act="' + symbol + '"]');
    slot.innerHTML =
      '<span class="confirm">Buy ' + money(view.order_usd) + ' of ' + esc(symbol) + '?</span>' +
      '<button class="btn btn-primary btn-sm" data-yes type="button">Confirm</button>' +
      '<button class="btn btn-quiet btn-sm" data-no type="button">Cancel</button>';
    slot.querySelector("[data-no]").onclick = function () { renderPicks(view); };
    slot.querySelector("[data-yes]").onclick = function () { buy(symbol, slot); };
  }

  async function buy(symbol, slot) {
    slot.innerHTML = '<span class="confirm">Placing order…</span>';
    var msg = document.querySelector('[data-msg="' + symbol + '"]');
    try {
      var r = await api("/api/picks/buy", { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ run_id: view.run_id, symbol: symbol }) });
      var ex = r.execution || {};
      if (ex.status === "submitted") {
        view.bought.push(symbol);
        renderPicks(view);
        var m2 = document.querySelector('[data-msg="' + symbol + '"]');
        m2.className = "pick-msg ok"; m2.textContent = "Order placed. It will show in your account shortly.";
        loadTrades(); loadAccount();
      } else {
        renderPicks(view);
        var m3 = document.querySelector('[data-msg="' + symbol + '"]');
        m3.className = "pick-msg bad"; m3.textContent = ex.message || "The order was not placed.";
      }
    } catch (err) {
      renderPicks(view);
      var m4 = document.querySelector('[data-msg="' + symbol + '"]');
      m4.className = "pick-msg bad"; m4.textContent = err.message;
    }
  }

  async function loadPicks(force) {
    $("refresh").disabled = true;
    if (!view || force) {
      $("picks").innerHTML = '<div class="picks-wait"><i class="pulse"></i><div><p>Reading today’s market news…</p>' +
        '<p class="panel-sub">This takes about a minute when the picks need updating.</p></div></div>';
    }
    try {
      renderPicks(await api(force ? "/api/picks/refresh" : "/api/picks", force ? { method: "POST" } : undefined));
    } catch (err) {
      $("picks").innerHTML = '<p class="empty">Could not load today’s picks. ' + esc(err.message) + '</p>';
    } finally {
      $("refresh").disabled = false;
    }
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
    // Days before the account was funded are not a $0 balance; never chart them.
    while (pts.length && !(pts[0].equity > 0)) pts.shift();
    if (pts.length < 2) {
      wrap.innerHTML = (pts.length ? '<div class="headline"><span class="big">' + money(pts[0].equity) + '</span></div>' : '') +
        '<p class="empty">Your account is new, so there is no history to chart yet. It fills in day by day from here.</p>';
      $("chart-table").innerHTML = "";
      return;
    }

    var first = pts[0].equity, last = pts[pts.length - 1].equity;
    var chg = last - first, pct = chg / first;
    var sign = chg >= 0 ? "+" : "−";
    var since = data.opened_in_period ? " since you opened the account" : " this period";

    wrap.innerHTML =
      '<div class="headline"><span class="big">' + money(last) + '</span>' +
      '<span class="chg">' + sign + money(Math.abs(chg)).slice(1) + " (" + sign + Math.abs(pct * 100).toFixed(2) + '%)' + since + '</span></div>';

    var W = Math.max(wrap.clientWidth, 320), H = 260;
    var ys = pts.map(function (p) { return p.equity; });
    var lo = Math.min.apply(null, ys), hi = Math.max.apply(null, ys);
    var pad = (hi - lo) * 0.12 || Math.max(hi * 0.01, 1);
    lo -= pad; hi += pad;
    if (Math.min.apply(null, ys) >= 0) lo = Math.max(lo, 0);  // an account value axis never goes negative

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

    var welcome = new URLSearchParams(location.search).get("welcome");
    if (welcome) {
      var name = welcome === "robinhood" ? "Robinhood" : "Alpaca paper";
      $("welcome").innerHTML = "You're set up. Your <b>" + name + "</b> account is connected. " +
        "Tap <b>Buy</b> on any pick below to place an order.";
      $("welcome").hidden = false;
      history.replaceState(null, "", "/app");
    }

    $("signout").onclick = async function () {
      try { await api("/api/auth/logout", { method: "POST" }); } catch (e) {}
      window.location.href = "/login";
    };
    $("refresh").onclick = function () { loadPicks(true); };

    bindPeriods();
    loadPicks(false);
    loadAccount();
    loadLimits();
    loadEquity();
    loadTrades();
  }

  boot();
})();
