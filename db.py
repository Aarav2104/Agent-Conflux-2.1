import os, sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
_p = os.getenv("DATABASE_URL", "conflux.db")
DB = _p if os.path.isabs(_p) else os.path.join(BASE, _p)  # always next to the code, wherever uvicorn is started
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE, pw_hash TEXT);
CREATE TABLE IF NOT EXISTS chats(id INTEGER PRIMARY KEY, owner TEXT, scope TEXT, title TEXT, created REAL DEFAULT (strftime('%s','now')));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, chat_id INTEGER, role TEXT, text TEXT, source TEXT, created REAL DEFAULT (strftime('%s','now')));
CREATE TABLE IF NOT EXISTS contacts(id INTEGER PRIMARY KEY, owner TEXT, name TEXT, email TEXT, phone TEXT);
CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY, owner TEXT, kind TEXT, to_label TEXT, to_addr TEXT, subject TEXT, body TEXT, status TEXT, result TEXT, created REAL, expires REAL);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, owner TEXT, kind TEXT, status TEXT, to_label TEXT, created REAL DEFAULT (strftime('%s','now')));
CREATE TABLE IF NOT EXISTS docs(id INTEGER PRIMARY KEY, owner TEXT, scope TEXT, filename TEXT, created REAL DEFAULT (strftime('%s','now')));
"""

def init():
    with sqlite3.connect(DB) as c:
        c.executescript(SCHEMA)
        try: c.execute("ALTER TABLE messages ADD COLUMN action_id TEXT")
        except sqlite3.OperationalError: pass

def q(sql, args=()):
    with sqlite3.connect(DB) as c:
        c.row_factory = sqlite3.Row
        cur = c.execute(sql, args)
        rows = [dict(r) for r in cur.fetchall()]
        return rows, cur.lastrowid

def ex(sql, args=()):
    """Run a write and return the number of rows changed (used for atomic status changes)."""
    with sqlite3.connect(DB) as c:
        return c.execute(sql, args).rowcount
