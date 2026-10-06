-- Заявки клиентов с формы на сайте
CREATE TABLE IF NOT EXISTS leads (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT    NOT NULL DEFAULT (datetime('now')),
  name       TEXT    NOT NULL,
  contact    TEXT    NOT NULL,          -- email / Telegram / WhatsApp
  message    TEXT    NOT NULL,
  budget     TEXT,
  lang       TEXT,
  source     TEXT,                      -- откуда пришёл посетитель (tiktok, reddit, fiverr…)
  country    TEXT,
  ip_hash    TEXT,                      -- хэш IP с суточной солью, сам IP не хранится
  status     TEXT    NOT NULL DEFAULT 'new'   -- new / replied / won / lost / spam
);
CREATE INDEX IF NOT EXISTS leads_created ON leads(created_at);
CREATE INDEX IF NOT EXISTS leads_ip ON leads(ip_hash, created_at);

-- Скачивания установщика через /dl/<file>
CREATE TABLE IF NOT EXISTS downloads (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT    NOT NULL DEFAULT (datetime('now')),
  file       TEXT    NOT NULL,
  source     TEXT,
  country    TEXT,
  ip_hash    TEXT
);
CREATE INDEX IF NOT EXISTS downloads_created ON downloads(created_at);

-- Просмотры страниц (без cookies)
CREATE TABLE IF NOT EXISTS page_views (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT    NOT NULL DEFAULT (datetime('now')),
  path       TEXT    NOT NULL,
  source     TEXT,
  referrer   TEXT,
  lang       TEXT,
  country    TEXT,
  device     TEXT,                      -- mobile / desktop
  ip_hash    TEXT
);
CREATE INDEX IF NOT EXISTS views_created ON page_views(created_at);
