"""Read the MQ AutoLogin database (config/login.db) as a SANITIZED roster feed.

login.db is where modern MQ AutoLogin records every character it has seen at
character select: characters(character, server, account_id), personas(class,
level, last_seen), profiles/profile_groups (launch groups).

HARD SECURITY RULE: the roster feed (read_roster) NEVER selects from the
`accounts` table — accounts leave it only as opaque "MQAcct<n>" keys. The ONE
exception is read_account_names(), the explicit user-initiated "Use login
names" action: it selects id + account (username) ONLY. The password column is
never selected by anything in this codebase, ever. Opened read-only (URI
mode=ro) so a running MQ is never disturbed.

WRITING (push_profile_group, 2026-08-07): the ONE write path. It creates a
profile group + its profiles so a login set shows up in the AutoLogin sidebar
with right-click -> Launch All. Three rules, each paid for by a past bug:
  - It touches `profile_groups` and `profiles` ONLY. The accounts table (which
    holds passwords) is never read and never written.
  - It refuses while MacroQuest.exe is up. The loader runs every profile-group
    query through an in-memory cache (db::login::ListProfileGroups /
    CacheResults in the binary), so a write behind its back is invisible until
    it restarts, and an app that caches can also overwrite you (the rgmercs
    lesson: 4 of 14 externally-written settings silently reverted).
  - login.db is in WAL mode. Any backup copies .db + -wal + -shm TOGETHER, or
    it is a copy of a stale snapshot pretending to be current.
"""
import os
import shutil
import sqlite3
import subprocess
import time
import urllib.request

LOADER_EXE = "MacroQuest.exe"


def loader_running():
    """Is MQ's loader up? True/False, or None when we genuinely can't tell.

    None is NOT False — the caller must treat 'unknown' as 'do not write'.
    """
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq " + LOADER_EXE, "/NH"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return LOADER_EXE.lower() in (out.stdout or "").lower()


BACKUP_TAG = ".bak-eqforge-"
KEEP_BACKUPS = 3


def backup_login_db(db_path, keep=KEEP_BACKUPS):
    """Copy the db AND its WAL/SHM siblings. Copying the .db alone would capture
    a snapshot that is missing every write still sitting in the 4MB -wal.

    Only the newest `keep` sets are retained — the WAL makes each one multi-MB,
    and a folder of them is how you stop noticing which is which.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = "%s%s%s" % (db_path, BACKUP_TAG, stamp)
    shutil.copy2(db_path, dest)
    for suffix in ("-wal", "-shm"):
        if os.path.isfile(db_path + suffix):
            shutil.copy2(db_path + suffix, dest + suffix)

    folder = os.path.dirname(os.path.abspath(db_path))
    prefix = os.path.basename(db_path) + BACKUP_TAG
    # Group by stamp so a set's -wal/-shm are never orphaned from their .db
    stamps = sorted({f[len(prefix):len(prefix) + 15] for f in os.listdir(folder)
                     if f.startswith(prefix)}, reverse=True)
    for old in stamps[keep:]:
        for f in os.listdir(folder):
            if f.startswith(prefix + old):
                try:
                    os.unlink(os.path.join(folder, f))
                except OSError:
                    pass
    return dest


def read_profile_group(db_path, group_name):
    """[{name, server}] in launch order, or None if the group doesn't exist."""
    if not db_path or not os.path.isfile(db_path):
        return None
    uri = "file:%s?mode=ro" % urllib.request.pathname2url(os.path.abspath(db_path))
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        g = conn.execute("SELECT id FROM profile_groups WHERE name = ?",
                         (group_name.lower(),)).fetchone()
        if g is None:
            return None
        return [{"name": r["character"][:1].upper() + r["character"][1:],
                 "server": r["server"], "will_load": r["will_load"]}
                for r in conn.execute(
                    """SELECT c.character, c.server, p.will_load
                       FROM profiles p JOIN characters c ON c.id = p.character_id
                       WHERE p.group_id = ? ORDER BY p.sort_order""", (g["id"],))]
    finally:
        conn.close()


def push_profile_group(db_path, group_name, members, replace=False):
    """Write `members` (in launch order) into AutoLogin as a profile group.

    members: [{"name": "Trixster", "server": "Frostreaver"}] — resolved against
    characters AutoLogin has already seen at char select. One it has never seen
    can't be added (we'd have to invent an account_id, which means touching the
    credential table) — those come back in `missing` for the UI to report.

    Group names are stored lowercased: the loader looks them up with
    `WHERE name = LOWER(?)`, so a mixed-case row would be invisible to it.
    Returns (http_code, payload).
    """
    if not db_path or not os.path.isfile(db_path):
        return 404, {"ok": False, "error": "AutoLogin's login.db not found at " + (db_path or "?")}
    group_name = (group_name or "").strip().lower()
    if not group_name:
        return 400, {"ok": False, "error": "Group name is required."}
    if not members:
        return 400, {"ok": False, "error": "Login set is empty."}

    running = loader_running()
    if running is not False:
        return 409, {"ok": False, "loader": True, "error":
                     ("MacroQuest is running — close the MQ loader (tray icon → Exit) "
                      "before pushing. It caches AutoLogin's profile list in memory, so "
                      "a write now is invisible to it and can be overwritten."
                      if running else
                      "Couldn't tell whether MacroQuest is running, so nothing was "
                      "written. Close the MQ loader and try again.")}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        found, missing = [], []
        for m in members:
            row = conn.execute(
                "SELECT id, character FROM characters WHERE LOWER(character)=? AND LOWER(server)=?",
                ((m.get("name") or "").lower(), (m.get("server") or "").lower())).fetchone()
            if row is None:
                missing.append({"name": m.get("name"), "server": m.get("server")})
            else:
                found.append({"id": row["id"], "name": m.get("name")})
        if not found:
            return 400, {"ok": False, "missing": missing, "error":
                         "AutoLogin has never seen any of these characters at character "
                         "select, so it has no profile to point at. Log them in once."}

        existing = conn.execute("SELECT id FROM profile_groups WHERE name = ?",
                                (group_name,)).fetchone()
        if existing and not replace:
            # Read the current membership through the normal read path, but only
            # after this connection is out of the way.
            conn.close()
            conn = None
            return 409, {"ok": False, "exists": True, "group": group_name,
                         "current": read_profile_group(db_path, group_name),
                         "error": "Profile group '%s' already exists." % group_name}

        backup = backup_login_db(db_path)
        if existing:
            gid = existing["id"]
            # Replace the membership wholesale — this button means "the set is
            # THIS", not "add to whatever was there".
            conn.execute("DELETE FROM profiles WHERE group_id = ?", (gid,))
        else:
            cur = conn.execute("INSERT INTO profile_groups(name) VALUES (?)", (group_name,))
            gid = cur.lastrowid
        for i, f in enumerate(found, start=1):
            conn.execute(
                """INSERT INTO profiles(character_id, group_id, sort_order, will_load,
                                        hotkey, additional_eqgame_args, sounds)
                   VALUES (?,?,?,1,'','',1)""", (f["id"], gid, i))
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        return 500, {"ok": False, "error": "login.db write failed: %s" % e}
    finally:
        # Every path closes, including the early returns above — a leaked handle
        # on someone's live login.db is not a thing to ship.
        if conn is not None:
            conn.close()

    # Re-read from disk: a write that isn't verified is a claim, not a fact.
    wrote = read_profile_group(db_path, group_name) or []
    return 200, {"ok": True, "group": group_name, "backup": backup,
                 "added": [w["name"] for w in wrote], "missing": missing,
                 "db_path": db_path}

CLASS_CODES = {
    "WAR": "Warrior", "CLR": "Cleric", "PAL": "Paladin", "RNG": "Ranger",
    "SHD": "Shadow Knight", "DRU": "Druid", "MNK": "Monk", "BRD": "Bard",
    "ROG": "Rogue", "SHM": "Shaman", "NEC": "Necromancer", "WIZ": "Wizard",
    "MAG": "Magician", "ENC": "Enchanter", "BST": "Beastlord", "BER": "Berserker",
}


def _full_class(cls):
    if not cls:
        return ""
    return CLASS_CODES.get(cls.strip().upper(), cls.strip())


def _nice_server(s):
    s = (s or "").strip()
    return s[:1].upper() + s[1:] if s.islower() else s


def read_account_names(db_path):
    """User-initiated ONLY ("Use login names" button): {account_id: username}.

    Selects id + account — the password column is never part of any query.
    """
    if not db_path or not os.path.isfile(db_path):
        return {}
    uri = "file:%s?mode=ro" % urllib.request.pathname2url(os.path.abspath(db_path))
    conn = sqlite3.connect(uri, uri=True)
    try:
        return {r[0]: r[1] for r in conn.execute("SELECT id, account FROM accounts")}
    finally:
        conn.close()


def read_roster(db_path):
    """Return {found, rows, accounts} or {found: False}. Never credentials.

    rows:     [{name, server, account, class, level}]  (account = "MQAcct<n>")
    accounts: [{key, char_count, groups}]              (per discovered account_id)
    """
    if not db_path or not os.path.isfile(db_path):
        return {"found": False, "rows": [], "accounts": [], "db_path": db_path or ""}
    uri = "file:%s?mode=ro" % urllib.request.pathname2url(os.path.abspath(db_path))
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        chars = conn.execute("""
            SELECT c.id, c.character AS name, c.server, c.account_id,
                   p.class AS cls, p.level, p.last_seen
            FROM characters c
            LEFT JOIN personas p ON p.character_id = c.id
            WHERE c.visible = 1
            ORDER BY c.account_id, c.character, p.level DESC
        """).fetchall()
        groups = {}
        for r in conn.execute("""
            SELECT pr.character_id, g.name
            FROM profiles pr JOIN profile_groups g ON g.id = pr.group_id
        """):
            groups.setdefault(r["character_id"], []).append(r["name"])
    finally:
        conn.close()

    rows, seen = [], set()
    acct_info = {}
    for r in chars:
        key = (r["name"].lower(), (r["server"] or "").lower())
        if key in seen:                      # multiple personas -> keep highest level
            continue
        seen.add(key)
        acct_key = "MQAcct%d" % r["account_id"]
        rows.append({
            # login.db stores names lowercased; EQ names are always Capitalized
            "name": r["name"][:1].upper() + r["name"][1:], "server": _nice_server(r["server"]),
            "account": acct_key, "class": _full_class(r["cls"]),
            "level": r["level"] if r["level"] else "",
        })
        info = acct_info.setdefault(acct_key, {"key": acct_key, "char_count": 0, "groups": set()})
        info["char_count"] += 1
        info["groups"].update(groups.get(r["id"], []))
    accounts = [{"key": a["key"], "char_count": a["char_count"], "groups": sorted(a["groups"])}
                for a in acct_info.values()]
    return {"found": True, "rows": rows, "accounts": accounts, "db_path": db_path}
