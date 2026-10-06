// POST /api/lead: saves an order request from the contact form and pings you in Telegram.
import { json, country, ipHash, clip } from '../_lib.js';

// The GitHub Pages mirror (for networks that block *.pages.dev) posts here cross-origin.
const MIRRORS = ['https://platonkuch-dev.github.io'];

export async function onRequestPost(ctx) {
  const res = await handle(ctx);
  const origin = ctx.request.headers.get('origin');
  if (MIRRORS.includes(origin)) {
    res.headers.set('access-control-allow-origin', origin);
    res.headers.set('vary', 'Origin');
  }
  return res;
}

async function handle({ request, env, waitUntil }) {
  let b;
  try { b = await request.json(); } catch { return json({ ok: false, error: 'bad_json' }, 400); }

  if (b.website) return json({ ok: true });                 // honeypot field filled → bot, pretend success
  const name = clip(b.name, 80), contact = clip(b.contact, 120), message = clip(b.message, 3000);
  if (!name || !contact || !message || message.length < 10) return json({ ok: false, error: 'missing_fields' }, 400);

  const hash = await ipHash(request, env);
  const recent = await env.DB.prepare(
    "SELECT COUNT(*) AS n FROM leads WHERE ip_hash = ? AND created_at > datetime('now', '-1 hour')").bind(hash).first();
  if (recent.n >= 5) return json({ ok: false, error: 'rate_limited' }, 429);

  const row = { name, contact, message, budget: clip(b.budget, 40), lang: clip(b.lang, 5), source: clip(b.source, 60), country: country(request) };
  const res = await env.DB.prepare(
    'INSERT INTO leads (name, contact, message, budget, lang, source, country, ip_hash) VALUES (?,?,?,?,?,?,?,?)')
    .bind(row.name, row.contact, row.message, row.budget, row.lang, row.source, row.country, hash).run();
  const id = res.meta.last_row_id;

  if (env.TELEGRAM_BOT_TOKEN && env.TELEGRAM_CHAT_ID) {
    const text = `🆕 Заявка #${id} с сайта\n👤 ${row.name}\n📬 ${row.contact}\n💰 ${row.budget || '—'} · 🌍 ${row.country || '?'} · из ${row.source || 'direct'}\n\n${row.message}`;
    // the lead is already saved, so a failed ping is only logged
    waitUntil(fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ chat_id: env.TELEGRAM_CHAT_ID, text: text.slice(0, 4000), disable_web_page_preview: true }),
    }).catch(e => console.log('telegram ping failed', e)));
  }
  return json({ ok: true, id });
}
