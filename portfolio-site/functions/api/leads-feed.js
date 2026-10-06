// GET /api/leads-feed?after=<id>: leads newer than <id>, oldest first. Polled by Jarvis on the owner's PC
// (livekit_agent/site_leads.py), which forwards them to Telegram from Jarvis's own account.
// Needs "Authorization: Bearer <NOTIFY_KEY>" — a read-only key separate from ADMIN_KEY.
import { json } from '../_lib.js';

function authorized(req, env) {
  const got = (req.headers.get('authorization') || '').replace(/^Bearer\s+/i, '');
  const want = env.NOTIFY_KEY || '';
  if (!want || got.length !== want.length) return false;
  let diff = 0;
  for (let i = 0; i < want.length; i++) diff |= got.charCodeAt(i) ^ want.charCodeAt(i);
  return diff === 0;
}

export async function onRequestGet({ request, env }) {
  if (!authorized(request, env)) return json({ ok: false, error: 'unauthorized' }, 401);
  const after = Math.max(0, parseInt(new URL(request.url).searchParams.get('after') || '0', 10) || 0);
  const { results } = await env.DB.prepare(
    `SELECT id, created_at, name, contact, message, budget, lang, source, country
       FROM leads WHERE id > ? AND status != 'spam' ORDER BY id LIMIT 50`).bind(after).all();
  const last = await env.DB.prepare('SELECT COALESCE(MAX(id), 0) AS id FROM leads').first();
  return json({ ok: true, leads: results, last_id: last.id });
}
