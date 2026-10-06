// GET /dl/<file>: counts the download, then redirects to the installer from the latest GitHub release.
import { isBot, country, ipHash, sourceOf, publicHost } from '../_lib.js';

const FILES = {
  jarvis: { repo: 'platonkuch-dev/jarvis-ai', asset: tag => `JarvisAI-Setup-${tag.replace(/^v/, '')}.exe` },
};

// The installer name carries the version (JarvisAI-Setup-1.3.15.exe). github.com/<repo>/releases/latest redirects to
// the newest tag; unlike api.github.com it isn't rate-limited per shared Cloudflare egress IP. Cached for 10 minutes.
async function latestAsset({ repo, asset }, waitUntil) {
  const latest = `https://github.com/${repo}/releases/latest`;
  const cache = caches.default, key = new Request(latest + '#tag');
  const hit = await cache.match(key);
  let tag = hit && (await hit.text());
  if (!tag) {
    const res = await fetch(latest, { redirect: 'manual', headers: { 'user-agent': 'jarvis-ai-site' } });
    const m = (res.headers.get('location') || '').match(/\/releases\/tag\/([^/?#]+)/);
    if (!m) return null;
    tag = decodeURIComponent(m[1]);
    waitUntil(cache.put(key, new Response(tag, { headers: { 'cache-control': 'public, max-age=600' } })));
  }
  return `https://github.com/${repo}/releases/download/${tag}/${asset(tag)}`;
}

export async function onRequestGet({ request, env, params, waitUntil }) {
  const spec = FILES[params.file];
  if (!spec) return new Response('Not found', { status: 404 });
  const target = (await latestAsset(spec, waitUntil)) || `https://github.com/${spec.repo}/releases/latest`;
  if (!isBot(request)) {
    const url = new URL(request.url);
    const src = sourceOf(url.searchParams.get('utm_source'), request.headers.get('referer'), publicHost(request, env));
    waitUntil(ipHash(request, env).then(h =>
      env.DB.prepare('INSERT INTO downloads (file, source, country, ip_hash) VALUES (?,?,?,?)')
        .bind(params.file, src, country(request, env), h).run()));
  }
  return Response.redirect(target, 302);
}
