"""DZ/expedition lockout import: parse mychars_lockouts.txt and apply to the grid.

The file is written per-toon by extras/mychars_lockouts.lua (pipe-delimited:
name|server|expedition|event|expires_epoch). Raid rows are auto-created as
"Expedition" or "Expedition: Event"; characters match by (server, name)
case-insensitively. Rows for unknown characters are reported, never guessed.
"""
import os
import time


def raid_display_name(expedition, event):
    expedition = (expedition or "").strip()
    event = (event or "").strip()
    if event and event.lower() != expedition.lower():
        return "%s: %s" % (expedition, event)
    return expedition


def parse_file(path):
    """Returns {found, rows}. Rows: {name, server, expedition, event, expires}."""
    if not path or not os.path.isfile(path):
        return {"found": False, "rows": [], "path": path or ""}
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.lower().startswith("name|"):
                continue
            parts = line.split("|")
            if len(parts) < 5:
                continue
            name, server, expedition, event, expires = parts[:5]
            if not name or not expedition:
                continue
            try:
                expires = int(expires)
            except ValueError:
                continue
            rows.append({"name": name.strip(), "server": server.strip(),
                         "expedition": expedition.strip(), "event": event.strip(),
                         "expires": expires})
    return {"found": True, "rows": rows, "path": path}


def apply(conn, rows, now=None):
    """Upsert lockouts. Returns {applied, skipped_expired, raids_created, unmatched}."""
    now = int(now if now is not None else time.time())
    chars = {(r["server"].lower(), r["name"].lower()): r["id"]
             for r in conn.execute("SELECT id, name, server FROM characters")}
    raids = {r["name"].lower(): r["id"] for r in conn.execute("SELECT id, name FROM raids")}
    applied, skipped, created, unmatched = 0, 0, [], set()

    for row in rows:
        if row["expires"] <= now:
            skipped += 1
            continue
        cid = chars.get((row["server"].lower(), row["name"].lower()))
        if cid is None:
            unmatched.add("%s (%s)" % (row["name"], row["server"]))
            continue
        rname = raid_display_name(row["expedition"], row["event"])
        rid = raids.get(rname.lower())
        if rid is None:
            rid = conn.execute("INSERT INTO raids(name) VALUES (?)", (rname,)).lastrowid
            raids[rname.lower()] = rid
            created.append(rname)
        conn.execute(
            "INSERT INTO raid_lockouts(character_id, raid_id, notes, expires_at, imported_at)"
            " VALUES (?,?,?,?,?) ON CONFLICT(character_id, raid_id)"
            " DO UPDATE SET expires_at=excluded.expires_at, imported_at=excluded.imported_at",
            (cid, rid, "from MQ", row["expires"], now))
        applied += 1

    conn.commit()
    return {"applied": applied, "skipped_expired": skipped,
            "raids_created": created, "unmatched": sorted(unmatched)}


def purge_expired(conn, now=None):
    """Drop lockouts whose expiry has passed (manual NULL-expiry rows are kept)."""
    now = int(now if now is not None else time.time())
    cur = conn.execute(
        "DELETE FROM raid_lockouts WHERE expires_at IS NOT NULL AND expires_at < ?", (now,))
    conn.commit()
    return cur.rowcount
