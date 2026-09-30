"""
Digital Ghost's storage layer: a small SQLite database (under the same
%APPDATA%\\Jarvis directory core/path_utils.py already uses for memory/
settings) holding two tables:

  ghost_state   Current known items per category -- one row per
                (category, item_key), updated in place. This is the
                "baseline": what JARVIS currently believes exists.

  ghost_events  Append-only change log: every time a scan finds an item
                that's new, gone, or changed since ghost_state's last
                value, one row is appended here. This is what "what
                changed in the last N hours" (Time Machine) queries
                against -- it never needs to diff two full snapshots
                against each other, just read events in a time range.

Deliberately NOT storing a full copy of every category on every scan
(that's the naive way to build "snapshots" and multiplies storage by the
scan count for no benefit -- 99% of items are identical between two scans
five minutes apart). ghost_state is always "the latest state"; history
lives entirely in ghost_events.

First scan per category is a silent baseline seed, not a flood of "new"
events -- see _seed_needed()/record_snapshot()'s docstring.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time

from core.path_utils import get_user_data_dir

DB_PATH = get_user_data_dir() / "ghost.sqlite3"

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _init_schema(_conn)
    return _conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ghost_state (
            category TEXT NOT NULL,
            item_key TEXT NOT NULL,
            value TEXT NOT NULL,
            first_seen REAL NOT NULL,
            last_seen REAL NOT NULL,
            PRIMARY KEY (category, item_key)
        );
        CREATE TABLE IF NOT EXISTS ghost_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            category TEXT NOT NULL,
            change TEXT NOT NULL,
            item_key TEXT NOT NULL,
            detail TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_events_ts ON ghost_events(ts);
        CREATE INDEX IF NOT EXISTS idx_events_cat_ts ON ghost_events(category, ts);
        CREATE TABLE IF NOT EXISTS ghost_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    conn.commit()


def get_meta(key: str, default: str | None = None) -> str | None:
    conn = _get_conn()
    with _lock:
        row = conn.execute("SELECT value FROM ghost_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(key: str, value: str) -> None:
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO ghost_meta(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()


def record_snapshot(category: str, current: dict[str, dict]) -> dict[str, int]:
    """Diffs `current` (item_key -> value dict) against ghost_state for
    this category, updates ghost_state to match, and appends ghost_events
    rows for anything new/removed/changed. Returns a {"new": n, "removed":
    n, "changed": n} count for this one scan.

    First call ever for a category (no existing ghost_state rows) seeds
    the baseline silently -- every current item is inserted with no
    events generated, since "JARVIS just started watching" is not the
    same thing as "everything on this PC just appeared". Tracked via a
    ghost_meta flag rather than "ghost_state was empty", so a category
    that's legitimately empty (e.g. no scheduled tasks matched) doesn't
    get re-seeded (and silently miss real changes) on every scan.
    """
    conn = _get_conn()
    now = time.time()
    baseline_key = f"baseline_seeded:{category}"
    is_first_scan = get_meta(baseline_key) is None

    with _lock:
        existing = {
            row[0]: (row[1], row[2], row[3])
            for row in conn.execute(
                "SELECT item_key, value, first_seen, last_seen FROM ghost_state WHERE category=?",
                (category,),
            )
        }
        counts = {"new": 0, "removed": 0, "changed": 0}
        events: list[tuple] = []

        for key, value in current.items():
            value_json = json.dumps(value, sort_keys=True, ensure_ascii=False)
            if key not in existing:
                conn.execute(
                    "INSERT INTO ghost_state(category, item_key, value, first_seen, last_seen) "
                    "VALUES(?,?,?,?,?)",
                    (category, key, value_json, now, now),
                )
                if not is_first_scan:
                    counts["new"] += 1
                    events.append((now, category, "new", key, value_json))
            else:
                old_value_json, first_seen, _ = existing[key]
                if old_value_json != value_json:
                    conn.execute(
                        "UPDATE ghost_state SET value=?, last_seen=? WHERE category=? AND item_key=?",
                        (value_json, now, category, key),
                    )
                    if not is_first_scan:
                        counts["changed"] += 1
                        detail = json.dumps({"old": json.loads(old_value_json), "new": value},
                                             sort_keys=True, ensure_ascii=False)
                        events.append((now, category, "changed", key, detail))
                else:
                    conn.execute(
                        "UPDATE ghost_state SET last_seen=? WHERE category=? AND item_key=?",
                        (now, category, key),
                    )

        gone = set(existing) - set(current)
        for key in gone:
            old_value_json = existing[key][0]
            conn.execute("DELETE FROM ghost_state WHERE category=? AND item_key=?", (category, key))
            if not is_first_scan:
                counts["removed"] += 1
                events.append((now, category, "removed", key, old_value_json))

        if events:
            conn.executemany(
                "INSERT INTO ghost_events(ts, category, change, item_key, detail) VALUES(?,?,?,?,?)",
                events,
            )
        conn.commit()

    if is_first_scan:
        set_meta(baseline_key, str(now))

    return counts


def record_events_raw(category: str, events: list[dict]) -> int:
    """For windows_events(): each entry is already a discrete new
    occurrence (a log entry doesn't have a 'previous value' to diff
    against), so these go straight into ghost_events with change='new'."""
    if not events:
        return 0
    conn = _get_conn()
    rows = [
        (e["ts"], category, "new", f"{e['log']}:{e['id']}:{e['ts']}",
         json.dumps({"log": e["log"], "id": e["id"], "message": e["message"]}, ensure_ascii=False))
        for e in events
    ]
    with _lock:
        conn.executemany(
            "INSERT INTO ghost_events(ts, category, change, item_key, detail) VALUES(?,?,?,?,?)",
            rows,
        )
        conn.commit()
    return len(rows)


def query_delta(hours: float) -> dict:
    """Everything ghost_events recorded in the last `hours`, grouped by
    category+change with counts, plus the single earliest event overall
    (labelled the first *change*, not a scored "anomaly" -- that judgement
    is Investigation AI / Hunter's job, not this query's)."""
    conn = _get_conn()
    since = time.time() - hours * 3600.0

    with _lock:
        rows = conn.execute(
            "SELECT category, change, COUNT(*) FROM ghost_events WHERE ts >= ? "
            "GROUP BY category, change",
            (since,),
        ).fetchall()
        first = conn.execute(
            "SELECT ts, category, change, item_key FROM ghost_events WHERE ts >= ? "
            "ORDER BY ts ASC LIMIT 1",
            (since,),
        ).fetchone()
        total = conn.execute(
            "SELECT COUNT(*) FROM ghost_events WHERE ts >= ?", (since,)
        ).fetchone()[0]

    by_category: dict[str, dict[str, int]] = {}
    for category, change, count in rows:
        by_category.setdefault(category, {})[change] = count

    return {
        "since_hours": hours,
        "total_events": total,
        "by_category": by_category,
        "first_event": (
            {"ts": first[0], "category": first[1], "change": first[2], "item_key": first[3]}
            if first else None
        ),
    }


def status() -> dict:
    conn = _get_conn()
    with _lock:
        per_category = conn.execute(
            "SELECT category, COUNT(*) FROM ghost_state GROUP BY category"
        ).fetchall()
        last_event = conn.execute("SELECT MAX(ts) FROM ghost_events").fetchone()[0]
        total_events = conn.execute("SELECT COUNT(*) FROM ghost_events").fetchone()[0]
    return {
        "categories": {c: n for c, n in per_category},
        "last_event_ts": last_event,
        "total_events": total_events,
    }


def prune_old_events(max_age_days: float = 14.0) -> int:
    """Keeps ghost_events from growing forever. ghost_state (the current
    baseline) is never pruned -- only the historical change log is."""
    conn = _get_conn()
    cutoff = time.time() - max_age_days * 86400.0
    with _lock:
        cur = conn.execute("DELETE FROM ghost_events WHERE ts < ?", (cutoff,))
        conn.commit()
        return cur.rowcount
