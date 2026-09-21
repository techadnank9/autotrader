/* The stock panel: context, the buy form, and the live order view.
   Opened by a pick's Buy button or by search, on any page. */
(function () {
  var money = function (v) { return window.Charts.money(v); };
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  async function api(u, o) { var r = await fetch(u, o); var d = await r.json().catch(function () { return {}; }); if (!r.ok) throw new Error(d.detail || ("HTTP " + r.status)); return d; }
  function pct(v, digits) { if (v == null || !isFinite(v)) return "—"; return (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(digits == null ? 2 : digits) + "%"; }
  function tone(v) { return v > 0 ? "up" : v < 0 ? "down" : ""; }
  var LABEL = { buy: "Buy", watch: "Watch", avoid: "Avoid" };

  var dlg, body, timers = [], state = {};
  function clearTimers() { timers.forEach(clearTimeout); timers = []; }

  function ensure() {
    if (dlg) return;
    dlg = document.createElement("dialog");
    dlg.className = "sheet";
    dlg.setAttribute("aria-labelledby", "sheet-title");
    dlg.innerHTML =
      '<div class="sheet-head"><div class="sheet-id"><h2 id="sheet-title">…</h2><span class="sheet-sym mono"></span><span class="verdict" hidden></span></div>' +
      '<button class="icon-btn" type="button" data-close aria-label="Close"><svg viewBox="0 0 24 24" width="18" height="18" fill="none"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg></button></div>' +
      '<div class="sheet-body"></div>';
    document.body.appendChild(dlg);
    body = dlg.querySelector(".sheet-body");
    dlg.querySelector("[data-close]").onclick = function () { dlg.close(); };
    dlg.addEventListener("close", function () { clearTimers(); if (state.onClose) state.onClose(); });
    dlg.addEventListener("click", function (e) { if (e.target === dlg) dlg.close(); });  // click on backdrop
  }

  function setHead(name, symbol, verdict) {
    dlg.querySelector("#sheet-title").textContent = name || symbol;
    dlg.querySelector(".sheet-sym").textContent = symbol;
    var v = dlg.querySelector(".verdict");
    v.hidden = !verdict; v.className = "verdict v-" + (verdict || ""); v.textContent = LABEL[verdict] || "";
  }

  /* ---------- context ---------- */
  function contextHtml(ctx) {
    var day = ctx.day_label || "Today";
    var typ = ctx.typical_move_pct, move = ctx.day_change_pct;
    var ratio = (typ && move != null) ? Math.abs(move) / typ : 0;
    var insight;
    if (ratio >= 2) {
      insight = (move > 0 ? "Already up " : "Down ") + Math.abs(move).toFixed(1) + "% " + (day === "Today" ? "today" : "on " + day) +
        ", about " + Math.round(ratio) + "× a normal day" +
        (move > 0 ? ". Much of the news may already be in the price." : ". A big move against the news; check why before buying.");
    } else if (typ) {
      insight = "Moves about ±" + typ.toFixed(1) + "% on a normal day.";
    }
    var tiles = [
      { k: ctx.symbol, v: move }, { k: "Market", v: ctx.market_change_pct },
    ];
    if (ctx.sector) tiles.push({ k: ctx.sector, v: ctx.sector_change_pct });
    return '<div class="ctx">' +
      '<p class="ctx-price"><span class="mono">' + money(ctx.price) + '</span><span class="ctx-day">' + esc(day) + '</span></p>' +
      '<div class="ctx-tiles">' + tiles.map(function (t) {
        return '<div><span>' + esc(t.k) + '</span><b class="mono ' + tone(t.v) + '">' + pct(t.v, 1) + '</b></div>';
      }).join("") + '</div>' +
      (insight ? '<p class="ctx-insight' + (ratio >= 2 ? " strong" : "") + '">' + esc(insight) + '</p>' : '') +
      '<p class="ctx-range">Past week <b class="mono ' + tone(ctx.week_pct) + '">' + pct(ctx.week_pct, 1) + '</b> · past month <b class="mono ' + tone(ctx.month_pct) + '">' + pct(ctx.month_pct, 1) + '</b></p>' +
    '</div>';
  }

  function whyHtml(pick) {
    if (!pick) return "";
    var src = (pick.sources || []).slice(0, 2).map(function (s) {
      return '<a href="' + esc(s.url) + '" target="_blank" rel="noopener">' + esc(s.outlet || "source") + ' ↗</a>';
    }).join(" ");
    return '<div class="why"><p>' + esc(pick.summary) + '</p>' + (src ? '<p class="why-src">' + src + '</p>' : '') + '</div>';
  }

  /* ---------- buy form ---------- */
  function formHtml(o) {
    return '<form class="buy" novalidate>' +
      '<div class="buy-row"><label class="field grow"><span>How much?</span><input data-f="amount" inputmode="decimal" autocomplete="off" value="' + esc(o.defaultAmount) + '"></label>' +
        '<div class="seg" role="group" aria-label="Amount in"><button type="button" data-m="dollars" aria-pressed="true">$</button><button type="button" data-m="shares" aria-pressed="false">Shares</button></div></div>' +
      '<p class="buy-hint" data-f="est"></p>' +
      '<fieldset class="exit"><legend>Exit plan <span>optional</span></legend>' +
        '<div class="exit-grid">' +
          '<label class="field"><span>Take profit at</span><div class="pct-in"><span>+</span><input data-f="tp" inputmode="decimal" placeholder="10"><span>%</span></div><em data-f="tp-at"></em></label>' +
          '<label class="field"><span>Stop loss at</span><div class="pct-in"><span>−</span><input data-f="sl" inputmode="decimal" placeholder="5"><span>%</span></div><em data-f="sl-at"></em></label>' +
        '</div><p class="buy-hint" data-f="exit-note"></p></fieldset>' +
      (o.marketOpen ? '' : '<p class="note-line">The market is closed. Your order is sent now and fills when it opens at 9:30 AM ET.</p>') +
      '<p class="err" data-f="err" role="alert"></p>' +
      '<div class="buy-actions"><button type="button" class="btn btn-ghost" data-cancel>Cancel</button>' +
        '<button type="submit" class="btn btn-primary grow" data-f="go">Buy ' + esc(o.symbol) + '</button></div>' +
      '<p class="fine-line">' + (o.live ? "Real money" : "Paper account · practice money") + ' · Not investment advice</p>' +
    '</form>';
  }

  function wireForm(ctx, o) {
    var form = body.querySelector("form.buy"), f = function (n) { return form.querySelector('[data-f="' + n + '"]'); };
    var mode = "dollars", price = Number(ctx.price);
    form.querySelectorAll(".seg button").forEach(function (b) {
      b.onclick = function () { mode = b.dataset.m; form.querySelectorAll(".seg button").forEach(function (x) { x.setAttribute("aria-pressed", String(x === b)); }); recalc(); };
    });
    form.querySelector("[data-cancel]").onclick = function () { dlg.close(); };
    ["amount", "tp", "sl"].forEach(function (n) { f(n).addEventListener("input", function () { f("err").textContent = ""; recalc(); }); });

    function num(n) { var v = parseFloat(String(f(n).value).replace(/[$,\s]/g, "")); return isFinite(v) ? v : null; }
    function recalc() {
      var amt = num("amount"), tp = num("tp"), sl = num("sl"), exit = tp != null || sl != null, ok = true, est = "", exitNote = "";
      f("tp-at").textContent = tp != null && tp > 0 ? "sells at " + money(price * (1 + tp / 100)) : "";
      f("sl-at").textContent = sl != null && sl > 0 && sl < 100 ? "sells at " + money(price * (1 - sl / 100)) : "";
      var cost = null;
      if (amt == null || amt <= 0) { ok = false; est = "Enter an amount."; }
      else if (mode === "dollars") {
        if (exit) {
          var whole = Math.floor(amt / price);
          if (whole < 1) { ok = false; exitNote = "An exit plan needs at least one whole share (" + money(price) + "). Raise the amount or clear the exit plan."; }
          else { cost = whole * price; exitNote = "Exit plans need whole shares, so this buys " + whole + " share" + (whole === 1 ? "" : "s") + " (≈" + money(cost) + ")."; }
        } else { cost = amt; est = "≈ " + (amt / price).toFixed(4) + " shares at " + money(price); }
      } else {
        if (exit && amt !== Math.floor(amt)) { ok = false; exitNote = "An exit plan needs a whole number of shares."; }
        cost = amt * price; est = "≈ " + money(cost) + " at " + money(price) + " a share";
      }
      if (sl != null && (sl <= 0 || sl >= 100)) { ok = false; exitNote = "Stop loss must be between 0% and 100%."; }
      if (tp != null && tp <= 0) { ok = false; exitNote = "Take profit must be above 0%."; }
      if (ok && cost != null && o.buyingPower != null && cost > o.buyingPower) {
        ok = false; est = "That's more than your buying power (" + money(o.buyingPower) + ").";
      }
      f("est").textContent = est; f("exit-note").textContent = exitNote;
      f("go").disabled = !ok;
      return { ok: ok, amt: amt, tp: tp, sl: sl };
    }
    recalc();

    form.addEventListener("submit", async function (e) {
      e.preventDefault();
      var c = recalc(); if (!c.ok) return;
      var go = f("go"); go.disabled = true; go.textContent = "Placing order…";
      try {
        var r = await api("/api/picks/buy", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({
          run_id: o.runId, symbol: o.symbol, mode: mode, amount: String(c.amt),
          take_profit_pct: c.tp, stop_loss_pct: c.sl }) });
        var ex = r.execution || {};
        if (ex.status === "submitted" && ex.order_id) {
          if (o.onBought) o.onBought(o.symbol);
          showOrder(ctx, o, ex.order_id, r);
        } else {
          f("err").textContent = ex.message || "The order was not placed."; go.disabled = false; go.textContent = "Buy " + o.symbol;
        }
      } catch (err) {
        f("err").textContent = err.message; go.disabled = false; go.textContent = "Buy " + o.symbol;
      }
    });
  }

  /* ---------- live order view ---------- */
  var DONE = { filled: 1, canceled: 1, expired: 1, rejected: 1, done_for_day: 1 };
  function steps(ord, marketOpen) {
    var s = ord.status || "new", failed = { canceled: 1, expired: 1, rejected: 1 }[s];
    var filled = s === "filled", part = s === "partially_filled";
    var t = function (iso) { return iso ? new Date(iso).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" }) : ""; };
    var rows = [
      { label: "Sent to your broker", done: true, time: t(ord.submitted_at) },
      { label: "Accepted", done: !!(ord.accepted_at || filled || part) && !failed, time: t(ord.accepted_at) },
      { label: failed ? ("Order " + s) : filled ? "Filled" : part ? "Partly filled" : (marketOpen ? "Filling…" : "Waiting for the market to open"),
        done: filled, bad: !!failed, live: !filled && !failed, time: t(ord.filled_at || ord.canceled_at) },
    ];
    return '<ol class="steps">' + rows.map(function (r) {
      return '<li class="' + (r.bad ? "bad" : r.done ? "done" : r.live ? "live" : "") + '"><i></i><span>' + esc(r.label) + '</span><time>' + esc(r.time) + '</time></li>';
    }).join("") + '</ol>';
  }

  function showOrder(ctx, o, orderId, placed) {
    clearTimers();
    body.innerHTML =
      '<div class="order">' +
        '<div class="order-status" data-o="steps"></div>' +
        '<p class="order-fill" data-o="fill"></p>' +
        (placed && placed.note ? '<p class="buy-hint">' + esc(placed.note) + '</p>' : '') +
        '<div class="live-head"><p class="live-price"><span class="mono" data-o="px">' + money(ctx.price) + '</span> <span class="mono" data-o="chg"></span></p><span class="live-dot" data-o="live">Live</span></div>' +
        '<div class="chart-wrap live-chart" data-o="chart"><p class="empty">Loading today’s prices…</p></div>' +
        '<div class="buy-actions"><a class="btn btn-ghost" href="/portfolio">View in portfolio</a><button type="button" class="btn btn-primary grow" data-close2>Done</button></div>' +
      '</div>';
    body.querySelector("[data-close2]").onclick = function () { dlg.close(); };
    var q = function (k) { return body.querySelector('[data-o="' + k + '"]'); };
    var fillPrice = null, polls = 0, priceTimer = null;
    function schedulePrice(ms) { clearTimeout(priceTimer); priceTimer = setTimeout(pollPrice, ms); timers.push(priceTimer); }

    async function pollOrder() {
      try {
        var ord = await api("/api/orders/" + encodeURIComponent(orderId));
        q("steps").innerHTML = steps(ord, state.marketOpen);
        if (ord.filled_avg_price) {
          var firstFill = fillPrice == null;
          fillPrice = Number(ord.filled_avg_price);
          if (firstFill) pollPrice();  // redraw at once with "Your price" and change since buying
          var sh = Number(ord.filled_qty || 0);
          q("fill").innerHTML = "Bought <b>" + (sh % 1 ? sh.toFixed(4) : sh) + " share" + (sh === 1 ? "" : "s") + "</b> at <b class=\"mono\">" + money(fillPrice) + "</b> · " + money(sh * fillPrice) +
            (ord.take_profit || ord.stop_loss ? '<br><span class="exit-legs">' +
              (ord.take_profit ? "Take profit at " + money(ord.take_profit) : "") + (ord.take_profit && ord.stop_loss ? " · " : "") +
              (ord.stop_loss ? "Stop loss at " + money(ord.stop_loss) : "") + "</span>" : "");
        }
        if (!DONE[ord.status]) {
          polls++;
          timers.push(setTimeout(pollOrder, !state.marketOpen ? 30000 : polls < 15 ? 2000 : 5000));
        }
      } catch (err) {
        q("steps").innerHTML = '<p class="err">Could not check the order: ' + esc(err.message) + '</p>';
        timers.push(setTimeout(pollOrder, 10000));
      }
    }

    async function pollPrice() {
      try {
        var d = await api("/api/stock/" + encodeURIComponent(o.symbol) + "/intraday");
        state.marketOpen = d.market_open;
        q("live").textContent = d.market_open ? "Live" : "Market closed";
        q("live").classList.toggle("off", !d.market_open);
        var pts = d.points.map(function (p) { return { t: p.t, v: p.price }; });
        var last = pts.length ? pts[pts.length - 1].v : d.price;
        q("px").textContent = money(last);
        var base = fillPrice || d.previous_close, since = fillPrice ? " since you bought" : " today";
        var ch = base ? (last / base - 1) * 100 : null;
        q("chg").className = "mono " + tone(ch);
        q("chg").textContent = ch == null ? "" : pct(ch) + since;
        var refs = [];
        if (d.previous_close) refs.push({ v: Number(d.previous_close), label: "Previous close " + money(d.previous_close) });
        if (fillPrice) refs.push({ v: fillPrice, label: "Your price " + money(fillPrice), strong: true });
        if (pts.length) {
          window.Charts.lineChart(q("chart"), pts, {
            height: 220, refs: refs, aria: o.symbol + " price today",
            xLabel: function (t) { return new Date(t * 1000).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" }); },
            tip: function (p) { return "<b>" + money(p.v) + "</b><span>" + new Date(p.t * 1000).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" }) + "</span>"; },
          });
        }
        if (d.market_open) schedulePrice(20000);
      } catch (err) {
        q("chart").innerHTML = '<p class="empty">Live prices unavailable right now.</p>';
        schedulePrice(30000);
      }
    }
    pollOrder(); pollPrice();
  }

  /* ---------- open ---------- */
  async function open(symbol, opts) {
    opts = opts || {};
    symbol = String(symbol || "").toUpperCase().trim();
    if (!symbol) return;
    ensure(); clearTimers(); state = { onClose: opts.onClose, marketOpen: false };
    setHead(symbol, symbol, opts.pick && opts.pick.verdict);
    body.innerHTML = '<p class="skel">Loading ' + esc(symbol) + '…</p>';
    if (!dlg.open) dlg.showModal();

    try {
      var results = await Promise.all([
        api("/api/stock/" + encodeURIComponent(symbol) + "/context"),
        opts.view ? Promise.resolve(opts.view) : api("/api/picks").catch(function () { return null; }),
        api("/api/broker/status").catch(function () { return {}; }),
      ]);
      var ctx = results[0], view = results[1], broker = results[2];
      state.marketOpen = ctx.market_open;
      var pick = opts.pick || (view && (view.picks || []).find(function (p) { return p.symbol === symbol; }));
      setHead(ctx.name, symbol, pick && pick.verdict);

      var bought = view && view.bought.indexOf(symbol) !== -1;
      var html = contextHtml(ctx) + whyHtml(pick), action;
      if (!pick || pick.verdict !== "buy") {
        action = '<p class="note-line">' + (pick ? symbol + " is on watch today, not a buy." : symbol + " isn't in today’s picks.") + ' Buying is available for today’s buy picks.</p>';
      } else if (bought) {
        action = '<p class="note-line">You already bought ' + esc(symbol) + ' from today’s picks. <a href="/portfolio">See it in your portfolio</a>.</p>';
      } else if (view && view.is_demo) {
        action = '<p class="note-line"><a href="/login">Create an account</a> and connect a broker to buy.</p>';
      } else if (!view || !view.has_broker) {
        action = '<p class="note-line"><a href="/connect">Connect a broker</a> to buy.</p>';
      } else {
        action = formHtml({ symbol: symbol, defaultAmount: view.default_amount, marketOpen: ctx.market_open, live: broker.mode === "live" });
      }
      body.innerHTML = html + action;
      if (body.querySelector("form.buy")) {
        wireForm(ctx, { symbol: symbol, runId: view.run_id, defaultAmount: view.default_amount,
          buyingPower: broker.buying_power != null ? Number(broker.buying_power) : null,
          live: broker.mode === "live", onBought: opts.onBought });
        body.querySelector('[data-f="amount"]').focus();
      }
    } catch (err) {
      body.innerHTML = '<p class="err">Could not load ' + esc(symbol) + ': ' + esc(err.message) + '</p>';
    }
  }

  window.StockPanel = { open: open };
})();
