"""CSV / JSON roster importer with preview, dedup, and merge-by-(server,name).

Security rule: ANY field whose name smells like a credential is stripped before
normalization and reported — passwords never reach the database, ever.

Extensibility: `parse_text` auto-detects JSON vs CSV today. Future sanitized
sources plug in as parsers that yield the same normalized row dicts:
  - MQ2AutoLogin ini      -> account alias + character name + server per profile
  - EQ character INIs     -> filenames like  <Name>_<server>.ini
  - MacroQuest configs    -> character/server pairs
  - RGMercs INIs          -> class + level hints
  - /outputfile inventory -> "<Name>-Inventory.txt" filenames (name only)
Each would produce rows for normalize_rows(); everything downstream (preview,
duplicate detection, merge, commit) is shared.
"""
import csv
import io
import json
import re

SECRET_RE = re.compile(r"pass|pwd|secret|token|auth|credential|login_?key", re.I)

# normalized header -> canonical field
FIELD_ALIASES = {
    "name": "name", "character": "name", "char": "name", "charname": "name",
    "server": "server",
    "account": "account", "accountalias": "account", "acct": "account",
    "class": "class_name", "classname": "class_name",
    "level": "level", "lvl": "level",
    "race": "race",
    "mainrole": "main_role", "role": "main_role",
    "status": "status",
    "grouptags": "group_tags", "tags": "group_tags",
    "geartier": "gear_tier", "gear": "gear_tier",
    "epicstatus": "epic_status", "epic": "epic_status",
    "portbotwhitelist": "portbot_whitelist", "portbot": "portbot_whitelist",
    "automationstatus": "automation_status", "automation": "automation_status",
    "notes": "notes", "note": "notes",
}
CHAR_FIELDS = ["name", "server", "class_name", "level", "race", "main_role", "status",
               "group_tags", "gear_tier", "epic_status", "portbot_whitelist",
               "automation_status", "notes"]
# Account-level columns the mychars_export.csv carries alongside character rows.
# The character importer skips them silently (api.load_membership consumes them).
SILENT_IGNORE = {"membership", "subdays", "asof"}
TRUTHY = {"1", "true", "yes", "y", "on"}


def _norm_key(k):
    return re.sub(r"[^a-z0-9]", "", (k or "").strip().lower())


def parse_text(text):
    """Auto-detect JSON (list/obj) vs CSV. Returns list of raw dicts."""
    t = (text or "").strip()
    if not t:
        return []
    if t[0] in "[{":
        data = json.loads(t)
        if isinstance(data, dict):  # allow {"characters": [...]}
            data = data.get("characters", data.get("rows", []))
        if not isinstance(data, list):
            raise ValueError("JSON must be a list of characters")
        return [dict(r) for r in data]
    reader = csv.DictReader(io.StringIO(t))
    return [dict(r) for r in reader]


def normalize_rows(raw_rows, default_server="Frostreaver"):
    """Map raw dicts to canonical fields. Returns (rows, stripped, ignored).

    stripped: credential-smelling keys removed (NEVER imported).
    ignored: unrecognized keys (harmless, reported so typos are visible).
    """
    rows, stripped, ignored = [], set(), set()
    for raw in raw_rows:
        row = {}
        for k, v in raw.items():
            nk = _norm_key(k)
            if SECRET_RE.search(k or ""):
                stripped.add(k)
                continue
            field = FIELD_ALIASES.get(nk)
            if field is None:
                if nk and nk not in SILENT_IGNORE:
                    ignored.add(k)
                continue
            v = ("" if v is None else str(v)).strip()
            if field == "level":
                row[field] = int(v) if v.isdigit() else None
            elif field == "portbot_whitelist":
                row[field] = 1 if v.lower() in TRUTHY else 0
            else:
                row[field] = v
        if not row.get("name"):
            continue
        row.setdefault("server", default_server)
        if not row["server"]:
            row["server"] = default_server
        rows.append(row)
    return rows, sorted(stripped), sorted(ignored)


def preview(conn, rows):
    """Dry-run: classify each row create/update/duplicate, flag missing accounts.

    Merge key is (server, name), case-insensitive. Duplicates INSIDE the batch
    keep the first occurrence. For updates only changed fields are listed.
    """
    accounts = {r["alias"].lower(): r["id"]
                for r in conn.execute("SELECT id, alias FROM accounts")}
    out = {"create": [], "update": [], "duplicate": [], "missing_accounts": []}
    seen = set()
    missing_accts = set()
    for row in rows:
        key = (row["server"].lower(), row["name"].lower())
        entry = {"row": row}
        acct = (row.get("account") or "").strip()
        if acct and acct.lower() not in accounts:
            entry["missing_account"] = acct
            missing_accts.add(acct)
        if key in seen:
            out["duplicate"].append(entry)
            continue
        seen.add(key)
        existing = conn.execute(
            "SELECT * FROM characters WHERE server=? COLLATE NOCASE AND name=? COLLATE NOCASE",
            (row["server"], row["name"])).fetchone()
        if existing is None:
            out["create"].append(entry)
            continue
        diff = {}
        for f in CHAR_FIELDS:
            if f in ("name", "server") or f not in row:
                continue
            new = row[f]
            if new in ("", None):
                continue                       # blank cells never clobber data
            if (existing[f] if existing[f] is not None else "") != new:
                diff[f] = {"old": existing[f], "new": new}
        if acct and acct.lower() in accounts and existing["account_id"] != accounts[acct.lower()]:
            diff["account"] = {"old": existing["account_id"], "new": acct}
        entry["id"] = existing["id"]
        entry["diff"] = diff
        (out["update"] if diff else out["duplicate"]).append(entry)
    out["missing_accounts"] = sorted(missing_accts)
    return out


def commit(conn, rows):
    """Apply an import batch (same classification as preview). Returns counts."""
    plan = preview(conn, rows)
    accounts = {r["alias"].lower(): r["id"]
                for r in conn.execute("SELECT id, alias FROM accounts")}
    for entry in plan["create"]:
        row = entry["row"]
        conn.execute(
            "INSERT INTO characters(name, server, account_id, class_name, level, race, main_role,"
            " status, group_tags, gear_tier, epic_status, portbot_whitelist, automation_status, notes)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["name"], row["server"], accounts.get((row.get("account") or "").lower()),
             row.get("class_name", ""), row.get("level"), row.get("race", ""),
             row.get("main_role", ""), row.get("status", "active"), row.get("group_tags", ""),
             row.get("gear_tier", ""), row.get("epic_status", ""),
             row.get("portbot_whitelist", 0), row.get("automation_status", "untested"),
             row.get("notes", "")))
    for entry in plan["update"]:
        sets, params = [], []
        for f, d in entry["diff"].items():
            if f == "account":
                sets.append("account_id=?")
                params.append(accounts.get(str(d["new"]).lower()))
            else:
                sets.append("%s=?" % f)
                params.append(d["new"])
        params.append(entry["id"])
        conn.execute("UPDATE characters SET %s WHERE id=?" % ", ".join(sets), params)
    conn.commit()
    return {"created": len(plan["create"]), "updated": len(plan["update"]),
            "skipped_duplicates": len(plan["duplicate"]),
            "missing_accounts": plan["missing_accounts"]}
