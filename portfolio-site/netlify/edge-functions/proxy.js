// Netlify front for the site: /api/* and /dl/* are forwarded to the backend on Cloudflare Pages, so the form,
// stats and download counter work for visitors whose network blocks *.pages.dev (Netlify talks to it server-side).
// PROXY_KEY (Netlify env var = Cloudflare secret) lets the backend trust the visitor IP and country sent here.
const BACKEND = 'https://jarvis-ai-site-98w.pages.dev';

export default async (request, context) => {
  const url = new URL(request.url);
  const headers = new Headers(request.headers);
  headers.delete('host');
  headers.set('x-proxy-key', Netlify.env.get('PROXY_KEY') || '');
  headers.set('x-forwarded-for', context.ip || '');
  headers.set('x-forwarded-host', url.hostname);
  const cc = context.geo && context.geo.country && context.geo.country.code;
  if (cc) headers.set('x-visitor-country', cc);

  const hasBody = !['GET', 'HEAD'].includes(request.method);
  const res = await fetch(BACKEND + url.pathname + url.search, {
    method: request.method,
    headers,
    body: hasBody ? await request.arrayBuffer() : undefined,
    redirect: 'manual',              // /dl/* answers with a redirect to GitHub; pass it on to the browser
  });

  // The backend falls back to the releases page when GitHub doesn't answer its egress IP; resolve the file here instead.
  const loc = res.headers.get('location') || '';
  const page = loc.match(/^https:\/\/github\.com\/([^/]+\/[^/]+)\/releases\/latest\/?$/);
  if (res.status === 302 && page && url.pathname === '/dl/jarvis') {
    const gh = await fetch(loc, { redirect: 'manual' }).catch(() => null);
    const tag = gh && (gh.headers.get('location') || '').match(/\/releases\/tag\/([^/?#]+)/);
    if (tag) {
      const t = decodeURIComponent(tag[1]);
      return Response.redirect(`https://github.com/${page[1]}/releases/download/${t}/JarvisAI-Setup-${t.replace(/^v/, '')}.exe`, 302);
    }
  }
  return new Response(res.body, { status: res.status, headers: res.headers });
};
