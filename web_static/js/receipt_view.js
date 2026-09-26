// ── ForgeMesh receipt view ───────────────────────────────────────────────────
// One data-driven renderer for the AnswerEnvelope receipt, used by two surfaces:
//   • the standalone /runs/{id} page (the target of every receipt_url link)
//   • the in-app Receipt pane (replaces the old Proof tab)
//
// Pure presentation: give it an envelope and a mount element, it renders the
// answer + the four-block traced-to-source receipt. Only populated fields render;
// nothing null is presented as if it were proven (mirrors render_receipt on the
// server). Everything is scoped under `.rcpt-root` so it never collides with the
// surrounding app styles.
(function (global) {
  "use strict";

  // ── scoped styles (injected once) ──────────────────────────────────────────
  var CSS = `
  @font-face{font-family:'FM Inter';src:url('/fonts/inter-400.woff2') format('woff2');font-weight:400;font-display:swap}
  @font-face{font-family:'FM Inter';src:url('/fonts/inter-500.woff2') format('woff2');font-weight:500;font-display:swap}
  @font-face{font-family:'FM Inter';src:url('/fonts/inter-600.woff2') format('woff2');font-weight:600;font-display:swap}
  @font-face{font-family:'FM Inter';src:url('/fonts/inter-700.woff2') format('woff2');font-weight:700;font-display:swap}
  @font-face{font-family:'FM Mono';src:url('/fonts/jetbrains-mono-400.woff2') format('woff2');font-weight:400;font-display:swap}
  @font-face{font-family:'FM Mono';src:url('/fonts/jetbrains-mono-600.woff2') format('woff2');font-weight:600;font-display:swap}
  .rcpt-root{
    --r-ground:#0C0E0D; --r-ground2:#0F1211; --r-panel:#161918; --r-panel2:#1C201E; --r-panel3:#232826;
    --r-line:#2A302D; --r-line-soft:#232826; --r-tear:#343B38;
    --r-ink:#EFEFEA; --r-dim:#A6A9A1; --r-faint:#6B706A;
    --r-mint:#1F6B53; --r-mint-bright:#3A9474; --r-mint-soft:#5CB394;
    --r-tint:rgba(58,148,116,.12); --r-tint2:rgba(58,148,116,.20);
    --r-warn:#F2B43C; --r-warn-tint:rgba(242,180,60,.13); --r-warn-line:rgba(242,180,60,.35);
    --r-sans:'FM Inter','Inter',ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
    --r-mono:'FM Mono','JetBrains Mono',ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    font-family:var(--r-sans); color:var(--r-ink); line-height:1.55; text-align:left;
  }
  .rcpt-root *{box-sizing:border-box}
  .rcpt-card{background:linear-gradient(180deg,var(--r-panel2),var(--r-panel));border:1px solid var(--r-line);
    border-radius:10px;overflow:hidden;box-shadow:0 24px 60px -34px rgba(0,0,0,.8)}
  .rcpt-tear{height:0;border-top:1px dashed var(--r-tear)}
  .rcpt-pad{padding:22px 26px}
  @media (max-width:560px){.rcpt-pad{padding:16px 15px}}
  .rcpt-mast{display:flex;align-items:center;gap:11px}
  .rcpt-mark{width:26px;height:26px;flex:none;display:block}
  .rcpt-word{font-weight:700;font-size:15px;letter-spacing:-.01em}
  .rcpt-word b{color:var(--r-mint-soft)}
  .rcpt-mast .sp{flex:1}
  .rcpt-stamp{font-family:var(--r-mono);font-size:10.5px;letter-spacing:.04em;color:var(--r-faint);text-align:right;line-height:1.5}
  .rcpt-stamp b{color:var(--r-dim);font-weight:500}
  .rcpt-headrow{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-top:20px}
  .rcpt-kicker{font-family:var(--r-mono);font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--r-faint)}
  .rcpt-h1{font-size:22px;line-height:1.24;letter-spacing:-.015em;margin:7px 0 0;font-weight:650;text-wrap:balance}
  .rcpt-pill{display:inline-flex;align-items:center;gap:7px;flex:none;font-size:10.5px;font-weight:600;letter-spacing:.06em;
    text-transform:uppercase;color:var(--r-mint-soft);background:var(--r-tint);border:1px solid var(--r-tint2);border-radius:999px;padding:4px 11px}
  .rcpt-pill .dot{width:6px;height:6px;border-radius:50%;background:var(--r-mint-bright);box-shadow:0 0 8px var(--r-mint-bright)}
  .rcpt-runid{font-family:var(--r-mono);font-size:12px;color:var(--r-mint-soft);background:var(--r-panel3);
    border:1px solid var(--r-line);border-radius:6px;padding:3px 9px;cursor:pointer}
  .rcpt-runid:hover{border-color:var(--r-mint)}
  .rcpt-cap{font-family:var(--r-mono);font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--r-faint);margin:0 0 12px}
  .rcpt-scroll{overflow:auto;max-height:320px;border:1px solid var(--r-line);border-radius:8px;background:var(--r-ground2)}
  table.rcpt-answer{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
  table.rcpt-answer th{text-align:left;font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--r-faint);
    font-weight:600;padding:8px 13px;white-space:nowrap;position:sticky;top:0;background:var(--r-ground2);z-index:1}
  table.rcpt-answer th.n,table.rcpt-answer td.n{text-align:right;font-family:var(--r-mono)}
  table.rcpt-answer td{padding:7px 13px;border-top:1px solid var(--r-line-soft);font-size:12px}
  table.rcpt-answer td.n{color:var(--r-dim)}
  table.rcpt-answer td.k{font-weight:600}
  .rcpt-ledgercap{display:flex;align-items:center;gap:10px;margin:2px 0 4px;font-family:var(--r-mono);font-size:11px;
    letter-spacing:.12em;text-transform:uppercase;color:var(--r-faint)}
  .rcpt-ledgercap .rule{flex:1;height:1px;background:var(--r-line-soft)}
  .rcpt-step{border-top:1px solid var(--r-line-soft)}
  .rcpt-step:first-of-type{border-top:none}
  .rcpt-btn{all:unset;display:grid;grid-template-columns:34px 1fr auto;gap:14px;align-items:baseline;width:100%;
    padding:15px 2px;cursor:pointer;box-sizing:border-box}
  .rcpt-btn:focus-visible{outline:2px solid var(--r-mint-bright);outline-offset:3px;border-radius:6px}
  .rcpt-num{font-family:var(--r-mono);font-size:12px;color:var(--r-mint-bright)}
  .rcpt-step.warn .rcpt-num{color:var(--r-warn)}
  .rcpt-title{font-size:14.5px;font-weight:600}
  .rcpt-sum{color:var(--r-dim);font-weight:400;font-size:13.5px}
  .rcpt-step.warn .rcpt-sum b{color:var(--r-warn);font-weight:600}
  .rcpt-chev{color:var(--r-faint);font-size:12px;transition:transform .18s ease;font-family:var(--r-mono);align-self:center}
  .rcpt-step[aria-expanded="true"] .rcpt-chev{transform:rotate(90deg);color:var(--r-mint-soft)}
  .rcpt-detail{display:grid;grid-template-rows:0fr;transition:grid-template-rows .22s ease}
  .rcpt-step[aria-expanded="true"] .rcpt-detail{grid-template-rows:1fr}
  .rcpt-detail > .in{overflow:hidden;min-height:0}
  .rcpt-dpad{padding:0 2px 20px 48px}
  @media (max-width:560px){.rcpt-dpad{padding-left:34px}.rcpt-btn{grid-template-columns:26px 1fr auto;gap:10px}}
  .rcpt-reading{display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-family:var(--r-mono);font-size:12px;margin-bottom:8px}
  .rcpt-chip{padding:4px 10px;border-radius:5px;background:var(--r-panel3);color:var(--r-ink);border:1px solid var(--r-line)}
  .rcpt-chip.read{color:var(--r-dim)}
  .rcpt-chip.grounded{background:var(--r-tint);color:var(--r-mint-soft);border-color:var(--r-tint2)}
  .rcpt-arr{color:var(--r-faint)}
  .rcpt-note{font-size:12.5px;color:var(--r-faint);margin:8px 0 0;line-height:1.6}
  .rcpt-note code{font-family:var(--r-mono);color:var(--r-mint-soft);font-size:11.5px}
  .rcpt-trace{display:flex;flex-direction:column;margin:2px 0 0}
  .rcpt-lane{display:flex;align-items:center;gap:12px;padding:10px 12px;border:1px solid var(--r-line);background:var(--r-ground2)}
  .rcpt-lane:first-child{border-radius:7px 7px 0 0}
  .rcpt-lane:last-child{border-radius:0 0 7px 7px}
  .rcpt-lane + .rcpt-flow + .rcpt-lane{border-top:none}
  .rcpt-lyr{font-family:var(--r-mono);font-size:9px;font-weight:600;letter-spacing:.09em;padding:3px 8px;border-radius:4px;flex:none;width:58px;text-align:center}
  .rcpt-lyr.gold{color:#E9C877;background:rgba(233,200,119,.10);border:1px solid rgba(233,200,119,.22)}
  .rcpt-lyr.silver{color:#C3CBD3;background:rgba(195,203,211,.09);border:1px solid rgba(195,203,211,.20)}
  .rcpt-lyr.bronze{color:#C79A6E;background:rgba(199,154,110,.10);border:1px solid rgba(199,154,110,.22)}
  .rcpt-lyr.graph{color:var(--r-mint-soft);background:var(--r-tint);border:1px solid var(--r-tint2)}
  .rcpt-lane .tbl{font-family:var(--r-mono);font-size:13px;color:var(--r-mint-soft)}
  .rcpt-lane .role{color:var(--r-faint);font-size:12px;margin-left:auto}
  .rcpt-lane .jk{font-family:var(--r-mono);font-size:10.5px;color:var(--r-mint-soft);background:var(--r-tint);
    border:1px solid var(--r-tint2);border-radius:4px;padding:1px 7px;margin-left:auto}
  .rcpt-flow{display:flex;justify-content:center;color:var(--r-faint);font-size:12px;line-height:1;margin:-1px 0}
  .rcpt-funnelline{font-size:13.5px;color:var(--r-dim);margin:0 0 12px}
  .rcpt-funnelline b{color:var(--r-ink);font-weight:600;font-family:var(--r-mono)}
  .rcpt-sample{overflow:auto;max-height:300px;border:1px solid var(--r-line);border-radius:7px;background:var(--r-ground2)}
  table.rcpt-mini{border-collapse:collapse;width:100%;font-family:var(--r-mono);font-size:11px;font-variant-numeric:tabular-nums}
  table.rcpt-mini th{text-align:left;font-family:var(--r-sans);font-size:8.5px;letter-spacing:.09em;text-transform:uppercase;
    color:var(--r-faint);font-weight:600;padding:7px 12px;background:var(--r-panel2);position:sticky;top:0;z-index:1}
  table.rcpt-mini th.n,table.rcpt-mini td.n{text-align:right}
  table.rcpt-mini td{padding:7px 12px;border-top:1px solid var(--r-line-soft);color:var(--r-dim)}
  table.rcpt-mini td.k{color:var(--r-ink)}
  .rcpt-rowset{display:flex;align-items:center;gap:10px;margin:12px 0 0;flex-wrap:wrap}
  .rcpt-link{font-size:12.5px;font-weight:600;color:var(--r-mint-soft);cursor:pointer;background:none;border:none;padding:0;font-family:var(--r-sans)}
  .rcpt-link:hover{color:var(--r-mint-bright)}
  .rcpt-handle{font-family:var(--r-mono);font-size:11px;color:var(--r-faint)}
  .rcpt-qt{margin-top:14px}
  .rcpt-qt summary{font-family:var(--r-mono);font-size:12px;color:var(--r-dim);cursor:pointer;list-style:none}
  .rcpt-qt summary::-webkit-details-marker{display:none}
  .rcpt-qt summary::before{content:"▸ ";color:var(--r-faint)}
  .rcpt-qt[open] summary::before{content:"▾ "}
  .rcpt-sql{margin:10px 0 0;padding:13px 14px;background:var(--r-ground);border:1px solid var(--r-line);border-radius:7px;
    overflow-x:auto;font-family:var(--r-mono);font-size:11.5px;line-height:1.6;color:var(--r-dim);white-space:pre-wrap}
  .rcpt-caveat{display:flex;gap:12px;align-items:flex-start;padding:13px 14px;border-radius:7px;background:var(--r-warn-tint);
    border:1px solid var(--r-warn-line);margin-bottom:8px}
  .rcpt-cvtag{font-family:var(--r-mono);font-size:9.5px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:var(--r-warn);
    background:rgba(242,180,60,.14);border:1px solid var(--r-warn-line);padding:3px 8px;border-radius:4px;flex:none;white-space:nowrap;margin-top:1px}
  .rcpt-cvtext{font-size:13px;color:var(--r-ink);line-height:1.55}
  .rcpt-clean{font-size:13px;color:var(--r-dim);display:flex;align-items:center;gap:9px}
  .rcpt-clean .tick{color:var(--r-mint-soft)}
  .rcpt-foot{display:flex;align-items:center;gap:14px;flex-wrap:wrap;color:var(--r-faint);font-family:var(--r-mono);font-size:11px}
  .rcpt-foot .permalink{display:flex;align-items:center;gap:9px;min-width:0}
  .rcpt-copy{all:unset;cursor:pointer;color:var(--r-mint-soft);border:1px solid var(--r-line);border-radius:5px;padding:4px 9px;font-family:var(--r-mono);font-size:11px}
  .rcpt-copy:hover{border-color:var(--r-mint)}
  .rcpt-copy:focus-visible{outline:2px solid var(--r-mint-bright);outline-offset:2px}
  .rcpt-foot .permalink code{color:var(--r-dim);word-break:break-all}
  .rcpt-foot .sp{flex:1;min-width:12px}
  .rcpt-meta b{color:var(--r-dim);font-weight:500}
  .rcpt-state{padding:60px 26px;text-align:center;color:var(--r-dim);font-family:var(--r-sans)}
  .rcpt-state .big{font-size:15px;color:var(--r-ink);margin-bottom:6px}
  .rcpt-spin{width:20px;height:20px;border:2px solid var(--r-line);border-top-color:var(--r-mint-bright);border-radius:50%;
    margin:0 auto 14px;animation:rcpt-spin .7s linear infinite}
  @keyframes rcpt-spin{to{transform:rotate(360deg)}}
  @media (prefers-reduced-motion:reduce){.rcpt-root *{transition:none!important;animation:none!important}}
  `;

  function injectCSS() {
    if (document.getElementById("rcpt-css")) return;
    var s = document.createElement("style");
    s.id = "rcpt-css";
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  // ── helpers ─────────────────────────────────────────────────────────────────
  function esc(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function isNum(v) { return typeof v === "number" && isFinite(v); }
  function fmt(v) { return isNum(v) ? v.toLocaleString() : esc(v); }
  function localName(uri) {
    if (!uri) return "";
    var s = String(uri);
    return s.split("#").pop().split("/").pop();
  }
  // Canonical ForgeMesh mark (matches the app header): a mint rounded square with
  // a dark four-way mesh node.
  var MARK = '<svg class="rcpt-mark" viewBox="0 0 200 200" fill="none" aria-hidden="true">' +
    '<rect width="200" height="200" rx="36" fill="#3A9474"/>' +
    '<circle cx="36" cy="100" r="22" fill="#091A12"/>' +
    '<circle cx="164" cy="100" r="22" fill="#091A12"/>' +
    '<circle cx="100" cy="100" r="22" fill="#091A12"/>' +
    '<line x1="58" y1="100" x2="78" y2="100" stroke="#091A12" stroke-width="16" stroke-linecap="round"/>' +
    '<line x1="122" y1="100" x2="142" y2="100" stroke="#091A12" stroke-width="16" stroke-linecap="round"/>' +
    '<line x1="100" y1="36" x2="100" y2="78" stroke="#091A12" stroke-width="16" stroke-linecap="round"/>' +
    '<line x1="100" y1="122" x2="100" y2="164" stroke="#091A12" stroke-width="16" stroke-linecap="round"/>' +
    '<circle cx="100" cy="36" r="16" fill="#091A12"/>' +
    '<circle cx="100" cy="164" r="16" fill="#091A12"/></svg>';

  // ── block builders (null-gated) ─────────────────────────────────────────────
  function answerBlock(answer) {
    if (!answer) return "";
    var cols = answer.columns || [];
    var rows = answer.rows || [];
    if (!cols.length || !rows.length) return "";
    var numCol = cols.map(function (_, i) {
      return rows.every(function (r) { return r[i] == null || isNum(r[i]); });
    });
    var th = cols.map(function (c, i) {
      return '<th class="' + (numCol[i] ? "n" : "") + '">' + esc(c) + "</th>";
    }).join("");
    var body = rows.map(function (r) {
      return "<tr>" + cols.map(function (_, i) {
        var cls = i === 0 ? "k" : (numCol[i] ? "n" : "");
        return '<td class="' + cls + '">' + fmt(r[i]) + "</td>";
      }).join("") + "</tr>";
    }).join("");
    return '<div class="rcpt-pad"><p class="rcpt-cap">The answer · ' + fmt(rows.length) +
      (rows.length === 1 ? " row" : " rows") + '</p><div class="rcpt-scroll"><table class="rcpt-answer">' +
      "<thead><tr>" + th + "</tr></thead><tbody>" + body + "</tbody></table></div></div>";
  }

  function step(num, warn, title, summary, detailHTML, open) {
    if (!detailHTML) return "";
    return '<section class="rcpt-step' + (warn ? " warn" : "") + '" aria-expanded="' + (open ? "true" : "false") + '">' +
      '<button class="rcpt-btn"><span class="rcpt-num">' + num + "</span>" +
      '<span><span class="rcpt-title">' + esc(title) + "</span>" +
      (summary ? ' &nbsp;<span class="rcpt-sum">' + summary + "</span>" : "") + "</span>" +
      '<span class="rcpt-chev">›</span></button>' +
      '<div class="rcpt-detail"><div class="in"><div class="rcpt-dpad">' + detailHTML + "</div></div></div></section>";
  }

  function understoodDetail(understood) {
    var terms = (understood || []).filter(function (t) { return t.term; });
    if (!terms.length) return "";
    var grounded = terms.filter(function (t) { return t.grounded_uri; });
    var rows = terms.map(function (t) {
      var g = !!t.grounded_uri;
      var read = t.reading || (g ? localName(t.grounded_uri) : "");
      if (!read) return "";
      return '<div class="rcpt-reading"><span class="rcpt-chip">"' + esc(t.term) + '"</span>' +
        '<span class="rcpt-arr">→</span><span class="rcpt-chip ' + (g ? "grounded" : "read") + '"' +
        (g ? ' title="' + esc(t.grounded_uri) + '"' : "") + ">" + esc(read) + "</span></div>";
    }).join("");
    var note = grounded.length ? "" :
      '<p class="rcpt-note">No ontology concept was resolved for this query — it mapped straight to columns, so nothing needed interpreting.</p>';
    return rows + note;
  }

  var LAYER_RANK = { graph: 0, gold: 1, silver: 2, bronze: 3 };
  function sourcesDetail(sources) {
    var s = (sources || []).filter(function (x) { return x.table; });
    if (!s.length) return "";
    var hasJoin = s.some(function (x) { return x.join_key; });

    if (hasJoin) {
      // join map
      var lanes = s.map(function (x) {
        var meta = [];
        if (x.layer) meta.push(esc(x.layer));
        if (x.mapping_confirmed) meta.push(esc(x.mapping_confirmed));
        return '<div class="rcpt-lane"><span class="tbl">' + esc(x.table) + "</span>" +
          (meta.length ? '<span class="role">' + meta.join(" · ") + "</span>" : "") +
          (x.join_key ? '<span class="jk">joined on ' + esc(x.join_key) + "</span>" : "") + "</div>";
      }).join('<div class="rcpt-flow">↕</div>');
      return '<div class="rcpt-trace">' + lanes + "</div>";
    }

    if (s.length > 1 && s.some(function (x) { return x.layer; })) {
      // Multiple layered sources. A vertical descent through DISTINCT layers
      // (graph → gold → silver → bronze) is a lineage chain; multiple entities at
      // the SAME layer (e.g. two graph classes an aggregate query grouped/joined)
      // are parallel sources, NOT a descent — render them without the ↑ arrow or
      // the "traced back through the pipeline" claim.
      var ordered = s.slice().sort(function (a, b) {
        return (LAYER_RANK[a.layer] == null ? 9 : LAYER_RANK[a.layer]) - (LAYER_RANK[b.layer] == null ? 9 : LAYER_RANK[b.layer]);
      });
      var layerSet = {};
      ordered.forEach(function (x) { if (x.layer) layerSet[x.layer] = 1; });
      var isDescent = Object.keys(layerSet).length > 1;
      var ROLE = { gold: "the answer was counted here", silver: "normalised", bronze: "source system", graph: "knowledge graph" };
      var lanes2 = ordered.map(function (x) {
        var lyr = (x.layer || "").toLowerCase();
        return '<div class="rcpt-lane"><span class="rcpt-lyr ' + esc(lyr) + '">' + esc((x.layer || "").toUpperCase()) + "</span>" +
          '<span class="tbl">' + esc(x.table) + "</span>" +
          '<span class="role">' + (ROLE[lyr] || "") + (x.mapping_confirmed ? " · " + esc(x.mapping_confirmed) : "") + "</span></div>";
      }).join('<div class="rcpt-flow">' + (isDescent ? "↑" : "+") + "</div>");
      var note = isDescent
        ? "One entity traced back through the pipeline to its source rows — a lineage chain, not " + ordered.length + " joined tables."
        : ordered.length + " entities were queried together in the knowledge graph. This answer aggregates across them, so there is no single entity to trace back to source rows.";
      return '<div class="rcpt-trace">' + lanes2 + "</div>" +
        '<p class="rcpt-note">' + note + "</p>";
    }

    // single source
    var one = s[0];
    var m = [];
    if (one.layer) m.push(esc(one.layer));
    if (one.mapping_confirmed) m.push(esc(one.mapping_confirmed));
    return '<div class="rcpt-trace"><div class="rcpt-lane"><span class="tbl">' + esc(one.table) + "</span>" +
      (m.length ? '<span class="role">' + m.join(" · ") + "</span>" : "") + "</div></div>";
  }

  function evidenceDetail(ev, runId, queryText) {
    if (!ev) return "";
    var html = "";
    var scanned = ev.rows_scanned, matched = ev.rows_matched;
    var funnel = (ev.funnel || []).filter(function (f) { return f.label; });
    if (isNum(scanned)) {
      html += '<p class="rcpt-funnelline"><b>' + fmt(scanned) + "</b> scanned → <b>" + fmt(matched) + "</b> matched.</p>";
      if (funnel.length > 2) {
        html += '<p class="rcpt-note">' + funnel.map(function (f) { return fmt(f.n) + " " + esc(f.label); }).join(" &nbsp;→&nbsp; ") + "</p>";
      }
    } else if (isNum(matched)) {
      html += '<p class="rcpt-funnelline"><b>' + fmt(matched) + "</b> " + (matched === 1 ? "row" : "rows") + " matched.</p>";
    }
    var sample = ev.sample || [];
    if (sample.length) {
      var cols = Object.keys(sample[0]);
      var numCol = cols.map(function (c) { return sample.every(function (r) { return r[c] == null || isNum(r[c]); }); });
      html += '<div class="rcpt-sample"><table class="rcpt-mini"><thead><tr>' +
        cols.map(function (c, i) { return '<th class="' + (numCol[i] ? "n" : "") + '">' + esc(c) + "</th>"; }).join("") +
        "</tr></thead><tbody>" +
        sample.map(function (r) {
          return "<tr>" + cols.map(function (c, i) {
            return '<td class="' + (i === 0 ? "k" : (numCol[i] ? "n" : "")) + '">' + fmt(r[c]) + "</td>";
          }).join("") + "</tr>";
        }).join("") + "</tbody></table></div>";
    }
    if (ev.row_set) {
      html += '<div class="rcpt-rowset"><button class="rcpt-link" data-openrows="' + esc(runId) + '">Open the underlying rows →</button>' +
        '<span class="rcpt-handle">' + esc(ev.row_set) + "</span></div>";
    }
    if (queryText) {
      html += '<details class="rcpt-qt"><summary>The query that ran</summary><div class="rcpt-sql">' + esc(queryText) + "</div></details>";
    }
    return html;
  }

  function caveatsDetail(caveats) {
    var c = caveats || [];
    if (!c.length) return "";
    return c.map(function (x) {
      var label = x.label || (x.kind ? x.kind.replace(/_/g, " ").toUpperCase() : "NOTE");
      var text = x.note || (fmt(x.count) + " " + label.toLowerCase());
      return '<div class="rcpt-caveat"><span class="rcpt-cvtag">' + esc(label) + '</span>' +
        '<span class="rcpt-cvtext">' + esc(text) + "</span></div>";
    }).join("");
  }

  function summarise(env) {
    var r = env.receipt || {};
    var out = {};
    var g = (r.understood || []).filter(function (t) { return t.grounded_uri; });
    out.understood = g.length ? g.length + (g.length === 1 ? " term grounded" : " terms grounded") : "read literally";
    var src = (r.sources || []).filter(function (x) { return x.table; });
    var srcLayers = {};
    src.forEach(function (x) { if (x.layer) srcLayers[x.layer] = 1; });
    var nLayers = Object.keys(srcLayers).length;
    out.sources = src.some(function (x) { return x.join_key; }) ? src.length + " tables joined"
      : (src.length > 1
          ? (nLayers > 1 ? "traced " + nLayers + " layers" : src.length + " " + (src[0].layer || "graph") + " entities")
          : (src[0] ? src[0].table : "source not captured"));
    var ev = r.evidence || {};
    out.evidence = isNum(ev.rows_scanned) ? fmt(ev.rows_scanned) + " → " + fmt(ev.rows_matched) + " matched"
      : (isNum(ev.rows_matched) ? fmt(ev.rows_matched) + (ev.rows_matched === 1 ? " row" : " rows") + " matched" : "");
    var cv = r.caveats || [];
    out.caveats = cv.length ? cv.map(function (x) { return fmt(x.count) + " " + (x.label || x.kind || "").toLowerCase().replace(/_/g, " "); }).join(", ") : "";
    return out;
  }

  // ── main render ─────────────────────────────────────────────────────────────
  function build(env) {
    var r = env.receipt || {};
    var sm = summarise(env);
    var question = r.question || ((r.understood || [])[0] || {}).term || "Answer receipt";
    var url = env.receipt_url || "";
    var permalink = url.replace(/^https?:\/\//, "");
    var runId = env.run_id || r.run_id || "";
    var footMeta = [];
    if (env.envelope_version) footMeta.push("envelope <b>v" + esc(env.envelope_version) + "</b>");
    if (r.depth) footMeta.push("depth <b>" + esc(r.depth) + "</b>");
    if (r.ontology_version) footMeta.push("ontology <b>" + esc(localName(r.ontology_version)) + "</b>");

    var understood = step("01", false, "What was understood", sm.understood, understoodDetail(r.understood), true);
    var sources = step("02", false, "Where the data lives", sm.sources, sourcesDetail(r.sources), true);
    var evidence = step("03", false, "What was counted", sm.evidence, evidenceDetail(r.evidence, runId, r.query_text), false);
    var caveatsHTML = caveatsDetail(r.caveats);
    var caveats = caveatsHTML
      ? step("04", true, "Worth knowing", "<b>" + esc(sm.caveats) + "</b>", caveatsHTML, true)
      : "";
    var cleanLine = caveatsHTML ? "" :
      '<div class="rcpt-clean"><span class="tick">✓</span> No caveats flagged on this answer.</div>';

    return '<div class="rcpt-card">' +
      '<div class="rcpt-pad">' +
        '<div class="rcpt-mast">' + MARK + '<span class="rcpt-word">Forge<b>Mesh</b></span>' +
          '<span class="sp"></span><span class="rcpt-stamp"><b>Answer receipt</b><br>traced to source</span></div>' +
        '<div class="rcpt-headrow"><span class="rcpt-kicker">Question</span>' +
          '<span class="rcpt-pill"><span class="dot"></span>Traced to source</span>' +
          (runId ? '<span class="rcpt-runid" data-runid>' + esc(runId) + "</span>" : "") + "</div>" +
        '<h1 class="rcpt-h1">' + esc(question) + "</h1>" +
      "</div>" +
      '<div class="rcpt-tear"></div>' +
      answerBlock(env.answer) +
      '<div class="rcpt-tear"></div>' +
      '<div class="rcpt-pad"><div class="rcpt-ledgercap"><span>How this was produced</span><span class="rule"></span></div>' +
        understood + sources + evidence + caveats +
        (cleanLine ? '<div style="padding-top:14px">' + cleanLine + "</div>" : "") +
      "</div>" +
      '<div class="rcpt-tear"></div>' +
      '<div class="rcpt-pad"><div class="rcpt-foot">' +
        (url ? '<span class="permalink"><button class="rcpt-copy" data-copy="' + esc(url) + '">Copy link ⧉</button>' +
          "<code>" + esc(permalink) + "</code></span>" : "") +
        '<span class="sp"></span>' +
        (footMeta.length ? '<span class="rcpt-meta">' + footMeta.join(" · ") + "</span>" : "") +
      "</div></div></div>";
  }

  function wire(el) {
    el.querySelectorAll(".rcpt-step").forEach(function (stp) {
      var btn = stp.querySelector(".rcpt-btn");
      btn.addEventListener("click", function () {
        stp.setAttribute("aria-expanded", stp.getAttribute("aria-expanded") === "true" ? "false" : "true");
      });
    });
    var copy = el.querySelector("[data-copy]");
    if (copy) copy.addEventListener("click", function () {
      var u = copy.getAttribute("data-copy");
      if (navigator.clipboard) navigator.clipboard.writeText(u);
      var t = copy.textContent; copy.textContent = "Copied ✓";
      setTimeout(function () { copy.textContent = t; }, 1400);
    });
    var rid = el.querySelector("[data-runid]");
    if (rid) rid.addEventListener("click", function () {
      var rng = document.createRange(); rng.selectNodeContents(rid);
      var sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(rng);
    });
    var openRows = el.querySelector("[data-openrows]");
    if (openRows) openRows.addEventListener("click", function () {
      var runId = openRows.getAttribute("data-openrows");
      openRows.textContent = "Loading…";
      fetch("/api/runs/" + encodeURIComponent(runId) + "/rows?page=1")
        .then(function (r) { return r.json(); })
        .then(function (d) {
          var rows = d.rows || [];
          if (!rows.length) { openRows.textContent = "No rows"; return; }
          var cols = d.columns && d.columns.length ? d.columns : Object.keys(rows[0]);
          var host = openRows.closest(".rcpt-dpad");
          var box = document.createElement("div");
          box.className = "rcpt-sample";
          box.style.marginTop = "10px";
          box.innerHTML = '<table class="rcpt-mini"><thead><tr>' +
            cols.map(function (c) { return "<th>" + esc(c) + "</th>"; }).join("") + "</tr></thead><tbody>" +
            rows.map(function (row) {
              return "<tr>" + cols.map(function (c) { return "<td>" + fmt(row[c]) + "</td>"; }).join("") + "</tr>";
            }).join("") + "</tbody></table>";
          host.appendChild(box);
          openRows.textContent = "Showing " + rows.length + " of " + (d.total_rows || rows.length) + " rows";
          openRows.disabled = true;
        })
        .catch(function () { openRows.textContent = "Could not load rows"; });
    });
  }

  function render(env, el) {
    injectCSS();
    el.classList.add("rcpt-root");
    el.innerHTML = build(env);
    wire(el);
  }

  function state(el, html) {
    injectCSS();
    el.classList.add("rcpt-root");
    el.innerHTML = '<div class="rcpt-card"><div class="rcpt-state">' + html + "</div></div>";
  }

  // Fetch + render an envelope by run id. Used by the standalone page and the pane.
  function load(runId, el) {
    if (!runId) { state(el, '<div class="big">No receipt selected</div>Run a query, then open its receipt.'); return; }
    state(el, '<div class="rcpt-spin"></div>Loading receipt ' + esc(runId) + "…");
    fetch("/api/runs/" + encodeURIComponent(runId) + "?deep=true")
      .then(function (r) { if (!r.ok) throw new Error(String(r.status)); return r.json(); })
      .then(function (env) {
        if (env && env.error) { state(el, '<div class="big">Receipt not found</div>No run with id ' + esc(runId) + "."); return; }
        render(env, el);
      })
      .catch(function () { state(el, '<div class="big">Could not load receipt</div>The run may have expired or the server is unreachable.'); });
  }

  global.ForgeReceipt = { render: render, load: load, state: state };
})(window);
