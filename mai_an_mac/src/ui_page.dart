/// The whole control page: one file, no build step, no network assets.
/// Everything from the phone (names, titles, file names) goes in with
/// textContent, never as HTML.
const uiHtml = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>Mai An Companion</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='15' fill='%23FFD60A'/%3E%3Cg fill='%23111'%3E%3Crect x='13' y='26' width='6' height='12' rx='3'/%3E%3Crect x='23' y='18' width='6' height='28' rx='3'/%3E%3Crect x='33' y='12' width='6' height='40' rx='3'/%3E%3Crect x='43' y='22' width='6' height='20' rx='3'/%3E%3C/g%3E%3C/svg%3E">
<style>
:root{
  --bg:#000; --card:#1c1c1e; --card2:#2c2c2e; --ink:#fff; --mute:#8e8e93; --faint:#636366;
  --line:rgba(255,255,255,.08); --line2:#38383a;
  --accent:#ffd60a; --accent-ink:#111; --accent-soft:rgba(255,214,10,.12);
  --ok:#30d158; --warn:#ff9f0a; --bad:#ff453a; --blue:#0a84ff; --violet:#bf5af2;
  --shadow:0 1px 0 rgba(255,255,255,.04) inset, 0 10px 30px rgba(0,0,0,.35);
}
@media (prefers-color-scheme:light){
  :root{--bg:#f2f2f7; --card:#fff; --card2:#f2f2f7; --ink:#1c1c1e; --mute:#6e6e73; --faint:#aeaeb2;
    --line:rgba(0,0,0,.07); --line2:#d1d1d6;
    --accent:#e0b400; --accent-ink:#111; --accent-soft:rgba(224,180,0,.14);
    --ok:#1f9d55; --warn:#c77700; --bad:#d63a2f; --blue:#0a6fd8; --violet:#8e3fc4;
    --shadow:0 1px 2px rgba(0,0,0,.04), 0 8px 24px rgba(0,0,0,.06);}
}
*{box-sizing:border-box}
html{background:var(--bg)}
body{margin:0;color:var(--ink);background:
  radial-gradient(1200px 500px at 15% -10%, var(--accent-soft), transparent 60%), var(--bg);
  min-height:100vh;
  font:14px/1.45 -apple-system,BlinkMacSystemFont,"SF Pro Text",system-ui,sans-serif;
  -webkit-font-smoothing:antialiased}
main{max-width:1120px;margin:0 auto;padding:22px 22px 60px}
.top{display:flex;align-items:center;gap:14px;margin-bottom:22px}
.logo{width:40px;height:40px;border-radius:11px;background:var(--accent);display:grid;place-items:center;
  box-shadow:0 6px 18px rgba(255,214,10,.25)}
.top h1{font-size:19px;margin:0;letter-spacing:-.015em;font-weight:700}
.top .sub{font-size:12px;color:var(--mute)}
.spacer{flex:1}
.pill{display:inline-flex;align-items:center;gap:8px;padding:6px 12px;border-radius:999px;
  background:var(--card);border:1px solid var(--line);font-size:12.5px;font-weight:500;white-space:nowrap}
.dot{width:8px;height:8px;border-radius:50%;background:var(--faint);flex:none}
.pill.on .dot{background:var(--ok);box-shadow:0 0 0 4px color-mix(in srgb,var(--ok) 22%,transparent);animation:pulse 2.4s infinite}
.pill.off .dot{background:var(--bad)}
@keyframes pulse{50%{box-shadow:0 0 0 7px transparent}}
.grid{display:grid;grid-template-columns:1fr;gap:14px}
@media (min-width:920px){.grid{grid-template-columns:1fr 1fr;align-items:start}}
/* tabs */
.tabs{position:sticky;top:10px;z-index:5;display:flex;gap:2px;width:fit-content;margin:0 0 18px;padding:3px;
  background:color-mix(in srgb,var(--card) 88%,transparent);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);
  border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow)}
.tabs button{border:0;background:none;color:var(--mute);padding:7px 16px;border-radius:9px;font-size:13px;display:flex;align-items:center;gap:7px}
.tabs button:hover{color:var(--ink);filter:none}
.tabs button.on{background:var(--card2);color:var(--ink);box-shadow:0 1px 3px rgba(0,0,0,.2)}
.count{font-size:10.5px;font-weight:650;min-width:17px;height:17px;padding:0 5px;border-radius:99px;display:inline-grid;place-items:center;
  background:var(--line2);color:var(--ink)}
.count:empty{display:none}
.count.acc{background:var(--accent);color:var(--accent-ink)} .count.bad{background:var(--bad);color:#fff} .count.ok{background:var(--ok);color:#fff}
.veil{position:fixed;inset:12px;border:2px dashed var(--accent);border-radius:20px;background:color-mix(in srgb,var(--bg) 70%,transparent);
  display:none;place-items:center;z-index:20;font-size:16px;font-weight:600;pointer-events:none;text-align:center;padding:20px}
body.dragging:not([data-tab=music]) .veil{display:grid}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px 18px 16px;box-shadow:var(--shadow);min-width:0}
.card.wide{grid-column:1/-1}
.head{display:flex;align-items:center;gap:10px;margin-bottom:12px}
.head h2{font-size:14.5px;margin:0;font-weight:650;letter-spacing:-.005em}
.head .ico{width:28px;height:28px;border-radius:8px;display:grid;place-items:center;flex:none}
.head .right{margin-left:auto;display:flex;gap:6px;align-items:center}
.ico svg{width:16px;height:16px}
.row{display:flex;align-items:center;justify-content:space-between;gap:12px}
.mute{color:var(--mute)} .faint{color:var(--faint)} .small{font-size:12.5px} .tiny{font-size:11.5px}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.strong{font-weight:600}
button{font:inherit;font-size:12.5px;font-weight:550;border:1px solid var(--line2);background:var(--card2);
  color:var(--ink);border-radius:9px;padding:6px 12px;cursor:pointer;white-space:nowrap;transition:filter .12s,border-color .12s}
button:hover{filter:brightness(1.12)}
button:active{transform:translateY(.5px)}
button.primary{background:var(--accent);color:var(--accent-ink);border-color:transparent}
button.ghost{background:none;border-color:transparent;color:var(--mute);padding:4px 8px}
button.ghost:hover{color:var(--ink);background:var(--card2)}
button.danger{color:var(--bad)}
button:disabled{opacity:.45;cursor:default;filter:none}
.btns{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
/* pairing */
.pinbox{display:flex;gap:8px;margin:6px 0 12px}
.pinbox span{width:52px;height:64px;border-radius:12px;background:var(--card2);border:1px solid var(--line2);
  display:grid;place-items:center;font:700 32px/1 ui-monospace,SFMono-Regular,Menlo,monospace}
.device{display:flex;gap:14px;align-items:center}
.phone-glyph{width:44px;height:44px;border-radius:12px;background:color-mix(in srgb,var(--ok) 16%,transparent);
  color:var(--ok);display:grid;place-items:center;flex:none}
.phone-glyph svg{width:22px;height:22px}
.badge{display:inline-flex;align-items:center;gap:5px;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600;
  background:var(--card2);color:var(--mute);border:1px solid var(--line)}
.badge.ok{color:var(--ok);background:color-mix(in srgb,var(--ok) 12%,transparent);border-color:transparent}
.badge.warn{color:var(--warn);background:color-mix(in srgb,var(--warn) 13%,transparent);border-color:transparent}
.badge.bad{color:var(--bad);background:color-mix(in srgb,var(--bad) 12%,transparent);border-color:transparent}
.badge.acc{color:var(--accent-ink);background:var(--accent);border-color:transparent}
.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 14px;font-size:12.5px;margin-top:12px}
.kv dt{color:var(--mute)} .kv dd{margin:0;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* progress */
.bar{height:6px;border-radius:99px;background:var(--card2);overflow:hidden;margin:10px 0 8px}
.bar>i{display:block;height:100%;background:var(--accent);width:0;transition:width .35s ease;border-radius:99px}
.bar.ok>i{background:var(--ok)}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:10px}
.stat{background:var(--card2);border-radius:10px;padding:8px 10px}
.stat b{display:block;font-size:17px;font-weight:650;letter-spacing:-.01em}
.stat span{font-size:11px;color:var(--mute)}
/* update steps */
.steps{display:flex;align-items:center;gap:0;margin:14px 0 6px}
.step{display:flex;align-items:center;gap:6px;font-size:11.5px;color:var(--faint);white-space:nowrap}
.step i{width:18px;height:18px;border-radius:50%;border:1.5px solid var(--line2);display:grid;place-items:center;font-style:normal;font-size:10px}
.step.done{color:var(--mute)} .step.done i{background:var(--ok);border-color:var(--ok);color:#fff}
.step.now{color:var(--ink);font-weight:600} .step.now i{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.step.fail{color:var(--bad)} .step.fail i{border-color:var(--bad);color:var(--bad)}
.link{flex:1;height:1.5px;background:var(--line2);margin:0 8px;min-width:12px}
.version{font-size:26px;font-weight:700;letter-spacing:-.02em;line-height:1.1}
/* forms */
.seg{display:inline-flex;background:var(--card2);border-radius:9px;padding:2px;border:1px solid var(--line)}
.seg button{border:0;background:none;padding:5px 10px;border-radius:7px;color:var(--mute);font-weight:550}
.seg button.on{background:var(--card);color:var(--ink);box-shadow:0 1px 3px rgba(0,0,0,.25)}
.switch{position:relative;width:38px;height:22px;flex:none}
.switch input{opacity:0;width:0;height:0}
.switch span{position:absolute;inset:0;background:var(--line2);border-radius:99px;transition:.18s;cursor:pointer}
.switch span::after{content:"";position:absolute;left:2px;top:2px;width:18px;height:18px;border-radius:50%;background:#fff;transition:.18s;box-shadow:0 1px 3px rgba(0,0,0,.3)}
.switch input:checked+span{background:var(--ok)}
.switch input:checked+span::after{transform:translateX(16px)}
.opt{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:10px 0;border-top:1px solid var(--line)}
.opt:first-of-type{border-top:0}
.drop{border:1.5px dashed var(--line2);border-radius:14px;padding:22px 16px;text-align:center;color:var(--mute);
  cursor:pointer;transition:.15s;margin-top:12px}
.drop b{color:var(--ink);font-weight:600}
.drop:hover{border-color:var(--mute)}
body.dragging .drop{border-color:var(--accent);background:var(--accent-soft);color:var(--ink)}
.drop svg{width:26px;height:26px;display:block;margin:0 auto 6px;opacity:.8}
/* lists */
ul{list-style:none;margin:10px 0 0;padding:0}
li{display:flex;align-items:center;gap:10px;padding:8px 0;border-top:1px solid var(--line)}
li:first-child{border-top:0}
li .t{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
li .t small{display:block;color:var(--mute);font-size:11.5px;overflow:hidden;text-overflow:ellipsis}
.thumb{width:30px;height:30px;border-radius:8px;display:grid;place-items:center;flex:none;background:var(--card2);color:var(--mute)}
.thumb svg{width:15px;height:15px}
.empty{color:var(--mute);font-size:12.5px;padding:14px 0 4px;text-align:center}
/* log */
.log{max-height:260px;overflow:auto;background:var(--card2);border-radius:10px;padding:8px 10px;margin-top:4px;
  font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace}
.log div{display:flex;gap:10px;white-space:pre-wrap;word-break:break-word}
.log time{color:var(--faint);flex:none}
.warnline{color:var(--warn);font-size:12.5px;margin-top:10px;display:flex;gap:6px;align-items:flex-start}
footer{color:var(--faint);font-size:11.5px;text-align:center;margin-top:26px}
.toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%) translateY(20px);opacity:0;transition:.25s;
  background:var(--ink);color:var(--bg);padding:9px 16px;border-radius:10px;font-size:13px;font-weight:550;pointer-events:none;
  box-shadow:0 10px 30px rgba(0,0,0,.35)}
.toast.show{opacity:1;transform:translateX(-50%)}
/* build */
.bar.indet>i{width:28%;animation:indet 1.3s ease-in-out infinite}
@keyframes indet{from{transform:translateX(-110%)}to{transform:translateX(390%)}}
.chips{display:flex;flex-wrap:wrap;gap:8px 18px;margin:12px 0 4px}
.chip{display:flex;align-items:center;gap:8px;font-size:12.5px;color:var(--mute);cursor:pointer}
.chip .switch{transform:scale(.82);transform-origin:left center;margin-right:-6px}
.result{font-size:12.5px;margin-top:10px;display:flex;gap:8px;align-items:flex-start}
.result.ok{color:var(--ok)} .result.bad{color:var(--bad)}
details.out{margin-top:10px}
details.out summary{font-size:12px;color:var(--mute);cursor:pointer;user-select:none}
details.out .log{max-height:220px;margin-top:6px}
/* services */
.svcs{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px}
.svc{background:var(--card2);border-radius:12px;padding:12px 12px 11px;display:flex;gap:12px;align-items:flex-start;border:1px solid var(--line);transition:opacity .15s}
.svc .d{flex:1;min-width:0}
.svc .t{font-weight:600;font-size:13px}
.svc .x{font-size:11.5px;color:var(--mute);margin-top:2px}
.svc .s{font-size:11.5px;margin-top:6px;display:flex;align-items:center;gap:6px;color:var(--mute)}
.svc .s .dot{width:6px;height:6px}
.svc .s.on .dot{background:var(--ok)} .svc .s.busy .dot{background:var(--accent)} .svc .s.on,.svc .s.busy{color:var(--ink)}
.svc.dis{opacity:.5}
/* notes and reports */
textarea{width:100%;min-height:74px;resize:vertical;font:12.5px/1.5 -apple-system,BlinkMacSystemFont,system-ui,sans-serif;color:var(--ink);
  background:var(--card2);border:1px solid var(--line2);border-radius:10px;padding:8px 10px;outline:none}
textarea:focus{border-color:var(--accent)}
.notes{margin-top:12px}
.notes .row{margin-bottom:6px}
.rep{cursor:pointer}
.rep.new .t{font-weight:600}
.rep .thumb.bug{color:var(--warn);background:color-mix(in srgb,var(--warn) 13%,transparent)}
.rep .thumb.error{color:var(--bad);background:color-mix(in srgb,var(--bad) 12%,transparent)}
pre.reptext{margin:0 0 6px;max-height:320px;overflow:auto;background:var(--card2);border-radius:10px;padding:10px 12px;
  font:11.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;word-break:break-word}
li.open{display:block}
[hidden]{display:none!important}
.stopped main{opacity:.45;pointer-events:none}
</style>
</head>
<body>
<main>
  <div class="top">
    <div class="logo"><svg width="22" height="22" viewBox="0 0 24 24" fill="#111"><rect x="3" y="9.5" width="3" height="5" rx="1.5"/><rect x="8" y="6" width="3" height="12" rx="1.5"/><rect x="13" y="3" width="3" height="18" rx="1.5"/><rect x="18" y="7.5" width="3" height="9" rx="1.5"/></svg></div>
    <div><h1>Mai An Companion</h1><div class="sub" id="sub">Starting…</div></div>
    <div class="spacer"></div>
    <span class="pill" id="pill"><span class="dot"></span><span id="pillText">Connecting…</span></span>
    <button class="ghost" id="quit" title="Stop the companion">Quit</button>
  </div>

  <nav class="tabs" id="tabs" role="tablist">
    <button role="tab" data-tab="home">Overview</button>
    <button role="tab" data-tab="music">Music<span class="count" id="tcMusic"></span></button>
    <button role="tab" data-tab="app">Phone app<span class="count" id="tcApp"></span></button>
    <button role="tab" data-tab="settings">Settings<span class="count" id="tcSettings"></span></button>
  </nav>

  <div class="tab grid" data-tab="home">
    <section class="card">
        <div class="head"><div class="ico" style="background:color-mix(in srgb,var(--ok) 15%,transparent);color:var(--ok)">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="6" y="2.5" width="12" height="19" rx="3"/><path d="M11 18.5h2"/></svg></div>
          <h2>Phone</h2><div class="right" id="phoneRight"></div></div>
        <div id="phoneBody"></div>
      </section>
    <section class="card">
        <div class="head"><div class="ico" style="background:color-mix(in srgb,var(--warn) 15%,transparent);color:var(--warn)">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 12h3l3-7 4 14 3-7h5"/></svg></div>
          <h2>Offload analysis</h2><div class="right" id="offRight"></div></div>
        <div id="offBody"></div>
      </section>
    <section class="card wide">
      <div class="head"><div class="ico" style="background:var(--card2);color:var(--mute)">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 6h16M4 12h16M4 18h10"/></svg></div>
        <h2>Activity</h2>
        <div class="right"><button class="ghost" id="logClear" title="Hide the lines so far">Clear</button></div></div>
      <div class="log" id="log"></div>
    </section>
  </div>

  <div class="tab grid" data-tab="music" hidden>
    <section class="card">
        <div class="head"><div class="ico" style="background:color-mix(in srgb,var(--blue) 15%,transparent);color:var(--blue)">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2 11 13"/><path d="M22 2 15 22l-4-9-9-4z"/></svg></div>
          <h2>Send to phone</h2></div>
        <div class="row" style="flex-wrap:wrap">
          <div class="seg" id="quality">
            <button data-q="original">Original</button><button data-q="aac_256k">AAC 256</button><button data-q="aac_vbr_max">AAC best</button>
          </div>
          <label class="row small" style="gap:8px">Analyse first <span class="switch"><input type="checkbox" id="dsp" checked><span></span></span></label>
        </div>
        <div class="drop" id="drop">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>
          <b>Drop tracks here</b> or <b>choose files</b><div class="tiny" style="margin-top:4px">Drop an .apk to offer it as a phone app update</div>
        </div>
        <div id="prep" hidden>
          <div class="bar"><i id="prepBar"></i></div>
          <div class="row small"><span id="prepText" class="mute"></span><button class="ghost" id="prepCancel">Cancel</button></div>
        </div>
        <ul id="pending"></ul>
        <div class="row small" id="pendingFoot" hidden style="margin-top:8px">
          <span class="mute" id="pendingText"></span><button class="ghost danger" id="clearQ">Clear all</button>
        </div>
      </section>
    <section class="card">
        <div class="head"><div class="ico" style="background:color-mix(in srgb,var(--violet) 15%,transparent);color:var(--violet)">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg></div>
          <h2>From the phone</h2>
          <div class="right"><button class="ghost" id="changeDir">Change…</button><button id="openFolder">Open folder</button></div></div>
        <div class="tiny mute mono" id="recvDir" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></div>
        <ul id="received"></ul>
        <ul id="backups"></ul>
      </section>
  </div>

  <div class="tab grid" data-tab="app" hidden>
    <section class="card">
        <div class="head"><div class="ico" style="background:color-mix(in srgb,var(--blue) 15%,transparent);color:var(--blue)">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m15 12-8.5 8.5a2.1 2.1 0 0 1-3-3L12 9"/><path d="M17.6 6.4 20 4l-2-2-2.4 2.4"/><path d="m11 7 6 6 3-3-6-6z"/></svg></div>
          <h2>Build the phone app</h2><div class="right" id="buildRight"></div></div>
        <div class="row" style="align-items:flex-start">
          <div style="min-width:0;flex:1">
            <div class="small strong" id="buildProj">No project chosen</div>
            <div class="tiny mute mono" id="buildPath" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></div>
          </div>
          <button class="ghost" id="buildPick">Change…</button>
        </div>
        <div class="chips">
          <label class="chip" title="Only the code every recent phone runs: the download is under half the size"><span class="switch"><input type="checkbox" id="optArm"><span></span></span>64-bit ARM only (smaller)</label>
          <label class="chip" title="Raise the +N build number in pubspec.yaml first, so the phone sees a newer version"><span class="switch"><input type="checkbox" id="optBump"><span></span></span>Raise build number</label>
        </div>
        <div id="buildRun" hidden>
          <div class="bar" id="buildBar"><i></i></div>
          <div class="row small"><span id="buildText"></span><button class="ghost danger" id="buildCancel">Cancel</button></div>
        </div>
        <div class="notes">
          <div class="row"><span class="small strong">What's new <span class="tiny mute" style="font-weight:400">· shown on the phone with the next build</span></span>
            <button class="ghost" id="gitNotes" title="Commit subjects since the last build made here">From git</button></div>
          <textarea id="draftNotes" placeholder="- Fixed the queue jumping when…"></textarea>
        </div>
        <div id="buildResult"></div>
        <div class="btns" style="margin-top:12px" id="buildBtns"><button class="primary" id="buildGo">Build &amp; offer to phone</button>
          <span class="tiny mute" id="buildHint"></span></div>
        <details class="out" id="buildOut"><summary>Build output</summary><div class="log" id="buildLog"></div></details>
      </section>
    <section class="card">
        <div class="head"><div class="ico" style="background:var(--accent-soft);color:var(--accent)">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></svg></div>
          <h2>Phone app updates</h2><div class="right" id="updRight"></div></div>
        <div id="updBody"></div>
        <div class="notes" id="offerNotesBox" hidden>
          <div class="row"><span class="small strong">What's new in this build</span><span class="tiny mute" id="offerNotesState"></span></div>
          <textarea id="offerNotes" placeholder="Nothing written: the phone shows no notes"></textarea>
        </div>
        <div class="opt" style="margin-top:12px;border-top:1px solid var(--line)">
          <div style="min-width:0"><div class="small strong">Offer new builds automatically</div>
            <div class="tiny mute" id="watchText" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></div></div>
          <div class="btns"><button class="ghost" id="watchPick">Folder…</button>
            <label class="switch"><input type="checkbox" id="auto"><span></span></label></div>
        </div>
      </section>
    <section class="card wide">
      <div class="head"><div class="ico" style="background:color-mix(in srgb,var(--bad) 13%,transparent);color:var(--bad)">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2l1.9 1.9M16 2l-1.9 1.9M9 7.1V6a3 3 0 1 1 6 0v1.1"/><path d="M12 20c-3.3 0-6-2.7-6-6v-3a4 4 0 0 1 4-4h4a4 4 0 0 1 4 4v3c0 3.3-2.7 6-6 6z"/><path d="M12 20v-9M6.5 13H3M21 13h-3.5M6 19l-2 2M18 19l2 2M6 9 4 7M18 9l2-2"/></svg></div>
        <h2>Problem reports</h2><span class="tiny mute">From the phone: Settings → Companion &amp; Sync → Report a problem, and errors the app catches.</span>
        <div class="right" id="repRight"></div></div>
      <ul id="reports" style="margin-top:0"></ul>
    </section>
  </div>

  <div class="tab grid" data-tab="settings" hidden>
    <section class="card wide">
      <div class="head"><div class="ico" style="background:color-mix(in srgb,var(--ok) 15%,transparent);color:var(--ok)">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v10"/><path d="M18.4 6.6a9 9 0 1 1-12.8 0"/></svg></div>
        <h2>Background</h2><span class="tiny mute">Turn off what you don't need to save battery. Switches are remembered.</span>
        <div class="right" id="svcRight"></div></div>
      <div class="svcs" id="svcs"></div>
    </section>
    <section class="card">
      <div class="head"><div class="ico" style="background:var(--card2);color:var(--mute)">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg></div>
        <h2>General</h2></div>
      <label class="opt"><div><div class="small strong">Notifications</div><div class="tiny mute">Tell me when tracks arrive, a build finishes or the phone installs an update.</div></div>
        <span class="switch"><input type="checkbox" id="notify"><span></span></span></label>
      <label class="opt"><div><div class="small strong">Open at login</div><div class="tiny mute">Start in the background when you log in to this Mac.</div></div>
        <span class="switch"><input type="checkbox" id="login"><span></span></span></label>
      <div class="opt"><div style="min-width:0"><div class="small strong">Tracks from the phone go to</div><div class="tiny mute mono" id="recvDir2" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></div></div>
        <button class="ghost" id="changeDir2">Change…</button></div>
    </section>
  </div>

  <footer id="foot"></footer>
</main>
<input type="file" id="file" multiple hidden>
<input type="file" id="apkFile" accept=".apk" hidden>
<div class="toast" id="toast"></div>
<div class="veil">Drop tracks to send them to the phone,<br>or an APK to offer it as an update</div>

<script>
const $ = id => document.getElementById(id);
const H = {'x-maian': '1'};
async function post(p, body) {
  const r = await fetch(p, {method: 'POST', headers: {...H, 'content-type': 'application/json'}, body: JSON.stringify(body || {})});
  const j = await r.json().catch(() => ({}));
  if (!r.ok && j.error) toast(j.error);
  return j;
}
function el(tag, props, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null) continue;
    if (k === 'class') e.className = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const c of kids) if (c != null) e.append(c);
  return e;
}
function svg(d) {
  const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  s.setAttribute('viewBox', '0 0 24 24'); s.setAttribute('fill', 'none'); s.setAttribute('stroke', 'currentColor');
  s.setAttribute('stroke-width', '2'); s.setAttribute('stroke-linecap', 'round'); s.setAttribute('stroke-linejoin', 'round');
  for (const p of d.split('|')) { const e = document.createElementNS(s.namespaceURI, 'path'); e.setAttribute('d', p); s.append(e); }
  return s;
}
const ICON = {
  note: 'M9 18V5l12-2v13|M9 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0|M21 16a3 3 0 1 1-6 0 3 3 0 0 1 6 0',
  box: 'M21 8 12 3 3 8v8l9 5 9-5z|M3 8l9 5 9-5|M12 13v8',
  x: 'M6 6l12 12|M18 6 6 18',
  phone: 'M8 2.5h8a2 2 0 0 1 2 2v15a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2v-15a2 2 0 0 1 2-2z|M11 18.5h2',
};
const size = n => n >= 1073741824 ? (n / 1073741824).toFixed(2) + ' GB' : n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(n / 1024)) + ' kB';
function ago(s) {
  if (s < 10) return 'just now';
  if (s < 90) return s + ' s ago';
  if (s < 5400) return Math.round(s / 60) + ' min ago';
  if (s < 129600) return Math.round(s / 3600) + ' h ago';
  return Math.round(s / 86400) + ' d ago';
}
const sinceMs = t => Math.max(0, Math.round((Date.now() - t) / 1000));
const qLabel = q => ({original: 'Original', aac_256k: 'AAC 256', aac_vbr_max: 'AAC best', aac_320k: 'AAC 320', aac_192k: 'AAC 192'}[q] || q);
let toastT;
function toast(t) { const e = $('toast'); e.textContent = t; e.classList.add('show'); clearTimeout(toastT); toastT = setTimeout(() => e.classList.remove('show'), 2600); }
function sw(id, v) { const e = $(id); if (document.activeElement !== e) e.checked = !!v; }

let S = null;
function render(s) {
  S = s;
  document.body.classList.remove('stopped');
  const live = s.clients.find(c => c.ago < 45);
  const pill = $('pill');
  pill.className = 'pill' + (live ? ' on' : '');
  $('pillText').textContent = live ? live.name : 'Waiting for phone';
  $('sub').textContent = s.name + ' · ' + (s.ips.length ? s.ips[0] + ':' + s.port : 'no network');

  renderPhone(s, live);
  renderUpdate(s, live);
  renderBuild(s);
  renderServices(s);
  renderOffload(s);
  renderSend(s);
  renderReceived(s);
  renderLog(s);
  renderTabs(s);
  renderNotes(s);
  renderReports(s);
  sw('notify', s.notify); sw('login', s.login_item);
  $('foot').textContent = 'Mai An Companion ' + s.version + (s.ffmpeg ? ' · ffmpeg ready' : '') + ' · ' + s.cached_tracks + ' tracks analysed on this Mac';
}

function renderTabs(s) {
  const set = (id, text, cls) => { const e = $(id); e.textContent = text; e.className = 'count' + (cls ? ' ' + cls : ''); };
  set('tcMusic', (s.pending.length + (s.prep ? s.prep.total - s.prep.done : 0)) || '');
  const b = s.build, st = s.update.phone && s.update.phone.state;
  if (b.phase === 'running' || b.phase === 'publishing') set('tcApp', '●', 'acc');
  else if (s.reports.unseen) set('tcApp', s.reports.unseen, 'bad');
  else if (b.phase === 'failed' || st === 'failed') set('tcApp', '!', 'bad');
  else set('tcApp', '');
  const off = SVCS.filter(([id]) => !s.services[id]).length;
  set('tcSettings', off ? off + ' off' : '');
}

// ── release notes ──
const saveLater = (fn, ms) => { let t; return v => { clearTimeout(t); t = setTimeout(() => fn(v), ms); }; };
let draftDirty = false, offerDirty = false;
const saveDraft = saveLater(async v => { await post('/api/update/draft-notes', {text: v}); draftDirty = false; }, 600);
const saveOffer = saveLater(async v => { await post('/api/update/notes', {text: v}); offerDirty = false; $('offerNotesState').textContent = 'Saved · the phone sees it on its next check'; }, 800);
function renderNotes(s) {
  const d = $('draftNotes');
  if (!draftDirty && document.activeElement !== d && d.value !== (s.update.draft_notes || '')) d.value = s.update.draft_notes || '';
  const c = s.update.current;
  $('offerNotesBox').hidden = !c;
  const o = $('offerNotes');
  if (c && !offerDirty && document.activeElement !== o && o.value !== (c.notes || '')) o.value = c.notes || '';
}
$('draftNotes').addEventListener('input', e => { draftDirty = true; saveDraft(e.target.value); });
$('offerNotes').addEventListener('input', e => { offerDirty = true; $('offerNotesState').textContent = 'Editing…'; saveOffer(e.target.value); });
$('gitNotes').addEventListener('click', async () => {
  const r = await post('/api/update/git-notes');
  if (r.text == null) return;
  if (!r.text) { toast('No new commits since the last build'); return; }
  const d = $('draftNotes');
  d.value = d.value.trim() ? d.value.trimEnd() + '\n' + r.text : r.text;
  draftDirty = true; saveDraft(d.value);
});

// ── problem reports ──
// New stays highlighted while the tab is open, though the Mac counts it seen.
let repOpen = null, repSig = '', repBase = null, repMarking = false;
const repText = {};
function renderReports(s) {
  const R = s.reports;
  const here = document.body.dataset.tab === 'app' && !document.hidden;
  if (!here) repBase = null;
  else if (R.unseen && !repMarking) {
    if (repBase == null) repBase = R.seen_at;
    repMarking = true; post('/api/report/seen').finally(() => repMarking = false);
  }
  const base = repBase ?? R.seen_at;
  const sig = R.items.map(r => r.id).join() + '|' + repOpen + '|' + base + '|' + (repOpen && repText[repOpen] ? 1 : 0);
  const right = $('repRight'); right.replaceChildren();
  if (R.unseen && !here) right.append(el('span', {class: 'badge bad'}, R.unseen + ' new'));
  if (R.items.length) right.append(el('button', {class: 'ghost danger', onclick: () => confirm('Delete all ' + R.total + ' reports?') && post('/api/report/clear')}, 'Clear all'));
  if (sig === repSig) return;
  repSig = sig;
  const ul = $('reports');
  if (!R.items.length) { ul.replaceChildren(el('li', {class: 'empty', style: 'display:block'}, 'No reports. When something goes wrong on the phone, report it from the app and it lands here with the app\'s recent log.')); return; }
  ul.replaceChildren(...R.items.map(r => {
    const isNew = r.at > base, open = repOpen === r.id;
    const li = el('li', {class: 'rep' + (isNew ? ' new' : '') + (open ? ' open' : '')});
    const row = el('div', {class: 'row', style: 'gap:10px', onclick: () => toggleReport(r.id)},
      el('div', {class: 'thumb ' + r.kind}, r.kind === 'error' ? '!' : '?'),
      el('div', {class: 't'}, r.title, el('small', {}, (r.kind === 'error' ? 'Caught error' : 'Reported') + ' · ' + r.device + (r.app_version ? ' · ' + r.app_version : '') + ' · ' + ago(sinceMs(r.at)) + (r.count > 1 ? ' · ' + r.count + '×' : '') + ' · ' + r.log_lines + ' log lines')),
      isNew ? el('span', {class: 'badge bad'}, 'New') : null);
    li.append(row);
    if (open) {
      li.append(el('pre', {class: 'reptext'}, repText[r.id] || 'Loading…'));
      li.append(el('div', {class: 'btns'},
        el('button', {class: 'primary', onclick: () => copyReport(r.id)}, 'Copy report'),
        el('button', {class: 'ghost', onclick: () => post('/api/report/reveal', {id: r.id})}, 'Show in Finder'),
        el('button', {class: 'ghost danger', onclick: () => { repOpen = null; post('/api/report/delete', {id: r.id}); }}, 'Delete')));
    }
    return li;
  }));
}
async function toggleReport(id) {
  repOpen = repOpen === id ? null : id;
  if (repOpen && !repText[id]) { const r = await post('/api/report/text', {id}); if (r.text) repText[id] = r.text; }
  if (S) renderReports(S);
}
async function copyReport(id) {
  const t = repText[id] || (await post('/api/report/text', {id})).text;
  if (!t) return;
  try { await navigator.clipboard.writeText(t); }
  catch (_) { const a = el('textarea', {}); a.value = t; document.body.append(a); a.select(); document.execCommand('copy'); a.remove(); }
  toast('Report copied: paste it into an issue or a chat');
}

function renderPhone(s, live) {
  const p = $('phoneBody'), r = $('phoneRight');
  p.replaceChildren(); r.replaceChildren();
  const addr = s.ips.length ? s.ips.map(i => i + ':' + s.port).join('  ·  ') : 'no network';
  if (live) {
    const via = (live.ip === '127.0.0.1' || live.ip === '::1') ? 'USB cable' : live.ip;
    const u = s.update.current;
    let badge = null;
    if (live.app_version && u && live.app_package === u.package) {
      const offered = u.version_name + '+' + u.version_code;
      badge = live.app_version === offered ? el('span', {class: 'badge ok'}, 'Up to date') : el('span', {class: 'badge warn'}, 'Update offered');
    }
    p.append(el('div', {class: 'device'},
      el('div', {class: 'phone-glyph'}, svg(ICON.phone)),
      el('div', {style: 'min-width:0;flex:1'},
        el('div', {class: 'row', style: 'justify-content:flex-start;gap:8px'}, el('span', {class: 'strong', style: 'font-size:15px'}, live.name), badge),
        el('div', {class: 'small mute'}, via + ' · seen ' + ago(live.ago)))));
    p.append(el('dl', {class: 'kv'},
      el('dt', {}, 'Mai An'), el('dd', {}, live.app_version ? live.app_version.replace('+', ' (') + ')' : 'version unknown (older app)'),
      el('dt', {}, 'Mac address'), el('dd', {class: 'mono'}, addr)));
    r.append(el('button', {class: 'ghost danger', onclick: () => confirm('Unpair every phone? They will need a new PIN to reconnect.') && post('/api/unpair')}, 'Unpair'));
  } else {
    p.append(el('div', {class: 'small mute'}, 'On the phone: Settings → Companion & Sync → Find Mac, then enter'));
    p.append(el('div', {class: 'pinbox'}, ...String(s.pin).split('').map(d => el('span', {}, d))));
    p.append(el('dl', {class: 'kv'},
      el('dt', {}, 'Or by hand'), el('dd', {class: 'mono'}, addr),
      s.last_client ? el('dt', {}, 'Last phone') : null, s.last_client ? el('dd', {}, s.last_client) : null));
    r.append(el('button', {class: 'ghost', onclick: () => post('/api/rotate-pin')}, 'New PIN'));
  }
}

function renderUpdate(s, live) {
  const u = s.update, b = $('updBody'), r = $('updRight');
  b.replaceChildren(); r.replaceChildren();
  sw('auto', u.auto);
  $('watchText').textContent = u.watch_dir ? (u.auto ? 'Watching ' : 'Paused · ') + u.watch_dir + '/app-release.apk' : 'Choose the folder flutter build apk writes to';
  if (u.watch_error) $('watchText').textContent = 'Could not read the last build: ' + u.watch_error;
  if (!u.current) {
    b.append(el('div', {class: 'empty', style: 'text-align:left;padding:2px 0 8px'}, 'No build on offer. Drop an APK anywhere on this page, choose one, or run flutter build apk with the folder below watched.'));
    b.append(el('div', {class: 'btns'}, el('button', {class: 'primary', onclick: chooseApk}, 'Choose APK…')));
    return;
  }
  const c = u.current;
  const head = el('div', {class: 'row', style: 'align-items:flex-end'},
    el('div', {},
      el('div', {class: 'tiny mute'}, 'Offering'),
      el('div', {class: 'version'}, c.version_name, el('span', {class: 'mute', style: 'font-size:15px;font-weight:500'}, '  build ' + c.version_code))),
    el('div', {class: 'btns'}, c.debuggable ? el('span', {class: 'badge warn'}, 'Debug build') : el('span', {class: 'badge'}, 'Release'), el('span', {class: 'badge'}, size(c.size))));
  b.append(head);
  // Where the phone is with it.
  const st = (u.phone && u.phone.state) || '';
  const phoneOn = live && live.app_version;
  const offered = c.version_name + '+' + c.version_code;
  // Only the phone's word: a rebuild can share the version it already runs.
  const installed = st === 'installed';
  const order = ['offered', 'downloading', 'installing', 'installed'];
  let idx = installed ? 3 : st === 'installing' ? 2 : (st === 'downloading' || st === 'downloaded') ? 1 : 0;
  const failed = st === 'failed' || st === 'declined';
  const labels = ['Offered', 'Downloaded', 'Installing', 'Installed'];
  const steps = el('div', {class: 'steps'});
  labels.forEach((l, i) => {
    if (i) steps.append(el('div', {class: 'link'}));
    const cls = failed && i === idx + 1 ? 'fail' : i < idx || (i === idx && installed) ? 'done' : i === idx ? 'now' : '';
    steps.append(el('div', {class: 'step ' + cls}, el('i', {}, i < idx || (installed && i === 3) ? '✓' : String(i + 1)), l));
  });
  b.append(steps);
  let note = 'The phone sees it on its next check (every 15 s while Companion Sync is on) and asks before installing.';
  if (installed) note = (u.phone && u.phone.device || 'The phone') + ' runs this build.';
  else if (failed) note = st === 'declined' ? 'Declined on the phone. It will ask again for the next build.' : 'Install failed: ' + (u.phone.error || 'unknown error');
  else if (st === 'installing') note = 'Waiting for the install to be confirmed on the phone.';
  else if (st === 'downloading') note = 'The phone is downloading it.';
  if (phoneOn && live.app_package && live.app_package !== c.package) note = 'This APK is ' + c.package + ' but the phone runs ' + live.app_package + ': it will not be offered.';
  b.append(el('div', {class: 'small ' + (failed ? 'warnline' : 'mute')}, note));
  b.append(el('dl', {class: 'kv'},
    el('dt', {}, 'Package'), el('dd', {class: 'mono'}, c.package),
    el('dt', {}, 'From'), el('dd', {}, (c.file_name || 'APK') + ' · ' + c.source + ' · ' + ago(sinceMs(c.published_at))),
    el('dt', {}, 'SHA-256'), el('dd', {class: 'mono', title: c.sha256}, c.sha256.slice(0, 16) + '…')));
  r.append(el('button', {class: 'ghost', onclick: () => post('/api/update/reveal')}, 'Show'),
    el('button', {class: 'ghost', onclick: chooseApk}, 'Replace…'),
    el('button', {class: 'ghost danger', onclick: () => post('/api/update/withdraw')}, 'Withdraw'));
}

const clock = ms => { const t = Math.max(0, Math.round(ms / 1000)); return Math.floor(t / 60) + ':' + String(t % 60).padStart(2, '0'); };
let buildLines = -1, buildFirst = '';
function renderBuild(s) {
  const b = s.build, running = b.phase === 'running' || b.phase === 'publishing';
  $('buildProj').textContent = b.project ? (b.project.split('/').pop() + (b.version ? '  ·  version ' + b.version : '')) : 'No Flutter project chosen';
  $('buildPath').textContent = b.project || 'Choose the folder with the app\'s pubspec.yaml';
  sw('optArm', b.arm64); sw('optBump', b.bump);
  $('optArm').disabled = $('optBump').disabled = running;
  $('buildPick').disabled = running;
  $('buildRun').hidden = !running;
  $('buildBtns').hidden = running;
  $('buildGo').disabled = !b.project;
  $('buildHint').textContent = b.last_ms ? 'Usually takes about ' + clock(b.last_ms) : 'The first build can take a few minutes';
  const r = $('buildRight'); r.replaceChildren();
  if (running) r.append(el('span', {class: 'badge acc'}, b.phase === 'publishing' ? 'Offering' : 'Building'));
  else if (b.phase === 'done') r.append(el('span', {class: 'badge ok'}, 'Built'));
  else if (b.phase === 'failed') r.append(el('span', {class: 'badge bad'}, 'Failed'));
  tickBuild();
  const res = $('buildResult'); res.replaceChildren();
  const took = b.ended_at && b.started_at ? ' in ' + clock(b.ended_at - b.started_at) : '';
  if (b.phase === 'done') res.append(el('div', {class: 'result ok'}, '✓ Built ' + b.built + took + ' and offered to the phone.'));
  else if (b.phase === 'failed') res.append(el('div', {class: 'result bad'}, '✕ ' + b.error));
  else if (b.phase === 'cancelled') res.append(el('div', {class: 'result mute'}, 'Build cancelled.'));
  // The output: only touched when it changed, so scrolling and the open state survive.
  const first = b.lines[0] || '';
  if (b.lines.length !== buildLines || first !== buildFirst) {
    const L = $('buildLog'), stick = L.scrollTop + L.clientHeight >= L.scrollHeight - 8;
    L.replaceChildren(...(b.lines.length ? b.lines.map(l => el('div', {}, l)) : [el('div', {class: 'faint'}, 'Nothing built yet.')]));
    if (stick) L.scrollTop = L.scrollHeight;
    if (b.phase === 'failed' && buildLines !== -1) $('buildOut').open = true;
    buildLines = b.lines.length; buildFirst = first;
  }
}
function tickBuild() {
  if (!S) return;
  const b = S.build;
  if (b.phase !== 'running' && b.phase !== 'publishing') return;
  const el_ = Date.now() - b.started_at;
  const bar = $('buildBar');
  if (b.phase === 'running' && b.last_ms) {
    bar.className = 'bar';
    bar.firstChild.style.width = Math.min(95, el_ / b.last_ms * 100) + '%';
  } else { bar.className = 'bar indet'; bar.firstChild.style.width = ''; }
  const last = b.lines.length ? b.lines[b.lines.length - 1] : '';
  $('buildText').textContent = (b.phase === 'publishing' ? 'Offering to the phone…' : 'Building ' + clock(el_)) + (b.phase === 'running' && last ? '  ·  ' + last.slice(0, 80) : '');
}
setInterval(tickBuild, 1000);

const SVCS = [
  ['phone', 'Phone connection', 'Lets the phone pair, sync, offload and fetch app updates.', s => s.services.listening ? ['on', 'Listening on port ' + s.port] : ['', 'Off: the phone can\'t reach this Mac']],
  ['beacon', 'Discovery', 'Announces this Mac on the network every 2 s so the phone finds it.', s => !s.services.phone ? ['', 'Needs the phone connection'] : s.services.beacon_live ? ['on', 'Announcing'] : ['', 'Off: a phone that paired before still connects']],
  ['offload', 'Analysis for the phone', 'Analyses tracks the phone offloads (ffmpeg).', s => s.services.offload ? (s.offload.active ? ['busy', 'Analysing now'] : ['on', 'Ready']) : ['', 'Off: the phone is told to analyse on its own']],
  ['gentle', 'Low-power analysis', 'One track at a time instead of one per core: slower, cooler, quieter.', s => [s.services.gentle ? 'on' : '', 'Uses ' + s.services.workers + (s.services.workers === 1 ? ' core' : ' cores')]],
  ['awake', 'Keep the Mac awake during jobs', 'Stops sleep while tracks are analysed or sent.', s => s.services.awake_now ? ['busy', 'Holding the Mac awake now'] : [s.services.awake ? 'on' : '', s.services.awake ? 'Only while a job runs' : 'Off: the Mac may sleep mid-job']],
  ['watch', 'Watch for new builds', 'Checks the build folder every 4 s and offers each new APK.', s => !s.update.watch_dir ? ['', 'No folder chosen'] : [s.services.watch ? 'on' : '', s.services.watch ? 'Watching' : 'Off']],
  ['stop_gradle', 'Stop Gradle after builds', 'Frees 1–2 GB of memory after each build; the next build starts a little slower.', s => [s.services.stop_gradle ? 'on' : '', s.services.stop_gradle ? 'Stops after each build' : 'Gradle stays running']],
];
function renderServices(s) {
  const box = $('svcs');
  let on = 0;
  SVCS.forEach(([id, title, desc, status]) => {
    const v = !!s.services[id];
    if (v) on++;
    let tile = box.querySelector('[data-svc="' + id + '"]');
    if (!tile) {
      tile = el('label', {class: 'svc', 'data-svc': id},
        el('div', {class: 'd'}, el('div', {class: 't'}, title), el('div', {class: 'x'}, desc), el('div', {class: 's'}, el('span', {class: 'dot'}), el('span', {}))),
        el('span', {class: 'switch'}, el('input', {type: 'checkbox', onchange: e => setService(id, e.target)}), el('span', {})));
      box.append(tile);
    }
    const input = tile.querySelector('input');
    if (document.activeElement !== input) input.checked = v;
    const [cls, text] = status(s);
    const st = tile.querySelector('.s');
    st.className = 's ' + cls; st.lastChild.textContent = text;
    const dis = (id === 'beacon' && !s.services.phone) || (id === 'gentle' && !s.services.offload);
    tile.classList.toggle('dis', dis);
  });
  $('svcRight').replaceChildren(el('span', {class: 'badge'}, on + ' of ' + SVCS.length + ' on'));
}
async function setService(id, input) {
  const on = input.checked;
  if (id === 'phone' && !on && !confirm('Turn off the phone connection? The phone disconnects until you turn it back on.')) { input.checked = true; return; }
  await post('/api/service', {name: id, on});
  input.blur();
}

function renderOffload(s) {
  const o = s.offload, ob = $('offBody'), r = $('offRight');
  ob.replaceChildren(); r.replaceChildren();
  if (!o.total) {
    ob.append(el('div', {class: 'mute small'}, 'Idle. Start “Offload to Mac” on the phone; progress shows here.'));
  } else {
    const pct = Math.min(100, Math.round(o.done / o.total * 100));
    const done = o.done >= o.total;
    r.append(el('span', {class: 'badge ' + (o.active ? 'acc' : done ? 'ok' : '')}, o.active ? 'Analysing' : done ? 'Finished' : 'Paused'));
    ob.append(el('div', {class: 'row'}, el('span', {class: 'strong'}, pct + '%'), el('span', {class: 'small mute'}, o.done + ' of ' + o.total + ' tracks' + (o.active && o.rate ? ' · ' + o.rate.toFixed(1) + '/s' : ''))));
    const bar = el('div', {class: 'bar' + (done ? ' ok' : '')}, el('i', {}));
    bar.firstChild.style.width = pct + '%';
    ob.append(bar);
    if (o.active && o.current) ob.append(el('div', {class: 'tiny mono mute', style: 'overflow:hidden;text-overflow:ellipsis;white-space:nowrap'}, o.current));
    ob.append(el('div', {class: 'stats'},
      el('div', {class: 'stat'}, el('b', {}, String(o.cached)), el('span', {}, 'from Mac cache')),
      el('div', {class: 'stat'}, el('b', {}, String(o.extracted)), el('span', {}, 'analysed now')),
      el('div', {class: 'stat'}, el('b', {style: o.failed ? 'color:var(--bad)' : null}, String(o.failed)), el('span', {}, 'failed'))));
  }
  if (!s.ffmpeg) ob.append(el('div', {class: 'warnline'}, 'ffmpeg is missing: offload and AAC conversion are off. Install it with: brew install ffmpeg'));
}

function renderSend(s) {
  for (const btn of $('quality').children) btn.classList.toggle('on', btn.dataset.q === s.quality);
  const pr = $('prep');
  pr.hidden = !s.prep;
  if (s.prep) {
    $('prepBar').style.width = Math.round(s.prep.done / s.prep.total * 100) + '%';
    $('prepText').textContent = 'Preparing ' + s.prep.done + ' of ' + s.prep.total + (s.prep.title ? ' · ' + s.prep.title : '');
  }
  $('pending').replaceChildren(...s.pending.map(t => el('li', {},
    el('div', {class: 'thumb'}, svg(ICON.note)),
    el('div', {class: 't'}, t.title, el('small', {}, t.artist + ' · ' + qLabel(t.quality) + ' · ' + size(t.size))),
    el('button', {class: 'ghost', title: 'Remove', onclick: () => post('/api/remove', {id: t.id})}, '✕'))));
  $('pendingFoot').hidden = !s.pending.length;
  $('pendingText').textContent = s.pending.length + ' waiting · the phone picks them up when connected';
}

function renderReceived(s) {
  $('recvDir').textContent = $('recvDir2').textContent = s.receive_dir;
  const rec = $('received');
  rec.replaceChildren(...s.received.slice(0, 10).map(r => el('li', {},
    el('div', {class: 'thumb'}, svg(ICON.note)),
    el('div', {class: 't'}, r.name, el('small', {}, r.from + ' · ' + ago(sinceMs(r.at)) + ' · ' + size(r.size))),
    el('button', {class: 'ghost', onclick: () => post('/api/reveal', {path: r.path})}, 'Show'))));
  if (!s.received.length) rec.append(el('li', {class: 'empty', style: 'display:block'}, 'Nothing yet. On the phone, long-press a track or album → Send to Mac.'));
  $('backups').replaceChildren(...s.backups.slice(0, 3).map(b => el('li', {},
    el('div', {class: 'thumb'}, svg(ICON.box)),
    el('div', {class: 't'}, 'Library backup', el('small', {}, b.name + ' · ' + size(b.size) + ' · ' + ago(sinceMs(b.at)))),
    el('button', {class: 'ghost', onclick: () => post('/api/reveal', {path: b.path})}, 'Show'))));
}

let logCount = -1, logLast = 0, logSince = 0;
function renderLog(s) {
  const L = $('log');
  const last = s.log.length ? s.log[s.log.length - 1].at : 0;
  if (s.log.length === logCount && last === logLast) return;
  logCount = s.log.length; logLast = last;
  const lines = s.log.filter(l => l.at > logSince);
  const stick = L.scrollTop + L.clientHeight >= L.scrollHeight - 8;
  const fmt = t => new Date(t).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'});
  L.replaceChildren(...lines.map(l => el('div', {}, el('time', {}, fmt(l.at)), l.text)));
  if (!lines.length) L.append(el('div', {class: 'faint'}, 'Nothing yet.'));
  if (stick) L.scrollTop = L.scrollHeight;
}

// ── actions ──
$('quality').addEventListener('click', e => { const q = e.target.dataset && e.target.dataset.q; if (q) { post('/api/quality', {quality: q}); for (const b of $('quality').children) b.classList.toggle('on', b === e.target); } });
$('prepCancel').addEventListener('click', () => post('/api/cancel-send'));
$('clearQ').addEventListener('click', () => post('/api/clear-queue'));
$('openFolder').addEventListener('click', () => post('/api/open-folder'));
$('changeDir').addEventListener('click', () => post('/api/choose-receive-dir'));
$('changeDir2').addEventListener('click', () => post('/api/choose-receive-dir'));
$('logClear').addEventListener('click', () => { if (S && S.log.length) { logSince = S.log[S.log.length - 1].at; logCount = -1; renderLog(S); } });

// ── tabs ──
const TABS = ['home', 'music', 'app', 'settings'];
function showTab(t) {
  if (!TABS.includes(t)) t = 'home';
  for (const p of document.querySelectorAll('.tab')) p.hidden = p.dataset.tab !== t;
  for (const b of $('tabs').children) { const on = b.dataset.tab === t; b.classList.toggle('on', on); b.setAttribute('aria-selected', on); }
  document.body.dataset.tab = t;
  if (S) renderReports(S);
  if (location.hash !== '#' + t) history.replaceState(null, '', '#' + t);
}
$('tabs').addEventListener('click', e => { const b = e.target.closest('button'); if (b) showTab(b.dataset.tab); });
window.addEventListener('hashchange', () => showTab(location.hash.slice(1)));
window.addEventListener('keydown', e => {
  if (e.metaKey || e.ctrlKey || e.altKey || /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) return;
  const i = '1234'.indexOf(e.key); if (i >= 0) showTab(TABS[i]);
});
showTab(location.hash.slice(1));
$('notify').addEventListener('change', e => post('/api/notify', {on: e.target.checked}));
$('login').addEventListener('change', e => post('/api/login-item', {on: e.target.checked}));
$('auto').addEventListener('change', e => post('/api/update/auto', {on: e.target.checked}));
$('watchPick').addEventListener('click', () => post('/api/update/watch-dir'));
$('buildPick').addEventListener('click', () => post('/api/build/project'));
$('optArm').addEventListener('change', e => post('/api/build/option', {name: 'arm64', on: e.target.checked}));
$('optBump').addEventListener('change', e => post('/api/build/option', {name: 'bump', on: e.target.checked}));
$('buildGo').addEventListener('click', async () => { $('buildGo').disabled = true; $('buildOut').open = false; await post('/api/build/start'); });
$('buildCancel').addEventListener('click', () => post('/api/build/cancel'));
$('quit').addEventListener('click', async () => {
  if (!confirm('Stop Mai An Companion? The phone will lose its connection until you open it again.')) return;
  await post('/api/quit'); stopped();
});
async function chooseApk() {
  const r = await post('/api/update/choose');
  if (r.update) toast('Offering ' + r.update.version_name + ' (' + r.update.version_code + ') to the phone');
}

const AUDIO = /\.(flac|mp3|m4a|aac|alac|wav|aiff?|ogg|opus|wv|ape)$/i;
async function handleFiles(files) {
  files = [...files].filter(f => f.size > 0);
  const apks = files.filter(f => /\.apk$/i.test(f.name));
  const audio = files.filter(f => AUDIO.test(f.name));
  if (apks.length) {
    showTab('app');
    toast('Reading ' + apks[0].name + '…');
    const r = await fetch('/api/update/upload?name=' + encodeURIComponent(apks[0].name), {method: 'POST', headers: H, body: apks[0]});
    const j = await r.json().catch(() => ({}));
    toast(j.update ? 'Offering ' + j.update.version_name + ' (' + j.update.version_code + ') to the phone' : (j.error || 'Could not read the APK'));
  }
  if (!audio.length) { if (!apks.length && files.length) toast('Not audio or an APK'); return; }
  showTab('music');
  const paths = [];
  const drop = $('drop'), was = drop.innerHTML;
  try {
    for (let i = 0; i < audio.length; i++) {
      drop.textContent = 'Reading ' + (i + 1) + ' of ' + audio.length + ' · ' + audio[i].name;
      const r = await fetch('/api/upload?name=' + encodeURIComponent(audio[i].name), {method: 'POST', headers: H, body: audio[i]});
      const j = await r.json();
      if (j.path) paths.push(j.path);
    }
    await post('/api/send', {paths, quality: S ? S.quality : 'aac_vbr_max', dsp: $('dsp').checked});
  } finally { drop.innerHTML = was; }
}
$('drop').addEventListener('click', async () => {
  // The Mac's own picker gives real paths (no copy); a browser upload is the fallback.
  const r = await post('/api/choose-send', {quality: S ? S.quality : null, dsp: $('dsp').checked});
  if (r.queued) toast('Preparing ' + r.queued + ' track' + (r.queued === 1 ? '' : 's'));
  else if (r.native === false) $('file').click();
});
$('file').addEventListener('change', e => { handleFiles(e.target.files); e.target.value = ''; });
let depth = 0;
window.addEventListener('dragenter', e => { e.preventDefault(); depth++; document.body.classList.add('dragging'); });
window.addEventListener('dragleave', e => { e.preventDefault(); if (--depth <= 0) { depth = 0; document.body.classList.remove('dragging'); } });
window.addEventListener('dragover', e => e.preventDefault());
window.addEventListener('drop', e => { e.preventDefault(); depth = 0; document.body.classList.remove('dragging'); handleFiles(e.dataTransfer.files); });

// ── live state ──
function stopped() {
  document.body.classList.add('stopped');
  $('pill').className = 'pill off'; $('pillText').textContent = 'Companion stopped';
  $('sub').textContent = 'Open Mai An Companion again to restart it.';
}
fetch('/api/state').then(r => r.json()).then(render).catch(stopped);
function connect() {
  const es = new EventSource('/api/events');
  es.onmessage = e => render(JSON.parse(e.data));
  es.onerror = () => { es.close(); stopped(); setTimeout(connect, 2000); };
}
connect();
setInterval(() => S && renderLog(S), 30000);
</script>
</body>
</html>
''';
