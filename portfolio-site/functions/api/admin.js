// /api/admin: data for admin.html. Needs "Authorization: Bearer <ADMIN_KEY>".
//   GET                       → totals, last 30 days by day, top sources/countries, latest leads
//   POST {id, status}         → change a lead's status
import { json, isAdmin } from '../_lib.js';

const STATUSES = ['new', 'replied', 'won', 'lost', 'spam'];

export async function onRequest({ request, env }) {
  if (!isAdmin(request, env)) return json({ ok: false, error: 'unauthorized' }, 401);

  if (request.method === 'POST') {
    const b = await request.json().catch(() => ({}));
    if (!Number.isInteger(b.id) || !STATUSES.includes(b.status)) return json({ ok: false, error: 'bad_request' }, 400);
    await env.DB.prepare('UPDATE leads SET status = ? WHERE id = ?').bind(b.status, b.id).run();
    return json({ ok: true });
  }
  if (request.method !== 'GET') return json({ ok: false }, 405);

  const days = "created_at > datetime('now', '-30 days')";
  const q = sql => env.DB.prepare(sql);
  const [totals, views, dls, leadsByDay, sources, countries, dlSources, leads] = await env.DB.batch([
    q(`SELECT
         (SELECT COUNT(*) FROM page_views) AS views,
         (SELECT COUNT(DISTINCT ip_hash || substr(created_at,1,10)) FROM page_views) AS visitors,
         (SELECT COUNT(*) FROM downloads) AS downloads,
         (SELECT COUNT(*) FROM leads WHERE status != 'spam') AS leads,
         (SELECT COUNT(*) FROM leads WHERE status = 'new') AS new_leads`),
    q(`SELECT substr(created_at,1,10) AS day, COUNT(*) AS n, COUNT(DISTINCT ip_hash) AS u FROM page_views WHERE ${days} GROUP BY day ORDER BY day`),
    q(`SELECT substr(created_at,1,10) AS day, COUNT(*) AS n FROM downloads WHERE ${days} GROUP BY day ORDER BY day`),
    q(`SELECT substr(created_at,1,10) AS day, COUNT(*) AS n FROM leads WHERE ${days} AND status != 'spam' GROUP BY day ORDER BY day`),
    q(`SELECT source, COUNT(*) AS n FROM page_views WHERE ${days} AND source != 'internal' GROUP BY source ORDER BY n DESC LIMIT 12`),
    q(`SELECT country, COUNT(*) AS n FROM page_views WHERE ${days} GROUP BY country ORDER BY n DESC LIMIT 12`),
    q(`SELECT source, COUNT(*) AS n FROM downloads WHERE ${days} GROUP BY source ORDER BY n DESC LIMIT 12`),
    q(`SELECT id, created_at, name, contact, message, budget, lang, source, country, status FROM leads ORDER BY id DESC LIMIT 200`),
  ]);
  return json({
    ok: true, totals: totals.results[0],
    views: views.results, downloads: dls.results, leadsByDay: leadsByDay.results,
    sources: sources.results, countries: countries.results, dlSources: dlSources.results, leads: leads.results,
  });
}
