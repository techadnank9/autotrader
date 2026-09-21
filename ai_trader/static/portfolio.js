(function () {
  var $ = function (id) { return document.getElementById(id); };
  var money = function (v) { return window.Charts.money(v); };
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  async function api(u, o) { var r = await fetch(u, o); var d = await r.json().catch(function () { return {}; }); if (!r.ok) throw new Error(d.detail || ("HTTP " + r.status)); return d; }
  function signed(v) { return (v > 0 ? "+" : v < 0 ? "−" : "") + money(Math.abs(v)).slice(0); }
  function pct(v) { return v == null ? "" : " (" + (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(2) + "%)"; }
  function tone(v) { return v > 0 ? "up" : v < 0 ? "down" : ""; }
  var period = "1M", isDemo = false;

  function renderSummary(p) {
    var t = function (k, v, cls) { return '<div><dt>' + esc(k) + '</dt><dd class="' + (cls || "") + '">' + v + '</dd></div>'; };
    $("summary").innerHTML =
      '<div class="pf-head"><p class="pf-total mono">' + money(p.total_value) + '</p>' +
        (p.day_change != null ? '<p class="pf-day mono ' + tone(p.day_change) + '">' + signed(p.day_change) + pct(p.day_change_pct) + ' today</p>' : '') +
        '<span class="pf-mode">' + (p.mode === "live" ? "Real money" : "Paper account") + ' · ' + esc(p.broker === "robinhood" ? "Robinhood" : "Alpaca") + '</span></div>' +
      '<dl class="tiles">' +
        t("Cash", money(p.cash)) + t("Invested", money(p.invested)) + t("Buying power", money(p.buying_power)) +
        t("Unrealized gain", signed(p.unrealized_pl) + pct(p.unrealized_pl_pct), tone(p.unrealized_pl)) +
      '</dl>' +
      '<p class="pf-counts">' + p.positions.length + ' open position' + (p.positions.length === 1 ? "" : "s") + ' · ' + p.open_orders.length + ' open order' + (p.open_orders.length === 1 ? "" : "s") + '</p>';
  }

  function renderPositions(p) {
    if (!p.positions.length) { $("positions").innerHTML = '<p class="empty">No positions yet. Buy a pick from <a href="/app">Today’s picks</a>.</p>'; return; }
    $("positions").innerHTML = '<div class="tbl" role="table"><div class="tr th" role="row"><span>Stock</span><span>Shares</span><span>Avg cost</span><span>Price</span><span>Value</span><span>Gain</span></div>' +
      p.positions.map(function (x) {
        var plpc = x.unrealized_plpc != null ? x.unrealized_plpc * 100 : null;
        return '<div class="tr" role="row"><span><a class="link-btn mono" href="/stock/' + encodeURIComponent(x.symbol) + '">' + esc(x.symbol) + '</a></span>' +
          '<span class="mono">' + (Number(x.quantity) % 1 ? Number(x.quantity).toFixed(4) : x.quantity) + '</span>' +
          '<span class="mono">' + (x.avg_entry_price ? money(x.avg_entry_price) : "—") + '</span>' +
          '<span class="mono">' + (x.current_price ? money(x.current_price) : "—") + '</span>' +
          '<span class="mono">' + money(x.market_value) + '</span>' +
          '<span class="mono ' + tone(x.unrealized_pl) + '">' + (x.unrealized_pl != null ? signed(x.unrealized_pl) + pct(plpc) : "—") + '</span></div>';
      }).join("") + '</div>';
  }

  function renderOrders(p) {
    if (!p.open_orders.length) { $("orders").innerHTML = '<p class="empty">Nothing open.</p>'; return; }
    $("orders").innerHTML = '<div class="tbl orders" role="table">' + p.open_orders.map(function (o) {
      var what = o.notional ? money(o.notional) : (o.qty ? o.qty + " sh" : "");
      var at = o.limit_price ? " at " + money(o.limit_price) : "";
      var till = o.time_in_force === "gtc" ? "until canceled" : "today";
      var legs = [till, o.take_profit ? "take profit " + money(o.take_profit) : "", o.stop_loss ? "stop loss " + money(o.stop_loss) : ""].filter(Boolean).join(" · ");
      return '<div class="tr" role="row"><span class="mono"><a class="link-btn" href="/stock/' + encodeURIComponent(o.symbol) + '"><b>' + esc(o.symbol) + '</b></a></span><span>' + esc(o.side || "buy") + ' ' + esc(what) + esc(at) + '</span>' +
        '<span><span class="tag">' + esc((o.status || "").replace(/_/g, " ")) + '</span></span><span class="muted">' + esc(legs) + '</span>' +
        '<span>' + (o.cancelable !== false ? '<button class="btn btn-ghost btn-sm" data-cancel="' + esc(o.id) + '" type="button">Cancel</button>' : '') + '</span></div>';
    }).join("") + '</div>';
    document.querySelectorAll("#orders [data-cancel]").forEach(function (b) {
      b.onclick = async function () {
        if (!confirm("Cancel this order?")) return;
        b.disabled = true; b.textContent = "Canceling…";
        try { await api("/api/orders/" + encodeURIComponent(b.dataset.cancel), { method: "DELETE" }); load(); loadHistory(); }
        catch (err) { b.disabled = false; b.textContent = "Cancel"; alert(err.message); }
      };
    });
  }

  async function loadChart() {
    $("chart").innerHTML = '<p class="empty">Loading…</p>';
    try {
      var d = await api("/api/portfolio/history?period=" + period);
      var pts = (d.points || []).filter(function (x) { return x.equity > 0; }).map(function (x) { return { t: x.t, v: x.equity }; });
      if (pts.length < 2) { $("chart").innerHTML = '<p class="empty">Your account is new, so there is no history to chart yet. It fills in day by day.</p>'; $("perf-sub").textContent = ""; return; }
      var first = pts[0].v, last = pts[pts.length - 1].v, ch = last - first;
      $("perf-sub").innerHTML = '<span class="mono ' + tone(ch) + '">' + signed(ch) + pct(ch / first * 100) + '</span> ' + (d.opened_in_period ? "since you opened the account" : "this period");
      window.Charts.lineChart($("chart"), pts, {
        height: 240, aria: "Account value",
        xLabel: function (t) { return new Date(t * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric" }); },
        tip: function (x, f) { return "<b>" + money(x.v) + "</b><span>" + new Date(x.t * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric" }) + " · " + signed(x.v - f.v) + "</span>"; },
      });
    } catch (err) { $("chart").innerHTML = '<p class="empty">Could not load account history: ' + esc(err.message) + '</p>'; }
  }

  async function loadHistory() {
    try {
      var d = await api("/api/orders");
      var orders = (d.orders || []).slice(0, 25);
      if (!orders.length) { $("history").innerHTML = '<p class="empty">No orders yet.</p>'; return; }
      $("history").innerHTML = '<div class="tbl orders" role="table">' + orders.map(function (o) {
        var what = o.notional ? money(o.notional) : (o.filled_qty && Number(o.filled_qty) ? o.filled_qty + " sh" : "");
        return '<div class="tr" role="row"><span class="mono muted">' + esc((o.submitted_at || "").slice(0, 10)) + '</span><span class="mono"><b>' + esc(o.symbol) + '</b> ' + esc(o.side || "") + ' ' + esc(what) + '</span>' +
          '<span><span class="tag ' + (o.status === "filled" ? "ok" : (/cancel|reject|expire/.test(o.status || "") ? "no" : "")) + '">' + esc((o.status || "").replace(/_/g, " ")) + '</span></span>' +
          '<span class="mono muted">' + (o.filled_avg_price ? "at " + money(o.filled_avg_price) : "") + '</span></div>';
      }).join("") + '</div>';
    } catch (err) { $("history").innerHTML = '<p class="empty">Could not load orders.</p>'; }
  }

  async function load() {
    try {
      var p = await api("/api/portfolio");
      if (!p.connected) {
        if (!isDemo) { location.href = "/connect"; return; }
        document.querySelectorAll("main .panel").forEach(function (el) { el.hidden = true; });
        $("summary").innerHTML = '<p class="empty">This is where your money shows up: value, cash, what you hold, and how it is doing. ' +
          'A demo has no brokerage, so there is nothing here yet. <a href="/login">Create an account</a> and connect a broker to see yours.</p>';
        return;
      }
      if (p.error) { $("summary").innerHTML = '<p class="empty">' + esc(p.error) + '</p>'; return; }
      renderSummary(p); renderPositions(p); renderOrders(p);
    } catch (err) { $("summary").innerHTML = '<p class="empty">Could not load your account: ' + esc(err.message) + '</p>'; }
  }

  async function boot() {
    var user = await window.Shell.mount({ active: "portfolio" });
    if (!user) return;
    isDemo = !!user.is_demo;
    document.querySelectorAll("#periods button").forEach(function (b) {
      b.onclick = function () { period = b.dataset.p; document.querySelectorAll("#periods button").forEach(function (o) { o.setAttribute("aria-pressed", String(o === b)); }); loadChart(); };
    });
    load();
    if (!isDemo) { loadChart(); loadHistory(); }
    setInterval(function () { if (!document.hidden) load(); }, 30000);  // keep values live while open
  }
  boot();
})();
