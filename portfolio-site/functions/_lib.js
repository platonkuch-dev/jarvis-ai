// Shared helpers for the Pages Functions.

export const json = (data, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' } });

const BOT_UA = /bot|crawl|spider|slurp|preview|facebookexternalhit|headless|lighthouse|monitor|curl|wget|python-requests/i;
export const isBot = req => BOT_UA.test(req.headers.get('user-agent') || '');

export const country = req => (req.cf && req.cf.country) || req.headers.get('cf-ipcountry') || null;

export const device = req => /mobi|android|iphone|ipad/i.test(req.headers.get('user-agent') || '') ? 'mobile' : 'desktop';

// The IP is never stored: SHA-256(ip + day + secret salt) lets a visitor be counted once a day without being tracked.
export async function ipHash(req, env) {
  const ip = req.headers.get('cf-connecting-ip') || '';
  const day = new Date().toISOString().slice(0, 10);
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(`${ip}|${day}|${env.IP_SALT || 'jarvis'}`));
  return [...new Uint8Array(buf)].slice(0, 12).map(b => b.toString(16).padStart(2, '0')).join('');
}

const KNOWN = [['tiktok', /tiktok/], ['reddit', /reddit/], ['fiverr', /fiverr/], ['github', /github/], ['youtube', /youtu/],
  ['google', /google\./], ['telegram', /t\.me|telegram/], ['instagram', /instagram/], ['x', /(^|\.)x\.com$|twitter|^t\.co$/]];

// utm_source wins, then the referrer host mapped to a short name.
export function sourceOf(utm, referrer, selfHost) {
  if (utm) return String(utm).toLowerCase().slice(0, 40);
  if (!referrer) return 'direct';
  let host;
  try { host = new URL(referrer).hostname.replace(/^www\./, ''); } catch { return 'direct'; }
  if (selfHost && host === selfHost.replace(/^www\./, '')) return 'internal';
  for (const [name, re] of KNOWN) if (re.test(host)) return name;
  return host.slice(0, 60);
}

export const clip = (v, n) => (v == null ? null : String(v).trim().slice(0, n)) || null;

// Admin endpoints: "Authorization: Bearer <ADMIN_KEY>", compared in constant time.
export function isAdmin(req, env) {
  const got = (req.headers.get('authorization') || '').replace(/^Bearer\s+/i, '');
  const want = env.ADMIN_KEY || '';
  if (!want || got.length !== want.length) return false;
  let diff = 0;
  for (let i = 0; i < want.length; i++) diff |= got.charCodeAt(i) ^ want.charCodeAt(i);
  return diff === 0;
}
