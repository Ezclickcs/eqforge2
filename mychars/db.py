"""SQLite layer for My Characters. Stdlib only.

The DB file lives next to serve.py (eqforge2/mychars.db) so backups are one file.
Every public function takes an open connection; api.py opens one per request
(cheap for a single-user local tool, and thread-safe by construction).
"""
import json
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(HERE, "schema.sql")
SEED_PATH = os.path.join(HERE, "seed.json")
DEFAULT_DB_PATH = os.environ.get(
    "EQFORGE_MYCHARS_DB", os.path.join(os.path.dirname(HERE), "mychars.db"))


def connect(path=None):
    conn = sqlite3.connect(path or DEFAULT_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init(conn)
    return conn


def init(conn):
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        conn.executescript(f.read())
    # migrations for DBs created before a column existed (ALTER is a no-op error if present)
    for ddl in ("ALTER TABLE raid_lockouts ADD COLUMN expires_at INTEGER",
                "ALTER TABLE raid_lockouts ADD COLUMN imported_at INTEGER",
                "ALTER TABLE accounts ADD COLUMN nickname TEXT DEFAULT ''",
                "ALTER TABLE accounts ADD COLUMN membership TEXT DEFAULT ''",
                "ALTER TABLE accounts ADD COLUMN sub_expires INTEGER",
                "ALTER TABLE accounts ADD COLUMN sub_expires_max INTEGER",
                "ALTER TABLE composition_slots ADD COLUMN gear_set_id INTEGER",
                # Two dead ends from 2026-08-09, both dropped the same day: comp_id
                # (one comp owns each set) and tag/gear_tag (hand-typed gear families).
                # Both tried to answer "which sets belong to this comp" with a field;
                # composition_slots.gear_set_id already answers it, and a set is shared
                # freely — Rogue Main goes on Gavriel in one comp and Zyrak in another.
                "ALTER TABLE gear_sets DROP COLUMN comp_id",
                "ALTER TABLE gear_sets DROP COLUMN tag",
                "ALTER TABLE compositions DROP COLUMN gear_tag"):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass
    conn.commit()


def is_empty(conn):
    n = conn.execute("SELECT (SELECT COUNT(*) FROM accounts) + (SELECT COUNT(*) FROM characters)").fetchone()[0]
    return n == 0


def rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def seed(conn, path=None, force=False):
    """Load seed.json (sample roster). No-op unless DB is empty or force=True."""
    if not force and not is_empty(conn):
        return False
    with open(path or SEED_PATH, encoding="utf-8") as f:
        data = json.load(f)

    acct_ids = {}
    for a in data.get("accounts", []):
        cur = conn.execute(
            "INSERT OR IGNORE INTO accounts(alias, account_number, status, autologin_group,"
            " launch_order, perks, notes) VALUES (?,?,?,?,?,?,?)",
            (a["alias"], a.get("account_number", ""), a.get("status", "active"),
             a.get("autologin_group", ""), a.get("launch_order", 0),
             json.dumps(a.get("perks", [])), a.get("notes", "")))
        acct_ids[a["alias"]] = cur.lastrowid or conn.execute(
            "SELECT id FROM accounts WHERE alias=?", (a["alias"],)).fetchone()[0]

    char_ids = {}
    for c in data.get("characters", []):
        cur = conn.execute(
            "INSERT OR IGNORE INTO characters(name, server, account_id, class_name, level, race,"
            " main_role, status, group_tags, gear_tier, epic_status, portbot_whitelist,"
            " automation_status, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (c["name"], c.get("server", "Frostreaver"),
             acct_ids.get(c.get("account")), c.get("class_name", ""), c.get("level"),
             c.get("race", ""), c.get("main_role", ""), c.get("status", "active"),
             c.get("group_tags", ""), c.get("gear_tier", ""), c.get("epic_status", ""),
             1 if c.get("portbot_whitelist") else 0,
             c.get("automation_status", "untested"), c.get("notes", "")))
        char_ids[c["name"]] = cur.lastrowid or conn.execute(
            "SELECT id FROM characters WHERE server=? AND name=? COLLATE NOCASE",
            (c.get("server", "Frostreaver"), c["name"])).fetchone()[0]

    for u in data.get("unlocks", []):
        cid = char_ids.get(u["character"])
        if not cid:
            continue
        conn.execute(
            "INSERT INTO unlocks(character_id, name, expansion, category, scope, priority,"
            " status, verified_date, notes) VALUES (?,?,?,?,?,?,?,?,?)",
            (cid, u["name"], u.get("expansion", ""), u.get("category", "key"),
             u.get("scope", "character"), u.get("priority", "normal"),
             u.get("status", "Unknown"), u.get("verified_date", ""), u.get("notes", "")))

    raid_ids = {}
    for r in data.get("raids", []):
        cur = conn.execute("INSERT OR IGNORE INTO raids(name) VALUES (?)", (r,))
        raid_ids[r] = cur.lastrowid or conn.execute(
            "SELECT id FROM raids WHERE name=?", (r,)).fetchone()[0]

    for lk in data.get("lockouts", []):
        cid, rid = char_ids.get(lk["character"]), raid_ids.get(lk["raid"])
        if cid and rid:
            conn.execute(
                "INSERT OR IGNORE INTO raid_lockouts(character_id, raid_id, notes) VALUES (?,?,?)",
                (cid, rid, lk.get("notes", "")))

    for comp in data.get("compositions", []):
        cur = conn.execute(
            "INSERT OR IGNORE INTO compositions(name, notes, required_unlocks) VALUES (?,?,?)",
            (comp["name"], comp.get("notes", ""), comp.get("required_unlocks", "")))
        comp_id = cur.lastrowid or conn.execute(
            "SELECT id FROM compositions WHERE name=?", (comp["name"],)).fetchone()[0]
        for i, name in enumerate((comp.get("slots") or [])[:6]):
            conn.execute(
                "INSERT OR REPLACE INTO composition_slots(composition_id, slot_index, character_id)"
                " VALUES (?,?,?)", (comp_id, i, char_ids.get(name)))

    conn.commit()
    return True
