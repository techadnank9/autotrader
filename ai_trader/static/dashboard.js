(function () {
  var $ = function (id) { return document.getElementById(id); };
  var money = function (v) { return window.Charts.money(v); };
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  async function api(u, o) { var r = await fetch(u, o); var d = await r.json().catch(function () { return {}; }); if (!r.ok) throw new Error(d.detail || ("HTTP " + r.status)); return d; }
  var LABEL = { buy: "Buy", watch: "Watch", avoid: "Avoid" };
  var view = null;

  function openStock(symbol, buy) { location.href = "/stock/" + encodeURIComponent(symbol) + (buy ? "?buy=1" : ""); }

  function pickRow(p, v) {
    var bought = v.bought.indexOf(p.symbol) !== -1;
    var action = p.verdict !== "buy" ? "" : bought
      ? '<span class="tag ok">Bought</span>'
      : '<button class="btn btn-primary btn-sm" data-open="' + esc(p.symbol) + '" data-buy="1" type="button">Buy</button>';
    return '<article class="pick v-' + esc(p.verdict) + '">' +
      '<div class="pick-id"><button class="pick-sym mono link-btn" data-open="' + esc(p.symbol) + '" type="button" aria-label="Open ' + esc(p.symbol) + '">' + esc(p.symbol) + '</button>' +
        '<span class="verdict v-' + esc(p.verdict) + '">' + (LABEL[p.verdict] || "Watch") + '</span></div>' +
      '<div class="pick-body"><p class="pick-sum">' + esc(p.summary) + '</p></div>' +
      '<div class="pick-act">' + action + '</div>' +
    '</article>';
  }

  function renderPicks(v) {
    view = v;
    $("picks-sub").textContent = "Updated " + new Date(v.updated_at * 1000).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" }) +
      (v.articles_read ? " · from " + v.articles_read + " news articles" : "");
    $("refresh").hidden = false;
    if (v.status !== "ok" || !v.picks.length) {
      $("picks").innerHTML = '<p class="empty">' + esc(v.message || "Today’s picks aren’t ready yet. Check back shortly.") + '</p>';
      return;
    }
    var buys = v.picks.filter(function (p) { return p.verdict === "buy"; }).length;
    $("picks").innerHTML =
      '<p class="picks-lead">' + (buys ? buys + (buys === 1 ? " stock looks" : " stocks look") + " worth buying today."
        : "No strong buys today. Nothing in today’s news clears the bar, so here is what we’re watching.") + '</p>' +
      '<div class="pick-list">' + v.picks.map(function (p) { return pickRow(p, v); }).join("") + '</div>';
    document.querySelectorAll("[data-open]").forEach(function (b) { b.onclick = function () { openStock(b.dataset.open, b.dataset.buy); }; });
  }

  async function loadPicks(force) {
    $("refresh").disabled = true;
    if (!view || force) {
      $("picks").innerHTML = '<div class="picks-wait"><i class="pulse"></i><div><p>Reading today’s market news…</p>' +
        '<p class="panel-sub">This takes about a minute when the picks need updating.</p></div></div>';
    }
    try { renderPicks(await api(force ? "/api/picks/refresh" : "/api/picks", force ? { method: "POST" } : undefined)); }
    catch (err) { $("picks").innerHTML = '<p class="empty">Could not load today’s picks. ' + esc(err.message) + '</p>'; }
    finally { $("refresh").disabled = false; }
  }

  async function loadAccount() {
    try {
      var p = await api("/api/portfolio");
      if (!p.connected) { $("acct").innerHTML = '<p class="empty"><a href="/connect">Connect a broker</a> to see your money here.</p>'; return; }
      if (p.error) { $("acct").innerHTML = '<p class="empty">' + esc(p.error) + '</p>'; return; }
      var tile = function (k, v) { return '<div><dt>' + esc(k) + '</dt><dd>' + v + '</dd></div>'; };
      $("acct").innerHTML = '<dl class="tiles">' +
        tile("Total value", money(p.total_value)) + tile("Cash", money(p.cash)) +
        tile("Invested", money(p.invested)) + tile("Open positions", String(p.positions.length)) + '</dl>';
    } catch (err) { $("acct").innerHTML = '<p class="empty">Could not load your account.</p>'; }
  }

  async function boot() {
    var user = await window.Shell.mount({ active: "picks" });
    if (!user) return;
    if (user.is_demo) $("demo-note").hidden = false;
    var welcome = new URLSearchParams(location.search).get("welcome");
    if (welcome) {
      $("welcome").innerHTML = "You’re set up. Your <b>" + (welcome === "robinhood" ? "Robinhood" : "Alpaca") + "</b> account is connected. Tap <b>Buy</b> on any pick to place an order.";
      $("welcome").hidden = false; history.replaceState(null, "", "/app");
    }
    $("refresh").onclick = function () { loadPicks(true); };
    loadPicks(false); loadAccount();
    if (!user.is_demo) api("/api/telegram").then(function (t) { $("tg-nudge").hidden = !(t.available && !t.connected); }).catch(function () {});
  }
  boot();
})();
