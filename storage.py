import sqlite3
import time

DB_PATH = "bot.db"

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER,
        slug TEXT,
        team TEXT,
        side TEXT,
        entry_price REAL,
        score TEXT,
        period TEXT,
        reason TEXT
    )""")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS results (
        slug TEXT PRIMARY KEY,
        winner TEXT,
        finished_ts INTEGER
    )""")
    return conn

def log_alert(slug, team, side, entry_price, score, period, reason):
    conn = db()
    conn.execute(
        "INSERT INTO alerts (ts, slug, team, side, entry_price, score, period, reason) VALUES (?,?,?,?,?,?,?,?)",
        (int(time.time()), slug, team, side, float(entry_price), score or "", period or "", reason or "")
    )
    conn.commit()
    conn.close()

def set_result(slug, winner):
    conn = db()
    conn.execute(
        "INSERT OR REPLACE INTO results (slug, winner, finished_ts) VALUES (?,?,?)",
        (slug, winner, int(time.time()))
    )
    conn.commit()
    conn.close()

def get_alerts_for_slug(slug):
    conn = db()
    cur = conn.execute(
        "SELECT ts, team, side, entry_price, score, period, reason FROM alerts WHERE slug=? ORDER BY ts ASC",
        (slug,)
    )
    rows = cur.fetchall()
    conn.close()
    return rows
