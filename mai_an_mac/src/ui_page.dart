/// The whole control page: one file, no build step, no network assets.
/// Everything from the phone (names, titles, file names) goes in with
/// textContent, never as HTML.
const uiHtml = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mai An Companion</title>
<style>
:root{
  --bg:#f5f5f7; --card:#fff; --ink:#1d1d1f; --mute:#6e6e73; --line:#e5e5ea;
  --accent:#0a7d6f; --accent-ink:#fff; --ok:#1f9d55; --bad:#c0392b; --track:#e5e5ea;
}
@media (prefers-color-scheme:dark){
  :root{--bg:#111214; --card:#1b1c1f; --ink:#f2f2f4; --mute:#9b9ba3; --line:#2b2c31;
    --accent:#2dbba8; --accent-ink:#06201c; --ok:#3ecf7a; --bad:#ff6b5b; --track:#2b2c31;}
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.45 -apple-system,BlinkMacSystemFont,"SF Pro Text",system-ui,sans-serif}
main{max-width:720px;margin:0 auto;padding:28px 18px 60px}
header{display:flex;align-items:center;justify-content:space-between;margin-bottom:18px}
h1{font-size:20px;margin:0;letter-spacing:-.01em}
.chip{display:inline-flex;align-items:center;gap:8px;padding:6px 12px;border-radius:999px;
  background:var(--card);border:1px solid var(--line);font-size:13px}
.dot{width:9px;height:9px;border-radius:50%;background:var(--mute)}
.chip.on .dot{background:var(--ok)}
section{background:var(--card);border:1px solid var(--line);border-radius:14px;
  padding:18px 20px;margin-bottom:14px}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--mute);
  margin:0 0 12px;font-weight:600}
.row{display:flex;align-items:center;justify-content:space-between;gap:12px}
.pin{font:600 40px/1 ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:.25em;
  margin:2px 0 6px -2px}
.mute{color:var(--mute)} .small{font-size:13px}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
button,.btn{white-space:nowrap;font:inherit;font-size:13px;border:1px solid var(--line);background:var(--card);
  color:var(--ink);border-radius:8px;padding:6px 12px;cursor:pointer}
button:hover{border-color:var(--mute)}
button.primary{background:var(--accent);color:var(--accent-ink);border-color:transparent}
button.link{border:0;background:none;color:var(--mute);padding:2px 6px}
button.link:hover{color:var(--ink)}
.bar{height:8px;border-radius:99px;background:var(--track);overflow:hidden;margin:10px 0 8px}
.bar>i{display:block;height:100%;background:var(--accent);width:0;transition:width .3s}
.drop{border:1.5px dashed var(--line);border-radius:12px;padding:22px;text-align:center;
  color:var(--mute);cursor:pointer;transition:.15s}
.drop.over{border-color:var(--accent);color:var(--ink);background:color-mix(in srgb,var(--accent) 8%,transparent)}
ul{list-style:none;margin:10px 0 0;padding:0}
li{display:flex;align-items:center;justify-content:space-between;gap:10px;
  padding:8px 0;border-top:1px solid var(--line)}
li:first-child{border-top:0}
li .t{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.opts{display:flex;align-items:center;gap:14px;margin:0 0 12px;flex-wrap:wrap}
select{font:inherit;font-size:13px;padding:5px 8px;border-radius:8px;border:1px solid var(--line);
  background:var(--card);color:var(--ink)}
label{font-size:13px}
.warn{color:var(--bad)}
footer{color:var(--mute);font-size:12px;text-align:center;margin-top:22px}
[hidden]{display:none!important}
</style>
</head>
<body>
<main>
  <header>
    <h1>Mai An Companion</h1>
    <span class="chip" id="chip"><span class="dot"></span><span id="chipText">Starting…</span></span>
  </header>

  <section id="phone">
    <h2>Phone</h2>
    <div id="phoneBody"></div>
  </section>

  <section>
    <h2>Offload analysis</h2>
    <div id="offBody"></div>
  </section>

  <section>
    <h2>Send to phone</h2>
    <div class="opts">
      <label>Quality
        <select id="quality">
          <option value="original">Original (no conversion)</option>
          <option value="aac_256k">AAC 256 kbps</option>
          <option value="aac_vbr_max">AAC best (VBR)</option>
        </select>
      </label>
      <label><input type="checkbox" id="dsp" checked> Analyse tracks first</label>
    </div>
    <div class="drop" id="drop">Drop audio files here, or click to choose</div>
    <input type="file" id="file" multiple accept="audio/*,.flac,.m4a,.mp3,.wav,.ogg,.opus,.aiff" hidden>
    <div id="prep" hidden>
      <div class="bar"><i id="prepBar"></i></div>
      <div class="row small"><span id="prepText" class="mute"></span>
        <button class="link" id="prepCancel">Cancel</button></div>
    </div>
    <ul id="pending"></ul>
    <div class="row small" id="pendingFoot" hidden style="margin-top:10px">
      <span class="mute">The phone picks these up when it is connected.</span>
      <button class="link" id="clearQ">Clear all</button>
    </div>
  </section>

  <section>
    <h2>From the phone</h2>
    <div class="row small">
      <span class="mute" id="recvDir"></span>
      <button id="openFolder">Open folder</button>
    </div>
    <ul id="received"></ul>
    <ul id="backups"></ul>
  </section>

  <footer id="foot"></footer>
</main>

<script>
const $ = id => document.getElementById(id);
const H = {'x-maian': '1'};
const post = (p, body) => fetch(p, {method: 'POST', headers: {...H, 'content-type': 'application/json'},
  body: JSON.stringify(body || {})}).then(r => r.json());

function el(tag, props, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (k === 'class') e.className = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const c of kids) e.append(c);
  return e;
}
const size = n => n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB' : Math.round(n / 1024) + ' kB';
function ago(s) {
  if (s < 10) return 'just now';
  if (s < 90) return s + ' s ago';
  if (s < 5400) return Math.round(s / 60) + ' min ago';
  return Math.round(s / 3600) + ' h ago';
}
const qLabel = q => ({original: 'Original', aac_256k: 'AAC 256', aac_vbr_max: 'AAC best',
  aac_320k: 'AAC 320', aac_192k: 'AAC 192'}[q] || q);

let S = null;

function render(s) {
  S = s;
  const live = s.clients.find(c => c.ago < 45);
  $('chip').classList.toggle('on', !!live);
  $('chipText').textContent = live ? 'Connected · ' + live.name : 'Waiting for phone';

  // Phone
  const p = $('phoneBody');
  p.replaceChildren();
  const addr = s.ips.length ? s.ips.map(i => i + ':' + s.port).join('  ·  ') : 'no network';
  if (live) {
    p.append(el('div', {class: 'row'},
      el('div', {}, el('strong', {}, live.name),
        el('div', {class: 'small mute'}, live.ip + ' · seen ' + ago(live.ago))),
      el('div', {}, el('button', {onclick: () => confirm('Unpair every phone? They will need the new PIN.') && post('/api/unpair')}, 'Unpair'))));
  } else {
    p.append(el('div', {class: 'mute small'}, 'On the phone open Companion sync and tap Find Mac, then enter this PIN:'));
    p.append(el('div', {class: 'pin'}, s.pin));
    p.append(el('div', {class: 'row small'},
      el('span', {class: 'mute'}, 'Or enter the address by hand: ', el('span', {class: 'mono'}, addr)),
      el('button', {class: 'link', onclick: () => post('/api/rotate-pin')}, 'New PIN')));
    if (s.last_client) p.append(el('div', {class: 'small mute', style: 'margin-top:8px'},
      'Last connected: ' + s.last_client));
  }

  // Offload
  const o = s.offload, ob = $('offBody');
  ob.replaceChildren();
  if (!o.total) {
    ob.append(el('div', {class: 'mute'}, 'Idle. Start an offload from the phone and its progress shows here.'));
    ob.append(el('div', {class: 'small mute', style: 'margin-top:6px'}, s.cached_tracks + ' tracks already analysed on this Mac.'));
  } else {
    const pct = Math.min(100, Math.round(o.done / o.total * 100));
    ob.append(el('div', {class: 'row'},
      el('strong', {}, o.active ? 'Analysing…' : (o.done >= o.total ? 'Finished' : 'Paused')),
      el('span', {class: 'small mute'}, o.done + ' / ' + o.total)));
    const bar = el('div', {class: 'bar'}, el('i', {}));
    bar.firstChild.style.width = pct + '%';
    ob.append(bar);
    const bits = [];
    if (o.cached) bits.push(o.cached + ' from Mac cache');
    if (o.extracted) bits.push(o.extracted + ' analysed');
    if (o.failed) bits.push(o.failed + ' failed');
    if (o.active && o.rate) bits.push(o.rate.toFixed(1) + ' tracks/s');
    ob.append(el('div', {class: 'small mute' + (o.failed ? '' : '')}, bits.join(' · ')));
    if (o.active && o.current) ob.append(el('div', {class: 'small mono mute', style: 'margin-top:4px'}, o.current));
  }
  if (!s.ffmpeg) ob.append(el('div', {class: 'small warn', style: 'margin-top:8px'},
    'ffmpeg is not installed: offload and AAC conversion are unavailable. Run: brew install ffmpeg'));

  // Send
  if (document.activeElement !== $('quality')) $('quality').value = s.quality;
  const pr = $('prep');
  pr.hidden = !s.prep;
  if (s.prep) {
    $('prepBar').style.width = Math.round(s.prep.done / s.prep.total * 100) + '%';
    $('prepText').textContent = 'Preparing ' + s.prep.done + ' / ' + s.prep.total + (s.prep.title ? ' · ' + s.prep.title : '');
  }
  const pend = $('pending');
  pend.replaceChildren(...s.pending.map(t => el('li', {},
    el('span', {class: 't'}, t.title, el('span', {class: 'mute'}, ' · ' + t.artist + ' · ' + qLabel(t.quality) + ' · ' + size(t.size))),
    el('button', {class: 'link', title: 'Remove', onclick: () => post('/api/remove', {id: t.id})}, '✕'))));
  $('pendingFoot').hidden = !s.pending.length;

  // Received
  $('recvDir').textContent = s.receive_dir;
  const rec = $('received');
  rec.replaceChildren(...s.received.slice(0, 12).map(r => el('li', {},
    el('span', {class: 't'}, r.name, el('span', {class: 'mute'}, ' · ' + r.from + ' · ' + ago(Math.round((Date.now() - r.at) / 1000)) + ' · ' + size(r.size))),
    el('button', {class: 'link', onclick: () => post('/api/reveal', {path: r.path})}, 'Show'))));
  if (!s.received.length) rec.append(el('li', {class: 'mute small'}, 'Nothing received yet. Use "Send to Mac" on the phone.'));
  const bk = $('backups');
  bk.replaceChildren(...s.backups.slice(0, 4).map(b => el('li', {},
    el('span', {class: 't'}, 'Backup · ' + b.name, el('span', {class: 'mute'}, ' · ' + size(b.size))),
    el('button', {class: 'link', onclick: () => post('/api/reveal', {path: b.path})}, 'Show'))));

  $('foot').textContent = 'Mai An Companion ' + s.version + ' · ' + s.name + (s.ffmpeg ? ' · ffmpeg ready' : '');
}

// ── actions ──
$('quality').addEventListener('change', e => post('/api/quality', {quality: e.target.value}));
$('prepCancel').addEventListener('click', () => post('/api/cancel-send'));
$('clearQ').addEventListener('click', () => post('/api/clear-queue'));
$('openFolder').addEventListener('click', () => post('/api/open-folder'));

async function sendFiles(files) {
  files = [...files].filter(f => f.size > 0);
  if (!files.length) return;
  const drop = $('drop');
  const paths = [];
  try {
    for (let i = 0; i < files.length; i++) {
      drop.textContent = 'Reading ' + (i + 1) + ' / ' + files.length + ' · ' + files[i].name;
      const r = await fetch('/api/upload?name=' + encodeURIComponent(files[i].name),
        {method: 'POST', headers: H, body: files[i]});
      const j = await r.json();
      if (j.path) paths.push(j.path);
    }
    await post('/api/send', {paths, quality: $('quality').value, dsp: $('dsp').checked});
  } finally {
    drop.textContent = 'Drop audio files here, or click to choose';
  }
}
const drop = $('drop');
drop.addEventListener('click', () => $('file').click());
$('file').addEventListener('change', e => { sendFiles(e.target.files); e.target.value = ''; });
['dragenter', 'dragover'].forEach(n => drop.addEventListener(n, e => { e.preventDefault(); drop.classList.add('over'); }));
['dragleave', 'drop'].forEach(n => drop.addEventListener(n, e => { e.preventDefault(); drop.classList.remove('over'); }));
drop.addEventListener('drop', e => sendFiles(e.dataTransfer.files));
// A file dropped beside the drop zone must not navigate the tab away.
window.addEventListener('dragover', e => e.preventDefault());
window.addEventListener('drop', e => e.preventDefault());

// ── live state ──
fetch('/api/state').then(r => r.json()).then(render);
function connect() {
  const es = new EventSource('/api/events');
  es.onmessage = e => render(JSON.parse(e.data));
  es.onerror = () => { es.close(); $('chipText').textContent = 'Companion stopped'; $('chip').classList.remove('on'); setTimeout(connect, 2000); };
}
connect();
</script>
</body>
</html>
''';
