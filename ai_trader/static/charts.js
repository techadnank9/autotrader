/* One line chart for the whole app: account value (Portfolio) and live price (order view).
   Single series, so no legend; a crosshair tooltip on hover; optional labeled reference
   lines (previous close, your price). Text wears text colors, never the series color. */
(function () {
  var NS = "http://www.w3.org/2000/svg";
  function el(tag, attrs) { var n = document.createElementNS(NS, tag); for (var k in attrs) n.setAttribute(k, attrs[k]); return n; }
  function money(v) { var n = Number(v); if (!isFinite(n)) n = 0; return "$" + n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }

  /**
   * wrap: element to draw into (its width is used)
   * pts:  [{t: epochSeconds, v: number}]
   * opts: {height, xLabel(t), refs: [{v, label}], tip(p, first) -> html, aria}
   */
  function lineChart(wrap, pts, opts) {
    opts = opts || {};
    var W = Math.max(wrap.clientWidth, 300), H = opts.height || 240;
    var ys = pts.map(function (p) { return p.v; });
    (opts.refs || []).forEach(function (r) { if (isFinite(r.v)) ys.push(r.v); });
    var lo = Math.min.apply(null, ys), hi = Math.max.apply(null, ys);
    var pad = (hi - lo) * 0.12 || Math.max(Math.abs(hi) * 0.01, 1);
    lo -= pad; hi += pad;
    if (Math.min.apply(null, ys) >= 0) lo = Math.max(lo, 0);   // money never charts below zero

    var widest = Math.max(money(lo).length, money(hi).length);
    var M = { t: 14, r: 18, b: 26, l: Math.max(56, Math.round(widest * 6.9) + 16) };
    var iw = W - M.l - M.r, ih = H - M.t - M.b;
    var x = function (i) { return M.l + (pts.length < 2 ? iw : (i / (pts.length - 1)) * iw); };
    var y = function (v) { return M.t + (1 - (v - lo) / (hi - lo)) * ih; };

    var svg = el("svg", { viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": opts.aria || "Price chart" });
    svg.style.height = H + "px";
    for (var g = 0; g <= 3; g++) {
      var gv = lo + (hi - lo) * (g / 3), gy = y(gv);
      svg.appendChild(el("line", { x1: M.l, x2: W - M.r, y1: gy, y2: gy, stroke: "#242A33", "stroke-width": 1 }));
      var tl = el("text", { x: M.l - 10, y: gy + 4, "text-anchor": "end", class: "axis" }); tl.textContent = money(gv); svg.appendChild(tl);
    }
    if (opts.xLabel && pts.length) {
      [0, Math.floor((pts.length - 1) / 2), pts.length - 1].forEach(function (i, k) {
        var t = el("text", { x: x(i), y: H - 6, class: "axis", "text-anchor": k === 0 ? "start" : (k === 2 ? "end" : "middle") });
        t.textContent = opts.xLabel(pts[i].t); svg.appendChild(t);
      });
    }
    (opts.refs || []).forEach(function (r) {
      if (!isFinite(r.v)) return;
      var ry = y(r.v);
      svg.appendChild(el("line", { x1: M.l, x2: W - M.r, y1: ry, y2: ry, stroke: r.strong ? "#A7AFBB" : "#4A5360", "stroke-width": 1 }));
      var lab = el("text", { x: W - M.r, y: ry - 5, "text-anchor": "end", class: "axis ref" }); lab.textContent = r.label; svg.appendChild(lab);
    });
    if (pts.length) {
      var d = pts.map(function (p, i) { return (i ? "L" : "M") + x(i).toFixed(1) + " " + y(p.v).toFixed(1); }).join(" ");
      svg.appendChild(el("path", { d: d, fill: "none", stroke: "#C8F24C", "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
      svg.appendChild(el("circle", { cx: x(pts.length - 1), cy: y(pts[pts.length - 1].v), r: 4.5, fill: "#C8F24C", stroke: "#101317", "stroke-width": 2 }));
    }

    var cross = el("line", { y1: M.t, y2: M.t + ih, stroke: "#8B95A3", "stroke-width": 1, opacity: 0 });
    var dot = el("circle", { r: 5, fill: "#C8F24C", stroke: "#101317", "stroke-width": 2, opacity: 0 });
    var hit = el("rect", { x: M.l, y: M.t, width: iw, height: ih, fill: "transparent" });
    svg.appendChild(cross); svg.appendChild(dot); svg.appendChild(hit);

    wrap.innerHTML = ""; wrap.style.position = "relative";
    wrap.appendChild(svg);
    var tip = document.createElement("div"); tip.className = "tip"; tip.hidden = true; wrap.appendChild(tip);

    function show(evt) {
      if (!pts.length) return;
      var r = svg.getBoundingClientRect();
      var px = (evt.clientX - r.left) * (W / r.width);
      var i = Math.max(0, Math.min(pts.length - 1, Math.round(((px - M.l) / iw) * (pts.length - 1))));
      var cx = x(i), cy = y(pts[i].v);
      cross.setAttribute("x1", cx); cross.setAttribute("x2", cx); cross.setAttribute("opacity", 1);
      dot.setAttribute("cx", cx); dot.setAttribute("cy", cy); dot.setAttribute("opacity", 1);
      tip.innerHTML = opts.tip ? opts.tip(pts[i], pts[0]) : "<b>" + money(pts[i].v) + "</b>";
      tip.style.left = (cx * r.width / W) + "px"; tip.style.top = (cy * r.height / H) + "px"; tip.hidden = false;
    }
    function hide() { cross.setAttribute("opacity", 0); dot.setAttribute("opacity", 0); tip.hidden = true; }
    hit.addEventListener("mousemove", show); hit.addEventListener("mouseleave", hide);
    hit.addEventListener("touchstart", function (e) { show(e.touches[0]); }, { passive: true });
    hit.addEventListener("touchmove", function (e) { show(e.touches[0]); }, { passive: true });
  }

  window.Charts = { lineChart: lineChart, money: money };
})();
