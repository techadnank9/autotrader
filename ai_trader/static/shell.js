/* App frame: left navigation, search at the top, account at the top right.
   Familiar placement on purpose; on phones the navigation becomes a bottom tab bar. */
(function () {
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  async function api(u, o) { var r = await fetch(u, o); var d = await r.json().catch(function () { return {}; }); if (!r.ok) throw new Error(d.detail || ("HTTP " + r.status)); return d; }

  var ICON = {
    picks: '<path d="M4 17l5-5 4 4 7-8" /><path d="M15 8h5v5" />',
    portfolio: '<rect x="3.5" y="7" width="17" height="12.5" rx="2.5" /><path d="M8.5 7V5.5A1.5 1.5 0 0 1 10 4h4a1.5 1.5 0 0 1 1.5 1.5V7M3.5 12.5h17" />',
    profile: '<circle cx="12" cy="8.5" r="3.5" /><path d="M5 19.5c1.2-3.3 3.9-5 7-5s5.8 1.7 7 5" />',
  };
  var NAV = [
    { id: "picks", href: "/app", label: "Today's picks" },
    { id: "portfolio", href: "/portfolio", label: "Portfolio" },
    { id: "profile", href: "/profile", label: "Profile" },
  ];
  function icon(id) { return '<svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + ICON[id] + '</svg>'; }
  function links(active, cls) {
    return NAV.map(function (n) {
      return '<a class="' + cls + '" href="' + n.href + '"' + (n.id === active ? ' aria-current="page"' : '') + '>' + icon(n.id) + '<span>' + n.label + '</span></a>';
    }).join("");
  }

  async function mount(opts) {
    var me;
    try { me = await api("/api/auth/me"); } catch (e) { location.href = "/login"; return; }
    if (!me.authenticated) { location.href = "/login"; return; }
    var user = me.user, initial = (user.email || "?").charAt(0).toUpperCase();

    document.body.classList.add("app");
    var side = document.createElement("aside");
    side.className = "side";
    side.innerHTML =
      '<a class="brand" href="/app"><svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="1.5" y="1.5" width="21" height="21" rx="6.5" stroke="#C8F24C" stroke-width="1.5"/><path d="M6.5 15.5L10 11l3 3 4.5-6" stroke="#C8F24C" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>AI Trader</a>' +
      '<nav class="side-nav" aria-label="Main">' + links(opts.active, "side-link") + '</nav>';

    var top = document.createElement("header");
    top.className = "top";
    top.innerHTML =
      '<form class="search" role="search"><svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4-4"/></svg>' +
        '<input type="search" placeholder="Search a stock, e.g. NVDA" aria-label="Search a stock" autocomplete="off" spellcheck="false">' +
        '<ul class="suggest" role="listbox" hidden></ul></form>' +
      '<div class="acct"><button class="acct-btn" type="button" aria-haspopup="menu" aria-expanded="false"><span class="avatar">' + esc(initial) + '</span><span class="acct-mail">' + esc(user.email) + '</span></button>' +
        '<div class="acct-menu" role="menu" hidden><p class="acct-who">' + esc(user.email) + (user.is_demo ? '<span>Demo session</span>' : '') + '</p>' +
          '<a role="menuitem" href="/profile">Profile</a><button role="menuitem" type="button" data-signout>Sign out</button></div></div>';

    var tabs = document.createElement("nav");
    tabs.className = "tabbar"; tabs.setAttribute("aria-label", "Main");
    tabs.innerHTML = links(opts.active, "tab-link");

    var main = document.querySelector("main");
    document.body.insertBefore(side, main);
    document.body.insertBefore(top, main);
    document.body.appendChild(tabs);

    // account menu
    var btn = top.querySelector(".acct-btn"), menu = top.querySelector(".acct-menu");
    function setMenu(open) { menu.hidden = !open; btn.setAttribute("aria-expanded", String(open)); }
    btn.onclick = function (e) { e.stopPropagation(); setMenu(menu.hidden); };
    document.addEventListener("click", function (e) { if (!menu.hidden && !menu.contains(e.target)) setMenu(false); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") setMenu(false); });
    top.querySelector("[data-signout]").onclick = async function () {
      try { await api("/api/auth/logout", { method: "POST" }); } catch (e) {}
      location.href = "/login";
    };

    // search: every US stock and ETF; your holdings and today's picks are labelled
    var form = top.querySelector(".search"), input = form.querySelector("input"), list = form.querySelector(".suggest");
    var notes = {}, seq = 0, timer = null, hits = [], active = -1;
    Promise.all([api("/api/picks").catch(function () { return null; }), api("/api/portfolio").catch(function () { return null; })])
      .then(function (r) {
        ((r[0] && r[0].picks) || []).forEach(function (p) { notes[p.symbol] = { buy: "Buy today", watch: "Watch", avoid: "Avoid" }[p.verdict] || ""; });
        ((r[1] && r[1].positions) || []).forEach(function (p) { notes[p.symbol] = "You own this"; });
      });
    function openStock(sym) { list.hidden = true; location.href = "/stock/" + encodeURIComponent(sym); }
    function paint() {
      list.innerHTML = hits.map(function (h, i) {
        var note = h.restricted ? "Not available" : (notes[h.symbol] || (h.type === "etf" ? "ETF" : ""));
        return '<li role="option" id="sg-' + i + '" aria-selected="' + (i === active) + '" data-s="' + esc(h.symbol) + '"><span class="sg-l"><b class="mono">' + esc(h.symbol) + '</b><em>' + esc(h.name || "") + '</em></span><span>' + esc(note) + '</span></li>';
      }).join("");
      list.hidden = !hits.length;
      input.setAttribute("aria-activedescendant", active >= 0 ? "sg-" + active : "");
    }
    input.addEventListener("input", function () {
      clearTimeout(timer);
      var q = input.value.trim(), my = ++seq;
      if (!q) { hits = []; paint(); return; }
      timer = setTimeout(function () {
        api("/api/search?q=" + encodeURIComponent(q)).then(function (d) {
          if (my !== seq) return;
          hits = (d.results || []).slice(0, 8); active = hits.length ? 0 : -1; paint();
        }).catch(function () {});
      }, 180);
    });
    input.addEventListener("keydown", function (e) {
      if (list.hidden || !hits.length) return;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault(); active = (active + (e.key === "ArrowDown" ? 1 : hits.length - 1)) % hits.length; paint();
      } else if (e.key === "Escape") { list.hidden = true; }
    });
    list.addEventListener("mousedown", function (e) { var li = e.target.closest("li"); if (li) { e.preventDefault(); openStock(li.dataset.s); } });
    input.addEventListener("blur", function () { setTimeout(function () { list.hidden = true; }, 120); });
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      if (!list.hidden && hits[active]) return openStock(hits[active].symbol);
      var q = input.value.trim().toUpperCase().replace(/[^A-Z.]/g, "");
      if (q) openStock(q);
    });

    return user;
  }

  window.Shell = { mount: mount };
})();
