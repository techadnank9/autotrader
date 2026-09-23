(function () {
  var $ = function (id) { return document.getElementById(id); };
  var money = function (v) { return window.Charts.money(v); };
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  async function api(u, o) { var r = await fetch(u, o); var d = await r.json().catch(function () { return {}; }); if (!r.ok) throw new Error(d.detail || ("HTTP " + r.status)); return d; }
  function pct(v, d) { if (v == null || !isFinite(v)) return "—"; return (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(d == null ? 2 : d) + "%"; }
  function tone(v) { return v > 0 ? "up" : v < 0 ? "down" : ""; }
  function big(v, isMoney) {
    if (v == null || !isFinite(v)) return "—";
    var a = Math.abs(v), u = a >= 1e12 ? [1e12, "T"] : a >= 1e9 ? [1e9, "B"] : a >= 1e6 ? [1e6, "M"] : a >= 1e3 ? [1e3, "K"] : [1, ""];
    return (isMoney ? "$" : "") + (v / u[0]).toFixed(u[1] ? 2 : 0) + u[1];
  }
  var SYM = decodeURIComponent(location.pathname.split("/").pop() || "").toUpperCase();
  var LABEL = { buy: "Buy", watch: "Watch", avoid: "Avoid" };
  var ov = null, range = "1M", chartTimer = null, orderTimer = null, ticketMode = "buy";

  /* ---------- header, chart ---------- */
  function renderHeader() {
    var s = ov.stats;
    document.title = SYM + " — " + s.name + " — AI Trader";
    $("sp-name").textContent = s.name;
    $("sp-sub").textContent = SYM + (s.exchange ? " · " + s.exchange : "");
    var v = $("sp-verdict");
    if (ov.pick) { v.hidden = false; v.className = "verdict v-" + ov.pick.verdict; v.textContent = (LABEL[ov.pick.verdict] || "") + " today"; }
    $("sp-price").textContent = money(s.price);
    $("sp-chg").className = "mono " + tone(s.change);
    $("sp-chg").textContent = s.change != null ? (s.change > 0 ? "+" : s.change < 0 ? "−" : "") + money(Math.abs(s.change)).slice(0) + " (" + pct(s.change_pct) + ") today" : "";
    $("sp-asof").innerHTML = (s.market_open ? '<span class="live-dot">Live</span>' : '<span class="live-dot off">Market closed</span>') +
      ' <span>· Prices from ' + esc(s.price_source) + '</span>';
  }

  async function loadChart() {
    clearTimeout(chartTimer);
    try {
      var d = await api("/api/stock/" + encodeURIComponent(SYM) + "/history?range=" + range);
      var pts = d.points.map(function (p) { return { t: p.t, v: p.price }; });
      if (pts.length < 2) { $("chart").innerHTML = '<p class="empty">Not enough price history for this range.</p>'; return; }
      var first = pts[0].v, last = pts[pts.length - 1].v;
      var base = (range === "1D" && ov && ov.stats.previous_close) ? ov.stats.previous_close : first;
      var label = { "1D": "today", "5D": "past 5 days", "1M": "past month", "3M": "past 3 months", "6M": "past 6 months", "1Y": "past year", "5Y": "past 5 years" }[range];
      $("range-chg").innerHTML = '<span class="mono ' + tone(last - base) + '">' + pct((last / base - 1) * 100) + '</span> ' + label;
      var intraday = range === "1D" || range === "5D";
      window.Charts.lineChart($("chart"), pts, {
        height: 320, aria: SYM + " price, " + label,
        refs: range === "1D" && ov && ov.stats.previous_close ? [{ v: ov.stats.previous_close, label: "Previous close " + money(ov.stats.previous_close) }] : [],
        xLabel: function (t) { var d8 = new Date(t * 1000); return intraday ? d8.toLocaleString("en-US", range === "1D" ? { hour: "numeric", minute: "2-digit" } : { weekday: "short", hour: "numeric" }) : d8.toLocaleDateString("en-US", range === "5Y" ? { month: "short", year: "numeric" } : { month: "short", day: "numeric" }); },
        tip: function (p, f) { var d8 = new Date(p.t * 1000); return "<b>" + money(p.v) + "</b><span>" + (intraday ? d8.toLocaleString("en-US", { weekday: "short", hour: "numeric", minute: "2-digit" }) : d8.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })) + " · " + pct((p.v / f.v - 1) * 100) + "</span>"; },
      });
      if (range === "1D" && ov && ov.stats.market_open) chartTimer = setTimeout(loadChart, 30000);
    } catch (err) { $("chart").innerHTML = '<p class="empty">Could not load the chart: ' + esc(err.message) + '</p>'; }
  }

  /* ---------- sections ---------- */
  function renderMoving() {
    var c = ov.context || {}, s = ov.stats;
    var tiles = [{ k: SYM, v: c.day_change_pct != null ? c.day_change_pct : s.change_pct }, { k: "Market", v: c.market_change_pct }];
    if (c.sector) tiles.push({ k: c.sector, v: c.sector_change_pct });
    var ratio = (c.typical_move_pct && c.day_change_pct != null) ? Math.abs(c.day_change_pct) / c.typical_move_pct : 0;
    var day = c.day_label === "Today" || !c.day_label ? "today" : "on " + c.day_label;
    var insight = ratio >= 2
      ? (c.day_change_pct > 0 ? "Already up " : "Down ") + Math.abs(c.day_change_pct).toFixed(1) + "% " + day + ", about " + Math.round(ratio) + "× a normal day" +
        (c.day_change_pct > 0 ? ". Much of the news may already be in the price." : ". A big move; check why before buying.")
      : (c.typical_move_pct ? "Moves about ±" + c.typical_move_pct.toFixed(1) + "% on a normal day." : "");
    $("moving").innerHTML =
      '<div class="ctx-tiles">' + tiles.map(function (t) { return '<div><span>' + esc(t.k) + '</span><b class="mono ' + tone(t.v) + '">' + pct(t.v, 1) + '</b></div>'; }).join("") + '</div>' +
      (insight ? '<p class="ctx-insight' + (ratio >= 2 ? " strong" : "") + '">' + esc(insight) + '</p>' : '') +
      '<p class="ctx-range">Past week <b class="mono ' + tone(c.week_pct) + '">' + pct(c.week_pct, 1) + '</b> · past month <b class="mono ' + tone(c.month_pct) + '">' + pct(c.month_pct, 1) + '</b> · past year <b class="mono ' + tone(s.year_change_pct) + '">' + pct(s.year_change_pct, 1) + '</b></p>';
  }

  function renderStats() {
    var s = ov.stats;
    var rows = [["Open", money(s.open)], ["High", money(s.day_high)], ["Low", money(s.day_low)], ["Previous close", money(s.previous_close)],
      ["Volume", big(s.volume)], ["Avg volume (3 mo)", big(s.avg_volume)], ["52-week high", money(s.week52_high)], ["52-week low", money(s.week52_low)],
      ["Market cap", big(s.market_cap, true)], ["P/E (last fiscal year)", s.pe_fy != null ? s.pe_fy.toFixed(1) : "—"],
      ["EPS (last fiscal year)", s.eps_fy != null ? money(s.eps_fy) : "—"], ["Exchange", esc(s.exchange || "—")]];
    $("stats").innerHTML = rows.map(function (r) { return '<div><dt>' + r[0] + '</dt><dd class="mono">' + r[1] + '</dd></div>'; }).join("");
    $("sources").textContent = "Prices from " + s.price_source + (s.fundamentals_source ? " · Company data from " + s.fundamentals_source : "");
  }

  function renderRead() {
    var p = ov.pick; $("read-sec").hidden = !p; if (!p) return;
    var src = (p.sources || []).map(function (x) { return '<li><a href="' + esc(x.url) + '" target="_blank" rel="noopener">' + esc(x.title || x.url) + '</a> <span>' + esc(x.outlet || "") + '</span></li>'; }).join("");
    var risks = (p.risks || []).map(function (r) { return '<li>' + esc(r) + '</li>'; }).join("");
    $("read").innerHTML = '<p class="read-v"><span class="verdict v-' + esc(p.verdict) + '">' + (LABEL[p.verdict] || "") + '</span></p>' +
      '<p class="read-sum">' + esc(p.summary) + '</p>' +
      (src ? '<p class="pick-h">In the news</p><ul class="pick-src">' + src + '</ul>' : '') +
      (risks ? '<p class="pick-h">Risks</p><ul class="pick-risk">' + risks + '</ul>' : '');
  }

  function renderPosition() {
    var p = ov.position; $("pos-sec").hidden = !p; if (!p) return;
    var t = function (k, v, cls) { return '<div><dt>' + k + '</dt><dd class="mono ' + (cls || "") + '">' + v + '</dd></div>'; };
    var plpc = p.unrealized_plpc != null ? p.unrealized_plpc * 100 : null;
    $("position").innerHTML = t("Shares", Number(p.quantity) % 1 ? Number(p.quantity).toFixed(4) : p.quantity) + t("Avg cost", p.avg_entry_price ? money(p.avg_entry_price) : "—") +
      t("Value", money(p.market_value)) + t("Gain", p.unrealized_pl != null ? (p.unrealized_pl >= 0 ? "+" : "−") + money(Math.abs(p.unrealized_pl)) + (plpc != null ? " (" + pct(plpc) + ")" : "") : "—", tone(p.unrealized_pl));
  }

  function orderText(o) {
    var what = o.notional ? money(o.notional) : (o.qty ? o.qty + " sh" : "");
    var at = o.limit_price ? " at " + money(o.limit_price) : " at market";
    var till = o.time_in_force === "gtc" ? " · until canceled" : o.time_in_force === "day" ? " · today" : "";
    var legs = [o.take_profit ? "take profit " + money(o.take_profit) : "", o.stop_loss ? "stop loss " + money(o.stop_loss) : ""].filter(Boolean).join(" · ");
    return esc((o.side || "buy") + " " + what + at + till) + (legs ? '<span class="muted"> · ' + esc(legs) + '</span>' : '');
  }

  function renderOrders() {
    var list = ov.open_orders || []; $("oo-sec").hidden = !list.length; if (!list.length) return;
    $("orders").innerHTML = list.map(function (o) {
      return '<div class="oo-row"><span>' + orderText(o) + '</span><span class="tag">' + esc((o.status || "").replace(/_/g, " ")) + '</span>' +
        (o.cancelable !== false ? '<button class="btn btn-ghost btn-sm" data-cancel="' + esc(o.id) + '" type="button">Cancel</button>' : '<span></span>') + '</div>';
    }).join("");
    document.querySelectorAll("#orders [data-cancel]").forEach(function (b) { b.onclick = function () { cancel(b.dataset.cancel, b); }; });
  }

  async function cancel(id, btn) {
    if (!confirm("Cancel this order?")) return;
    btn.disabled = true; btn.textContent = "Canceling…";
    try { await api("/api/orders/" + encodeURIComponent(id), { method: "DELETE" }); await loadOverview(); }
    catch (err) { btn.disabled = false; btn.textContent = "Cancel"; alert(err.message); }
  }

  /* ---------- order ticket ---------- */
  function tabs() {
    if (!(ov.shares_available > 0)) return "";
    return '<div class="seg full tk-tabs" role="group" aria-label="Buy or sell">' +
      '<button type="button" data-tk="buy" aria-pressed="' + (ticketMode !== "sell") + '">Buy</button>' +
      '<button type="button" data-tk="sell" aria-pressed="' + (ticketMode === "sell") + '">Sell</button></div>';
  }
  function wireTabs() {
    document.querySelectorAll("#ticket [data-tk]").forEach(function (b) {
      b.onclick = function () { ticketMode = b.dataset.tk; renderTicket(); };
    });
  }

  function renderSell() {
    var t = $("ticket"), s = ov.stats, held = Number(ov.shares_available) || 0;
    var whole = Math.floor(held), price = Number(s.price) || 0;
    t.innerHTML = tabs() +
      '<h2>Sell ' + esc(SYM) + '</h2>' +
      '<p class="ticket-note">You hold <b class="mono">' + (held % 1 ? held.toFixed(4) : held) + '</b> share' + (held === 1 ? "" : "s") +
        (price ? ' \u00b7 about ' + money(held * price) : '') + '</p>' +
      '<form class="buy" novalidate>' +
        '<div class="seg full" role="group" aria-label="Order type"><button type="button" data-t="market" aria-pressed="true">Sell now</button><button type="button" data-t="limit" aria-pressed="false">At a price</button></div>' +
        '<div class="buy-row"><label class="field grow"><span>Shares</span><input data-f="qty" inputmode="decimal" autocomplete="off" value="' + (held % 1 ? held.toFixed(4) : held) + '"></label>' +
          '<button class="btn btn-ghost" type="button" data-f="all">All</button></div>' +
        '<div data-f="limit-box" hidden><label class="field"><span>Sell when the price is at or above</span><input data-f="limit" inputmode="decimal" autocomplete="off" value="' + (price ? price.toFixed(2) : "") + '"></label>' +
          '<div class="seg full" role="group" aria-label="Good for" style="margin-top:10px"><button type="button" data-g="day" aria-pressed="true">Today only</button><button type="button" data-g="gtc" aria-pressed="false">Until canceled</button></div></div>' +
        '<p class="buy-hint" data-f="est"></p>' +
        (s.market_open ? '' : '<p class="note-line">The market is closed. The sell is sent now and works from 9:30 AM ET.</p>') +
        '<p class="err" data-f="err" role="alert"></p>' +
        '<button type="submit" class="btn btn-primary btn-block" data-f="go">Sell ' + esc(SYM) + '</button>' +
      '</form>';
    wireTabs();
    var f = function (n) { return t.querySelector('[data-f="' + n + '"]'); };
    var type = "market", good = "day";
    function num(n) { var v = parseFloat(String(f(n).value).replace(/[$,\s]/g, "")); return isFinite(v) ? v : null; }
    var seg = function (attr, set) { t.querySelectorAll("[data-" + attr + "]").forEach(function (b) { b.onclick = function () { set(b.dataset[attr]); t.querySelectorAll("[data-" + attr + "]").forEach(function (x) { x.setAttribute("aria-pressed", String(x === b)); }); recalc(); }; }); };
    seg("t", function (v) { type = v; f("limit-box").hidden = v !== "limit"; });
    seg("g", function (v) { good = v; });
    f("all").onclick = function () { f("qty").value = held % 1 ? held.toFixed(4) : held; recalc(); };
    ["qty", "limit"].forEach(function (n) { f(n).addEventListener("input", function () { f("err").textContent = ""; recalc(); }); });

    function recalc() {
      var q = num("qty"), lim = type === "limit" ? num("limit") : null, ok = true, msg = "";
      if (!(q > 0)) { ok = false; msg = "Enter how many shares to sell."; }
      else if (q > held + 1e-9) { ok = false; msg = "You only hold " + (held % 1 ? held.toFixed(4) : held) + " shares."; }
      else if (type === "limit" && !(lim > 0)) { ok = false; msg = "Enter the price you want to sell at."; }
      else if (type === "limit" && q !== Math.floor(q)) { ok = false; msg = "Selling at a price needs whole shares."; }
      else { msg = "\u2248 " + money(q * (lim || price)) + (type === "limit" ? " if it fills at " + money(lim) : ""); }
      if (ok && type === "limit" && lim < price * 0.999) msg += " Your price is below the current price, so it would fill right away.";
      f("est").textContent = msg; f("go").disabled = !ok;
      return { ok: ok, q: q, lim: lim };
    }
    recalc();

    t.querySelector("form").addEventListener("submit", async function (e) {
      e.preventDefault();
      var c = recalc(); if (!c.ok) return;
      var go = f("go"); go.disabled = true; go.textContent = "Placing order\u2026";
      try {
        var r = await api("/api/orders/sell", { method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ symbol: SYM, qty: String(c.q), order_type: type,
            limit_price: c.lim != null ? String(c.lim) : null, good_until: good }) });
        var ex = r.execution || {};
        if (ex.status === "submitted" && ex.order_id) showOrder(ex.order_id, null);
        else { f("err").textContent = ex.message || "The order was not placed."; recalc(); }
      } catch (err) { f("err").textContent = err.message; recalc(); }
    });
  }

  function renderTicket() {
    var t = $("ticket"), s = ov.stats;
    if (ticketMode === "sell" && ov.shares_available > 0 && ov.can_trade && !ov.is_demo) return renderSell();
    if (ov.is_demo) { t.innerHTML = '<h2>Buy ' + esc(SYM) + '</h2><p class="note-line"><a href="/login">Create an account</a> and connect a broker to trade.</p>'; return; }
    if (!ov.can_trade) { t.innerHTML = '<h2>Buy ' + esc(SYM) + '</h2><p class="note-line"><a href="/connect">Connect a broker</a> to trade.</p>'; return; }
    if (ov.restricted) { t.innerHTML = '<h2>Buy ' + esc(SYM) + '</h2><p class="note-line">' + esc(ov.restricted) + '</p>'; return; }
    var pickNote = ov.pick
      ? (ov.pick.verdict === "buy" ? (ov.bought_from_picks ? "You already bought this pick today." : "A buy in today’s picks.") : "Our read today is " + LABEL[ov.pick.verdict].toLowerCase() + ", not buy.")
      : "Not in today’s picks. This is your own call.";
    t.innerHTML = tabs() +
      '<h2>Buy ' + esc(SYM) + '</h2><p class="ticket-note">' + esc(pickNote) + '</p>' +
      '<form class="buy" novalidate>' +
        '<div class="seg full" role="group" aria-label="Order type"><button type="button" data-t="market" aria-pressed="true">Buy now</button><button type="button" data-t="limit" aria-pressed="false">At a price</button></div>' +
        '<div class="buy-row"><label class="field grow"><span>Amount</span><input data-f="amount" inputmode="decimal" autocomplete="off" value="' + esc(ov.default_amount) + '"></label>' +
          '<div class="seg" role="group" aria-label="Amount in"><button type="button" data-m="dollars" aria-pressed="true">$</button><button type="button" data-m="shares" aria-pressed="false">Shares</button></div></div>' +
        '<div data-f="limit-box" hidden><label class="field"><span>Buy when the price is at or below</span><input data-f="limit" inputmode="decimal" autocomplete="off" value="' + (s.price ? Number(s.price).toFixed(2) : "") + '"></label>' +
          '<div class="seg full" role="group" aria-label="Good for" style="margin-top:10px"><button type="button" data-g="day" aria-pressed="true">Today only</button><button type="button" data-g="gtc" aria-pressed="false">Until canceled</button></div></div>' +
        '<details class="exit-d"><summary>Add an exit plan</summary><div class="exit-grid">' +
          '<label class="field"><span>Take profit at</span><div class="pct-in"><span>+</span><input data-f="tp" inputmode="decimal" placeholder="10"><span>%</span></div><em data-f="tp-at"></em></label>' +
          '<label class="field"><span>Stop loss at</span><div class="pct-in"><span>−</span><input data-f="sl" inputmode="decimal" placeholder="5"><span>%</span></div><em data-f="sl-at"></em></label>' +
        '</div></details>' +
        '<p class="buy-hint" data-f="est"></p>' +
        (s.market_open ? '' : '<p class="note-line">The market is closed. Orders are sent now and work from 9:30 AM ET.</p>') +
        '<p class="err" data-f="err" role="alert"></p>' +
        '<button type="submit" class="btn btn-primary btn-block" data-f="go">Buy ' + esc(SYM) + '</button>' +
        '<p class="fine-line">' + (ov.broker === "paper" ? "Practice money" : (ov.mode === "live" ? "Real money" : "Paper account · practice money")) + (ov.buying_power ? " · Buying power " + money(ov.buying_power) : "") + '</p>' +
      '</form>';
    wireTabs();
    wireTicket();
  }

  function wireTicket() {
    var t = $("ticket"), f = function (n) { return t.querySelector('[data-f="' + n + '"]'); };
    var price = Number(ov.stats.price), type = "market", mode = "dollars", good = "day";
    var seg = function (attr, set) { t.querySelectorAll("[data-" + attr + "]").forEach(function (b) { b.onclick = function () { set(b.dataset[attr]); t.querySelectorAll("[data-" + attr + "]").forEach(function (x) { x.setAttribute("aria-pressed", String(x === b)); }); recalc(); }; }); };
    seg("t", function (v) { type = v; f("limit-box").hidden = v !== "limit"; });
    seg("m", function (v) { mode = v; });
    seg("g", function (v) { good = v; });
    ["amount", "limit", "tp", "sl"].forEach(function (n) { f(n).addEventListener("input", function () { f("err").textContent = ""; recalc(); }); });
    function num(n) { var v = parseFloat(String(f(n).value).replace(/[$,\s]/g, "")); return isFinite(v) ? v : null; }

    function recalc() {
      var amt = num("amount"), tp = num("tp"), sl = num("sl"), lim = type === "limit" ? num("limit") : null;
      var entry = lim || price, exit = tp != null || sl != null, stays = exit || (type === "limit" && good === "gtc");
      var ok = true, msg = "", cost = null;
      f("tp-at").textContent = tp > 0 ? "sells at " + money(entry * (1 + tp / 100)) : "";
      f("sl-at").textContent = sl > 0 && sl < 100 ? "sells at " + money(entry * (1 - sl / 100)) : "";
      if (type === "limit" && !(lim > 0)) { ok = false; msg = "Enter the price you want to buy at."; }
      else if (!(amt > 0)) { ok = false; msg = "Enter an amount."; }
      else if (mode === "dollars") {
        if (type === "market" && !exit) { cost = amt; msg = "≈ " + (amt / price).toFixed(4) + " shares at " + money(price); }
        else if (!stays) { cost = amt; msg = "≈ " + (amt / entry).toFixed(4) + " shares at " + money(entry); }
        else {
          var whole = Math.floor(amt / entry);
          if (whole < 1) { ok = false; msg = "Needs at least one whole share (" + money(entry) + ") " + (exit ? "for an exit plan." : "to stay open."); }
          else { cost = whole * entry; msg = "Buys " + whole + " whole share" + (whole === 1 ? "" : "s") + " (≈" + money(cost) + ") " + (exit ? "so the exit plan can attach." : "so it can stay open."); }
        }
      } else {
        if (stays && amt !== Math.floor(amt)) { ok = false; msg = exit ? "An exit plan needs whole shares." : "Orders that stay open need whole shares."; }
        else { cost = amt * entry; msg = "≈ " + money(cost) + (type === "limit" ? " if it fills at " + money(entry) : ""); }
      }
      if (ok && sl != null && (sl <= 0 || sl >= 100)) { ok = false; msg = "Stop loss must be between 0% and 100%."; }
      if (ok && tp != null && tp <= 0) { ok = false; msg = "Take profit must be above 0%."; }
      if (ok && cost != null && ov.buying_power && cost > Number(ov.buying_power)) { ok = false; msg = "More than your buying power (" + money(ov.buying_power) + ")."; }
      if (ok && type === "limit" && lim > price * 1.001) msg += " Your price is above the current price, so it would fill right away.";
      f("est").textContent = msg; f("go").disabled = !ok;
      f("go").textContent = type === "limit" ? "Place order at " + (lim > 0 ? money(lim) : "your price") : "Buy " + SYM;
      return { ok: ok, amt: amt, tp: tp, sl: sl, lim: lim };
    }
    recalc();

    t.querySelector("form").addEventListener("submit", async function (e) {
      e.preventDefault();
      var c = recalc(); if (!c.ok) return;
      var go = f("go"); go.disabled = true; go.textContent = "Placing order…";
      var usePick = ov.pick && ov.pick.verdict === "buy" && !ov.bought_from_picks && ov.run_id;
      try {
        var r = await api("/api/orders", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({
          symbol: SYM, mode: mode, amount: String(c.amt), order_type: type, limit_price: c.lim != null ? String(c.lim) : null,
          good_until: good, take_profit_pct: c.tp, stop_loss_pct: c.sl, run_id: usePick ? ov.run_id : null }) });
        var ex = r.execution || {};
        if (ex.status === "submitted" && ex.order_id) showOrder(ex.order_id, r.note);
        else { f("err").textContent = ex.message || "The order was not placed."; recalc(); }
      } catch (err) { f("err").textContent = err.message; recalc(); }
    });
  }

  var DONE = { filled: 1, canceled: 1, expired: 1, rejected: 1 };
  function showOrder(id, note) {
    var t = $("ticket");
    t.innerHTML = '<h2>Your order</h2>' + (note ? '<p class="ticket-note">' + esc(note) + '</p>' : '') +
      '<div data-o="status"><p class="skel">Checking…</p></div>' +
      '<div class="ticket-actions"><button class="btn btn-ghost btn-block" data-o="cancel" type="button" hidden>Cancel order</button>' +
      '<button class="btn btn-quiet btn-block" data-o="again" type="button">Place another order</button></div>';
    var q = function (k) { return t.querySelector('[data-o="' + k + '"]'); };
    q("again").onclick = function () { clearTimeout(orderTimer); renderTicket(); };
    q("cancel").onclick = async function () {
      if (!confirm("Cancel this order?")) return;
      q("cancel").disabled = true; q("cancel").textContent = "Canceling…";
      try { await api("/api/orders/" + encodeURIComponent(id), { method: "DELETE" }); poll(); loadOverview(); }
      catch (err) { q("cancel").disabled = false; q("cancel").textContent = "Cancel order"; alert(err.message); }
    };
    var n = 0;
    async function poll() {
      clearTimeout(orderTimer);
      try {
        var o = await api("/api/orders/" + encodeURIComponent(id));
        var st = o.status || "new", filled = st === "filled", bad = { canceled: 1, expired: 1, rejected: 1 }[st];
        var tm = function (iso) { return iso ? new Date(iso).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" }) : ""; };
        var waiting = o.limit_price ? "Waiting for " + money(o.limit_price) : (ov.stats.market_open ? "Filling…" : "Waiting for the market to open");
        q("status").innerHTML = '<p class="oo-desc">' + orderText(o) + '</p><ol class="steps">' +
          '<li class="done"><i></i><span>Sent</span><time>' + tm(o.submitted_at) + '</time></li>' +
          '<li class="' + (bad ? "" : (o.accepted_at || filled ? "done" : "live")) + '"><i></i><span>Accepted</span><time>' + tm(o.accepted_at) + '</time></li>' +
          '<li class="' + (bad ? "bad" : filled ? "done" : "live") + '"><i></i><span>' + (bad ? "Order " + st : filled ? "Filled" : waiting) + '</span><time>' + tm(o.filled_at || o.canceled_at) + '</time></li></ol>' +
          (o.filled_avg_price ? '<p class="order-fill">Bought <b>' + o.filled_qty + ' sh</b> at <b class="mono">' + money(o.filled_avg_price) + '</b></p>' : '');
        q("cancel").hidden = !o.cancelable;
        if (!DONE[st]) { n++; orderTimer = setTimeout(poll, !ov.stats.market_open ? 20000 : n < 15 ? 2000 : 6000); }
        else loadOverview();
      } catch (err) { q("status").innerHTML = '<p class="err">' + esc(err.message) + '</p>'; orderTimer = setTimeout(poll, 10000); }
    }
    poll(); loadOverview();
  }

  /* ---------- load ---------- */
  async function loadOverview() {
    try {
      ov = await api("/api/stock/" + encodeURIComponent(SYM) + "/overview");
      renderHeader(); renderMoving(); renderStats(); renderRead(); renderPosition(); renderOrders();
      return ov;
    } catch (err) {
      $("sp-name").textContent = SYM;
      $("sp-sub").textContent = err.message;
      $("chart").innerHTML = ""; $("ticket").innerHTML = '<p class="empty">Nothing to trade here.</p>';
      throw err;
    }
  }

  async function boot() {
    var user = await window.Shell.mount({ active: "" });
    if (!user) return;
    document.querySelectorAll("#ranges button").forEach(function (b) {
      b.onclick = function () { range = b.dataset.r; document.querySelectorAll("#ranges button").forEach(function (x) { x.setAttribute("aria-pressed", String(x === b)); }); loadChart(); };
    });
    try { await loadOverview(); } catch (e) { return; }
    renderTicket(); loadChart();
    if (new URLSearchParams(location.search).get("sell") === "1" && ov.shares_available > 0) { ticketMode = "sell"; renderTicket(); }
    if (new URLSearchParams(location.search).get("buy") === "1") { var a = document.querySelector('#ticket [data-f="amount"]'); if (a) { a.focus(); a.select(); } }
    setInterval(function () { if (!document.hidden && ov && ov.stats.market_open) loadOverview(); }, 30000);
  }
  boot();
})();
