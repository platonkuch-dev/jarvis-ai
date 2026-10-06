import * as THREE from '../vendor/three.module.min.js';
import { RU, RU_META, SCENARIOS_RU } from './i18n.js';

/* ==========================================================================
   SITE SETTINGS — edit these, then run  build.bat  (or leave empty for defaults)
   ========================================================================== */
// The backend (form, stats, download counter) lives on Cloudflare Pages. The public address is on Netlify,
// which proxies /api/* and /dl/* to it (netlify.toml), so the API is same-origin there too. Only a static copy
// without a proxy (e.g. GitHub Pages) has to call the API cross-origin.
const API_ORIGIN = 'https://jarvis-ai-site-98w.pages.dev';
const ON_API_HOST = /^https?:/.test(location.protocol) && !location.hostname.endsWith('.github.io');
const API = ON_API_HOST ? '' : API_ORIGIN;

const SITE = {
  name: 'Platon_ind', // shown in the logo and footer
  fiverrUrl: 'https://www.fiverr.com/s/WeEzaZE',
  email: 'platonindustries@gmail.com',
  // Files offered on the "Downloads" section. An item with an empty url is skipped;
  // the whole section stays hidden until at least one url is filled in.
  // url: a direct link (GitHub Releases, Google Drive, your own server) or a relative path like 'downloads/file.zip'.
  downloads: [
    { title: 'JARVIS AI — Windows installer', titleRu: 'JARVIS AI — установщик для Windows',
      desc: 'One-click installer with everything bundled.', descRu: 'Установщик «в один клик», всё уже внутри.',
      meta: 'JarvisAI-Setup.exe · 280 MB',
      // on the live site /dl/jarvis counts the download and redirects to the latest GitHub release
      // the mirror links straight to GitHub, which opens where Cloudflare is blocked
      url: ON_API_HOST ? '/dl/jarvis' : 'https://github.com/platonkuch-dev/jarvis-ai/releases/latest' },
  ],
};

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const lerp = (a, b, t) => a + (b - a) * t;
const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
const isTouch = matchMedia('(hover: none)').matches;

/* ---------- shared state between the terminal demo and the 3D scene ---------- */
const voice = { level: 0, target: 0 };

/* ---------- language ---------- */
let lang = 'en';
try { const saved = localStorage.getItem('lang'); if (saved === 'ru' || saved === 'en') lang = saved; } catch (e) { /* storage blocked */ }

/* ==========================================================================
   SITE SETTINGS → DOM
   ========================================================================== */
(function applySettings() {
  if (SITE.name) {
    $('.logo-by').textContent = SITE.name.trim().toUpperCase();
  }
  if (SITE.fiverrUrl) {
    $$('[data-fiverr]').forEach(a => { a.href = SITE.fiverrUrl; a.target = '_blank'; a.rel = 'noopener'; });
  }
  if (SITE.email) $$('[data-mail]').forEach(a => { a.href = `mailto:${SITE.email}`; });
})();

/* ==========================================================================
   REVEAL ON SCROLL + COUNTERS
   ========================================================================== */
$$('.reveal').forEach(el => el.style.setProperty('--d', el.dataset.delay || 0));

function runCounter(el) {
  const to = +el.dataset.count, prefix = el.dataset.prefix || '', suf = el.dataset.suffix || '';
  if (reduceMotion) { el.textContent = prefix + to + suf; return; }
  const t0 = performance.now(), dur = 1600;
  (function tick(now) {
    const p = clamp((now - t0) / dur, 0, 1), e = 1 - Math.pow(1 - p, 4);
    el.textContent = prefix + Math.round(to * e) + suf;
    if (p < 1) requestAnimationFrame(tick);
  })(t0);
}

const io = new IntersectionObserver(entries => {
  for (const en of entries) {
    if (!en.isIntersecting) continue;
    en.target.classList.add('in');
    $$('[data-count]', en.target).forEach(runCounter);
    io.unobserve(en.target);
  }
}, { threshold: .15, rootMargin: '0px 0px -6% 0px' });
$$('.reveal').forEach(el => io.observe(el));

/* ==========================================================================
   NAV
   ========================================================================== */
const nav = $('#nav'), progress = $('#progress');
const burger = $('#burger'), navLinks = $('#navLinks');
function setMenu(open) {
  navLinks.classList.toggle('open', open);
  burger.setAttribute('aria-expanded', open);
  document.documentElement.classList.toggle('menu-open', open);   // lock page scroll behind the menu
}
burger.addEventListener('click', () => setMenu(!navLinks.classList.contains('open')));
navLinks.addEventListener('click', e => { if (e.target.closest('a')) setMenu(false); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') setMenu(false); });
addEventListener('resize', () => { if (innerWidth > 820) setMenu(false); });

const sectionEls = $$('main section[id]');
const linkFor = id => $(`.nav-links a[href="#${id}"]`);
let lastY = 0;
function onScroll() {
  const y = scrollY, max = document.documentElement.scrollHeight - innerHeight;
  progress.style.transform = `scaleX(${max > 0 ? y / max : 0})`;
  nav.classList.toggle('scrolled', y > 30);
  nav.classList.toggle('hide', y > lastY && y > 500 && !navLinks.classList.contains('open'));
  lastY = y;
  const mid = y + innerHeight * .4;
  let cur = null;
  for (const s of sectionEls) if (!s.hidden && s.offsetTop <= mid) cur = s.id;
  $$('.nav-links a').forEach(a => a.classList.toggle('active', a === linkFor(cur)));
  stackScroll();
}
addEventListener('scroll', onScroll, { passive: true });

/* ==========================================================================
   CURSOR GLOW, MAGNETIC BUTTONS, CARD TILT
   ========================================================================== */
const cursor = $('#cursor');
const mouse = { x: 0, y: 0, nx: 0, ny: 0 };
addEventListener('pointermove', e => {
  mouse.x = e.clientX; mouse.y = e.clientY;
  mouse.nx = e.clientX / innerWidth * 2 - 1;
  mouse.ny = e.clientY / innerHeight * 2 - 1;
  cursor.classList.add('on');
  cursor.style.transform = `translate(${e.clientX}px, ${e.clientY}px)`;
}, { passive: true });
document.documentElement.addEventListener('pointerleave', () => cursor.classList.remove('on'));

if (!isTouch && !reduceMotion) {
  $$('.magnetic').forEach(btn => {
    btn.addEventListener('pointermove', e => {
      const r = btn.getBoundingClientRect();
      const x = e.clientX - (r.left + r.width / 2), y = e.clientY - (r.top + r.height / 2);
      btn.style.transform = `translate(${x * .25}px, ${y * .35}px)`;
    });
    btn.addEventListener('pointerleave', () => { btn.style.transform = ''; });
  });

  $$('.tilt').forEach(card => {
    card.addEventListener('pointermove', e => {
      const r = card.getBoundingClientRect();
      const px = (e.clientX - r.left) / r.width, py = (e.clientY - r.top) / r.height;
      card.style.setProperty('--ry', `${(px - .5) * 14}deg`);
      card.style.setProperty('--rx', `${(.5 - py) * 14}deg`);
      card.style.setProperty('--mx', `${px * 100}%`);
      card.style.setProperty('--my', `${py * 100}%`);
    });
    card.addEventListener('pointerleave', () => {
      card.style.setProperty('--rx', '0deg'); card.style.setProperty('--ry', '0deg');
    });
  });
}

/* ==========================================================================
   HEADLINE WORD SCRAMBLE
   ========================================================================== */
const wordList = () => $('#scramble').dataset[lang === 'ru' ? 'wordsRu' : 'words'].split('|').map(w => w + '.');
function buildSizer() {                       // one invisible copy per word, so the block always fits the longest
  const box = $('#swap');
  $$('.swap-ghost', box).forEach(g => g.remove());
  for (const w of wordList()) {
    const g = document.createElement('span');
    g.className = 'swap-ghost'; g.setAttribute('aria-hidden', 'true'); g.textContent = w;
    box.insertBefore(g, $('#scramble'));
  }
}
let resetScramble = () => { buildSizer(); $('#scramble').textContent = wordList()[0]; };
(function scramble() {
  const el = $('#scramble');
  if (reduceMotion) return;
  const glyphs = '01<>/\\[]{}—=+*^?#';
  let i = 0;
  function swap(next) {
    const from = el.textContent, len = Math.max(from.length, next.length);
    const t0 = performance.now(), dur = 700;
    (function frame(now) {
      const p = clamp((now - t0) / dur, 0, 1);
      let out = '';
      for (let k = 0; k < len; k++) {
        const settle = k / len * .7 + .1;
        out += p > settle ? (next[k] ?? '') : glyphs[(Math.random() * glyphs.length) | 0];
      }
      el.textContent = out;
      if (p < 1) requestAnimationFrame(frame); else el.textContent = next;
    })(t0);
  }
  resetScramble = () => { i = 0; buildSizer(); el.textContent = wordList()[0]; };
  setInterval(() => { const w = wordList(); i = (i + 1) % w.length; swap(w[i]); }, 2600);
})();

/* ==========================================================================
   3D ARCHITECTURE STACK (scroll-driven explode)
   ========================================================================== */
const stackEl = $('#stack'), stackStage = $('#stackStage');
const layers = $$('.layer', stackEl);
function stackScroll() {
  const r = stackStage.getBoundingClientRect();
  const p = clamp(1 - (r.top + r.height * .3) / (innerHeight * .9), 0, 1);
  const narrow = innerWidth <= 820;
  const rx = lerp(58, narrow ? 40 : 44, p), rz = lerp(-26, narrow ? -10 : -14, p);
  stackEl.style.transform = `rotateX(${rx}deg) rotateZ(${rz}deg)${narrow ? ' scale(.7)' : ''}`;
  layers.forEach((l, i) => {
    const order = layers.length - 1 - i;           // l4 (top) has order 0 … l1 has order 3
    l.style.transform = `translateZ(${(3 - order) * p * 38}px)`;
    l.style.opacity = .55 + .45 * clamp(p * 1.4, 0, 1);
  });
}

/* ==========================================================================
   LIVE TERMINAL DEMO
   ========================================================================== */
const SCENARIOS_EN = [
  [
    { k: 'sys', t: 'session started · microphone live' },
    { k: 'user', t: '“Jarvis, open Notepad and write down my meeting notes.”', s: 'listening' },
    { k: 'tool', t: 'open_app(app="Notepad")        → window focused  ✓', s: 'thinking', wait: 700 },
    { k: 'tool', t: 'ui_automation(type, 214 chars) → text entered    ✓', wait: 600 },
    { k: 'ai', t: 'Done, sir. Notepad is open and your notes are written.', s: 'speaking' },
  ],
  [
    { k: 'sys', t: 'session started · microphone live' },
    { k: 'user', t: '“What’s on my screen right now?”', s: 'listening' },
    { k: 'ai', t: 'Looking at your screen now, sir.', s: 'speaking' },
    { k: 'tool', t: 'screen_process(angle="screen") → frame captured  ✓', s: 'thinking', wait: 500 },
    { k: 'ai', t: 'A code editor with a Python file is open, and the terminal below shows every test passing.', s: 'speaking' },
  ],
  [
    { k: 'sys', t: 'session started · microphone live' },
    { k: 'user', t: '“Give me the latest AI news.”', s: 'listening' },
    { k: 'tool', t: 'web_search(mode="news")        → 2 backends racing…', s: 'thinking', wait: 600 },
    { k: 'tool', t: 'first valid result wins        → article links  ✓', wait: 800 },
    { k: 'ai', t: 'Here are today’s top stories, sir — each with its source and a direct link.', s: 'speaking' },
  ],
  [
    { k: 'sys', t: 'session started · microphone live' },
    { k: 'user', t: '“Run a diagnostic on my PC.”', s: 'listening' },
    { k: 'tool', t: 'system_status()                → CPU · RAM · GPU · disk  ✓', s: 'thinking', wait: 600 },
    { k: 'sys', t: 'CPU ▮▮▯▯▯▯▯▯  RAM ▮▮▮▮▮▯▯▯  GPU ▮▮▮▯▯▯▯▯  DISK ok', wait: 300 },
    { k: 'ai', t: 'All systems nominal, sir. Nothing needs your attention.', s: 'speaking' },
  ],
];

const scenarios = () => (lang === 'ru' ? SCENARIOS_RU : SCENARIOS_EN);
const term = { body: $('#termBody'), state: $('#termState'), wave: $('#wave') };
const bars = [];
for (let i = 0; i < 56; i++) { const b = document.createElement('span'); term.wave.appendChild(b); bars.push(b); }

let runToken = 0, scenarioIdx = 0, demoVisible = false, waveState = 'idle';
const sleep = ms => new Promise(r => setTimeout(r, ms));

function setState(s) {
  waveState = s;
  term.state.dataset.s = s;
  term.state.innerHTML = `<i></i>${s.toUpperCase()}`;
  voice.target = s === 'speaking' ? 1 : s === 'listening' ? .55 : s === 'thinking' ? .2 : 0;
}

async function playScenario(idx) {
  const my = ++runToken;
  term.body.innerHTML = '';
  setState('idle');
  const who = lang === 'ru' ? RU_META.who : { user: 'you ›', ai: 'jarvis ›', tool: 'tool ›', sys: '#' };
  for (const step of scenarios()[idx]) {
    if (my !== runToken) return;
    if (step.s) setState(step.s);
    if (step.wait) await sleep(step.wait);
    const line = document.createElement('div');
    line.className = `ln ${step.k}`;
    line.innerHTML = `<span class="who">${who[step.k]}</span><span class="txt"></span><span class="caret"></span>`;
    term.body.appendChild(line);
    const txt = $('.txt', line);
    const speed = step.k === 'tool' || step.k === 'sys' ? 9 : step.k === 'user' ? 30 : 20;
    if (reduceMotion) txt.textContent = step.t;
    else for (const ch of step.t) {
      if (my !== runToken) return;
      txt.textContent += ch;
      await sleep(speed);
    }
    $('.caret', line).remove();
    await sleep(step.k === 'user' ? 500 : 350);
  }
  if (my !== runToken) return;
  setState('idle');
  await sleep(4200);
  if (my === runToken && demoVisible) playScenario(idx);
}

$('#scenarioChips').addEventListener('click', e => {
  const b = e.target.closest('.chip'); if (!b) return;
  $$('.chip').forEach(c => c.classList.toggle('active', c === b));
  scenarioIdx = +b.dataset.scenario;
  playScenario(scenarioIdx);
});

new IntersectionObserver(([en]) => {
  const was = demoVisible; demoVisible = en.isIntersecting;
  if (demoVisible && !was) playScenario(scenarioIdx);
  if (!demoVisible) { runToken++; setState('idle'); }
}, { threshold: .35 }).observe($('#terminal'));

/* waveform bars */
let waveT = 0;
function updateWave(dt) {
  waveT += dt;
  const n = bars.length;
  for (let i = 0; i < n; i++) {
    let h = 3;
    const env = Math.sin((i / (n - 1)) * Math.PI);           // taller in the middle
    if (waveState === 'speaking') h = 6 + env * 40 * (.35 + .65 * Math.abs(Math.sin(waveT * 9 + i * .7) * Math.cos(waveT * 4.3 + i * .21)));
    else if (waveState === 'listening') h = 4 + env * 22 * (.2 + .8 * Math.abs(Math.sin(waveT * 6 + i * 1.3) * Math.sin(waveT * 2.1 + i * .4)));
    else if (waveState === 'thinking') h = 4 + env * 8 * (.5 + .5 * Math.sin(waveT * 3 - i * .35));
    bars[i].style.height = `${h}px`;
    bars[i].style.opacity = waveState === 'idle' ? .25 : .95;
  }
}

/* ==========================================================================
   LANGUAGE (EN / RU)
   ========================================================================== */
const i18nEls = $$('[data-i18n]');
i18nEls.forEach(el => { el.dataset.en = el.innerHTML; });       // English = whatever is in the HTML
const EN_META = { title: document.title, description: $('meta[name="description"]').content };
const langBtns = $$('#lang button');
const phEls = $$('[data-i18n-ph]');
phEls.forEach(el => { el.dataset.enPh = el.placeholder; });

function renderDownloads() {
  const items = (SITE.downloads || []).filter(d => d.url);
  $('#downloads').hidden = !items.length;
  $('#navDl').hidden = !items.length;
  const grid = $('#dlGrid'); grid.textContent = '';
  const ru = lang === 'ru';
  for (const d of items) {
    const card = document.createElement('article');
    card.className = 'dl reveal';
    card.innerHTML = '<div class="ico"><svg viewBox="0 0 24 24"><path d="M12 3v12M7 10l5 5 5-5M4 20h16"/></svg></div><h3></h3><p></p><span class="dl-meta"></span><a class="btn"></a>';
    $('h3', card).textContent = ru && d.titleRu ? d.titleRu : d.title;
    $('p', card).textContent = ru && d.descRu ? d.descRu : d.desc;
    $('.dl-meta', card).textContent = ru && d.metaRu ? d.metaRu : d.meta;
    const a = $('.btn', card);
    a.textContent = ru ? RU_META.dlBtn : 'Download';
    a.href = d.url;
    if (/^https?:/i.test(d.url)) { a.target = '_blank'; a.rel = 'noopener'; } else if (!d.url.startsWith('/dl/')) a.setAttribute('download', '');
    grid.appendChild(card);
    io.observe(card);
  }
}

function applyLang(next, restartDemo = true) {
  lang = next;
  i18nEls.forEach(el => {
    el.innerHTML = lang === 'ru' ? (RU[el.dataset.i18n] ?? el.dataset.en) : el.dataset.en;
  });
  phEls.forEach(el => { el.placeholder = lang === 'ru' ? (RU[el.dataset.i18nPh] ?? el.dataset.enPh) : el.dataset.enPh; });
  const meta = lang === 'ru' ? RU_META : EN_META;
  document.title = meta.title;
  $('meta[name="description"]').content = meta.description;
  document.documentElement.lang = lang;
  document.documentElement.classList.toggle('is-ru', lang === 'ru');

  const name = SITE.name || 'Platon_ind', year = new Date().getFullYear();
  $('#footCopy').textContent = lang === 'ru' ? RU_META.copy(year, name) : `© ${year} ${name}. All rights reserved.`;
  langBtns.forEach(b => b.setAttribute('aria-pressed', b.dataset.lang === lang));
  renderDownloads();
  resetScramble();
  if (restartDemo && demoVisible) playScenario(scenarioIdx);
  try { localStorage.setItem('lang', lang); } catch (e) { /* storage blocked */ }
}
langBtns.forEach(b => b.addEventListener('click', () => { if (b.dataset.lang !== lang) applyLang(b.dataset.lang); }));
applyLang(lang, false);

/* ==========================================================================
   VISIT STATS + LEAD FORM (Cloudflare Pages Functions → D1, see functions/)
   ========================================================================== */
const live = /^https?:/.test(location.protocol);
// where the visitor came from: kept for the session so a lead sent later still knows it was TikTok/Reddit/…
const visit = (() => {
  const q = new URLSearchParams(location.search);
  let v = { utm: q.get('utm_source') || q.get('ref') || '', ref: document.referrer || '' };
  try {
    const saved = JSON.parse(sessionStorage.getItem('visit') || 'null');
    if (saved) v = saved; else sessionStorage.setItem('visit', JSON.stringify(v));
  } catch (e) { /* storage blocked */ }
  return v;
})();
const sourceGuess = () => {
  if (visit.utm) return visit.utm.toLowerCase();
  try { return visit.ref ? new URL(visit.ref).hostname.replace(/^www\./, '') : 'direct'; } catch (e) { return 'direct'; }
};
if (live) {
  const path = (ON_API_HOST ? '' : 'mirror:') + location.pathname;
  const body = JSON.stringify({ path, ref: document.referrer, utm: visit.utm, lang });
  try { navigator.sendBeacon(API + '/api/hit', new Blob([body], { type: 'text/plain' })); } catch (e) { /* stats are best-effort */ }
}

(function leadForm() {
  const form = $('#leadForm'); if (!form) return;
  const status = $('#leadStatus'), btn = $('button[type=submit]', form);
  const T = () => lang === 'ru' ? RU_META.form : {
    sending: 'Sending…', ok: 'Done! I’ll reply within 24 hours.', fill: 'Please fill in your name, contact and task (10+ characters).',
    limit: 'Too many requests, please try again in an hour.', fail: 'Couldn’t send it. Please email me or message me on Fiverr.',
    offline: 'Your network blocks the form. Tap here to send the same request by email →' };
  const say = (msg, cls = '') => { status.textContent = msg; status.className = 'lf-status ' + cls; };
  form.addEventListener('submit', async e => {
    e.preventDefault();
    const d = Object.fromEntries(new FormData(form));
    if (!d.name.trim() || !d.contact.trim() || d.message.trim().length < 10) return say(T().fill, 'err');
    btn.disabled = true; say(T().sending);
    try {
      // text/plain keeps it a "simple" request, so the mirror needs no CORS preflight
      const r = await fetch(API + '/api/lead', { method: 'POST', headers: { 'content-type': 'text/plain' },
        body: JSON.stringify({ ...d, lang, source: sourceGuess() + (ON_API_HOST ? '' : ' (mirror)') }) });
      if (r.ok) { form.reset(); say(T().ok, 'ok'); }
      else say(r.status === 429 ? T().limit : T().fail, 'err');
    } catch (err) {
      // the API is unreachable (blocked network): offer the same request as a ready-made email
      const subject = `JARVIS request from ${d.name}`;
      const text = `${d.message}\n\nContact: ${d.contact}\nBudget: ${d.budget || '-'}`;
      say('', 'err');
      const a = document.createElement('a');
      a.href = `mailto:${SITE.email}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(text)}`;
      a.textContent = T().offline; a.style.color = 'inherit'; a.style.textDecoration = 'underline';
      status.appendChild(a);
    }
    btn.disabled = false;
  });
})();

/* ==========================================================================
   THREE.JS — FIXED BACKGROUND SCENE
   ========================================================================== */
const canvas = $('#bg');
let renderer;
try {
  renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, powerPreference: 'high-performance' });
} catch (err) {
  document.documentElement.classList.add('no-webgl');
}

if (renderer) initScene();
else { onScroll(); stackScroll(); requestAnimationFrame(function loop(t) { updateWave(.016); requestAnimationFrame(loop); }); }

function initScene() {
  const small = innerWidth < 720;
  renderer.setPixelRatio(Math.min(devicePixelRatio, small ? 1.5 : 1.75));
  renderer.setClearColor(0x000000, 0);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(45, 1, .1, 100);
  camera.position.z = 9;

  const COL = { cyan: new THREE.Color(0x22d3ee), indigo: new THREE.Color(0x6366f1), violet: new THREE.Color(0xa78bfa) };
  const tint = COL.cyan.clone();

  /* ---- rig: everything that forms the "arc reactor" ---- */
  const rig = new THREE.Group();     // positioned per section
  const core = new THREE.Group();    // rotates with mouse
  rig.add(core);
  scene.add(rig);

  // glow sprite
  const glowTex = (() => {
    const c = document.createElement('canvas'); c.width = c.height = 256;
    const g = c.getContext('2d'), gr = g.createRadialGradient(128, 128, 0, 128, 128, 128);
    gr.addColorStop(0, 'rgba(255,255,255,.9)'); gr.addColorStop(.25, 'rgba(255,255,255,.35)'); gr.addColorStop(1, 'rgba(255,255,255,0)');
    g.fillStyle = gr; g.fillRect(0, 0, 256, 256);
    return new THREE.CanvasTexture(c);
  })();
  const glow = new THREE.Sprite(new THREE.SpriteMaterial({ map: glowTex, color: tint, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false, opacity: .8 }));
  glow.scale.set(6.5, 6.5, 1);
  rig.add(glow);

  // fresnel energy sphere
  const sphereMat = new THREE.ShaderMaterial({
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    uniforms: { uTime: { value: 0 }, uColor: { value: tint }, uVoice: { value: 0 }, uOpacity: { value: 1 } },
    vertexShader: `
      uniform float uTime; uniform float uVoice;
      varying vec3 vN; varying vec3 vV; varying float vD;
      void main(){
        vec3 p = position;
        float d = sin(p.x*3.1 + uTime*1.4) * sin(p.y*2.7 - uTime*1.1) * sin(p.z*3.3 + uTime*.9);
        p += normal * d * (.07 + uVoice * .16);
        vD = d;
        vec4 mv = modelViewMatrix * vec4(p, 1.0);
        vN = normalize(normalMatrix * normal);
        vV = normalize(-mv.xyz);
        gl_Position = projectionMatrix * mv;
      }`,
    fragmentShader: `
      uniform vec3 uColor; uniform float uOpacity; uniform float uVoice; uniform float uTime;
      varying vec3 vN; varying vec3 vV; varying float vD;
      void main(){
        float f = pow(1.0 - abs(dot(normalize(vN), normalize(vV))), 2.2);
        // Thin bands of brighter energy creeping across the surface over time —
        // cheap (one extra sine of the fresnel term), but reads as circuitry
        // alive under the shell rather than a static gradient up close.
        float band = smoothstep(.75, 1.0, sin(f * 18.0 - uTime * 1.6)) * .5;
        vec3 c = mix(uColor, vec3(1.0), f * .35 + vD * .25 + uVoice * .15 + band);
        gl_FragColor = vec4(c, (f * .95 + .16 + uVoice * .15 + band * .3) * uOpacity);
      }`,
  });
  const sphere = new THREE.Mesh(new THREE.IcosahedronGeometry(1.25, 5), sphereMat);
  core.add(sphere);

  // inner power source — a small, hot, independently-pulsing core visible
  // through the translucent shell, so the reactor reads as "something is
  // generating this" rather than one flat glowing ball.
  const innerCoreMat = new THREE.ShaderMaterial({
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    uniforms: { uTime: { value: 0 }, uColor: { value: tint }, uVoice: { value: 0 }, uOpacity: { value: 1 } },
    vertexShader: `
      uniform float uTime; uniform float uVoice;
      varying vec3 vN; varying vec3 vV;
      void main(){
        vec3 p = position * (1.0 + sin(uTime * 3.1) * .05 + uVoice * .08);
        vec4 mv = modelViewMatrix * vec4(p, 1.0);
        vN = normalize(normalMatrix * normal);
        vV = normalize(-mv.xyz);
        gl_Position = projectionMatrix * mv;
      }`,
    fragmentShader: `
      uniform vec3 uColor; uniform float uOpacity;
      varying vec3 vN; varying vec3 vV;
      void main(){
        float f = pow(1.0 - abs(dot(normalize(vN), normalize(vV))), 1.4);
        gl_FragColor = vec4(mix(uColor, vec3(1.0), .6 + f * .4), (.5 + f * .5) * uOpacity);
      }`,
  });
  const innerCore = new THREE.Mesh(new THREE.IcosahedronGeometry(.5, 3), innerCoreMat);
  core.add(innerCore);

  // wireframe shell
  const shellMat = new THREE.LineBasicMaterial({ color: tint, transparent: true, opacity: .35, blending: THREE.AdditiveBlending, depthWrite: false });
  const shell = new THREE.LineSegments(new THREE.WireframeGeometry(new THREE.IcosahedronGeometry(1.75, 1)), shellMat);
  core.add(shell);

  // a finer, counter-rotating inner cage between the core and the outer shell
  // — extra layers of geometry read as "detailed" far more than extra polish
  // on any single layer, and the parallax between the two rotations sells
  // depth even in a still frame.
  const shell2Mat = new THREE.LineBasicMaterial({ color: COL.violet, transparent: true, opacity: .22, blending: THREE.AdditiveBlending, depthWrite: false });
  const shell2 = new THREE.LineSegments(new THREE.WireframeGeometry(new THREE.IcosahedronGeometry(1.45, 2)), shell2Mat);
  core.add(shell2);

  // flickering surface sparks — a handful of point-lights pinned to the
  // sphere's own vertices that blink on and off out of phase, like current
  // arcing across the shell. Cheap: reuses the same additive point-sprite
  // technique as the background particle field below.
  const sparkPositions = new THREE.IcosahedronGeometry(1.32, 2).attributes.position;
  const sparkCount = sparkPositions.count;
  const skGeo = new THREE.BufferGeometry();
  const skPhase = new Float32Array(sparkCount);
  for (let i = 0; i < sparkCount; i++) skPhase[i] = Math.random() * Math.PI * 2;
  skGeo.setAttribute('position', sparkPositions.clone());
  skGeo.setAttribute('aPhase', new THREE.BufferAttribute(skPhase, 1));
  const sparksMat = new THREE.ShaderMaterial({
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    uniforms: { uTime: { value: 0 }, uColor: { value: tint }, uOpacity: { value: 1 }, uPx: { value: 1 } },
    vertexShader: `
      attribute float aPhase; uniform float uTime; uniform float uPx;
      varying float vA;
      void main(){
        float blink = pow(max(0.0, sin(uTime * 2.4 + aPhase)), 6.0);
        vA = blink;
        vec4 mv = modelViewMatrix * vec4(position, 1.0);
        gl_PointSize = (2.5 + blink * 3.5) * uPx;
        gl_Position = projectionMatrix * mv;
      }`,
    fragmentShader: `
      uniform vec3 uColor; uniform float uOpacity; varying float vA;
      void main(){
        float d = length(gl_PointCoord - .5);
        gl_FragColor = vec4(mix(uColor, vec3(1.0), .7), smoothstep(.5, 0.0, d) * vA * uOpacity);
      }`,
  });
  const sparks = new THREE.Points(skGeo, sparksMat);
  core.add(sparks);

  // orbital rings
  const ringMats = [], rings = [];
  [[2.35, .012, .3, .2, 0], [2.75, .008, 1.15, -.5, 0], [3.15, .006, -.6, .9, 0]].forEach(([r, w, rx, ry], i) => {
    const m = new THREE.MeshBasicMaterial({ color: i === 1 ? COL.indigo : tint, transparent: true, opacity: .7, blending: THREE.AdditiveBlending, depthWrite: false });
    const ring = new THREE.Mesh(new THREE.TorusGeometry(r, w, 10, 160), m);
    ring.rotation.set(rx, ry, 0);
    // satellite, with a short trailing "comet" ghost a few degrees behind it
    // so the orbit reads as motion even in a still screenshot, not just a dot.
    const satSize = .07 - i * .012;
    const sat = new THREE.Mesh(new THREE.SphereGeometry(satSize, 16, 16), new THREE.MeshBasicMaterial({ color: 0xffffff }));
    const trailMat = new THREE.MeshBasicMaterial({ color: i === 1 ? COL.indigo : tint, transparent: true, opacity: .5, blending: THREE.AdditiveBlending, depthWrite: false });
    const trail = new THREE.Mesh(new THREE.SphereGeometry(satSize * .55, 10, 10), trailMat);
    sat.userData = { r, a: i * 2.1, sp: .6 - i * .17 };
    ring.add(sat); ring.add(trail);
    core.add(ring); rings.push(ring); ringMats.push(m);
  });

  // voice rings (line loops displaced by the "audio" level)
  const N = 160;
  function makeVoiceRing(radius, color, opacity) {
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(N * 3), 3));
    const mat = new THREE.LineBasicMaterial({ color, transparent: true, opacity, blending: THREE.AdditiveBlending, depthWrite: false });
    const line = new THREE.LineLoop(geo, mat);
    line.userData = { radius };
    rig.add(line);
    return line;
  }
  const vr1 = makeVoiceRing(2.0, tint, .9);
  const vr2 = makeVoiceRing(2.0, COL.violet, .55);
  vr1.rotation.x = vr2.rotation.x = -.35;

  function updateVoiceRing(line, t, phase, amp) {
    const pos = line.geometry.attributes.position, R = line.userData.radius;
    for (let i = 0; i < N; i++) {
      const a = (i / N) * Math.PI * 2;
      const n = Math.sin(a * 6 + t * 2.2 + phase) * .5 + Math.sin(a * 11 - t * 3.1 + phase * 2) * .3 + Math.sin(a * 3 + t * 1.3) * .2;
      const r = R + n * amp;
      pos.setXYZ(i, Math.cos(a) * r, Math.sin(a) * r, 0);
    }
    pos.needsUpdate = true;
  }

  /* ---- particle field ---- */
  const COUNT = small ? 600 : 1800;
  const pGeo = new THREE.BufferGeometry();
  const pp = new Float32Array(COUNT * 3), ps = new Float32Array(COUNT), pc = new Float32Array(COUNT);
  for (let i = 0; i < COUNT; i++) {
    const r = 5 + Math.random() * 16, th = Math.random() * Math.PI * 2, ph = Math.acos(2 * Math.random() - 1);
    pp[i * 3] = r * Math.sin(ph) * Math.cos(th) * 1.4;
    pp[i * 3 + 1] = r * Math.sin(ph) * Math.sin(th) * .9;
    pp[i * 3 + 2] = r * Math.cos(ph) - 6;
    ps[i] = Math.random() * 1.6 + .4;
    pc[i] = Math.random();
  }
  pGeo.setAttribute('position', new THREE.BufferAttribute(pp, 3));
  pGeo.setAttribute('aSize', new THREE.BufferAttribute(ps, 1));
  pGeo.setAttribute('aMix', new THREE.BufferAttribute(pc, 1));
  const pMat = new THREE.ShaderMaterial({
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    uniforms: { uTime: { value: 0 }, uPx: { value: renderer.getPixelRatio() }, uA: { value: tint }, uB: { value: COL.indigo }, uOpacity: { value: 1 } },
    vertexShader: `
      attribute float aSize; attribute float aMix;
      uniform float uTime; uniform float uPx;
      varying float vMix; varying float vTw;
      void main(){
        vec3 p = position;
        p.y += sin(uTime*.25 + p.x*.4) * .35;
        p.x += cos(uTime*.2 + p.y*.3) * .25;
        vec4 mv = modelViewMatrix * vec4(p,1.0);
        gl_PointSize = aSize * uPx * (34.0 / -mv.z);
        vMix = aMix; vTw = .55 + .45*sin(uTime*1.5 + aMix*40.0);
        gl_Position = projectionMatrix * mv;
      }`,
    fragmentShader: `
      uniform vec3 uA; uniform vec3 uB; uniform float uOpacity;
      varying float vMix; varying float vTw;
      void main(){
        float d = length(gl_PointCoord - .5);
        float a = smoothstep(.5, .0, d) * vTw * uOpacity;
        gl_FragColor = vec4(mix(uA, uB, vMix), a * .85);
      }`,
  });
  const points = new THREE.Points(pGeo, pMat);
  scene.add(points);

  /* ---- section-driven targets ---- */
  const S = {
    hero:         { x: .52,  y: 0,   s: 1,   o: 1,   c: COL.cyan },
    work:         { x: -.95, y: .05, s: .5,  o: .34,  c: COL.indigo },
    capabilities: { x: .98,  y: 0,   s: .42, o: .3,  c: COL.violet },
    demo:         { x: -.98, y: .1,  s: .5,  o: .34,  c: COL.cyan },
    architecture: { x: .98,  y: 0,   s: .42, o: .3,  c: COL.indigo },
    process:      { x: .98,   y: 0,   s: .42, o: .3,  c: COL.cyan },
    faq:          { x: -.98, y: 0,   s: .42, o: .26,  c: COL.indigo },
    contact:      { x: 0,    y: .1,  s: .95, o: .75, c: COL.cyan },
  };
  const sections = $$('main section[id]').filter(s => S[s.id]);
  const st = { x: S.hero.x, y: 0, s: 1, o: 1 };
  const targetTint = COL.cyan.clone();

  let halfW = 5, halfH = 4;
  function resize() {
    const w = innerWidth, h = innerHeight;
    renderer.setSize(w, h, false);
    camera.aspect = w / h; camera.updateProjectionMatrix();
    halfH = Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)) * camera.position.z;
    halfW = halfH * camera.aspect;
    pMat.uniforms.uPx.value = renderer.getPixelRatio();
    sparksMat.uniforms.uPx.value = renderer.getPixelRatio();
  }
  addEventListener('resize', () => { resize(); onScroll(); });
  resize(); onScroll();

  const clock = new THREE.Clock();
  const tm = reduceMotion ? .25 : 1;
  let mx = 0, my = 0, running = true;
  document.addEventListener('visibilitychange', () => { running = !document.hidden; if (running) clock.getDelta(); });

  renderer.setAnimationLoop(() => {
    if (!running) return;
    const dt = Math.min(clock.getDelta(), .05) * tm, t = clock.elapsedTime;

    // which section holds the viewport's centre?
    const mid = scrollY + innerHeight * .5;
    let cur = sections[0];
    for (const s of sections) if (!s.hidden && s.offsetTop <= mid) cur = s;
    const tg = S[cur.id];
    const narrow = innerWidth < 900;
    const tx = narrow ? 0 : tg.x * halfW, ty = narrow ? (cur.id === 'hero' ? halfH * .42 : 0) : tg.y * halfH;
    const to = narrow ? tg.o * .5 : tg.o;
    const k = 1 - Math.pow(.04, dt);
    st.x = lerp(st.x, tx, k); st.y = lerp(st.y, ty, k);
    const fit = camera.aspect < 1.1 ? clamp(camera.aspect * .95, .4, 1) : 1;   // keep the reactor inside portrait screens
    st.s = lerp(st.s, (narrow ? tg.s * .8 : tg.s) * fit, k); st.o = lerp(st.o, to, k);
    targetTint.copy(tg.c); tint.lerp(targetTint, k);

    rig.position.set(st.x, st.y, 0);
    rig.scale.setScalar(st.s);
    sphereMat.uniforms.uOpacity.value = st.o;
    innerCoreMat.uniforms.uOpacity.value = st.o;
    glow.material.opacity = .8 * st.o;
    shellMat.opacity = .35 * st.o;
    shell2Mat.opacity = .22 * st.o;
    sparksMat.uniforms.uOpacity.value = st.o;
    ringMats.forEach(m => m.opacity = .7 * st.o);
    vr1.material.opacity = .9 * st.o; vr2.material.opacity = .55 * st.o;
    pMat.uniforms.uOpacity.value = .55 + .45 * (cur.id === 'hero' ? 1 : .7);

    // voice level eases toward the demo's state; idle gets a gentle breath
    voice.level = lerp(voice.level, voice.target + (voice.target === 0 ? (Math.sin(t * 1.2) * .5 + .5) * .12 : 0), 1 - Math.pow(.02, dt));
    sphereMat.uniforms.uTime.value = t; sphereMat.uniforms.uVoice.value = voice.level;
    innerCoreMat.uniforms.uTime.value = t; innerCoreMat.uniforms.uVoice.value = voice.level;
    sparksMat.uniforms.uTime.value = t;
    pMat.uniforms.uTime.value = t;

    // pointer parallax
    mx = lerp(mx, mouse.nx, k * .6); my = lerp(my, mouse.ny, k * .6);
    core.rotation.y += dt * .22;
    core.rotation.x = lerp(core.rotation.x, my * .35, k * .6);
    rig.rotation.y = mx * .25;
    shell.rotation.y -= dt * .12; shell.rotation.x += dt * .05;
    shell2.rotation.y += dt * .17; shell2.rotation.z -= dt * .08;
    rings.forEach((r, i) => {
      r.rotation.z += dt * (.18 + i * .09) * (i % 2 ? -1 : 1);
      const sat = r.children[0], trail = r.children[1], u = sat.userData;
      u.a += dt * u.sp;
      sat.position.set(Math.cos(u.a) * u.r, Math.sin(u.a) * u.r, 0);
      const lag = u.a - Math.sign(u.sp || 1) * .22;
      trail.position.set(Math.cos(lag) * u.r, Math.sin(lag) * u.r, 0);
    });
    const amp = .07 + voice.level * .3;
    updateVoiceRing(vr1, t, 0, amp);
    updateVoiceRing(vr2, t * 1.3, 2, amp * .8);
    vr1.scale.setScalar(1 + voice.level * .05);

    // background drift — reacts to scroll and pointer
    points.rotation.y = t * .012 + mx * .04 + scrollY * .00008;
    points.rotation.x = my * .03;
    points.position.y = scrollY * .0007;

    camera.position.x = lerp(camera.position.x, mx * .35, k * .5);
    camera.position.y = lerp(camera.position.y, -my * .25, k * .5);
    camera.lookAt(0, 0, 0);

    updateWave(dt || .016);
    renderer.render(scene, camera);
  });
}
