"""skill_ab_app.py — the `skill-ab serve` frontend (one self-contained SPA).

This module owns *only* the browser-facing UI. `app_shell_html(token)` returns a
single HTML document (no external assets, no framework, no build step) that talks
to the server's frozen HTTP/SSE API (see plans/021). Three pieces:

- `_APP_CSS`   — app chrome layered on top of the harness report's design system.
- `_APP_JS`    — vanilla JS: a hash router over 5 views + the live SSE renderer.
- `app_shell_html(token)` — assembles the document and embeds the session token.

WHY reuse `skill_ab_harness._HTML_STYLE`: the serve UI must read as the same
product as the generated `report.html`, so all color/shadow/radius/dark-mode
tokens come from there; we only add the shell (sidebar, top bar, forms, the live
cell grid + console). The frontend is dumb on purpose — every statistic, verdict
and render still comes from the engine; this file just orchestrates fetches and
paints the stream.
"""

import json

import skill_ab_harness as h

# ---------------------------------------------------------------------------
# App chrome CSS (sits on top of _HTML_STYLE's tokens; never redefines them).
# ---------------------------------------------------------------------------
_APP_CSS = """
  html,body{height:100%}
  body.app-body{background-attachment:fixed}

  /* ---------- shell layout ---------- */
  .app{display:grid; grid-template-columns:1fr; min-height:100vh}
  @media (min-width:700px){.app{grid-template-columns:236px 1fr}}

  .sidebar{display:flex; flex-direction:column; gap:4px; padding:18px 14px;
    border-right:1px solid var(--line); background:var(--card);
    position:sticky; top:0; align-self:start; min-height:100vh}
  @media (max-width:699px){
    .sidebar{position:static; min-height:0; border-right:none;
      border-bottom:1px solid var(--line)}
  }
  .brand{display:flex; align-items:center; gap:10px; padding:4px 6px 16px}
  .brand .glyph{width:32px; height:32px; border-radius:9px; flex:0 0 auto;
    background:linear-gradient(135deg,#1f77b4,#5aa9dd); display:grid;
    place-items:center; box-shadow:var(--shadow-sm)}
  .brand .bn{font-weight:680; font-size:15px; letter-spacing:-.01em}
  .brand .bt{font-size:10px; color:var(--muted); font-weight:650;
    letter-spacing:.10em; text-transform:uppercase; margin-top:1px}
  .nav{display:flex; flex-direction:column; gap:2px}
  .nav a{display:flex; align-items:center; gap:11px; padding:9px 11px;
    border-radius:10px; color:var(--ink-2); text-decoration:none;
    font-size:13.5px; font-weight:560}
  .nav a:hover{background:var(--card-2); color:var(--ink)}
  .nav a.active{color:var(--ink); font-weight:650;
    background:color-mix(in srgb,#1f77b4 14%, transparent)}
  .nav a .ic{flex:0 0 auto; color:var(--muted)}
  .nav a.active .ic{color:#1f77b4}
  .side-foot{margin-top:auto; font-size:11px; color:var(--faint);
    line-height:1.55; padding:16px 8px 2px}

  .main{display:flex; flex-direction:column; min-width:0}
  .appbar{display:flex; align-items:center; justify-content:space-between;
    gap:14px; padding:13px 22px; border-bottom:1px solid var(--line);
    position:sticky; top:0; z-index:5;
    background:color-mix(in srgb,var(--card) 84%, transparent);
    backdrop-filter:blur(8px); -webkit-backdrop-filter:blur(8px)}
  .appbar-title{font-size:16px; font-weight:680; letter-spacing:-.01em}
  .appbar-right{display:flex; align-items:center; gap:18px}
  .health{display:flex; align-items:center; gap:7px; font-size:12px;
    color:var(--muted); font-weight:560}
  .health-dot{width:9px; height:9px; border-radius:50%; background:var(--faint);
    box-shadow:0 0 0 4px color-mix(in srgb,var(--faint) 20%, transparent)}
  .health-dot.ok{background:var(--good);
    box-shadow:0 0 0 4px color-mix(in srgb,var(--good) 22%, transparent)}
  .health-dot.warn{background:#d98a15;
    box-shadow:0 0 0 4px color-mix(in srgb,#d98a15 22%, transparent)}
  .health-dot.bad{background:var(--bad);
    box-shadow:0 0 0 4px color-mix(in srgb,var(--bad) 22%, transparent)}
  .view{max-width:1080px; width:100%; margin:0 auto; padding:24px 22px 64px}

  /* ---------- top-bar usage ticker (live runs) ---------- */
  .ticker{display:flex; align-items:center; gap:9px; font-size:12px;
    color:var(--ink-2)}
  .ticker .tk-cap{color:var(--muted); font-weight:650}
  .ticker .tk-bar{width:80px; height:7px; border-radius:5px;
    background:var(--grid); overflow:hidden}
  .ticker .tk-fill{height:100%; width:0%;
    background:linear-gradient(90deg,#1f77b4,#5aa9dd);
    transition:width .3s ease}
  .ticker .num{font-weight:650; color:var(--ink);
    font-variant-numeric:tabular-nums}

  /* ---------- buttons ---------- */
  .btn{font-family:var(--sans); font-size:13px; font-weight:620;
    color:var(--ink); background:var(--card); border:1px solid var(--line-2);
    border-radius:10px; padding:9px 15px; cursor:pointer; gap:8px;
    box-shadow:var(--shadow-sm); display:inline-flex; align-items:center;
    transition:transform .1s ease, box-shadow .12s ease}
  .btn:hover{transform:translateY(-1px); box-shadow:var(--shadow)}
  .btn:active{transform:translateY(0)}
  .btn:disabled{opacity:.5; cursor:not-allowed; transform:none;
    box-shadow:var(--shadow-sm)}
  .btn:focus-visible{outline:none;
    box-shadow:0 0 0 3px color-mix(in srgb,#1f77b4 30%, transparent)}
  .btn-primary{color:#fff; border-color:#1f77b4;
    background:linear-gradient(135deg,#1f77b4,#3f93cf)}
  .btn-primary:hover{box-shadow:0 8px 22px
    color-mix(in srgb,#1f77b4 32%, transparent)}
  .btn-danger{color:var(--bad); background:var(--bad-bg);
    border-color:var(--bad-line)}
  .btn-ghost{background:transparent; box-shadow:none}
  .row{display:flex; gap:10px; flex-wrap:wrap; align-items:center}

  /* ---------- forms ---------- */
  .form{display:grid; gap:16px; max-width:580px}
  .field{display:grid; gap:6px}
  .field label{font-size:12.5px; font-weight:620; color:var(--ink-2)}
  .field .hint{font-size:11.5px; color:var(--muted); font-weight:500}
  .src-hint{display:block; font-size:11px; font-weight:600; margin-top:5px}
  .src-ok{color:var(--good)} .src-warn{color:var(--muted)}
  .inp{font-family:var(--sans); font-size:13.5px; color:var(--ink);
    background:var(--card); border:1px solid var(--line-2); border-radius:10px;
    padding:10px 12px; width:100%; box-shadow:var(--shadow-sm)}
  .inp:focus{outline:none; border-color:#1f77b4;
    box-shadow:0 0 0 3px color-mix(in srgb,#1f77b4 22%, transparent)}
  select.inp{appearance:none; -webkit-appearance:none; cursor:pointer;
    padding-right:34px; background-repeat:no-repeat;
    background-image:
      linear-gradient(45deg,transparent 50%,var(--muted) 50%),
      linear-gradient(135deg,var(--muted) 50%,transparent 50%);
    background-position:calc(100% - 17px) 53%, calc(100% - 12px) 53%;
    background-size:5px 5px,5px 5px}
  .field-row{display:flex; gap:14px; flex-wrap:wrap}
  .field-row .field{flex:1; min-width:170px}
  input[type=range]{width:100%; accent-color:#1f77b4}
  .krow{display:flex; align-items:center; gap:13px}
  .kval{font-variant-numeric:tabular-nums; font-weight:680; font-size:16px;
    min-width:24px; text-align:center}
  .switch{display:inline-flex; align-items:center; gap:11px; cursor:pointer;
    position:relative}
  .switch input{position:absolute; opacity:0; width:0; height:0}
  .switch .track{width:40px; height:23px; border-radius:999px;
    background:var(--grid); border:1px solid var(--line-2);
    position:relative; transition:background .15s ease}
  .switch .knob{position:absolute; top:2px; left:2px; width:17px; height:17px;
    border-radius:50%; background:#fff; box-shadow:var(--shadow-sm);
    transition:transform .15s ease}
  .switch input:checked + .track{background:#1f77b4; border-color:#1f77b4}
  .switch input:checked + .track .knob{transform:translateX(17px)}
  .switch input:focus-visible + .track{
    box-shadow:0 0 0 3px color-mix(in srgb,#1f77b4 30%, transparent)}

  /* ---------- run cards / grid ---------- */
  .card-pad{padding:20px 22px}
  .run-grid{display:grid; gap:14px;
    grid-template-columns:repeat(auto-fill,minmax(280px,1fr))}
  .run-card{display:flex; flex-direction:column; gap:11px; cursor:pointer;
    padding:16px 16px 14px; text-align:left; color:inherit;
    transition:transform .14s ease, box-shadow .14s ease}
  .run-card:hover{transform:translateY(-2px); box-shadow:var(--shadow-lg)}
  .run-card .rc-top{display:flex; align-items:center; gap:8px;
    justify-content:space-between}
  .run-card .rc-title{font-family:var(--mono); font-size:13px; font-weight:600;
    color:var(--ink); overflow:hidden; text-overflow:ellipsis;
    white-space:nowrap}
  .run-card .rc-title .vs{color:var(--faint); font-weight:500; padding:0 .3em}
  .run-card .badge-wrap{min-height:20px}
  .run-card .badge-wrap img{display:block; height:20px; max-width:100%}
  .run-card .rc-meta{display:flex; flex-wrap:wrap; gap:6px 14px;
    font-size:11.5px; color:var(--muted)}
  .run-card .rc-meta b{color:var(--ink-2); font-weight:620}

  /* ---------- pills ---------- */
  .pill{display:inline-flex; align-items:center; gap:6px; font-size:11px;
    font-weight:680; letter-spacing:.02em; padding:3px 9px; border-radius:999px;
    border:1px solid var(--line-2); background:var(--pill-grey-bg);
    color:var(--pill-grey-ink); white-space:nowrap}
  .pill .dot{width:6px; height:6px; border-radius:50%; background:currentColor}
  .pill.good{color:var(--good); background:var(--good-bg);
    border-color:var(--good-line)}
  .pill.bad{color:var(--bad); background:var(--bad-bg);
    border-color:var(--bad-line)}
  .pill.run{color:#1f77b4; border-color:color-mix(in srgb,#1f77b4 30%,
    transparent); background:color-mix(in srgb,#1f77b4 12%, transparent)}
  .demo-badge{display:inline-flex; align-items:center; gap:6px; font-size:11px;
    font-weight:700; letter-spacing:.03em; color:#7a5800; background:#fff3d6;
    border:1px solid #f0d488; border-radius:7px; padding:3px 9px}
  @media (prefers-color-scheme:dark){
    .demo-badge{color:#f0c869; background:#2a2310; border-color:#5a4a1f}
  }

  /* ---------- generic blocks ---------- */
  .sec-h{display:flex; align-items:baseline; justify-content:space-between;
    gap:12px; margin:4px 2px 14px}
  .sec-h h2{font-size:17px; font-weight:680; letter-spacing:-.01em}
  .sec-h .hint{font-size:12px; color:var(--muted)}
  .empty-state{display:grid; place-items:center; gap:14px; text-align:center;
    padding:54px 24px}
  .empty-state .es-ic{width:46px; height:46px; border-radius:13px;
    display:grid; place-items:center; color:var(--muted);
    background:var(--card-2); border:1px solid var(--line)}
  .empty-state h3{font-size:16px; font-weight:660}
  .empty-state p{color:var(--muted); font-size:13px; max-width:44ch; margin:0}
  .note-card{background:var(--card-2); border:1px solid var(--line);
    border-radius:12px; padding:13px 15px; font-size:12.5px; color:var(--ink-2);
    line-height:1.6}
  .note-card b{color:var(--ink)}
  .loading{display:flex; align-items:center; gap:10px; color:var(--muted);
    font-size:13px; padding:30px 4px}
  .spin{display:inline-block; width:15px; height:15px; border-radius:50%;
    border:2px solid var(--line-2); border-top-color:#1f77b4;
    animation:rot .7s linear infinite}
  @keyframes rot{to{transform:rotate(360deg)}}

  /* ---------- estimate ---------- */
  .estimate{display:grid; gap:13px; margin-top:2px}
  .estimate.stale{opacity:.5}
  .est-grid{display:grid; gap:12px;
    grid-template-columns:repeat(auto-fit,minmax(120px,1fr))}
  .est-cell{background:var(--card-2); border:1px solid var(--line);
    border-radius:11px; padding:12px 13px}
  .est-cell .ec-cap{font-size:10.5px; font-weight:650; letter-spacing:.06em;
    text-transform:uppercase; color:var(--muted)}
  .est-cell .ec-val{font-size:21px; font-weight:720; letter-spacing:-.02em;
    margin-top:5px; font-variant-numeric:tabular-nums}
  .est-cell .ec-sub{font-size:11px; color:var(--muted); margin-top:2px}

  /* ---------- live run ---------- */
  .live-head{display:flex; align-items:center; justify-content:space-between;
    gap:12px; flex-wrap:wrap; margin-bottom:6px}
  .live-id{font-family:var(--mono); font-size:11.5px; color:var(--muted)}
  .cell-grid{display:grid; gap:8px;
    grid-template-columns:repeat(auto-fill,minmax(134px,1fr))}
  .cell{border:1px solid var(--line-2); border-radius:10px; padding:10px 11px;
    background:var(--card-2); display:grid; gap:5px; min-height:56px;
    overflow:hidden; transition:background .2s ease, border-color .2s ease}
  .cell .c-lab{font-family:var(--mono); font-size:11px; font-weight:600;
    color:var(--ink-2); overflow:hidden; text-overflow:ellipsis;
    white-space:nowrap}
  .cell .c-st{font-size:10px; font-weight:680; letter-spacing:.04em;
    color:var(--muted); text-transform:uppercase}
  .cell.running{border-color:color-mix(in srgb,#1f77b4 45%, var(--line-2));
    background:color-mix(in srgb,#1f77b4 9%, var(--card-2))}
  .cell.running .c-st{color:#1f77b4}
  .cell.running::after{content:""; height:2px; border-radius:2px;
    background:linear-gradient(90deg,transparent,#1f77b4,transparent);
    background-size:200% 100%; animation:scan 1.1s linear infinite}
  @keyframes scan{0%{background-position:200% 0}100%{background-position:-200% 0}}
  .cell.valid{border-color:var(--good-line); background:var(--good-bg)}
  .cell.valid .c-st{color:var(--good)}
  .cell.invalid{border-color:var(--bad-line); background:var(--bad-bg)}
  .cell.invalid .c-st{color:var(--bad)}
  .cell.contaminated{border-color:#f0d488; background:#fff3d6}
  .cell.contaminated .c-st{color:#a5790f}
  @media (prefers-color-scheme:dark){
    .cell.contaminated{border-color:#5a4a1f; background:#2a2310}
    .cell.contaminated .c-st{color:#f0c869}
  }
  .legend-row{display:flex; gap:15px; flex-wrap:wrap; margin-top:11px;
    font-size:11px; color:var(--muted)}
  .legend-row .lg{display:inline-flex; align-items:center; gap:6px}
  .legend-row .sw{width:10px; height:10px; border-radius:3px;
    border:1px solid var(--line-2)}

  .console{font-family:var(--mono); font-size:11.5px; line-height:1.55;
    background:var(--card-2); border:1px solid var(--line); border-radius:12px;
    padding:10px 12px; max-height:340px; overflow:auto; margin-top:4px}
  .cline{padding:2px 0 2px 9px; margin:3px 0; border-left:3px solid var(--line);
    color:var(--ink-2); white-space:pre-wrap; word-break:break-word}
  .cline .lab{font-weight:680; margin-right:7px}
  .cline .tool{color:var(--muted)}
  .cline.sys{border-left-color:var(--line); color:var(--muted)}
  .lab-0{border-left-color:#1f77b4} .lab-0 .lab{color:#1f77b4}
  .lab-1{border-left-color:#2ca02c} .lab-1 .lab{color:#2ca02c}
  .lab-2{border-left-color:#ff7f0e} .lab-2 .lab{color:#ff7f0e}
  .lab-3{border-left-color:#9467bd} .lab-3 .lab{color:#9467bd}
  .lab-4{border-left-color:#17a2b8} .lab-4 .lab{color:#17a2b8}
  .lab-5{border-left-color:#d62728} .lab-5 .lab{color:#d62728}
  .console-empty{color:var(--faint); font-style:italic; padding:8px 2px}
  .done-cta{display:flex; gap:11px; align-items:center; flex-wrap:wrap;
    margin-top:2px}

  /* ---------- results / gallery iframes ---------- */
  .frame{width:100%; height:80vh; border:1px solid var(--line);
    border-radius:14px; background:var(--card); box-shadow:var(--shadow)}
  .back-link{display:inline-flex; align-items:center; gap:6px; font-size:12.5px;
    color:var(--muted); text-decoration:none; font-weight:560; margin-bottom:8px}
  .back-link:hover{color:var(--ink)}
  .md-box{font-family:var(--mono); font-size:11.5px; color:var(--ink-2);
    background:var(--card-2); border:1px solid var(--line); border-radius:9px;
    padding:9px 11px; word-break:break-all; margin-top:8px}

  /* ---------- settings ---------- */
  .kv{display:grid; grid-template-columns:170px 1fr; gap:11px 16px;
    font-size:13px}
  .kv dt{color:var(--muted); font-weight:600}
  .kv dd{margin:0; color:var(--ink); word-break:break-word;
    font-variant-numeric:tabular-nums}
  .kv dd.mono{font-family:var(--mono); font-size:12px}

  /* ---------- toasts ---------- */
  .toasts{position:fixed; right:18px; bottom:18px; z-index:60; display:flex;
    flex-direction:column; gap:8px; max-width:340px}
  .toast{background:var(--card); border:1px solid var(--line-2);
    border-radius:11px; box-shadow:var(--shadow-lg); padding:11px 14px;
    font-size:12.5px; color:var(--ink-2); animation:rise .18s ease}
  .toast.err{border-color:var(--bad-line); color:var(--bad)}
  @keyframes rise{from{opacity:0; transform:translateY(6px)}
    to{opacity:1; transform:translateY(0)}}
"""


# The static shell painted on first load (sidebar + top bar + empty view).
# Inline SVG icons keep it asset-free; JS only fills #view and toggles state.
_SHELL_HTML = """
<div class="app">
  <aside class="sidebar">
    <div class="brand">
      <span class="glyph">
        <svg width="17" height="17" viewBox="0 0 24 24" fill="none">
          <rect x="4" y="10" width="6" height="10" rx="1.5" fill="#fff"
            opacity=".9"/>
          <rect x="14" y="5" width="6" height="15" rx="1.5" fill="#fff"/>
        </svg>
      </span>
      <span>
        <span class="bn">skill-ab</span><br>
        <span class="bt">local app</span>
      </span>
    </div>
    <nav class="nav">
      <a href="#/" data-route="">
        <svg class="ic" width="17" height="17" viewBox="0 0 24 24" fill="none">
          <rect x="3" y="3" width="7" height="7" rx="1.5" stroke="currentColor"
            stroke-width="1.8"/>
          <rect x="14" y="3" width="7" height="7" rx="1.5" stroke="currentColor"
            stroke-width="1.8"/>
          <rect x="3" y="14" width="7" height="7" rx="1.5" stroke="currentColor"
            stroke-width="1.8"/>
          <rect x="14" y="14" width="7" height="7" rx="1.5"
            stroke="currentColor" stroke-width="1.8"/>
        </svg>Dashboard</a>
      <a href="#/new" data-route="new">
        <svg class="ic" width="17" height="17" viewBox="0 0 24 24" fill="none">
          <circle cx="12" cy="12" r="9" stroke="currentColor"
            stroke-width="1.8"/>
          <path d="M12 8v8M8 12h8" stroke="currentColor" stroke-width="1.8"
            stroke-linecap="round"/>
        </svg>New run</a>
      <a href="#/gallery" data-route="gallery">
        <svg class="ic" width="17" height="17" viewBox="0 0 24 24" fill="none">
          <path d="M12 3l9 5-9 5-9-5 9-5z" stroke="currentColor"
            stroke-width="1.8" stroke-linejoin="round"/>
          <path d="M3 13l9 5 9-5" stroke="currentColor" stroke-width="1.8"
            stroke-linejoin="round"/>
        </svg>Gallery</a>
      <a href="#/settings" data-route="settings">
        <svg class="ic" width="17" height="17" viewBox="0 0 24 24" fill="none">
          <path d="M4 7h10M18 7h2M4 17h2M10 17h10" stroke="currentColor"
            stroke-width="1.8" stroke-linecap="round"/>
          <circle cx="16" cy="7" r="2.2" stroke="currentColor"
            stroke-width="1.8"/>
          <circle cx="8" cy="17" r="2.2" stroke="currentColor"
            stroke-width="1.8"/>
        </svg>Settings</a>
    </nav>
    <div class="side-foot">
      Runs use your Claude Code <b>subscription</b> via <span
      class="mono">claude -p</span> — never an API key. Cost shown is a usage
      proxy.
    </div>
  </aside>
  <div class="main">
    <header class="appbar">
      <div class="appbar-title" id="appbar-title">Dashboard</div>
      <div class="appbar-right">
        <div class="ticker" id="ticker" hidden>
          <span class="tk-cap">usage</span>
          <span class="num" id="tk-num">0</span>
          <div class="tk-bar"><div class="tk-fill" id="tk-fill"></div></div>
        </div>
        <div class="health" title="Claude Code status">
          <span class="health-dot" id="health-dot"></span>
          <span id="health-text">checking…</span>
        </div>
      </div>
    </header>
    <main class="view" id="view"></main>
  </div>
</div>
<div class="toasts" id="toasts"></div>
"""


# ---------------------------------------------------------------------------
# Frontend JS (vanilla). Raw string so regex/escapes stay literal. Talks only
# to the frozen API; the token rides on every request (header for fetch, query
# for EventSource/iframe/img which cannot set headers).
# ---------------------------------------------------------------------------
_APP_JS = r"""
(function(){
  "use strict";
  var TOKEN = window.SKILL_AB_TOKEN || "";
  var view = document.getElementById("view");
  var titleEl = document.getElementById("appbar-title");
  var dotEl = document.getElementById("health-dot");
  var htxtEl = document.getElementById("health-text");
  var tickerEl = document.getElementById("ticker");
  var tkNum = document.getElementById("tk-num");
  var tkFill = document.getElementById("tk-fill");
  var toastsEl = document.getElementById("toasts");
  var cleanup = null;  // teardown for the active view (closes EventSource etc.)

  // ---- tiny DOM builder: textContent for data => no HTML injection ----
  function E(tag, attrs, kids){
    var n = document.createElement(tag);
    if(attrs){
      for(var k in attrs){
        var v = attrs[k];
        if(v == null) continue;
        if(k === "class") n.className = v;
        else if(k === "text") n.textContent = v;
        else if(k === "html") n.innerHTML = v;          // trusted SVG only
        else if(k.slice(0,2) === "on") n.addEventListener(k.slice(2), v);
        else n.setAttribute(k, v);
      }
    }
    if(kids != null){
      if(!Array.isArray(kids)) kids = [kids];
      for(var i=0;i<kids.length;i++){
        var c = kids[i];
        if(c == null) continue;
        n.appendChild(typeof c === "string"
          ? document.createTextNode(c) : c);
      }
    }
    return n;
  }
  function clear(n){ while(n.firstChild) n.removeChild(n.firstChild); }

  function toast(msg, kind){
    var t = E("div", {class:"toast" + (kind ? " " + kind : ""), text:msg});
    toastsEl.appendChild(t);
    setTimeout(function(){
      t.style.opacity = "0";
      setTimeout(function(){ if(t.parentNode) t.remove(); }, 220);
    }, 4200);
  }
  function showError(err){
    toast((err && err.message) ? err.message : String(err), "err");
  }

  // ---- fetch wrapper: token header on every /api call ----
  function api(path, opts){
    opts = opts || {};
    var headers = opts.headers || {};
    headers["X-Skill-AB-Token"] = TOKEN;
    if(opts.json !== undefined){
      opts.body = JSON.stringify(opts.json);
      headers["Content-Type"] = "application/json";
      opts.method = opts.method || "POST";
      delete opts.json;
    }
    opts.headers = headers;
    return fetch(path, opts).then(function(r){
      if(!r.ok){
        return r.text().then(function(t){
          throw new Error("HTTP " + r.status + (t ? ": " + t.slice(0,140) : ""));
        });
      }
      var ct = r.headers.get("content-type") || "";
      return ct.indexOf("application/json") >= 0 ? r.json() : r.text();
    });
  }
  function withTok(url){
    return url + (url.indexOf("?") >= 0 ? "&" : "?") +
      "token=" + encodeURIComponent(TOKEN);
  }

  // ---- formatters ----
  function fmtTs(ts){
    if(!ts) return "—";
    var d = new Date(ts * 1000);
    if(isNaN(d.getTime()) || d.getFullYear() < 1990) return "—";
    return d.toLocaleDateString(undefined, {month:"short", day:"numeric"}) +
      " " + d.toLocaleTimeString(undefined, {hour:"2-digit", minute:"2-digit"});
  }
  function fmtUsage(x){
    return (x == null) ? "—" : Number(x).toFixed(2);
  }
  function fmtDur(s){
    if(s == null) return "—";
    s = Math.round(s);
    if(s < 90) return s + "s";
    var m = Math.round(s / 60);
    if(m < 90) return m + " min";
    return (m / 60).toFixed(1) + " h";
  }

  function verdictPill(v){
    var tone = "muted", label = v || "—";
    if(v === "verified"){ tone = "good"; }
    else if(v === "regressed"){ tone = "bad"; }
    else if(v === "running"){ tone = "run"; }
    else if(v === "error" || v === "aborted"){ tone = "bad"; }
    var cls = (tone === "muted") ? "pill" : "pill " + tone;
    return E("span", {class:cls}, [E("span", {class:"dot"}), label]);
  }
  function skillTitle(a, b){
    return E("span", {class:"rc-title"}, [
      (a || "?"),
      E("span", {class:"vs"}, "vs"),
      (b || "control")
    ]);
  }

  // ---- usage ticker (top bar; driven by the live view's cost events) ----
  function setTicker(spent, ceiling){
    tickerEl.hidden = false;
    var s = Number(spent || 0);
    tkNum.textContent = fmtUsage(s) +
      (ceiling != null ? " / " + Number(ceiling).toFixed(2) : "");
    var pct = ceiling ? Math.min(100, (s / ceiling) * 100) : 0;
    tkFill.style.width = pct + "%";
  }
  function hideTicker(){ tickerEl.hidden = true; tkFill.style.width = "0%"; }

  // ---- health (top-bar dot + Settings view) ----
  function refreshHealth(){
    return api("/api/health").then(function(hd){
      var on = hd && hd.claude_on_path;
      dotEl.className = "health-dot " + (on ? "ok" : (hd && hd.ok
        ? "warn" : "bad"));
      htxtEl.textContent = on
        ? ("claude " + (hd.claude_version || "ready"))
        : "claude not detected";
      return hd;
    }).catch(function(){
      dotEl.className = "health-dot bad";
      htxtEl.textContent = "offline";
      return null;
    });
  }

  // ===================== DASHBOARD =====================
  function viewDashboard(){
    view.appendChild(E("div", {class:"sec-h"}, [
      E("h2", {text:"Runs"}),
      E("a", {class:"btn btn-primary", href:"#/new"}, "New run")
    ]));
    var slot = E("div", {});
    view.appendChild(slot);
    slot.appendChild(loadingEl("loading runs…"));
    api("/api/runs").then(function(d){
      clear(slot);
      var runs = (d && d.runs) || [];
      if(!runs.length){ slot.appendChild(emptyState()); return; }
      var grid = E("div", {class:"run-grid"});
      runs.forEach(function(r){ grid.appendChild(runCard(r)); });
      slot.appendChild(grid);
    }).catch(function(e){ clear(slot); slot.appendChild(emptyState());
      showError(e); });
  }
  function loadingEl(msg){
    return E("div", {class:"loading"}, [E("span", {class:"spin"}), msg]);
  }
  function emptyState(){
    return E("div", {class:"card empty-state"}, [
      E("div", {class:"es-ic", html:
        "<svg width='22' height='22' viewBox='0 0 24 24' fill='none'>" +
        "<path d='M5 12h14M12 5v14' stroke='currentColor' stroke-width='2' " +
        "stroke-linecap='round'/></svg>"}),
      E("h3", {text:"No runs yet"}),
      E("p", {text:"Start a measured A/B run, or replay the bundled demo " +
        "end-to-end with zero spend to see the whole flow."}),
      E("div", {class:"row"}, [
        E("button", {class:"btn btn-primary", onclick:startDemo},
          "Run the demo (no spend)"),
        E("a", {class:"btn", href:"#/new"}, "Configure a run")
      ])
    ]);
  }
  function runCard(r){
    var dest = (r.status === "running")
      ? "#/run/" + encodeURIComponent(r.id)
      : "#/results/" + encodeURIComponent(r.id);
    var badge = r.badge_url
      ? E("img", {src:withTok(r.badge_url), alt:"verdict badge"})
      : null;
    var statusPill = (r.status && r.status !== "done")
      ? verdictPill(r.status) : null;
    var card = E("a", {class:"card run-card", href:dest}, [
      E("div", {class:"rc-top"}, [
        skillTitle(r.skill_a, r.skill_b),
        verdictPill(r.verdict)
      ]),
      E("div", {class:"badge-wrap"}, badge),
      E("div", {class:"rc-meta"}, [
        E("span", {}, [E("b", {text:"target "}), (r.target || ".")]),
        E("span", {}, [E("b", {text:"usage "}), fmtUsage(r.cost_usd)]),
        E("span", {}, [E("b", {text:"valid "}), String(r.n_valid != null
          ? r.n_valid : "—")]),
        E("span", {text:fmtTs(r.created_ts)}),
        statusPill
      ])
    ]);
    return card;
  }

  // ===================== NEW RUN =====================
  var SUB_NOTE = "Estimates are a usage proxy bounded by your Claude plan's " +
    "limits — not a dollar bill. Runs spawn claude -p under your subscription.";
  function viewNew(){
    var countHint = E("span", {class:"hint",
      text:"skill A vs skill B (or a no-skill control)"});
    view.appendChild(E("div", {class:"sec-h"}, [
      E("h2", {text:"New run"}), countHint
    ]));
    var skillA = E("input", {class:"inp", type:"text", id:"f-skill-a",
      name:"skill_a", autocomplete:"off", list:"skills-list",
      placeholder:"pick or type a skill", "aria-label":"skill A"});
    var skillB = E("input", {class:"inp", type:"text", id:"f-skill-b",
      name:"skill_b", autocomplete:"off", list:"skills-list",
      placeholder:"none = control", "aria-label":"skill B"});
    // Skill picker: a shared <datalist> populated from /api/skills (the same skills
    // resolve_skill would find). Free text still works for an uninstalled name/path.
    var skillsList = E("datalist", {id:"skills-list"});
    var skillsByName = {};
    var aSrc = E("span", {class:"src-hint"});
    var bSrc = E("span", {class:"src-hint"});
    function srcFor(input, hintEl){
      var v = input.value.trim();
      if(!v || v.toLowerCase() === "none"){ hintEl.textContent = ""; return; }
      var s = skillsByName[v.toLowerCase()];
      hintEl.textContent = s ? ("✓ found · " + s.source)
        : "not in your installed skills (resolves at run time)";
      hintEl.className = "src-hint " + (s ? "src-ok" : "src-warn");
    }
    skillA.addEventListener("input", function(){ srcFor(skillA, aSrc); });
    skillB.addEventListener("input", function(){ srcFor(skillB, bSrc); });
    // Per-arm MODEL selectors: pick different models on A vs B to compare MODELS
    // (e.g. the same skill under sonnet vs opus). "default" uses the experiment model.
    var MODELS = [
      {v:"", t:"default"}, {v:"claude-opus-4-8", t:"opus"},
      {v:"claude-sonnet-4-6", t:"sonnet"}, {v:"claude-haiku-4-5-20251001", t:"haiku"}
    ];
    function modelSelect(id, lbl){
      return E("select", {class:"inp", id:id, name:id, "aria-label":lbl},
        MODELS.map(function(m){ return E("option", {value:m.v}, m.t); }));
    }
    var modelA = modelSelect("f-model-a", "model for arm A");
    var modelB = modelSelect("f-model-b", "model for arm B");
    var includeControl = E("input", {type:"checkbox", id:"f-include-control",
      name:"include_control", "aria-label":"include control"});
    includeControl.checked = true;
    var target = E("input", {class:"inp", type:"text", id:"f-target",
      name:"target", autocomplete:"off",
      placeholder:"PR URL, branch, or .", value:".", "aria-label":"target"});
    var kVal = E("span", {class:"kval", text:"3"});
    var kSlider = E("input", {class:"krange", type:"range", min:"1", max:"10",
      id:"f-k", name:"k", step:"1", value:"3", "aria-label":"runs per cell"});
    kSlider.addEventListener("input", function(){
      kVal.textContent = kSlider.value; invalidate();
    });
    var iso = E("select", {class:"inp", id:"f-iso", name:"isolation",
      "aria-label":"isolation"}, [
      E("option", {value:"inject"}, "inject (append-system-prompt)"),
      E("option", {value:"worktree"}, "worktree (install SKILL.md)")
    ]);
    var judge = E("input", {type:"checkbox", id:"f-judge", name:"judge",
      "aria-label":"blind judge"});
    var startBtn = E("button", {class:"btn btn-primary", disabled:"",
      onclick:doStart}, "Start run");
    var estBox = E("div", {class:"estimate", hidden:""});

    function req(demo){
      var sb = skillB.value.trim();
      return {
        skill_a: skillA.value.trim(),
        skill_b: (sb && sb.toLowerCase() !== "none") ? sb : null,
        model_a: modelA.value || null,
        model_b: modelB.value || null,
        include_control: includeControl.checked,
        target: target.value.trim() || ".",
        k: parseInt(kSlider.value, 10),
        isolation: iso.value,
        judge: judge.checked,
        demo: !!demo
      };
    }
    function invalidate(){
      startBtn.disabled = true;
      if(!estBox.hidden) estBox.classList.add("stale");
    }
    [skillA, skillB, target, iso, judge, modelA, modelB, includeControl]
        .forEach(function(n){
      n.addEventListener("input", invalidate);
      n.addEventListener("change", invalidate);
    });
    function doEstimate(){
      if(!skillA.value.trim()){ toast("skill A is required", "err"); return; }
      estBox.classList.remove("stale");
      clear(estBox); estBox.hidden = false;
      estBox.appendChild(loadingEl("estimating…"));
      api("/api/estimate", {json:req(false)}).then(function(e){
        renderEstimate(estBox, e); startBtn.disabled = false;
      }).catch(function(err){ estBox.hidden = true; showError(err); });
    }
    function doStart(){
      if(!skillA.value.trim()){ toast("skill A is required", "err"); return; }
      startBtn.disabled = true;
      api("/api/runs", {json:req(false)}).then(function(d){
        location.hash = "#/run/" + encodeURIComponent(d.run_id);
      }).catch(function(err){ startBtn.disabled = false; showError(err); });
    }

    // Load the installed-skill list (best-effort: the picker enriches the form but
    // free text still works if the fetch fails or nothing is installed).
    api("/api/skills").then(function(d){
      var skills = (d && d.skills) || [];
      skills.forEach(function(s){
        skillsByName[s.name.toLowerCase()] = s;
        skillsList.appendChild(E("option", {value:s.name, label:s.source}));
      });
      var n = skills.length;
      countHint.textContent = n ? (n + " skill" + (n === 1 ? "" : "s") +
        " found in your project · ~/.claude · plugins — pick one or type a path")
        : "no installed skills found — type a name or path";
    }).catch(function(){ /* picker is optional; free text still works */ });

    var form = E("div", {class:"card card-pad"}, [
      skillsList,
      E("div", {class:"form"}, [
        E("div", {class:"field-row"}, [
          field("Skill A", "the skill under test", E("div", {}, [skillA, aSrc])),
          field("Model A", "run arm A on", modelA)
        ]),
        E("div", {class:"field-row"}, [
          field("Skill B", "another skill, or \"none\" for a control",
            E("div", {}, [skillB, bSrc])),
          field("Model B", "run arm B on", modelB)
        ]),
        E("p", {class:"hint", style:"margin:-4px 0 0",
          text:"Pick different models on A vs B to compare MODELS (same skill, " +
            "sonnet vs opus). Set Skill B = none + a Model B to A/B your skill " +
            "across two models."}),
        field("Target", "PR URL fetched non-invasively, a branch, or \".\"",
          target),
        E("div", {class:"field-row"}, [
          field("Runs per cell (k)", "more k = tighter CIs, more usage",
            E("div", {class:"krow"}, [kSlider, kVal])),
          field("Isolation", "how the skill is delivered", iso)
        ]),
        E("div", {class:"field"}, [
          E("label", {class:"switch"}, [
            includeControl, E("span", {class:"track"}, E("span", {class:"knob"})),
            E("span", {text:"Include a no-skill control arm"})
          ]),
          E("span", {class:"hint",
            text:"off = pure A vs B (cheaper; no no-skill baseline)"})
        ]),
        E("div", {class:"field"}, [
          E("label", {class:"switch"}, [
            judge, E("span", {class:"track"}, E("span", {class:"knob"})),
            E("span", {text:"Run the blind qualitative judge"})
          ]),
          E("span", {class:"hint", text:"extra claude calls; compares diffs " +
            "with arm labels stripped"})
        ]),
        E("div", {class:"row"}, [
          E("button", {class:"btn", onclick:doEstimate}, "Estimate"),
          startBtn,
          E("button", {class:"btn btn-ghost", onclick:startDemo},
            "Try the demo (no spend)")
        ]),
        estBox
      ])
    ]);
    view.appendChild(form);
    view.appendChild(E("p", {class:"note-card", style:"margin-top:14px",
      text:SUB_NOTE}));
  }
  function field(label, hint, control){
    // Wire the visible label to its control's id (the control may be the input
    // itself or a wrapper holding one, e.g. the slider row) so clicking the label
    // focuses the field and screen readers associate them.
    var cid = control.id ||
      (control.querySelector && (control.querySelector("input,select,textarea")||{}).id);
    return E("div", {class:"field"}, [
      E("label", cid ? {text:label, "for":cid} : {text:label}),
      hint ? E("span", {class:"hint", text:hint}) : null,
      control
    ]);
  }
  function renderEstimate(box, e){
    clear(box);
    box.appendChild(E("div", {class:"est-grid"}, [
      estCell("Runs", String(e.n_runs != null ? e.n_runs : "—"),
        "task x arm x k"),
      estCell("Usage proxy", "≈ " + fmtUsage(e.projected_usd),
        "not dollars billed"),
      estCell("Wall time", fmtDur(e.projected_wall_seconds), "rough estimate"),
      estCell("Judge calls", String(e.n_judge_calls != null
        ? e.n_judge_calls : 0), "if judge on")
    ]));
    box.appendChild(E("p", {class:"note-card",
      text:(e.note ? e.note + " " : "") + SUB_NOTE}));
  }
  function estCell(cap, val, sub){
    return E("div", {class:"est-cell"}, [
      E("div", {class:"ec-cap", text:cap}),
      E("div", {class:"ec-val", text:val}),
      E("div", {class:"ec-sub", text:sub})
    ]);
  }

  // shared: start a zero-spend demo run, then jump to its live view.
  function startDemo(){
    var req = {skill_a:"demo-skill", skill_b:null, target:".", k:2,
      isolation:"inject", judge:false, demo:true};
    api("/api/runs", {json:req}).then(function(d){
      location.hash = "#/run/" + encodeURIComponent(d.run_id) + "?demo=1";
    }).catch(showError);
  }

  // ===================== LIVE RUN =====================
  function viewLive(id, isDemo){
    var CELLS = {};            // label -> {el, st}
    var LAB_IDX = {}, LAB_N = 0;
    var finished = false, es = null;
    var spent = 0, ceiling = null;

    var demoBadge = isDemo
      ? E("span", {class:"demo-badge"}, "DEMO · no spend") : null;
    var abortBtn = E("button", {class:"btn btn-danger", onclick:doAbort},
      "Abort");
    var head = E("div", {class:"live-head"}, [
      E("div", {}, [
        E("h2", {style:"font-size:17px;font-weight:680",
          text:"Live run"}),
        E("div", {class:"live-id", text:id})
      ]),
      E("div", {class:"row"}, [demoBadge, abortBtn])
    ]);
    var gridEl = E("div", {class:"cell-grid"});
    var legend = E("div", {class:"legend-row"}, [
      legendItem("var(--card-2)", "pending"),
      legendItem("color-mix(in srgb,#1f77b4 30%,var(--card-2))", "running"),
      legendItem("var(--good-bg)", "valid"),
      legendItem("var(--bad-bg)", "invalid"),
      legendItem("#fff3d6", "contaminated")
    ]);
    var consoleEl = E("div", {class:"console"}, [
      E("div", {class:"console-empty", text:"waiting for agent output…"})
    ]);
    var doneSlot = E("div", {});

    view.appendChild(head);
    view.appendChild(E("div", {class:"sec-h",
      style:"margin-top:18px"}, [E("h2", {style:"font-size:15px",
      text:"Cells"})]));
    view.appendChild(gridEl);
    view.appendChild(legend);
    view.appendChild(E("div", {class:"sec-h",
      style:"margin-top:18px"}, [E("h2", {style:"font-size:15px",
      text:"Live console"})]));
    view.appendChild(consoleEl);
    view.appendChild(doneSlot);

    function legendItem(bg, label){
      var sw = E("span", {class:"sw"});
      sw.style.background = bg;
      return E("span", {class:"lg"}, [sw, label]);
    }
    function labClass(label){
      if(!label) return "sys";
      if(!(label in LAB_IDX)){ LAB_IDX[label] = (LAB_N++) % 6; }
      return "lab-" + LAB_IDX[label];
    }
    function appendLine(label, text, kind){
      if(consoleEl.firstChild &&
         consoleEl.firstChild.className === "console-empty"){
        clear(consoleEl);
      }
      var line = E("div", {class:"cline " + labClass(label)});
      if(label) line.appendChild(E("span", {class:"lab", text:label}));
      if(kind === "tool"){
        line.appendChild(E("span", {class:"tool", text:text}));
      } else {
        line.appendChild(document.createTextNode(text));
      }
      var atBottom = consoleEl.scrollTop + consoleEl.clientHeight >=
        consoleEl.scrollHeight - 8;
      consoleEl.appendChild(line);
      if(atBottom) consoleEl.scrollTop = consoleEl.scrollHeight;
    }
    function setCell(label, status){
      var c = CELLS[label];
      if(!c) return;
      c.el.className = "cell " + status;
      c.st.textContent = status;
    }
    function resetLive(ev){       // reconnect-safe: replays always start here
      CELLS = {}; clear(gridEl); clear(consoleEl);
      consoleEl.appendChild(E("div", {class:"console-empty",
        text:"waiting for agent output…"}));
      (ev.cells || []).forEach(function(cell){
        var st = E("div", {class:"c-st", text:"pending"});
        var box = E("div", {class:"cell pending"}, [
          E("div", {class:"c-lab", title:cell.label, text:cell.label}), st
        ]);
        CELLS[cell.label] = {el:box, st:st};
        gridEl.appendChild(box);
      });
    }

    var HANDLERS = {
      experiment_start: function(ev){ resetLive(ev); },
      run_start: function(ev){
        setCell(ev.label, "running");
        appendLine(ev.label, "started", "sys");
      },
      agent: function(ev){
        if(ev.kind === "text" && ev.text){
          appendLine(ev.label, ev.text, "text");
        } else if(ev.kind === "tool"){
          appendLine(ev.label, "tool: " + (ev.tool || "?"), "tool");
        } else if(ev.kind === "result"){
          appendLine(ev.label, "↳ turns " +
            (ev.turns == null ? "?" : ev.turns) +
            " · usage " + fmtUsage(ev.cost_usd), "sys");
        }
      },
      run_done: function(ev){
        var status = ev.contaminated_by ? "contaminated"
          : (ev.itt_valid ? "valid" : "invalid");
        setCell(ev.label, status);
        var bits = [status];
        if(ev.contaminated_by) bits.push("by " + ev.contaminated_by);
        if(ev.activated) bits.push("skill fired");
        bits.push("turns " + (ev.turns == null ? "?" : ev.turns));
        bits.push("usage " + fmtUsage(ev.cost_usd));
        bits.push((ev.diff_lines || 0) + " diff lines");
        appendLine(ev.label, "done — " + bits.join(" · "), "sys");
      },
      cost: function(ev){
        spent = ev.spent_usd != null ? ev.spent_usd : spent;
        ceiling = ev.ceiling_usd;
        setTicker(spent, ceiling);
      },
      experiment_done: function(ev){
        finished = true;
        if(es) es.close();
        abortBtn.disabled = true;
        clear(doneSlot);
        doneSlot.appendChild(E("div", {class:"card card-pad",
          style:"margin-top:18px"}, [
          E("div", {class:"row", style:"justify-content:space-between"}, [
            E("div", {class:"row"}, [
              E("strong", {style:"font-size:15px", text:"Run complete"}),
              verdictPill(ev.verdict)
            ]),
            E("div", {class:"done-cta"}, [
              E("a", {class:"btn btn-primary",
                href:"#/results/" + encodeURIComponent(ev.run_id || id)},
                "View report"),
              E("a", {class:"btn btn-ghost", href:"#/"}, "Dashboard")
            ])
          ])
        ]));
      },
      error: function(ev){
        finished = true;
        if(es) es.close();
        var msg = ev.message || "run error";
        appendLine(null, "error: " + msg, "sys");
        toast(msg, "err");
      }
    };

    function doAbort(){
      api("/api/runs/" + encodeURIComponent(id) + "/abort",
        {method:"POST"}).then(function(){
        toast("abort requested");
      }).catch(showError);
    }

    es = new EventSource(withTok("/api/runs/" +
      encodeURIComponent(id) + "/events"));
    es.onmessage = function(m){
      var ev;
      try { ev = JSON.parse(m.data); } catch(e){ return; }
      var fn = HANDLERS[ev.type];
      if(fn) fn(ev);
    };
    es.onerror = function(){
      if(finished){ if(es) es.close(); return; }
      // A permanent CLOSED (e.g. 404: unknown run after a server restart) never
      // reconnects -- tell the user instead of leaving an empty "waiting" screen.
      // A transient drop (readyState CONNECTING) auto-reconnects + replays backlog.
      if(es && es.readyState === EventSource.CLOSED){
        appendLine(null, "stream closed — run not found or server restarted", "sys");
        toast("run unavailable", "err");
        es.close();
      }
    };

    cleanup = function(){
      finished = true;
      if(es) es.close();
      hideTicker();
    };
  }

  // ===================== RESULTS =====================
  function viewResults(id){
    var reportUrl = "/api/runs/" + encodeURIComponent(id) + "/report";
    var badgeUrl = "/api/runs/" + encodeURIComponent(id) + "/badge";
    view.appendChild(E("a", {class:"back-link", href:"#/"}, "← Dashboard"));
    view.appendChild(E("div", {class:"sec-h"}, [
      E("h2", {text:"Results"}),
      E("span", {class:"live-id", text:id})
    ]));
    var badgeRow = E("div", {class:"card card-pad",
      style:"margin-bottom:14px"}, [
      E("div", {class:"row", style:"justify-content:space-between"}, [
        E("img", {src:withTok(badgeUrl), alt:"verdict badge",
          style:"height:20px"}),
        E("button", {class:"btn copybtn", onclick:copyMd},
          "Copy badge markdown")
      ]),
      E("div", {class:"md-box", id:"md-box", text:mdText()})
    ]);
    view.appendChild(badgeRow);
    var frame = E("iframe", {class:"frame", src:withTok(reportUrl),
      title:"run report", loading:"lazy"});
    view.appendChild(frame);

    function mdText(){
      // Never embed the session token in shareable markdown (it's a full-capability
      // secret, and the badge is meant to be pasted into a README/PR). Point at the
      // on-disk badge.svg the engine writes next to the run; commit that for sharing.
      return "![skill-ab](badge.svg)  <!-- " + location.origin
        + badgeUrl + " (local only; commit run badge.svg to share) -->";
    }
    function copyMd(e){
      var btn = e.currentTarget, txt = mdText();
      var done = function(){
        btn.textContent = "Copied!";
        setTimeout(function(){ btn.textContent = "Copy badge markdown"; }, 1400);
      };
      if(navigator.clipboard && navigator.clipboard.writeText){
        navigator.clipboard.writeText(txt).then(done, function(){ done(); });
      } else { done(); }
    }
  }

  // ===================== GALLERY =====================
  function viewGallery(){
    view.appendChild(E("div", {class:"sec-h"}, [
      E("h2", {text:"Gallery"}),
      E("span", {class:"hint", text:"self-reported summaries across all runs"})
    ]));
    view.appendChild(E("iframe", {class:"frame",
      src:withTok("/api/gallery"), title:"gallery", loading:"lazy"}));
  }

  // ===================== SETTINGS / HEALTH =====================
  function viewSettings(){
    view.appendChild(E("div", {class:"sec-h"}, [E("h2", {text:"Settings"})]));
    var slot = E("div", {class:"card card-pad"});
    view.appendChild(slot);
    slot.appendChild(loadingEl("checking claude…"));
    refreshHealth().then(function(hd){
      clear(slot);
      hd = hd || {};
      var hint = hd.claude_on_path
        ? ("Claude Code detected" + (hd.claude_version
            ? " (" + hd.claude_version + ")" : "") +
           ". Runs will use your logged-in subscription.")
        : "claude was not found on PATH — install Claude Code and log in " +
          "before starting a real run (demo runs still work).";
      slot.appendChild(E("div", {class:"row",
        style:"margin-bottom:14px"}, [
        E("span", {class:"pill " + (hd.claude_on_path ? "good" : "bad")}, [
          E("span", {class:"dot"}),
          hd.claude_on_path ? "looks logged in" : "not detected"
        ]),
        E("span", {class:"hint", text:hint})
      ]));
      var dl = E("dl", {class:"kv"}, [
        E("dt", {text:"claude on PATH"}),
        E("dd", {text:hd.claude_on_path ? "yes" : "no"}),
        E("dt", {text:"claude version"}),
        E("dd", {class:"mono", text:hd.claude_version || "—"}),
        E("dt", {text:"model"}),
        E("dd", {class:"mono", text:hd.model || "—"}),
        E("dt", {text:"runs dir"}),
        E("dd", {class:"mono", text:hd.runs_dir || "—"}),
        E("dt", {text:"harness version"}),
        E("dd", {class:"mono", text:hd.harness_version || "—"})
      ]);
      slot.appendChild(dl);
    }).catch(function(e){ clear(slot);
      slot.appendChild(E("p", {text:"health check failed"})); showError(e); });
    view.appendChild(E("p", {class:"note-card", style:"margin-top:14px",
      text:"Every run uses your Claude Code subscription via the claude -p " +
        "CLI under your existing login — no separate credentials are read. " +
        "The “cost” figure is a usage proxy bounded by your plan's " +
        "limits, shown so you can gauge spend before confirming a run."}));
  }

  // ===================== ROUTER =====================
  var ROUTES = {
    "": {title:"Dashboard", nav:"", fn:viewDashboard},
    "new": {title:"New run", nav:"new", fn:viewNew},
    "gallery": {title:"Gallery", nav:"gallery", fn:viewGallery},
    "settings": {title:"Settings & health", nav:"settings", fn:viewSettings}
  };
  function parseQuery(q){
    var o = {};
    if(!q) return o;
    q.split("&").forEach(function(p){
      var kv = p.split("=");
      o[decodeURIComponent(kv[0])] = decodeURIComponent(kv[1] || "");
    });
    return o;
  }
  function setActiveNav(key){
    var links = document.querySelectorAll(".nav a");
    for(var i=0;i<links.length;i++){
      links[i].classList.toggle("active",
        links[i].getAttribute("data-route") === key);
    }
  }
  function render(){
    if(cleanup){ try { cleanup(); } catch(e){} cleanup = null; }
    hideTicker();
    clear(view);
    window.scrollTo(0, 0);
    var hash = location.hash.replace(/^#/, "");
    var qi = hash.indexOf("?");
    var query = parseQuery(qi >= 0 ? hash.slice(qi + 1) : "");
    if(qi >= 0) hash = hash.slice(0, qi);
    if(hash.charAt(0) === "/") hash = hash.slice(1);
    var parts = hash.split("/").filter(Boolean);
    var seg0 = parts[0] || "";

    if(seg0 === "run" && parts[1]){
      titleEl.textContent = "Live run"; setActiveNav("");
      viewLive(decodeURIComponent(parts[1]), query.demo === "1");
      return;
    }
    if(seg0 === "results" && parts[1]){
      titleEl.textContent = "Results"; setActiveNav("");
      viewResults(decodeURIComponent(parts[1]));
      return;
    }
    var r = ROUTES[seg0] || ROUTES[""];
    titleEl.textContent = r.title; setActiveNav(r.nav);
    r.fn();
  }

  window.addEventListener("hashchange", render);
  refreshHealth();
  render();
})();
"""


def app_shell_html(token: str) -> str:
    """Return the full single-page app document with the session token embedded.

    WHY json.dumps + the `<` escape: json.dumps yields a valid JS string literal
    but does NOT escape `/`, so a token containing `</script>` would otherwise
    close the script tag. Replacing `<` with its `\\u003c` escape neutralizes
    `</script>`/`<!--` breakouts — defense-in-depth even though the server token
    is a random per-process nonce, not user input.
    """
    token_js = json.dumps(token).replace("<", "\\u003c")
    return "".join([
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<title>skill-ab</title>",
        "<style>", h._HTML_STYLE, _APP_CSS, "</style></head>",
        "<body class='app-body'>",
        _SHELL_HTML,
        "<script>window.SKILL_AB_TOKEN=", token_js, ";</script>",
        "<script>", _APP_JS, "</script>",
        "</body></html>",
    ])
