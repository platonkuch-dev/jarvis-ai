// GET /dl/<file>: counts the download, then redirects to the installer from the latest GitHub release.
import { isBot, country, ipHash, sourceOf } from '../_lib.js';

const FILES = {
  jarvis: { repo: 'platonkuch-dev/jarvis-ai', asset: /^JarvisAI-Setup.*\.exe$/i },
};

// The installer name carries the version (JarvisAI-Setup-1.3.14.exe), so look it up and cache the answer for 10 minutes.
async function latestAsset({ repo, asset }, waitUntil) {
  const api = `https://api.github.com/repos/${repo}/releases/latest`;
  const cache = caches.default, key = new Request(api);
  let res = await cache.match(key);
  if (!res) {
    res = await fetch(api, { headers: { 'user-agent': 'jarvis-ai-site', accept: 'application/vnd.github+json' } });
    if (!res.ok) return null;
    res = new Response(res.body, res);
    res.headers.set('cache-control', 'public, max-age=600');
    waitUntil(cache.put(key, res.clone()));
  }
  const rel = await res.json();
  const a = (rel.assets || []).find(x => asset.test(x.name));
  return a ? a.browser_download_url : rel.html_url;
}

export async function onRequestGet({ request, env, params, waitUntil }) {
  const spec = FILES[params.file];
  if (!spec) return new Response('Not found', { status: 404 });
  const target = (await latestAsset(spec, waitUntil)) || `https://github.com/${spec.repo}/releases/latest`;
  if (!isBot(request)) {
    const url = new URL(request.url);
    const src = sourceOf(url.searchParams.get('utm_source'), request.headers.get('referer'), url.hostname);
    waitUntil(ipHash(request, env).then(h =>
      env.DB.prepare('INSERT INTO downloads (file, source, country, ip_hash) VALUES (?,?,?,?)')
        .bind(params.file, src, country(request), h).run()));
  }
  return Response.redirect(target, 302);
}
