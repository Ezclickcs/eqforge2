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
"""
import os
import sqlite3
import urllib.request

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
