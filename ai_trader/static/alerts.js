(function () {
  var $ = function (id) { return document.getElementById(id); };
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  async function api(u, o) { var r = await fetch(u, o); var d = await r.json().catch(function () { return {}; }); if (!r.ok) throw new Error(d.detail || ("HTTP " + r.status)); return d; }
  var state = null, link = null, waiting = false, poll = null, linkTimer = null;

  var im = null, imPoll = null;
  function setPill(el, on, label) { el.className = "pill" + (on ? " on" : ""); el.innerHTML = "<i></i>" + (label || (on ? "Connected" : "Not connected")); }
  function pill(s) {
    setPill($("tg-pill2"), s.connected);
    var any = (s && s.connected) || (im && im.connected);
    setPill($("tg-pill"), any, any ? "Alerts on" : "Alerts off");
  }

  function sw(key, on, label, sub) {
    return '<div class="sw-row"><div><b>' + label + '</b><span>' + sub + '</span></div>' +
      '<button class="switch" type="button" role="switch" aria-checked="' + !!on + '" data-pref="' + key + '" aria-label="' + label + '"><i></i></button></div>';
  }

  function render() {
    var s = state, el = $("tg");
    pill(s);
    if (s.is_demo) { el.innerHTML = '<p class="note-line">Alerts go to your own Telegram, so they need an account. <a href="/login">Create one</a> in a few seconds.</p>'; return; }
    if (!s.available) { el.innerHTML = '<p class="note-line">Telegram alerts aren’t available right now. Check back soon.</p>'; return; }

    if (s.connected) {
      stopPoll();
      var who = s.username ? "@" + s.username : (s.name || "your Telegram");
      var since = s.linked_at ? new Date(s.linked_at * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }) : "";
      el.innerHTML =
        '<div class="tg-conn"><span class="tg-avatar">' + esc((s.name || s.username || "T").charAt(0).toUpperCase()) + '</span>' +
          '<div><b>' + esc(who) + '</b><span>Connected' + (since ? " since " + esc(since) : "") + '</span></div></div>' +
        '<div class="sw-list">' +
          sw("picks", s.prefs.picks, "Today's picks", "Weekdays around 9 AM ET") +
          sw("orders", s.prefs.orders, "Order updates", "Sent, filled and canceled") +
        '</div>' +
        '<p class="err" id="tg-err" role="alert"></p>' +
        '<div class="tg-actions">' +
          '<button class="btn btn-primary" type="button" id="tg-test">Send a test message</button>' +
          (s.bot ? '<a class="btn btn-ghost" href="https://t.me/' + esc(s.bot) + '" target="_blank" rel="noopener">Open Telegram</a>' : '') +
          '<button class="btn btn-quiet" type="button" id="tg-off">Disconnect</button>' +
        '</div>';
      el.querySelectorAll("[data-pref]").forEach(function (b) {
        b.onclick = async function () {
          var on = b.getAttribute("aria-checked") !== "true", body = {};
          body[b.dataset.pref] = on; b.setAttribute("aria-checked", String(on));
          try { state = await api("/api/telegram/prefs", { method: "PATCH", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }); }
          catch (err) { b.setAttribute("aria-checked", String(!on)); $("tg-err").textContent = err.message; }
        };
      });
      $("tg-test").onclick = async function () {
        var b = this; b.disabled = true; b.textContent = "Sending…"; $("tg-err").textContent = "";
        try { await api("/api/telegram/test", { method: "POST" }); b.textContent = "Sent. Check Telegram"; }
        catch (err) { $("tg-err").textContent = err.message; b.textContent = "Send a test message"; }
        setTimeout(function () { b.disabled = false; b.textContent = "Send a test message"; }, 4000);
      };
      $("tg-off").onclick = async function () {
        if (!confirm("Disconnect Telegram? You'll stop getting alerts.")) return;
        try { await api("/api/telegram/link", { method: "DELETE" }); await refresh(); } catch (err) { $("tg-err").textContent = err.message; }
      };
      return;
    }

    el.innerHTML =
      '<ol class="tg-steps">' +
        '<li><b>Get Telegram</b><span>Free on <a href="https://apps.apple.com/app/telegram-messenger/id686449807" target="_blank" rel="noopener">iPhone</a>, ' +
          '<a href="https://play.google.com/store/apps/details?id=org.telegram.messenger" target="_blank" rel="noopener">Android</a> and ' +
          '<a href="https://desktop.telegram.org" target="_blank" rel="noopener">computer</a>. Skip this if you have it.</span></li>' +
        '<li><b>Tap Connect Telegram</b><span>It opens the AI Trader bot in Telegram.</span></li>' +
        '<li><b>Tap Start in Telegram</b><span>That’s it. This page switches to Connected by itself.</span></li>' +
      '</ol>' +
      '<p class="err" id="tg-err" role="alert"></p>' +
      '<div class="tg-connect">' +
        (link ? '<a class="btn btn-primary" id="tg-go" href="' + esc(link.url) + '" target="_blank" rel="noopener">Connect Telegram</a>'
              : '<button class="btn btn-primary" type="button" disabled>Preparing your link…</button>') +
        (waiting ? '<p class="tg-wait"><span class="live-dot">Waiting for you to tap Start in Telegram</span></p>' : '') +
      '</div>' +
      (link && link.qr_svg ? '<div class="tg-qr"><div class="qr">' + link.qr_svg + '</div><p><b>On a computer?</b> Scan this with your phone’s camera to connect Telegram on your phone.</p></div>' : '');
    var go = $("tg-go");
    if (go) go.onclick = function () { waiting = true; startPoll(); setTimeout(render, 50); };
  }

  async function getLink() {
    clearTimeout(linkTimer);
    try { link = await api("/api/telegram/link", { method: "POST" }); }
    catch (err) { link = null; render(); var e = $("tg-err"); if (e) e.textContent = err.message; return; }
    render();
    linkTimer = setTimeout(getLink, Math.max(60, link.expires_in - 120) * 1000); // renew before it expires
  }

  function startPoll() { stopPoll(); var n = 0; poll = setInterval(function () { if (document.hidden) return; if (++n > 400) return stopPoll(); refresh(); }, 3000); }
  function stopPoll() { if (poll) clearInterval(poll); poll = null; }

  async function refresh() {
    try {
      var was = state && state.connected;
      state = await api("/api/telegram");
      if (!state.connected && state.available && !poll) startPoll(); // a QR scan never touches this page
      if (!state.connected && state.available && !link) { render(); return getLink(); }
      if (!was || state.connected !== was) render(); else pill(state);
    } catch (err) { $("tg").innerHTML = '<p class="empty">' + esc(err.message) + '</p>'; }
  }

  async function preview() {
    try {
      var d = await api("/api/picks");
      var buy = (d.picks || []).filter(function (p) { return p.verdict === "buy"; })[0];
      var watch = (d.picks || []).filter(function (p) { return p.verdict === "watch"; }).map(function (p) { return p.symbol; }).slice(0, 4);
      $("pv-day").textContent = new Date().toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
      if (buy) {
        $("pv-sym").textContent = buy.symbol;
        $("pv-sum").textContent = (buy.summary || "").split(/(?<=[a-z]{2}\.)\s/)[0];
        $("pv-buy").textContent = "Buy $" + Number(d.budget || 100).toFixed(0) + " of " + buy.symbol;
      }
      if (watch.length) $("pv-rest").textContent = "Watch: " + watch.join(", ");
    } catch (e) {}
  }

  /* ---------- iMessage ---------- */
  function renderIm() {
    var s = im, el = $("im");
    $("im-sec").hidden = !(s.available || s.is_demo) || s.is_demo;
    if (s.is_demo || !s.available) return;
    setPill($("im-pill"), s.connected, s.connected ? "Connected" : (s.waiting ? "Waiting for your reply" : "Not connected"));
    if (state) pill(state);
    if (s.connected) {
      el.innerHTML =
        '<div class="tg-conn"><span class="tg-avatar im">' + '\u2709' + '</span><div><b class="mono">' + esc(s.phone) + '</b>' +
          '<span>Texts come from ' + esc(s.line || "AI Trader") + '. Save it as AI Trader.</span></div></div>' +
        '<div class="sw-list">' + sw("picks", s.prefs.picks, "Today's picks", "Weekdays around 9 AM ET, with a Buy poll") +
          sw("orders", s.prefs.orders, "Order updates", "Sent, filled and canceled") + '</div>' +
        '<p class="err" id="im-err" role="alert"></p>' +
        '<div class="tg-actions"><button class="btn btn-primary" type="button" id="im-test">Text me today\u2019s picks</button>' +
          '<button class="btn btn-quiet" type="button" id="im-off">Disconnect</button></div>';
      el.querySelectorAll("[data-pref]").forEach(function (b) {
        b.onclick = async function () {
          var on = b.getAttribute("aria-checked") !== "true", body = {}; body[b.dataset.pref] = on; b.setAttribute("aria-checked", String(on));
          try { im = await api("/api/imessage/prefs", { method: "PATCH", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }); }
          catch (err) { b.setAttribute("aria-checked", String(!on)); $("im-err").textContent = err.message; }
        };
      });
      $("im-test").onclick = async function () {
        var b = this; b.disabled = true; b.textContent = "Sending\u2026"; $("im-err").textContent = "";
        try { await api("/api/imessage/test", { method: "POST" }); b.textContent = "Sent. Check Messages"; }
        catch (err) { $("im-err").textContent = err.message; }
        setTimeout(function () { b.disabled = false; b.textContent = "Text me today\u2019s picks"; }, 4000);
      };
      $("im-off").onclick = async function () {
        if (!confirm("Turn off iMessage alerts?")) return;
        try { await api("/api/imessage", { method: "DELETE" }); await refreshIm(); } catch (err) { $("im-err").textContent = err.message; }
      };
      return;
    }
    if (s.waiting) {
      el.innerHTML =
        '<p class="im-wait-t">We texted <b class="mono">' + esc(s.phone) + '</b> from ' + esc(s.line || "AI Trader") + '.</p>' +
        '<p class="tg-wait"><span class="live-dot">Reply YES in Messages to finish</span></p>' +
        '<p class="note-line">Nothing arrived? Make sure it\u2019s the number your iPhone uses for iMessage: Settings \u203a Messages \u203a Send &amp; Receive, and pick your phone number under \u201cStart new conversations from\u201d.</p>' +
        '<p class="err" id="im-err" role="alert"></p>' +
        '<div class="tg-actions"><button class="btn btn-ghost" type="button" id="im-again">Text me again</button>' +
          '<button class="btn btn-quiet" type="button" id="im-change">Use a different number</button></div>';
      $("im-again").onclick = async function () {
        var b = this; b.disabled = true;
        try { await api("/api/imessage/resend", { method: "POST" }); b.textContent = "Sent again"; }
        catch (err) { $("im-err").textContent = err.message; }
        setTimeout(function () { b.disabled = false; b.textContent = "Text me again"; }, 5000);
      };
      $("im-change").onclick = async function () { try { await api("/api/imessage", { method: "DELETE" }); } catch (e) {} refreshIm(); };
      startImPoll();
      return;
    }
    el.innerHTML =
      '<p class="panel-sub">Get picks as a normal text on your iPhone. Vote Buy or reply YES, and the order is placed.</p>' +
      '<form class="im-form" novalidate><label class="field grow"><span>Your iPhone number</span>' +
        '<input id="im-phone" type="tel" inputmode="tel" autocomplete="tel" placeholder="(415) 555-0123"></label>' +
        '<button class="btn btn-primary" type="submit" id="im-go">Text me</button></form>' +
      '<p class="err" id="im-err" role="alert"></p>' +
      '<p class="fine-line">We\u2019ll text you once to confirm. Reply YES and you\u2019re set. Reply STOP any time.</p>';
    el.querySelector("form").onsubmit = async function (e) {
      e.preventDefault();
      var go = $("im-go"), phone = $("im-phone").value; go.disabled = true; go.textContent = "Texting\u2026"; $("im-err").textContent = "";
      try { im = await api("/api/imessage/start", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ phone: phone }) }); im._raw = phone; renderIm(); }
      catch (err) { $("im-err").textContent = err.message; go.disabled = false; go.textContent = "Text me"; }
    };
  }
  function startImPoll() { if (imPoll) return; var n = 0; imPoll = setInterval(async function () { if (document.hidden) return; if (++n > 300) { clearInterval(imPoll); imPoll = null; return; } var raw = im && im._raw; try { var nx = await api("/api/imessage"); nx._raw = raw; var changed = nx.connected !== im.connected || nx.waiting !== im.waiting; im = nx; if (changed) { clearInterval(imPoll); imPoll = null; renderIm(); } } catch (e) {} }, 3000); }
  async function refreshIm() { try { im = await api("/api/imessage"); renderIm(); } catch (e) {} }

  async function boot() {
    var user = await window.Shell.mount({ active: "alerts" });
    if (!user) return;
    refreshIm();
    await refresh();
    preview();
    window.addEventListener("focus", function () { if (state && !state.connected) refresh(); });
  }
  boot();
})();
