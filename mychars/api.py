"""JSON API for My Characters, mounted by serve.py under /roster/*.

handle(method, path, payload) -> (status_code, json-able dict). No HTTP types in
here, so tests call it directly. One short-lived sqlite connection per request.
"""
import json
import os
import sqlite3
import time

from . import autologin
from . import db as dbm
from . import domain
from . import gear as gearmod
from . import gearsets as gsmod
from . import harvest as harvestmod
from . import importer
from . import lockouts as lockmod

DB_PATH = None  # tests point this at a temp file; None = dbm.DEFAULT_DB_PATH

# NOTE: these two are FALLBACKS ONLY. serve.py calls mychars.paths.apply() at startup
# (and after every Setup save), which overwrites both with the properly resolved paths.
# They matter solely for a direct `import mychars.api` with no server — tests set their
# own. Don't add resolution logic here; paths.py owns it.
# MQ config dir (serve.py overwrites this with its own MQ_CONFIG_DIR at import time
# so both stay in sync; env var wins for both).
MQ_CONFIG_DIR = os.environ.get(
    "EQFORGE_MQ_CONFIG", r"C:\MacroQuest\config")
# EQ install dir where /outputfile inventory dumps land (serve.py syncs this too).
EQ_DIR = os.environ.get(
    "EQFORGE_EQ_DIR", r"C:\Users\Public\Daybreak Game Company\Installed Games\EverQuest")
DUMP_SUFFIX = "-Inventory.txt"


def _conn():
    # Deliberately does NOT seed. seed.json is a SAMPLE roster with real character
    # names in it; auto-loading it meant a fresh install opened showing someone
    # else's toons and accounts, which reads as a bug and has to be deleted row by
    # row before the real import. An empty roster is the honest first-run state -
    # the Import tab is the front door. POST /roster/seed {"force":true} still
    # loads the sample on request.
    return dbm.connect(DB_PATH)


# --- data assembly ----------------------------------------------------------

ACCT_FIELDS = ["alias", "nickname", "account_number", "status", "autologin_group",
               "launch_order", "notes"]
CHAR_FIELDS = ["name", "server", "account_id", "class_name", "level", "race", "main_role",
               "status", "group_tags", "gear_tier", "epic_status", "portbot_whitelist",
               "automation_status", "notes"]
UNLOCK_FIELDS = ["character_id", "name", "expansion", "category", "scope", "priority",
                 "status", "verified_date", "notes"]


def load_characters(conn):
    chars = dbm.rows(conn, """
        SELECT c.*, a.alias AS account_alias FROM characters c
        LEFT JOIN accounts a ON a.id = c.account_id ORDER BY a.launch_order, c.name""")
    ovr = {}
    for r in conn.execute("SELECT * FROM capability_overrides"):
        ovr.setdefault(r["character_id"], {})[r["capability"]] = bool(r["enabled"])
    locks = {}
    for r in conn.execute("SELECT character_id, raid_id, expires_at, imported_at FROM raid_lockouts"):
        locks.setdefault(r["character_id"], []).append(
            {"raid_id": r["raid_id"], "expires_at": r["expires_at"],
             "imported_at": r["imported_at"]})
    for c in chars:
        c["overrides"] = ovr.get(c["id"], {})
        c["caps"] = domain.resolve_capabilities(c["class_name"], c["overrides"])
        c["lockouts"] = locks.get(c["id"], [])
        c["lockout_raid_ids"] = [lk["raid_id"] for lk in c["lockouts"]]
    return chars


def unlocks_by_char(conn):
    out = {}
    for r in conn.execute("SELECT character_id, name, status FROM unlocks"):
        out.setdefault(r["character_id"], {})[r["name"]] = r["status"]
    return out


def load_compositions(conn, chars):
    by_id = {c["id"]: c for c in chars}
    ubc = unlocks_by_char(conn)
    comps = dbm.rows(conn, "SELECT * FROM compositions ORDER BY name")
    for comp in comps:
        raw = [r for r in conn.execute(
            "SELECT slot_index, character_id, gear_set_id FROM composition_slots"
            " WHERE composition_id=?", (comp["id"],)) if 0 <= r["slot_index"] < 24]
        rows = {r["slot_index"]: r["character_id"] for r in raw}
        size = max([6] + [i + 1 for i in rows])
        slots = [rows.get(i) for i in range(size)]
        comp["slots"] = slots
        comp["gear_sets"] = {str(r["character_id"]): r["gear_set_id"] for r in raw
                             if r["character_id"] and r["gear_set_id"]}
        # warnings judge the LIVE six only; bench toons don't fight
        comp["members"] = [by_id[cid] for cid in slots[:6] if cid in by_id]
        comp["bench"] = [by_id[cid]["name"] for cid in slots[6:] if cid in by_id]
        req = [s.strip() for s in (comp["required_unlocks"] or "").split(",") if s.strip()]
        comp["warnings"] = domain.validate_composition(comp["members"], req, ubc)
    return comps


def bootstrap(conn):
    lockmod.purge_expired(conn)          # expired saves drop off automatically
    accounts = dbm.rows(conn, "SELECT * FROM accounts ORDER BY launch_order, alias")
    for a in accounts:
        try:
            a["perks"] = json.loads(a["perks"] or "[]")
        except ValueError:
            a["perks"] = []
    chars = load_characters(conn)
    unlocks = dbm.rows(conn, "SELECT * FROM unlocks ORDER BY priority, name")
    raids = dbm.rows(conn, "SELECT * FROM raids ORDER BY id")
    comps = load_compositions(conn, chars)
    recs = domain.recommendations(accounts, chars, unlocks, comps, now=int(time.time()))
    return {
        "accounts": accounts, "characters": chars, "unlocks": unlocks,
        "raids": raids, "compositions": comps, "recommendations": recs,
        "meta": {
            "capabilities": [{"key": k, "label": v} for k, v in domain.CAPABILITIES],
            "classes": domain.CLASSES,
            "class_defaults": {k: sorted(v) for k, v in domain.CLASS_DEFAULTS.items()},
            "unlock_examples": domain.UNLOCK_EXAMPLES,
            "unlock_statuses": ["Complete", "In Progress", "Missing", "Unknown", "Not Needed"],
            "claim_items": domain.CLAIM_ITEMS,
        },
    }


def scan_claims(conn):
    """Scan inventory dumps for account-bound claim items and merge into account perks.

    Claims are account-bound: the item in ANY toon's dump proves the whole
    account+server has it. Toon -> account comes from the roster.
    """
    chars = {}
    for r in conn.execute("""SELECT c.name, c.server, a.id AS aid, a.alias FROM characters c
                             JOIN accounts a ON a.id = c.account_id"""):
        chars[r["name"].lower()] = {"server": r["server"].lower(),
                                    "aid": r["aid"], "alias": r["alias"]}
    found = {}                                  # alias -> {claim -> [toons]}
    scanned = 0
    unmatched = set()
    if not os.path.isdir(EQ_DIR):
        return {"found": False, "error": "EQ dir not found: %s" % EQ_DIR}
    for fn in os.listdir(EQ_DIR):
        if not fn.endswith(DUMP_SUFFIX):
            continue
        toon = fn[:-len(DUMP_SUFFIX)].split("_")[0]
        info = chars.get(toon.lower())
        if info is None:
            unmatched.add(toon)
            continue
        try:
            with open(os.path.join(EQ_DIR, fn), encoding="latin-1") as f:
                text = f.read()
        except OSError:
            continue
        scanned += 1
        for claim in domain.CLAIM_ITEMS:
            if claim["name"] in text:
                found.setdefault(info["alias"], {}).setdefault(claim["name"], []).append(toon)
    applied = {}
    for r in conn.execute("SELECT id, alias, perks FROM accounts"):
        claims = sorted(found.get(r["alias"], {}))
        if not claims:
            continue
        try:
            perks = json.loads(r["perks"] or "[]")
        except ValueError:
            perks = []
        new = [c for c in claims if c not in perks]
        if new:
            conn.execute("UPDATE accounts SET perks=? WHERE id=?",
                         (json.dumps(perks + new), r["id"]))
            applied[r["alias"]] = new
    conn.commit()
    return {"found": True, "dumps_scanned": scanned, "unmatched_dumps": sorted(unmatched),
            "detected": {al: {c: ts for c, ts in cl.items()} for al, cl in found.items()},
            "applied": applied}


def apply_login_names(conn):
    """User-initiated: rename roster account aliases to the real login usernames.

    Mapping login.db account -> roster account is derived from shared character
    membership (name+server), the same evidence used for the import mapping.
    Usernames only — passwords are never selected (see autologin.py).
    """
    db_path = os.path.join(MQ_CONFIG_DIR, "login.db")
    names = autologin.read_account_names(db_path)
    feed = autologin.read_roster(db_path)
    if not names or not feed["found"]:
        return 404, {"ok": False, "error": "login.db not found or empty at " + db_path}
    rchars = {}
    for r in conn.execute("SELECT name, server, account_id FROM characters"
                          " WHERE account_id IS NOT NULL"):
        rchars[(r["name"].lower(), r["server"].lower())] = r["account_id"]
    votes = {}                                  # login_id -> {roster_acct_id: count}
    for row in feed["rows"]:
        rid = rchars.get((row["name"].lower(), row["server"].lower()))
        if rid is None:
            continue
        login_id = int(row["account"].replace("MQAcct", ""))
        votes.setdefault(login_id, {})
        votes[login_id][rid] = votes[login_id].get(rid, 0) + 1
    renamed, skipped = {}, []
    taken = set()
    for login_id, counts in sorted(votes.items()):
        target = max(counts, key=lambda k: counts[k])
        if target in taken:
            continue
        taken.add(target)
        uname = (names.get(login_id) or "").strip()
        if not uname:
            continue
        row = conn.execute("SELECT alias FROM accounts WHERE id=?", (target,)).fetchone()
        if row is None or row["alias"] == uname:
            continue
        try:
            conn.execute("UPDATE accounts SET alias=? WHERE id=?", (uname, target))
            renamed[row["alias"]] = uname
        except sqlite3.IntegrityError:
            skipped.append(uname)               # duplicate username (other server_type)
    conn.commit()
    return 200, {"ok": True, "renamed": renamed, "skipped_duplicates": skipped}


def load_membership(conn):
    """Apply membership level + sub expiry window from mychars_export.csv to accounts.

    Membership is account-level, so any toon's row covers its whole account;
    the freshest export (asof) sets the membership LEVEL.

    Expiry is a WINDOW, not a point. `Me.SubscriptionDays` is a floor'd whole-day
    count, so a reading of N days at time T only proves the true expiry lies in
    [T + N days, T + N+1 days). Storing T + N alone (what we used to do) is the
    earliest possible instant and reads as "lapses today" up to 24h early -- worst
    exactly at the end, when N collapses to 0 while the account is still GOLD.

    Because several toons share an account and export at different times, we
    intersect every reading's window to narrow it. Real example (kurakoo):
        CharA 07-28 23:38 N=3 -> [07-31 23:38, 08-01 23:38)
        CharB 07-31 04:36 N=1 -> [08-01 04:36, 08-02 04:36)
        CharC 08-01 00:27 N=0 -> [08-01 00:27, 08-02 00:27)
    intersection -> [08-01 04:36, 08-01 23:34) -- a 19h window instead of "already
    lapsed". Applying a krono invalidates older readings, which shows up as an
    EMPTY intersection; in that case the freshest reading alone wins.
    """
    import csv as _csv
    # one file per toon (mychars_export_<Name>.csv) to avoid the broadcast write race;
    # the prefix match also still picks up a legacy shared mychars_export.csv.
    paths = []
    if os.path.isdir(MQ_CONFIG_DIR):
        paths = [os.path.join(MQ_CONFIG_DIR, fn) for fn in sorted(os.listdir(MQ_CONFIG_DIR))
                 if fn.startswith("mychars_export") and fn.endswith(".csv")]
    if not paths:
        return {"found": False, "path": os.path.join(MQ_CONFIG_DIR, "mychars_export_*.csv")}
    chars = {}
    for r in conn.execute("SELECT name, server, account_id FROM characters"
                          " WHERE account_id IS NOT NULL"):
        chars[(r["name"].lower(), r["server"].lower())] = r["account_id"]
    rows = []
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                rows.extend(_csv.DictReader(f))
        except OSError:
            continue
    best = {}                                   # account_id -> (asof, membership)
    windows = {}                                # account_id -> [(asof, lo, hi), ...]
    for row in rows:
        member = (row.get("membership") or "").strip().upper()
        if not member or member == "NULL":
            continue                            # old 5-column rows or bad TLO read
        aid = chars.get(((row.get("name") or "").strip().lower(),
                         (row.get("server") or "").strip().lower()))
        if aid is None:
            continue
        try:
            asof = int(row.get("asof") or 0)
            days = int(row.get("subdays") or -1)
        except ValueError:
            continue
        if days >= 0:
            windows.setdefault(aid, []).append(
                (asof, asof + days * 86400, asof + (days + 1) * 86400))
        if aid not in best or asof > best[aid][0]:
            best[aid] = (asof, member)
    applied = {}
    for aid, (asof, member) in best.items():
        seen = windows.get(aid, [])
        lo, hi, used = _expiry_window(seen)
        conn.execute("UPDATE accounts SET membership=?, sub_expires=?, sub_expires_max=?"
                     " WHERE id=?", (member, lo, hi, aid))
        alias = conn.execute("SELECT alias FROM accounts WHERE id=?", (aid,)).fetchone()
        if alias:
            applied[alias["alias"]] = {"membership": member, "expires": lo,
                                       "expires_max": hi,
                                       # readings that actually constrain the window;
                                       # `stale` are pre-krono ones we dropped.
                                       "readings": used, "stale": len(seen) - used}
    conn.commit()
    return {"found": True, "applied": applied, "rows_matched": len(best)}


def _expiry_window(readings):
    """Intersect [lo, hi) expiry windows into the tightest one that still holds.

    readings: list of (asof, lo, hi). Returns (earliest, latest, used_count),
    or (None, None, 0) when there is nothing usable.

    Applying a krono invalidates every reading taken before it, so we cannot just
    intersect the lot. We anchor on the FRESHEST reading (the only one guaranteed
    to describe the current subscription) and fold in older readings newest-first,
    keeping one only while it agrees. A reading that contradicts is pre-krono and
    is dropped individually -- dropping the whole set instead would pin the account
    at a permanent 24h window, because the stale per-toon CSVs stay on disk until
    that toon happens to export again.
    """
    if not readings:
        return None, None, 0
    ordered = sorted(readings, key=lambda r: -r[0])
    lo, hi, used = ordered[0][1], ordered[0][2], 1
    for _, r_lo, r_hi in ordered[1:]:
        new_lo, new_hi = max(lo, r_lo), min(hi, r_hi)
        if new_lo < new_hi:                     # agrees -- keep the tighter bound
            lo, hi, used = new_lo, new_hi, used + 1
    return lo, hi, used


def load_exports():
    """Read every mychars_export_*.csv as CHARACTER rows (name/server/class/level/race).

    This is the freshest level source — the in-game lua exports live Me.Level —
    and reading each file separately (own header) avoids the repeated-header mess
    of pasting concatenated CSVs. Rows are fed through the normal importer
    preview/commit pipeline client-side, so blank-never-clobbers and credential
    stripping still apply. Freshest asof wins when a name appears in several
    files (legacy shared mychars_export.csv vs per-toon files).
    """
    import csv as _csv
    paths = []
    if os.path.isdir(MQ_CONFIG_DIR):
        paths = [os.path.join(MQ_CONFIG_DIR, fn) for fn in sorted(os.listdir(MQ_CONFIG_DIR))
                 if fn.startswith("mychars_export") and fn.endswith(".csv")]
    if not paths:
        return {"found": False, "rows": [], "files": [],
                "path": os.path.join(MQ_CONFIG_DIR, "mychars_export_*.csv")}
    best = {}                                   # (name,server) lower -> (asof, row)
    files = []
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                frows = list(_csv.DictReader(f))
        except OSError:
            continue
        files.append(os.path.basename(path))
        for r in frows:
            name = (r.get("name") or "").strip()
            if not name:
                continue
            server = (r.get("server") or "").strip()
            try:
                asof = int(r.get("asof") or 0)
            except ValueError:
                asof = 0
            key = (name.lower(), server.lower())
            if key in best and best[key][0] >= asof:
                continue
            best[key] = (asof, {
                "name": name[:1].upper() + name[1:],
                "server": autologin._nice_server(server),
                "class": (r.get("class") or "").strip(),
                "level": (r.get("level") or "").strip(),
                "race": (r.get("race") or "").strip(),
            })
    rows = [row for _, row in sorted(best.values(), key=lambda t: t[1]["name"])]
    return {"found": True, "rows": rows, "files": files}


DEFAULT_LOGIN_GROUP = "eqf login set"


def push_loginset(conn, char_ids, group_name=None, replace=False):
    """Publish the one-per-account login set to MQ, launch-order first.

    Two artifacts, and only one of them is the point:
      1. an AutoLogin PROFILE GROUP in login.db — what makes the set appear in
         the AutoLogin sidebar so you can right-click -> Launch All. This is the
         thing the button is for.
      2. mychars_loginset.lua in the MQ config folder — a plain data export for
         in-game scripts. Nothing in MQ reads this by itself; it is written
         because it costs nothing and the format is already published.
    A failure to write (1) is reported as a failure even though (2) succeeded,
    because (2) launches nothing.
    """
    if not char_ids:
        return 400, {"ok": False, "error": "Login set is empty."}
    marks = ",".join("?" * len(char_ids))
    rows = dbm.rows(conn, """SELECT c.name, c.server, c.class_name, c.level, a.alias,
                                    a.autologin_group, a.launch_order
                             FROM characters c LEFT JOIN accounts a ON a.id=c.account_id
                             WHERE c.id IN (%s) ORDER BY a.launch_order""" % marks, char_ids)
    lines = ["-- generated by EQ Forge My Characters (login set) - do not hand-edit",
             "return {"]
    for r in rows:
        lines.append("  { account=%r, group=%r, launch=%d, name=%r, class=%r, level=%d }," % (
            str(r["alias"] or ""), str(r["autologin_group"] or ""),
            r["launch_order"] or 0, str(r["name"]), str(r["class_name"] or ""),
            r["level"] or 0))
    lines.append("}")
    path = os.path.join(MQ_CONFIG_DIR, "mychars_loginset.lua")
    try:
        os.makedirs(MQ_CONFIG_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as e:
        return 500, {"ok": False, "error": str(e)}

    code, res = autologin.push_profile_group(
        os.path.join(MQ_CONFIG_DIR, "login.db"),
        group_name or DEFAULT_LOGIN_GROUP,
        [{"name": r["name"], "server": r["server"]} for r in rows],
        replace=replace)
    res["lua_path"] = path
    res["members"] = [r["name"] for r in rows]
    return code, res


def _gear_dumps(conn):
    """Roster characters joined to their inventory dumps: [(char, path, mtime)]."""
    dumps = gearmod.list_dumps(EQ_DIR)
    out = []
    for c in dbm.rows(conn, """SELECT c.id, c.name, c.server, c.class_name, c.level,
                                      c.account_id, a.alias AS account_alias
                               FROM characters c LEFT JOIN accounts a ON a.id=c.account_id
                               WHERE c.status != 'retired' ORDER BY c.name"""):
        hit = dumps.get((c["name"].lower(), c["server"].lower()))
        if hit:
            out.append((c, hit[0], hit[1]))
    return out


def gear_summary(conn):
    db = gearmod.load_item_db()
    now = int(time.time())
    rows = []
    for c, path, mtime in _gear_dumps(conn):
        parsed = gearmod.parse_dump(path)
        s = gearmod.worn_stats(parsed["worn"], db)
        rows.append({"character_id": c["id"], "name": c["name"], "class_name": c["class_name"],
                     "level": c["level"], "account_id": c["account_id"],
                     "account_alias": c["account_alias"], "dump_age_h": (now - mtime) // 3600,
                     **s})
    have = {r["name"] for r in rows}
    missing = [c["name"] for c in dbm.rows(
        conn, "SELECT name FROM characters WHERE status != 'retired' ORDER BY name")
        if c["name"] not in have]
    return {"rows": rows, "no_dump": missing,
            "stats_meta": [{"key": k, "label": v} for k, v in gearmod.STAT_FIELDS]}


def harvest_report(conn, stale_h=None):
    """Dump-coverage census: every roster toon vs its dump on disk vs the run log.

    Deliberately does NOT filter to characters that have a dump (unlike _gear_dumps) —
    a toon with no dump is the single most important row in this report.
    """
    chars = dbm.rows(conn, """SELECT c.id, c.name, c.server, c.class_name, c.level,
                                     c.account_id, c.group_tags, a.alias AS account_alias
                              FROM characters c LEFT JOIN accounts a ON a.id=c.account_id
                              WHERE c.status != 'retired'
                              ORDER BY a.launch_order, c.name""")
    run = harvestmod.read_runs(MQ_CONFIG_DIR)
    result = harvestmod.coverage(
        EQ_DIR, chars, run=run,
        stale_h=int(stale_h) if stale_h else harvestmod.DEFAULT_STALE_H)
    result["eq_dir"] = EQ_DIR
    result["mq_config_dir"] = MQ_CONFIG_DIR
    return result


def harvest_arm(conn, char_ids):
    """Arm an unattended harvest: one queue file per ACCOUNT, in launch order.

    Grouped by account because that is how the rotation runs — each client walks its
    own account's list sequentially via /switchchar. A toon needs the banker leg if
    it's tagged `hoard` OR its current dump already proves it owns one.
    """
    ids = [int(i) for i in (char_ids or []) if str(i).isdigit() or isinstance(i, int)]
    if not ids:
        return 400, {"ok": False, "error": "Pick at least one character to harvest."}
    rows = dbm.rows(conn, """SELECT c.id, c.name, c.server, c.group_tags, a.alias
                             FROM characters c LEFT JOIN accounts a ON a.id=c.account_id
                             WHERE c.id IN (%s) ORDER BY a.launch_order, c.name"""
                    % ",".join("?" * len(ids)), ids)
    if not rows:
        return 400, {"ok": False, "error": "None of those characters exist."}

    dumps = gearmod.list_dumps(EQ_DIR)
    by_acct = {}
    no_account = []
    for r in rows:
        if not r["alias"]:
            no_account.append(r["name"])      # can't /switchchar without a station name
            continue
        hoard = harvestmod.HOARD_TAG in (r["group_tags"] or "").lower()
        if not hoard:
            hit = dumps.get((r["name"].lower(), r["server"].lower()))
            if hit:
                hoard = harvestmod.scan_dump(hit[0])["items"]["hoard"] > 0
        by_acct.setdefault(r["alias"], []).append({"name": r["name"], "hoard": hoard})

    written = []
    for alias, entries in sorted(by_acct.items()):
        path = harvestmod.write_queue(MQ_CONFIG_DIR, alias, entries)
        written.append({"account": alias, "count": len(entries), "path": path,
                        "hoard": [e["name"] for e in entries if e["hoard"]],
                        "names": [e["name"] for e in entries]})
    return 200, {"ok": True, "queues": written, "no_account": no_account,
                 "mq_config_dir": MQ_CONFIG_DIR,
                 "total": sum(q["count"] for q in written)}


def harvest_tag_hoard(conn):
    """Tag every character whose dump already contains Dragon's Hoard rows.

    Evidence-based: nothing in an EQ dump declares "this toon owns a hoard", so the
    only honest signal is having seen its contents at least once. Idempotent.
    """
    tagged = []
    dumps = gearmod.list_dumps(EQ_DIR)
    for c in dbm.rows(conn, "SELECT id, name, server, group_tags FROM characters"
                            " WHERE status != 'retired'"):
        hit = dumps.get((c["name"].lower(), c["server"].lower()))
        if not hit or harvestmod.scan_dump(hit[0])["items"]["hoard"] == 0:
            continue
        tags = [t.strip() for t in (c["group_tags"] or "").split(",") if t.strip()]
        if harvestmod.HOARD_TAG in [t.lower() for t in tags]:
            continue
        tags.append(harvestmod.HOARD_TAG)
        conn.execute("UPDATE characters SET group_tags=? WHERE id=?",
                     (", ".join(tags), c["id"]))
        tagged.append(c["name"])
    conn.commit()
    return 200, {"ok": True, "tagged": tagged}


def gear_best(conn, stat, class_name=None):
    valid = {k for k, _ in gearmod.SEARCHABLE_STATS}
    if stat not in valid:
        return 400, {"ok": False, "error": "Unknown stat: %s" % stat}
    db = gearmod.load_item_db()
    dumps = [(c["name"], gearmod.parse_dump(path)) for c, path, _ in _gear_dumps(conn)]
    rows = gearmod.best_stat(stat, dumps, db, class_name or None)
    return 200, {"ok": True, "stat": stat, "rows": rows,
                 "stats_meta": [{"key": k, "label": v} for k, v in gearmod.SEARCHABLE_STATS]}


# --- mutations --------------------------------------------------------------

def _upsert(conn, table, fields, payload, row_id=None, extra=None):
    vals = {f: payload[f] for f in fields if f in payload}
    if extra:
        vals.update(extra)
    if not vals:
        return None
    if row_id is None:
        cols = ", ".join(vals)
        marks = ", ".join("?" * len(vals))
        cur = conn.execute("INSERT INTO %s(%s) VALUES (%s)" % (table, cols, marks),
                           list(vals.values()))
        return cur.lastrowid
    sets = ", ".join("%s=?" % f for f in vals)
    conn.execute("UPDATE %s SET %s WHERE id=?" % (table, sets), list(vals.values()) + [row_id])
    return row_id


def save_composition(conn, payload, comp_id=None):
    # slots 0-5 = the LIVE six (hard one-per-account rule); 6+ = bench/swaps
    # (exempt from the account rule — a bench toon is a logout-swap candidate).
    slots = list(payload.get("slots") or [])[:24]
    slots += [None] * (6 - len(slots))
    ids = [cid for cid in slots if cid]
    if len(ids) != len(set(ids)):
        return 400, {"ok": False, "error": "The same character is listed twice."}
    live_ids = [cid for cid in slots[:6] if cid]
    if live_ids:
        marks = ",".join("?" * len(live_ids))
        accts = {}
        for r in conn.execute(
                "SELECT c.id, c.name, a.alias FROM characters c LEFT JOIN accounts a"
                " ON a.id=c.account_id WHERE c.id IN (%s) AND c.account_id IS NOT NULL"
                " ORDER BY c.id" % marks, live_ids):
            accts.setdefault(r["alias"], []).append(r["name"])
        clash = {al: ns for al, ns in accts.items() if len(ns) > 1}
        if clash:
            detail = "; ".join("%s: %s" % (al, ", ".join(ns)) for al, ns in sorted(clash.items()))
            return 400, {"ok": False,
                         "error": "Account conflict — only one character per account. " + detail}
    name = (payload.get("name") or "").strip()
    if not name:
        return 400, {"ok": False, "error": "Composition needs a name."}
    if comp_id is None:
        row = conn.execute("SELECT id FROM compositions WHERE name=?", (name,)).fetchone()
        if row:
            comp_id = row["id"]     # save-as-existing-name overwrites that comp
    comp_id = _upsert(conn, "compositions", ["name", "notes", "required_unlocks"],
                      {"name": name, "notes": payload.get("notes", ""),
                       "required_unlocks": payload.get("required_unlocks", "")}, comp_id)
    while len(slots) > 6 and slots[-1] is None:     # don't persist empty bench slots
        slots.pop()
    # Slots are rewritten wholesale, so read the gear-set mapping out FIRST and put
    # it back keyed by CHARACTER, not slot_index — reordering the comp must not hand
    # Zyrak's set to Kaelor. Without this, every comp edit would silently clear the
    # mapping, which is the same write-path-destroys-what-it-never-read shape as the
    # gear-set steal bug (2026-08-09).
    keep_sets = {r["character_id"]: r["gear_set_id"] for r in conn.execute(
        "SELECT character_id, gear_set_id FROM composition_slots"
        " WHERE composition_id=? AND character_id IS NOT NULL", (comp_id,))}
    if payload.get("gear_sets"):        # explicit picks from the comp UI win
        for cid, sid in payload["gear_sets"].items():
            keep_sets[int(cid)] = int(sid) if sid else None
    conn.execute("DELETE FROM composition_slots WHERE composition_id=?", (comp_id,))
    for i, cid in enumerate(slots):
        conn.execute("INSERT INTO composition_slots(composition_id, slot_index,"
                     " character_id, gear_set_id) VALUES (?,?,?,?)",
                     (comp_id, i, cid, keep_sets.get(cid) if cid else None))
    conn.commit()
    return 200, {"ok": True, "id": comp_id}


# --- router -----------------------------------------------------------------

def handle(method, path, payload=None):
    """path is everything after /roster, e.g. '/bootstrap'. Returns (code, obj)."""
    parts = [p for p in path.strip("/").split("/") if p]
    payload = payload or {}
    conn = _conn()
    try:
        return _route(conn, method, parts, payload)
    except Exception as e:                          # surface, don't 500-blank
        return 500, {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    finally:
        conn.close()


def _route(conn, method, parts, payload):
    head = parts[0] if parts else ""
    rid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None

    if method == "GET" and head == "bootstrap":
        return 200, bootstrap(conn)

    if method == "GET" and head == "exports":
        result = load_exports()
        result["ok"] = True
        return 200, result

    if method == "GET" and head == "autologin":
        result = autologin.read_roster(os.path.join(MQ_CONFIG_DIR, "login.db"))
        result["ok"] = True
        return 200, result

    if method == "POST" and head == "autologin" and len(parts) > 1 and parts[1] == "aliases":
        return apply_login_names(conn)

    if head == "accounts":
        if method == "POST":
            payload.setdefault("perks", [])
            aid = _upsert(conn, "accounts", ACCT_FIELDS, payload,
                          extra={"perks": json.dumps(payload.get("perks") or [])})
            conn.commit()
            return 200, {"ok": True, "id": aid}
        if method == "PUT" and rid:
            extra = {"perks": json.dumps(payload["perks"])} if "perks" in payload else None
            _upsert(conn, "accounts", ACCT_FIELDS, payload, rid, extra)
            conn.commit()
            return 200, {"ok": True, "id": rid}
        if method == "DELETE" and rid:
            conn.execute("DELETE FROM accounts WHERE id=?", (rid,))
            conn.commit()
            return 200, {"ok": True}

    if head == "characters":
        if method == "POST":
            name = (payload.get("name") or "").strip()
            if not name:
                return 400, {"ok": False, "error": "Character needs a name."}
            dup = conn.execute(
                "SELECT id FROM characters WHERE server=? COLLATE NOCASE AND name=? COLLATE NOCASE",
                (payload.get("server") or "Frostreaver", name)).fetchone()
            if dup:
                return 400, {"ok": False, "error": "That character already exists on that server."}
            cid = _upsert(conn, "characters", CHAR_FIELDS, payload)
            conn.commit()
            return 200, {"ok": True, "id": cid}
        if rid and len(parts) > 2 and parts[2] == "capabilities" and method == "PUT":
            for cap, val in (payload.get("overrides") or {}).items():
                if cap not in domain.CAP_KEYS:
                    continue
                if val is None:
                    conn.execute("DELETE FROM capability_overrides WHERE character_id=? AND capability=?",
                                 (rid, cap))
                else:
                    conn.execute("INSERT OR REPLACE INTO capability_overrides(character_id, capability,"
                                 " enabled) VALUES (?,?,?)", (rid, cap, 1 if val else 0))
            conn.commit()
            return 200, {"ok": True}
        if rid and len(parts) > 2 and parts[2] == "lockouts" and method == "PUT":
            # keep imported expiry/freshness for raids that stay checked
            prior = {r["raid_id"]: (r["expires_at"], r["imported_at"]) for r in conn.execute(
                "SELECT raid_id, expires_at, imported_at FROM raid_lockouts WHERE character_id=?", (rid,))}
            conn.execute("DELETE FROM raid_lockouts WHERE character_id=?", (rid,))
            for raid_id in payload.get("raid_ids") or []:
                exp, imp = prior.get(raid_id, (None, None))
                conn.execute("INSERT OR IGNORE INTO raid_lockouts(character_id, raid_id,"
                             " expires_at, imported_at) VALUES (?,?,?,?)", (rid, raid_id, exp, imp))
            conn.commit()
            return 200, {"ok": True}
        if method == "PUT" and rid:
            _upsert(conn, "characters", CHAR_FIELDS, payload, rid)
            conn.commit()
            return 200, {"ok": True, "id": rid}
        if method == "DELETE" and rid:
            conn.execute("DELETE FROM characters WHERE id=?", (rid,))
            conn.commit()
            return 200, {"ok": True}

    if head == "unlocks":
        if method == "POST":
            uid = _upsert(conn, "unlocks", UNLOCK_FIELDS, payload)
            conn.commit()
            return 200, {"ok": True, "id": uid}
        if method == "PUT" and rid:
            _upsert(conn, "unlocks", UNLOCK_FIELDS, payload, rid)
            conn.commit()
            return 200, {"ok": True}
        if method == "DELETE" and rid:
            conn.execute("DELETE FROM unlocks WHERE id=?", (rid,))
            conn.commit()
            return 200, {"ok": True}

    if head == "raids":
        if method == "POST":
            name = (payload.get("name") or "").strip()
            if not name:
                return 400, {"ok": False, "error": "Raid needs a name."}
            conn.execute("INSERT OR IGNORE INTO raids(name) VALUES (?)", (name,))
            conn.commit()
            return 200, {"ok": True}
        if method == "DELETE" and rid:
            conn.execute("DELETE FROM raids WHERE id=?", (rid,))
            conn.commit()
            return 200, {"ok": True}

    if head == "compositions":
        if len(parts) > 1 and parts[1] == "auto" and method == "POST":
            chars = load_characters(conn)
            result = domain.auto_comp(chars, payload.get("requirements") or [])
            result["ok"] = True
            if result["slots"]:
                by_id = {c["id"]: c for c in chars}
                members = [by_id[cid] for cid in result["slots"] if cid in by_id]
                req = [s.strip() for s in (payload.get("required_unlocks") or "").split(",") if s.strip()]
                result["comp_warnings"] = domain.validate_composition(members, req, unlocks_by_char(conn))
            return 200, result
        if len(parts) > 1 and parts[1] == "validate" and method == "POST":
            chars = load_characters(conn)
            by_id = {c["id"]: c for c in chars}
            members = [by_id[cid] for cid in (payload.get("slots") or [])[:6] if cid in by_id]
            req = [s.strip() for s in (payload.get("required_unlocks") or "").split(",") if s.strip()]
            return 200, {"ok": True,
                         "warnings": domain.validate_composition(members, req, unlocks_by_char(conn))}
        if method == "POST":
            return save_composition(conn, payload)
        if method == "PUT" and rid:
            return save_composition(conn, payload, rid)
        if method == "DELETE" and rid:
            conn.execute("DELETE FROM compositions WHERE id=?", (rid,))
            conn.commit()
            return 200, {"ok": True}

    if head == "import":
        action = parts[1] if len(parts) > 1 else ""
        try:
            raw = importer.parse_text(payload.get("text", ""))
        except Exception as e:
            return 400, {"ok": False, "error": "Could not parse input: %s" % e}
        rows, stripped, ignored = importer.normalize_rows(raw)
        if action == "preview" and method == "POST":
            plan = importer.preview(conn, rows)
            plan.update({"ok": True, "total": len(rows),
                         "stripped_fields": stripped, "ignored_fields": ignored})
            return 200, plan
        if action == "commit" and method == "POST":
            result = importer.commit(conn, rows)
            result.update({"ok": True, "stripped_fields": stripped})
            return 200, result

    if head == "gearsets":
        sub = parts[1] if len(parts) > 1 else ""
        if method == "GET" and not parts[1:]:
            sets = gsmod.list_sets(conn)
            fits = gsmod.fit_counts(conn, EQ_DIR)
            for s in sets:
                s["fit"] = fits.get(s["id"])
            return 200, {"ok": True, "sets": sets}
        if method == "POST" and sub == "snapshot":
            return gsmod.snapshot(conn, EQ_DIR, int(payload.get("char_id") or 0),
                                  payload.get("name"))
        if method == "POST" and sub == "candidates":
            result = gsmod.candidates(conn, EQ_DIR, payload.get("class_name"),
                                      payload.get("exclude_set_id"),
                                      target_char_id=payload.get("target_char_id"))
            result["ok"] = True
            return 200, result
        if method == "GET" and sub == "compmap":
            comp_id = int(parts[2]) if len(parts) > 2 else 0
            return 200, {"ok": True, "mapping": gsmod.comp_gear_map(conn, comp_id)}
        if method == "POST" and sub == "clone":
            return gsmod.clone_set(conn, int(payload.get("set_id") or 0),
                                   payload.get("name"))
        if method == "POST" and sub == "compchoice":
            return gsmod.set_comp_choice(conn, int(payload.get("comp_id") or 0),
                                         int(payload.get("character_id") or 0),
                                         payload.get("gear_set_id"))
        if method == "POST" and sub == "apply":
            return gsmod.apply_comp_gear(conn, int(payload.get("comp_id") or 0),
                                         payload.get("choices"))
        if method == "POST" and sub == "compcheck":
            result = gsmod.comp_gear_check(conn, EQ_DIR, payload.get("slots"))
            result["ok"] = True
            return 200, result
        if method == "POST" and sub == "compplan":
            result = gsmod.comp_readiness(conn, EQ_DIR, payload.get("slots"),
                                          payload.get("login"),
                                          stale_h=payload.get("stale_h"))
            result["ok"] = True
            return 200, result
        if method == "POST" and sub == "plan":
            result = gsmod.build_plans(conn, EQ_DIR, payload.get("login"),
                                       stale_h=payload.get("stale_h"),
                                       focus_ids=payload.get("focus"))
            result["ok"] = True
            return 200, result
        if method == "POST" and sub == "export":
            result = gsmod.build_plans(conn, EQ_DIR, payload.get("login"),
                                       stale_h=payload.get("stale_h"),
                                       focus_ids=payload.get("focus"))
            built = gsmod.build_plans_lua(result)
            if built is None:
                return 400, {"ok": False, "error":
                             ("Nothing to send — this comp's sets have no movable "
                              "pieces still needing sourcing."
                              if payload.get("focus") else
                              "Nothing to send — no active set has movable pieces "
                              "still needing sourcing.")}
            return 200, {"ok": True, "plan_lua": built["text"],
                         "parcel_lua": gsmod.build_parcel_source_lua(built["plans"]),
                         "plans": built["plans"]}
        if method == "POST" and not sub:
            return gsmod.save_set(conn, payload, eq_dir=EQ_DIR)
        if method == "PUT" and rid:
            return gsmod.save_set(conn, payload, rid, eq_dir=EQ_DIR)
        if method == "DELETE" and rid:
            conn.execute("DELETE FROM gear_sets WHERE id=?", (rid,))
            conn.commit()
            return 200, {"ok": True}

    if head == "harvest":
        sub = parts[1] if len(parts) > 1 else ""
        if method == "GET" and not sub:
            result = harvest_report(conn, payload.get("stale_h"))
            result["ok"] = True
            return 200, result
        if method == "POST" and sub == "queue":
            return harvest_arm(conn, payload.get("char_ids"))
        if method == "POST" and sub == "disarm":
            removed = harvestmod.disarm(MQ_CONFIG_DIR, payload.get("account"))
            return 200, {"ok": True, "disarmed": removed}
        if method == "POST" and sub == "taghoard":
            return harvest_tag_hoard(conn)

    if head == "gear" and len(parts) > 1 and method == "GET":
        if parts[1] == "summary":
            result = gear_summary(conn)
            result["ok"] = True
            return 200, result
        if parts[1] == "best":
            return gear_best(conn, payload.get("stat", "haste"), payload.get("cls"))
        if parts[1] == "raceguess":
            cid = int(payload.get("char_id") or 0)
            rows = dbm.rows(conn, "SELECT name, server FROM characters WHERE id=?", (cid,))
            if not rows:
                return 404, {"ok": False, "error": "no such character"}
            hit = gearmod.list_dumps(EQ_DIR).get(
                (rows[0]["name"].lower(), rows[0]["server"].lower()))
            if not hit:
                return 200, {"ok": True, "dumped": False, "possible": [], "evidence": []}
            g = gearmod.race_guess(gearmod.parse_dump(hit[0])["worn"],
                                   gearmod.load_item_db())
            return 200, {"ok": True, "dumped": True, **g}

    if head == "perks" and len(parts) > 1 and parts[1] == "scan" and method == "POST":
        result = scan_claims(conn)
        result["ok"] = True
        return 200, result

    if head == "membership" and len(parts) > 1 and parts[1] == "load" and method == "POST":
        result = load_membership(conn)
        result["ok"] = True
        return 200, result

    if head == "loginset" and len(parts) > 1 and parts[1] == "push" and method == "POST":
        return push_loginset(conn, [int(x) for x in (payload.get("slots") or []) if x],
                             payload.get("group_name"), bool(payload.get("replace")))

    if head == "lockouts" and len(parts) > 1 and parts[1] == "load" and method == "POST":
        # per-toon files (mychars_lockouts_<Name>.txt) + legacy shared mychars_lockouts.txt
        rows, found = [], False
        if os.path.isdir(MQ_CONFIG_DIR):
            for fn in sorted(os.listdir(MQ_CONFIG_DIR)):
                if fn.startswith("mychars_lockouts") and fn.endswith(".txt"):
                    parsed = lockmod.parse_file(os.path.join(MQ_CONFIG_DIR, fn))
                    if parsed["found"]:
                        found = True
                        rows.extend(parsed["rows"])
        if not found:
            return 200, {"ok": True, "found": False,
                         "path": os.path.join(MQ_CONFIG_DIR, "mychars_lockouts_*.txt")}
        result = lockmod.apply(conn, rows)
        result.update({"ok": True, "found": True, "rows_in_file": len(rows)})
        return 200, result

    if head == "seed" and method == "POST":
        did = dbm.seed(conn, force=bool(payload.get("force")))
        return 200, {"ok": True, "seeded": did}

    return 404, {"ok": False, "error": "Unknown roster endpoint: %s" % "/".join(parts)}
