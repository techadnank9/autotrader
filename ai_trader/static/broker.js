/* Brokerage connect UI, shared by /connect (onboarding) and /profile. */
(function () {
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  function money(v) { var n = Number(v); if (!isFinite(n)) n = 0; return "$" + n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
  async function api(u, o) { var r = await fetch(u, o); var d = await r.json().catch(function () { return {}; }); if (!r.ok) throw new Error(d.detail || ("HTTP " + r.status)); return d; }

  /**
   * opts.root      element that holds the UI
   * opts.note      element for the ?ok= / ?error= banner (optional)
   * opts.returnTo  "/connect" or "/profile": where Robinhood's sign-in comes back to
   * opts.onChange  called after a successful connect or disconnect
   */
  function mount(opts) {
    var root = opts.root, mode = "paper";
    var q = function (sel) { return root.querySelector(sel); };

    if (opts.note) {
      var p = new URLSearchParams(location.search);
      if (p.get("ok")) { opts.note.textContent = p.get("ok"); opts.note.className = "note ok"; opts.note.hidden = false; }
      else if (p.get("error")) { opts.note.textContent = p.get("error"); opts.note.className = "note bad"; opts.note.hidden = false; }
      if (p.get("ok") || p.get("error")) history.replaceState(null, "", location.pathname);
    }

    async function render() {
      var b;
      try { b = await api("/api/broker/status"); }
      catch (e) { root.innerHTML = '<p class="empty">Could not check your brokerage.</p>'; return; }

      if (b.demo) {
        root.innerHTML = '<p class="empty">Demo sessions are shared between visitors, so they cannot hold brokerage keys. <a href="/login">Create an account</a> to connect a broker.</p>';
        return;
      }
      if (b.saved) {
        var sv = b.saved, isRh = b.provider === "robinhood";
        var label = isRh ? "Robinhood · " + esc(sv.masked_key_id) + " · real money"
                         : "Alpaca " + esc(sv.live ? "live" : "paper") + ' · key <span class="mono">' + esc(sv.masked_key_id) + '</span>';
        root.innerHTML =
          '<div class="bk-live">' +
            '<span class="pill ' + (b.connected ? "on" : "warn") + '"><i></i>' + (b.connected ? "Connected" : "Needs attention") + '</span>' +
            '<span class="conn-detail">' + label + (b.connected && b.buying_power ? ' · buying power ' + money(b.buying_power) : '') + '</span>' +
            '<button class="btn btn-ghost btn-sm" data-act="off" type="button">Disconnect</button>' +
          '</div>' +
          (b.connected ? '' : '<p class="err">' + esc(b.message || "") + '</p>') +
          (isRh && !b.live_trading_allowed ? '<p class="note warn">Robinhood always trades real money, and live trading is not enabled on this platform yet, so approved calls will not place orders. You can still view your account.</p>' : '');
        q('[data-act="off"]').onclick = async function () {
          if (!confirm("Disconnect " + (isRh ? "Robinhood" : "Alpaca") + "? Approved calls will stop placing orders until you reconnect.")) return;
          this.disabled = true;
          try { await api("/api/broker/" + (isRh ? "robinhood" : "alpaca"), { method: "DELETE" }); } catch (e) {}
          await render(); if (opts.onChange) opts.onChange({ connected: false });
        };
        return;
      }
      if (!b.can_connect) {
        root.innerHTML = '<p class="empty">Connecting a brokerage is not available right now. Please try again later.</p>';
        return;
      }

      var rhHref = "/auth/robinhood?return_to=" + encodeURIComponent(opts.returnTo || "/profile");
      root.innerHTML =
        '<div class="choices">' +
        '<div class="choice"><h3>Alpaca</h3>' +
        '<p class="help" style="margin-top:8px">Free paper account with practice money. The fastest way to start.</p>' +
        '<form class="bk-form" novalidate>' +
          '<div class="mode" role="group" aria-label="Account type">' +
            '<button type="button" data-m="paper" aria-pressed="true">Paper</button>' +
            '<button type="button" data-m="live"' + (b.live_trading_allowed ? '' : ' disabled title="Live trading is not enabled on this platform"') + '>Live</button>' +
          '</div>' +
          '<label class="field"><span>API key ID</span><input data-f="key" autocomplete="off" spellcheck="false" placeholder="PK…"></label>' +
          '<label class="field"><span>Secret key</span><input data-f="secret" type="password" autocomplete="off" spellcheck="false"></label>' +
          '<label class="check" data-f="live-wrap" hidden><input type="checkbox" data-f="confirm"> <span>I understand approved calls will place orders with real money.</span></label>' +
          '<p class="err" data-f="err" role="alert"></p>' +
          '<button class="btn btn-primary" data-f="go" type="submit">Connect Alpaca</button>' +
          '<p class="help">Find these at <a href="https://app.alpaca.markets" target="_blank" rel="noopener">app.alpaca.markets</a> → Paper account → API Keys. ' +
          'We check the keys with Alpaca before saving. Your secret is encrypted and never shown again.</p>' +
        '</form></div>' +
        '<div class="choice"><h3>Robinhood</h3>' +
          '<p class="help" style="margin-top:8px">Sign in on Robinhood\'s own page; we never see your password. ' +
          'Orders go to your <b>Agentic account</b>, which you open and fund on robinhood.com from a computer. ' +
          'Robinhood has no paper trading: every order is real money.</p>' +
          '<a class="btn btn-ghost" href="' + rhHref + '" style="margin-top:16px">Connect Robinhood</a>' +
        '</div></div>' +
        '<p class="help">Connecting one broker disconnects the other, so there is never doubt about which account an approval spends from.</p>';

      var f = function (name) { return q('[data-f="' + name + '"]'); };
      Array.prototype.forEach.call(root.querySelectorAll(".mode button"), function (btn) {
        btn.onclick = function () {
          if (btn.disabled) return;
          mode = btn.dataset.m;
          Array.prototype.forEach.call(root.querySelectorAll(".mode button"), function (o) { o.setAttribute("aria-pressed", String(o === btn)); });
          f("live-wrap").hidden = mode !== "live";
        };
      });
      ["key", "secret"].forEach(function (n) { f(n).addEventListener("input", function () { f("err").textContent = ""; }); });

      q("form").addEventListener("submit", async function (e) {
        e.preventDefault();
        var key = f("key").value.trim(), secret = f("secret").value.trim();
        if (!key) { f("err").textContent = "Enter your API key ID."; f("key").focus(); return; }
        if (!secret) { f("err").textContent = "Enter your secret key."; f("secret").focus(); return; }
        if (mode === "live" && !f("confirm").checked) { f("err").textContent = "Confirm that live orders use real money."; return; }
        var go = f("go"); go.disabled = true; go.textContent = "Checking with Alpaca…";
        try {
          await api("/api/broker/alpaca", { method: "POST", headers: { "content-type": "application/json" },
            body: JSON.stringify({ key_id: key, secret_key: secret, live: mode === "live", confirm_live: f("confirm").checked }) });
          f("secret").value = "";
          if (opts.onChange) { opts.onChange({ connected: true, provider: "alpaca" }); }
          await render();
        } catch (err) {
          f("err").textContent = err.message; go.disabled = false; go.textContent = "Connect Alpaca";
        }
      });
    }
    render();
    return { refresh: render };
  }

  window.BrokerConnect = { mount: mount };
})();
