// POST /api/hit: one page view, sent by navigator.sendBeacon from app.js.
import { isBot, country, device, ipHash, sourceOf, clip } from '../_lib.js';

export async function onRequestPost({ request, env }) {
  if (isBot(request)) return new Response(null, { status: 204 });
  let b = {};
  try { b = JSON.parse((await request.text()) || '{}'); } catch { /* keep empty */ }
  const self = new URL(request.url).hostname;
  await env.DB.prepare(
    'INSERT INTO page_views (path, source, referrer, lang, country, device, ip_hash) VALUES (?,?,?,?,?,?,?)')
    .bind(clip(b.path, 200) || '/', sourceOf(b.utm, b.ref, self), clip(b.ref, 300), clip(b.lang, 5),
      country(request), device(request), await ipHash(request, env)).run();
  return new Response(null, { status: 204 });
}
