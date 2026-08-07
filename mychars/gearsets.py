"""Gear-set planner: snapshot worn loadouts, reassign them across the roster, and
route every piece by its real transfer cost.

Why this lives in My Characters and not the macro builder: routing needs ACCOUNTS.
Two toons on the same account can never be online together, but they share a bank —
so a same-account move is a shared-bank hand-off during the swap you are already
doing, while a cross-account move needs a parcel (or a live trade). The roster DB
is the only place that knows who lives on which account.

Route ladder, cheapest first (cost = your time/logins, not game mechanics):
  worn    already equipped on the target                        0 logins
  have    already on the target (bags/bank) — just equip it     0 logins
  grab    in the target's OWN account shared bank               0 logins
  swap    a same-account toon has it -> shared-bank hand-off    1 login, no parcel NPC
  trade   a different-account toon that is ONLINE NOW has it    0 extra logins
  parcel  a different-account offline toon has it               1 login + parcel NPC
  reserved / notrade / lore / missing                           can't move (yet)

Known truths encoded here (see forge.js Gear Planner history):
  - Tradability = fvnodrop (Frostreaver is FREE-TRADE), never plain nodrop.
  - loregroup == -1 means LORE: a character can never hold two copies.
  - Prefer a WORN copy over a banked one on the same holder (Lore pulls fail if the
    holder wears one) -> BUCKET_RANK puts worn above bank.
  - Hoard/persona locations cannot be auto-lifted by MQ — they are manual pulls.
  - One physical copy can only be promised once across ALL active sets.
"""
import re
import time

from . import db as dbm
from . import gear as gearmod
from . import harvest as harvestmod

BUCKET_RANK = {"bags": 0, "worn": 1, "bank": 2, "shared": 3, "keyring": 4,
               "depot": 5, "hoard": 6, "persona": 7}
MANUAL_BUCKETS = {"hoard", "persona"}      # MQ has no slot names for these
MOVE_STATUSES = ("swap", "trade", "parcel")
SATISFIED_STATUSES = ("worn", "have", "grab")

# EQ wearable-slot bitmask (items.txt "slots" column) — same table forge.js uses.
# Labels match the DUMP's worn-slot names (so set items line up with snapshots);
# paired slots (both ears/wrists/fingers) get two editor rows via PAIRED_SLOTS.
SLOT_BITS = [("Charm", 1), ("Ear", 2 | 16), ("Head", 4), ("Face", 8), ("Neck", 32),
             ("Shoulders", 64), ("Arms", 128), ("Back", 256), ("Wrist", 512 | 1024),
             ("Range", 2048), ("Hands", 4096), ("Primary", 8192), ("Secondary", 16384),
             ("Fingers", 32768 | 65536), ("Chest", 131072), ("Legs", 262144),
             ("Feet", 524288), ("Waist", 1048576), ("Power", 2097152), ("Ammo", 4194304)]
PAIRED_SLOTS = {"Ear", "Wrist", "Fingers"}
AUG_ITEMTYPE = 54                          # augs carry the HOST slot's bitmask — never gear
MAX_CANDIDATES = 60                        # per slot, ranked

# Class-weighted stat scoring, ported from forge.js's Upgrade Finder (ARCH_W —
# Scoring priorities: AC/HP/Mana/resists first, INT/WIS for casters, STR/DEX for
# melee, haste heavy). Effects the stat-sum can't value (clickies, procs, focus,
# worn fx) get presence credit so an epic outranks an effectless stat-stick.
CLASS_ARCH = {
    "Warrior": "melee", "Monk": "melee", "Rogue": "melee", "Berserker": "melee",
    "Paladin": "hybrid", "Shadow Knight": "hybrid", "Ranger": "hybrid",
    "Bard": "hybrid", "Beastlord": "hybrid",
    "Cleric": "priest", "Druid": "priest", "Shaman": "priest",
    "Wizard": "caster", "Magician": "caster", "Necromancer": "caster", "Enchanter": "caster",
}
ARCH_W = {
    "melee":  {"ac": 1.5, "hp": .7, "astr": .7, "adex": .6, "asta": .4, "aagi": .3,
               "attack": .4, "haste": 2.0, "regen": .8, "resist": .5},
    "hybrid": {"ac": 1.2, "hp": .65, "astr": .5, "adex": .4, "asta": .4, "aagi": .25,
               "attack": .35, "haste": 2.0, "mana": .3, "awis": .4, "aint": .4, "resist": .5},
    "priest": {"ac": .9, "hp": .6, "asta": .35, "mana": .5, "awis": .8, "resist": .6},
    "caster": {"ac": .8, "hp": .6, "asta": .3, "mana": .5, "aint": .8, "resist": .5},
}
RESIST_CAP = 45          # SoV-era resist gear tops out here; bigger values are DB outliers


def score_item(dbi, class_name):
    w = ARCH_W.get(CLASS_ARCH.get(class_name or ""), ARCH_W["hybrid"])
    s = 0.0
    for col, wt in w.items():
        if col == "resist":
            continue
        s += (dbi.get(col, 0) or 0) * wt
    def capr(v):
        return max(-RESIST_CAP, min(RESIST_CAP, v or 0))
    s += sum(capr(dbi.get(k, 0)) for k in ("mr", "fr", "cr", "dr", "pr")) * w.get("resist", 0)
    if dbi.get("clickname"):
        s += 15
    if dbi.get("procname"):
        s += 12
    if dbi.get("focusname"):
        s += 10
    if dbi.get("wornname"):
        s += 8
    return s


# --- sets CRUD ---------------------------------------------------------------

def list_sets(conn):
    sets = dbm.rows(conn, """
        SELECT g.*, sc.name AS source_name, ac.name AS assigned_name,
               ac.account_id AS assigned_account_id
        FROM gear_sets g
        LEFT JOIN characters sc ON sc.id = g.source_char_id
        LEFT JOIN characters ac ON ac.id = g.assigned_char_id
        ORDER BY g.active DESC, g.name COLLATE NOCASE""")
    items = {}
    for r in conn.execute(
            "SELECT * FROM gear_set_items ORDER BY set_id, slot, slot_index"):
        items.setdefault(r["set_id"], []).append(dict(r))
    for s in sets:
        s["items"] = items.get(s["id"], [])
    return sets


def save_set(conn, payload, set_id=None, eq_dir=None):
    """Create/update a set. payload: name, class_name, source_char_id,
    assigned_char_id, active, notes, items:[{item_id,item_name,slot,slot_index}].
    Items are replaced only when the payload carries an "items" key.

    payload["steal"]: when this save over-claims an item (active sets together now
    want more copies than the roster owns), TAKE the shortfall from other active
    sets — one claim per copy short, donors in set-id order — and report what was
    taken. Copies are fungible, so this only fires when copies are actually short;
    owning spares means nobody loses anything. Needs eq_dir for owned counts."""
    now = int(time.time())
    if set_id is None:
        name = (payload.get("name") or "").strip()
        if not name:
            return 400, {"ok": False, "error": "Gear set needs a name."}
        row = conn.execute("SELECT id FROM gear_sets WHERE name=?", (name,)).fetchone()
        if row:
            set_id = row["id"]      # same-name save overwrites (like comps)
    fields = {}
    for f in ("name", "class_name", "source_char_id", "assigned_char_id", "notes"):
        if f in payload:
            fields[f] = payload[f]
    if "active" in payload:
        fields["active"] = 1 if payload["active"] else 0
    if "name" in fields:
        fields["name"] = (fields["name"] or "").strip()
        if not fields["name"]:
            return 400, {"ok": False, "error": "Gear set needs a name."}
    fields["updated_at"] = now
    if set_id is None:
        fields.setdefault("active", 1)
        fields["created_at"] = now
        cols = ", ".join(fields)
        marks = ", ".join("?" * len(fields))
        cur = conn.execute("INSERT INTO gear_sets(%s) VALUES (%s)" % (cols, marks),
                           list(fields.values()))
        set_id = cur.lastrowid
    else:
        sets = ", ".join("%s=?" % f for f in fields)
        conn.execute("UPDATE gear_sets SET %s WHERE id=?" % sets,
                     list(fields.values()) + [set_id])
    if "items" in payload:
        conn.execute("DELETE FROM gear_set_items WHERE set_id=?", (set_id,))
        seen = {}
        for it in payload.get("items") or []:
            iid = int(it.get("item_id") or 0)
            if iid <= 0:
                continue
            slot = (it.get("slot") or "").strip()
            k = it.get("slot_index")
            if k is None:
                k = seen.get(slot, 0)
            seen[slot] = max(seen.get(slot, 0), int(k) + 1)
            conn.execute(
                "INSERT OR REPLACE INTO gear_set_items(set_id, slot, slot_index,"
                " item_id, item_name) VALUES (?,?,?,?,?)",
                (set_id, slot, int(k), iid, it.get("item_name") or ""))
    taken = []
    if payload.get("steal") and "items" in payload and eq_dir:
        taken = _steal_overclaims(conn, set_id, eq_dir)
    conn.commit()
    return 200, {"ok": True, "id": set_id, "taken": taken}


def _steal_overclaims(conn, set_id, eq_dir):
    """For every item this set claims beyond what the roster owns (counting all
    ACTIVE sets), delete surplus claims from OTHER active sets. Returns
    [{"item", "from_set"}] describing what was taken."""
    world = _load_world(conn, eq_dir)
    holdings = _holdings(world)
    owned = {iid: sum(e["count"] for e in entries) for iid, entries in holdings.items()}
    my_counts = {}
    for r in conn.execute("SELECT item_id, item_name FROM gear_set_items WHERE set_id=?",
                          (set_id,)):
        my_counts.setdefault(r["item_id"], {"name": r["item_name"], "n": 0})["n"] += 1
    taken = []
    for iid, mine in my_counts.items():
        total = conn.execute("""
            SELECT COUNT(*) FROM gear_set_items gi JOIN gear_sets g ON g.id = gi.set_id
            WHERE gi.item_id=? AND g.active=1""", (iid,)).fetchone()[0]
        over = total - owned.get(iid, 0)
        while over > 0:
            row = conn.execute("""
                SELECT gi.rowid AS rid, g.name AS set_name
                FROM gear_set_items gi JOIN gear_sets g ON g.id = gi.set_id
                WHERE gi.item_id=? AND g.active=1 AND gi.set_id != ?
                ORDER BY g.id LIMIT 1""", (iid, set_id)).fetchone()
            if row is None:
                break               # only this set claims it (e.g. both ears, 1 copy)
            conn.execute("DELETE FROM gear_set_items WHERE rowid=?", (row["rid"],))
            taken.append({"item": mine["name"], "from_set": row["set_name"]})
            over -= 1
    return taken


def snapshot(conn, eq_dir, char_id, name=None):
    """Save what a toon is WEARING right now (from its newest inventory dump)."""
    char = conn.execute("SELECT * FROM characters WHERE id=?", (char_id,)).fetchone()
    if char is None:
        return 404, {"ok": False, "error": "No such character."}
    hit = gearmod.list_dumps(eq_dir).get((char["name"].lower(), char["server"].lower()))
    if not hit:
        return 400, {"ok": False, "error":
                     "No inventory dump for %s — run /outputfile inventory on them first."
                     % char["name"]}
    parsed = gearmod.parse_dump(hit[0])
    if not parsed["worn"]:
        return 400, {"ok": False, "error": "%s's dump shows no worn gear." % char["name"]}
    name = (name or "").strip() or "%s (%s)" % (char["name"], char["class_name"] or "worn")
    items, seen = [], {}
    for slot, iid, nm in parsed["worn"]:
        k = seen.get(slot, 0)
        seen[slot] = k + 1
        items.append({"item_id": iid, "item_name": nm, "slot": slot, "slot_index": k})
    code, res = save_set(conn, {
        "name": name, "class_name": char["class_name"] or "",
        "source_char_id": char_id, "assigned_char_id": char_id,
        "items": items,
    })
    if code == 200:
        res.update({"name": name, "pieces": len(items),
                    "dump_age_h": (int(time.time()) - hit[1]) // 3600})
    return code, res


# --- world state -------------------------------------------------------------

def _is_banker(c):
    """Mule/banker toons are the PREFERRED source for every move — gear should live
    spread out on them, not on mains. Explicit: 'banker' in group_tags. Implicit:
    level <= 5 (the usual level-1 town mules)."""
    if "banker" in (c.get("group_tags") or "").lower():
        return True
    lvl = c.get("level")
    return lvl is not None and lvl <= 5


# --- shared-bank capacity ----------------------------------------------------
# A swap route parks pieces in the ACCOUNT's shared bank while you camp one toon
# and logs the next. That bank is 8 top-level slots, each holding either ONE item or
# a bag — so its real capacity is a number the planner has to respect: promise more
# hand-offs than there are free slots and the run stalls halfway through, holding
# items with nowhere to put them (2026-08-06: "my shared bank doesn't have enough
# bag slots to cover all the swaps").
#
# Read RAW from the dump, not through gear.parse_dump: capacity lives entirely in the
# rows parse_dump throws away — the "Empty" ones. A complete dump lists every shared
# slot and every bag sub-slot, Empty included (harvest.py verified this over 36 real
# dumps), so free space is countable with no item DB and no guessing at bag sizes.
SHARED_TOP_RE = re.compile(r"^SharedBank(\d+)$")
SHARED_SUB_RE = re.compile(r"^SharedBank(\d+)-Slot(\d+)$")
EMPTY_NAMES = ("", "Empty")


def shared_capacity(path):
    """Free item slots in one account's shared bank, from one toon's dump.

    -> {"free_direct", "free_bag", "free_total", "used", "bags": [{slot,name,free,size}],
        "ok"}. free_direct = empty top-level slots (each takes one item); free_bag =
    empty slots inside bags already sitting in the shared bank.
    """
    out = {"free_direct": 0, "free_bag": 0, "free_total": 0, "used": 0,
           "bags": [], "ok": False}
    top, sub = {}, {}
    try:
        with open(path, encoding="latin-1") as f:
            if not f.readline().startswith("Location"):
                return out
            for line in f:
                parts = line.rstrip("\r\n").split("\t")
                if len(parts) < 2:
                    continue
                loc, name = parts[0], parts[1]
                m = SHARED_TOP_RE.match(loc)
                if m:
                    top[int(m.group(1))] = name
                    continue
                m = SHARED_SUB_RE.match(loc)
                if m:
                    sub.setdefault(int(m.group(1)), []).append(name)
    except OSError:
        return out
    if not top:
        return out
    for n, name in sorted(top.items()):
        kids = sub.get(n) or []
        if name in EMPTY_NAMES:
            out["free_direct"] += 1
        elif kids:                       # a bag: its interior is the usable space
            free = sum(1 for k in kids if k in EMPTY_NAMES)
            out["free_bag"] += free
            out["used"] += len(kids) - free
            out["bags"].append({"slot": n, "name": name, "free": free, "size": len(kids)})
        else:
            out["used"] += 1             # a plain item parked in a top slot
    out["free_total"] = out["free_direct"] + out["free_bag"]
    out["ok"] = True
    return out


def _shared_by_account(world):
    """account_id -> capacity, read from the FRESHEST dump on that account.

    Shared bank is account storage that every toon's dump repeats, so any toon can
    report it — but they report it as of THEIR dump time, and stale copies disagree
    (2026-08-06: three toons on one account each reported a different free count).
    Newest observation wins, and its age rides along so the UI can say so.
    """
    newest = {}
    for cid, path in (world.get("paths") or {}).items():
        c = world["by_id"].get(cid)
        acct = c and c.get("account_id")
        if acct is None:
            continue
        mt = world["mtimes"].get(cid) or 0
        if acct not in newest or mt > newest[acct][0]:
            newest[acct] = (mt, cid, path)
    out = {}
    for acct, (mt, cid, path) in newest.items():
        cap = shared_capacity(path)
        cap["observed_from"] = world["by_id"][cid]["name"]
        cap["observed_at"] = mt or None
        out[acct] = cap
    return out


def _load_world(conn, eq_dir):
    chars = dbm.rows(conn, """
        SELECT c.id, c.name, c.server, c.class_name, c.level, c.race, c.account_id,
               c.group_tags, a.alias AS account_alias
        FROM characters c LEFT JOIN accounts a ON a.id = c.account_id
        WHERE c.status != 'retired'""")
    dumps = gearmod.list_dumps(eq_dir)
    world = {"chars": chars, "by_id": {c["id"]: c for c in chars},
             "inv": {}, "mtimes": {}, "paths": {}}
    for c in chars:
        hit = dumps.get((c["name"].lower(), c["server"].lower()))
        if hit:
            world["inv"][c["id"]] = gearmod.parse_dump(hit[0])
            world["mtimes"][c["id"]] = hit[1]
            world["paths"][c["id"]] = hit[0]     # raw path — shared_capacity needs
    return world                                 # the "Empty" rows parse_dump drops


def _holdings(world):
    """item_id -> [entry]. entry = one stack of copies somewhere in the world.
    Shared bank is ACCOUNT storage that every toon's dump repeats — dedupe to one
    entry per (account, item, loc). Keyrings are mounts etc, not transferable."""
    idx = {}
    shared_seen = set()
    for cid, parsed in world["inv"].items():
        c = world["by_id"][cid]
        rows = [(loc, iid, nm, 1) for loc, iid, nm in parsed["worn"]] + \
               [tuple(h) for h in parsed["held"]]
        for loc, iid, nm, n in rows:
            bucket = gearmod.loc_bucket(loc)
            if bucket == "keyring":
                continue
            if bucket == "shared":
                key = (c["account_id"], iid, loc)
                if key in shared_seen:
                    continue
                shared_seen.add(key)
            idx.setdefault(iid, []).append({
                "char_id": cid, "holder": c["name"], "account_id": c["account_id"],
                "account_alias": c["account_alias"] or "", "loc": loc,
                "bucket": bucket, "count": max(1, int(n)), "left": max(1, int(n)),
                "banker": _is_banker(c), "level": c.get("level"), "claimed_by": [],
                # When this holder's inventory was last observed. A plan is only ever
                # as true as the dump it was routed from (2026-07-22: a plan promised
                # gear the holder no longer had), so the age rides along to the row.
                "mtime": world["mtimes"].get(cid),
            })
    return idx


# --- the planner -------------------------------------------------------------

def build_plans(conn, eq_dir, login_ids=None, db=None, stale_h=None):
    """Route every active set's pieces. All active sets are planned TOGETHER so the
    one-copy-one-promise reservation math holds across sets (the '33 rows for 24
    items' bug class). login_ids = char ids currently online (the login set) —
    holders in it get the trade route (no extra login needed).

    stale_h: dumps older than this mark their rows `stale_source`. The planner still
    routes them — it does NOT refuse — because a stale dump is usually still right;
    it just stops being something to trust silently. Same window as the Harvest tab.
    """
    db = db or gearmod.load_item_db()
    stale_h = int(stale_h) if stale_h else harvestmod.DEFAULT_STALE_H
    world = _load_world(conn, eq_dir)
    holdings = _holdings(world)
    shared_cap = _shared_by_account(world)
    login = {int(x) for x in (login_ids or []) if x}
    sets = [s for s in list_sets(conn) if s["active"] and s["items"]]
    sets.sort(key=lambda s: s["id"])            # deterministic claim order

    # A toon that is the target of an active set keeps the pieces its OWN set needs —
    # other sets may only take its spares (the "4 rogues" bug class).
    target_of = {}
    for s in sets:
        tgt = s["assigned_char_id"] or s["source_char_id"]
        if tgt:
            target_of.setdefault(tgt, set()).update(i["item_id"] for i in s["items"])

    coverage = {}       # holder char_id -> pieces already sourced (login consolidation)
    now = int(time.time())
    plans = []
    for s in sets:
        tgt_id = s["assigned_char_id"] or s["source_char_id"]
        target = world["by_id"].get(tgt_id)
        plan = {"set_id": s["id"], "name": s["name"], "rows": [], "counts": {}}
        if target is None:
            plan["target"] = None
            plan["error"] = "Set has no target toon — assign it to somebody."
            plans.append(plan)
            continue
        tinv = world["inv"].get(tgt_id)
        plan["target"] = {
            "id": target["id"], "name": target["name"],
            "class_name": target["class_name"], "account_id": target["account_id"],
            "account_alias": target["account_alias"] or "",
            "dumped": tinv is not None,
            "dump_age_h": ((now - world["mtimes"][tgt_id]) // 3600
                           if tgt_id in world["mtimes"] else None),
        }
        plan["target"]["stale"] = (plan["target"]["dump_age_h"] is not None
                                   and plan["target"]["dump_age_h"] >= stale_h)
        # target's own stock, consumed row by row (two Ear rows of the same item
        # must not both be satisfied by one worn copy)
        have_worn, have_held = {}, {}
        if tinv:
            for _, iid, _ in tinv["worn"]:
                have_worn[iid] = have_worn.get(iid, 0) + 1
            for loc, iid, _, n in tinv["held"]:
                have_held.setdefault(iid, []).append({"loc": loc, "left": max(1, int(n))})
        set_claims = {}     # item_id -> copies this set has sourced (lore guard)
        tgt_mtime = world["mtimes"].get(tgt_id)
        for it in s["items"]:
            row = _route_item(it, s, target, have_worn, have_held, holdings,
                              db, login, coverage, target_of, set_claims,
                              now, stale_h, tgt_mtime)
            plan["rows"].append(row)
        counts = {}
        for r in plan["rows"]:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        plan["counts"] = counts
        plan["stale_rows"] = sum(1 for r in plan["rows"] if r["stale_source"])
        # Does the shared-bank leg physically fit? Every swap row parks one item in
        # the account's shared bank between logins, so free slots is a hard ceiling
        # per round — over it, the run stalls holding gear with nowhere to put it.
        cap = dict(shared_cap.get(target["account_id"]) or {})
        if cap.get("ok"):
            need = counts.get("swap", 0)
            free = cap["free_total"]
            cap["needed"] = need
            cap["rounds"] = 1 if need <= free else (need + free - 1) // free if free else None
            cap["overflow"] = bool(need > free)
            cap["age_h"] = ((now - cap["observed_at"]) // 3600
                            if cap.get("observed_at") else None)
            plan["shared_bank"] = cap
        else:
            plan["shared_bank"] = None
        plans.append(plan)

    return {"plans": plans, "workorder": _workorder(plans, world),
            "summary": _summary(plans, stale_h)}


def _age_of(row, mtime, now, stale_h):
    """Stamp the row with the age of the dump this routing decision came from."""
    row["source_age_h"] = None if mtime is None else (now - mtime) // 3600
    row["stale_source"] = (row["source_age_h"] is not None
                           and row["source_age_h"] >= stale_h)
    return row


def _route_item(it, s, target, have_worn, have_held, holdings, db, login,
                coverage, target_of, set_claims, now, stale_h, tgt_mtime):
    iid = it["item_id"]
    dbi = db.get(iid) or {}
    row = {"slot": it["slot"], "slot_index": it["slot_index"], "item_id": iid,
           "item_name": it["item_name"], "status": "", "holder": "",
           "holder_id": None, "account_alias": "", "from_loc": "", "bucket": "",
           "manual": False, "attune_risk": False, "note": "",
           "source_age_h": None, "stale_source": False}

    # already on the target?  worn/have/grab are read off the TARGET's own dump, so
    # their trustworthiness is the target's dump age, not any holder's.
    if have_worn.get(iid, 0) > 0:
        have_worn[iid] -= 1
        row["status"] = "worn"
        return _age_of(row, tgt_mtime, now, stale_h)
    for h in have_held.get(iid, []):
        if h["left"] > 0:
            h["left"] -= 1
            row["from_loc"] = h["loc"]
            row["bucket"] = gearmod.loc_bucket(h["loc"])
            # a copy in the account's shared bank is a withdrawal, not a "have"
            row["status"] = "grab" if row["bucket"] == "shared" else "have"
            row["manual"] = row["bucket"] in MANUAL_BUCKETS
            if row["manual"]:
                row["note"] = "on the target but in the %s — manual pull" % row["bucket"]
            if row["bucket"] == "shared":
                # shared-bank stacks are deduped per ACCOUNT in holdings and may be
                # keyed to a sibling toon's dump — burn the copy there too, or another
                # set could promise this same physical item.
                for e in holdings.get(iid, []):
                    if (e["bucket"] == "shared" and e["loc"] == h["loc"]
                            and e["account_id"] == target["account_id"] and e["left"] > 0):
                        e["left"] -= 1
                        e["claimed_by"].append("%s → %s (own shared bank)"
                                               % (s["name"], target["name"]))
                        break
            return _age_of(row, tgt_mtime, now, stale_h)

    # LORE: one copy per character, ever. If this set already sourced one, a second
    # is impossible; if the target owns one anywhere it would have matched above.
    lore = int(dbi.get("loregroup", 0) or 0) == -1
    if lore and set_claims.get(iid, 0) > 0:
        row["status"] = "lore"
        row["note"] = "LORE — a character can only carry one"
        return row

    movable = int(dbi.get("fvnodrop", 0) or 0) == 0
    cands = [e for e in holdings.get(iid, []) if e["char_id"] != target["id"]
             or e["bucket"] == "shared"]
    if not movable:
        row["status"] = "notrade"
        row["note"] = ("NO TRADE (fvnodrop)" +
                       (" — %s has one" % cands[0]["holder"] if cands else ""))
        return row

    best = None
    for e in cands:
        protected = 1 if (e["char_id"] in target_of and iid in target_of[e["char_id"]]
                          and e["char_id"] != target["id"]) else 0
        if e["left"] - protected <= 0:
            continue
        grab = (e["bucket"] == "shared" and e["account_id"] is not None
                and e["account_id"] == target["account_id"])
        same = (not grab and e["account_id"] is not None
                and e["account_id"] == target["account_id"])
        online = e["char_id"] in login
        key = (0 if grab else 1,               # your own shared bank beats everything
               0 if e["banker"] else 1,        # bankers/mules ALWAYS beat mains (
                                               # gear lives spread out on town level-1s)
               -coverage.get(e["char_id"], 0),  # stack pieces on holders already owed a login
               0 if same else 1,               # same account: piggyback the swap
               0 if online else 1,             # already online: trade now
               BUCKET_RANK.get(e["bucket"], 9))
        if best is None or key < best[0]:
            best = (key, e, grab, same, online)

    if best is None:
        if cands:
            owners = sorted({o for e in cands for o in e["claimed_by"]})
            row["status"] = "reserved"
            row["note"] = ("every copy is promised elsewhere" +
                           (": " + ", ".join(owners[:4]) if owners else ""))
        else:
            row["status"] = "missing"
            row["note"] = "nobody on the roster has one"
        return row

    _, e, grab, same, online = best
    e["left"] -= 1
    e["claimed_by"].append("%s → %s" % (s["name"], target["name"]))
    set_claims[iid] = set_claims.get(iid, 0) + 1
    if not grab:
        coverage[e["char_id"]] = coverage.get(e["char_id"], 0) + 1
    row.update(holder=e["holder"], holder_id=e["char_id"], holder_banker=e["banker"],
               account_alias=e["account_alias"], from_loc=e["loc"], bucket=e["bucket"])
    row["manual"] = e["bucket"] in MANUAL_BUCKETS
    row["attune_risk"] = bool(int(dbi.get("attunable", 0) or 0)) and e["bucket"] == "worn"
    row["status"] = "grab" if grab else "swap" if same else "trade" if online else "parcel"
    if row["status"] == "swap":
        row["note"] = "same account — shared bank, then log %s" % target["name"]
    _age_of(row, e["mtime"], now, stale_h)
    if row["stale_source"]:
        row["note"] = ((row["note"] + " · ") if row["note"] else "") + \
            "%s's dump is %dd old — confirm they still have it" % (
                e["holder"], row["source_age_h"] // 24)
    return row


def _workorder(plans, world):
    """Who to log in, biggest first. grab rows need no holder login and are excluded;
    swap sessions must run BEFORE the target logs in (same account)."""
    holders = {}
    for p in plans:
        if not p.get("target"):
            continue
        for r in p["rows"]:
            if r["status"] not in MOVE_STATUSES:
                continue
            h = holders.get(r["holder_id"])
            if h is None:
                c = world["by_id"].get(r["holder_id"], {})
                h = holders[r["holder_id"]] = {
                    "holder_id": r["holder_id"], "holder": r["holder"],
                    "account_id": c.get("account_id"),
                    "account_alias": r["account_alias"],
                    "banker": _is_banker(c),
                    # the work-order card tells you how old the picture of this
                    # holder's bags is before you go log them in
                    "dump_age_h": r["source_age_h"], "stale": r["stale_source"],
                    "total": 0, "manual": 0, "routes": {}, "items": []}
            h["total"] += 1
            h["routes"][r["status"]] = h["routes"].get(r["status"], 0) + 1
            if r["manual"]:
                h["manual"] += 1
            h["items"].append({"item": r["item_name"], "slot": r["slot"],
                               "to": p["target"]["name"], "set": p["name"],
                               "route": r["status"], "loc": r["from_loc"],
                               "bucket": r["bucket"], "manual": r["manual"]})
    return sorted(holders.values(), key=lambda h: -h["total"])


def _summary(plans, stale_h=None):
    tot = {}
    for p in plans:
        for k, v in p.get("counts", {}).items():
            tot[k] = tot.get(k, 0) + v
    pieces = sum(tot.values())
    done = sum(tot.get(k, 0) for k in SATISFIED_STATUSES)
    moves = sum(tot.get(k, 0) for k in MOVE_STATUSES)
    blocked = pieces - done - moves
    # How much of this plan rests on inventory we haven't looked at lately. Counted
    # over MOVE rows only — a stale "worn" row costs nothing, a stale "parcel" row
    # is a promise about an item that may already be gone.
    move_rows = [r for p in plans for r in p.get("rows", [])
                 if r["status"] in MOVE_STATUSES]
    stale_moves = sum(1 for r in move_rows if r["stale_source"])
    ages = [r["source_age_h"] for r in move_rows if r["source_age_h"] is not None]
    return {"pieces": pieces, "satisfied": done, "moves": moves,
            "blocked": blocked, "by_status": tot,
            "stale_moves": stale_moves, "stale_h": stale_h,
            "oldest_source_h": max(ages) if ages else None}


# --- Lua exports (format-compatible with forge.js buildPlansLua) ---------------

def _lua_str(s):
    return '"%s"' % str("" if s is None else s).replace("\\", "\\\\") \
        .replace('"', '\\"').replace("\n", "\\n")


def build_plans_lua(plan_result):
    """Same shape mailgear.lua / TrixBox already parse: a `plans` list plus the
    first plan mirrored at top level for older builds. Returns None if no moves."""
    plans = []
    for p in plan_result["plans"]:
        if not p.get("target"):
            continue
        moves = [r for r in p["rows"] if r["status"] in MOVE_STATUSES]
        if moves:
            plans.append({"name": p["name"], "target": p["target"]["name"],
                          "moves": moves})
    if not plans:
        return None

    def move_lua(r, target, indent):
        return ("%s{ id = %d, name = %s, from = %s, to = %s, slot = %s, "
                "fromLoc = %s, attuneRisk = %s },"
                % (indent, r["item_id"] or 0, _lua_str(r["item_name"]),
                   _lua_str(r["holder"]), _lua_str(target), _lua_str(r["slot"]),
                   _lua_str(r["from_loc"]),
                   "true" if r["attune_risk"] else "false"))

    plan_blocks = []
    for p in plans:
        moves = "\n".join(move_lua(r, p["target"], "      ") for r in p["moves"])
        plan_blocks.append(
            "    { name = %s, set = %s, target = %s,\n"
            "      moves = {\n%s\n      } }," %
            (_lua_str(p["name"]), _lua_str(p["name"]), _lua_str(p["target"]), moves))
    first = plans[0]
    first_moves = "\n".join(move_lua(r, first["target"], "    ") for r in first["moves"])
    header = ("-- Gear plans - generated by EQ Forge My Characters (Gear Sets)\n" +
              "\n".join("--   %s -> %s (%d move(s))" % (p["name"], p["target"], len(p["moves"]))
                        for p in plans) + "\n" +
              "-- In game (mailgear): /mailgear plans, /mailgear dequip (on a holder),\n"
              "-- /mailgear getbank (at a banker), /mailgear equip (on the target).\n"
              "-- Dry-run until /mailgear live on.  TrixBox users: /trix plans, /trix sendgear.\n")
    text = (header + "return {\n  plans = {\n" + "\n".join(plan_blocks) + "\n  },\n"
            "  -- back-compat: first plan mirrored for older TrixBox builds\n"
            "  name = %s, set = %s, target = %s,\n  moves = {\n%s\n  },\n}\n" %
            (_lua_str(first["name"]), _lua_str(first["name"]),
             _lua_str(first["target"]), first_moves))
    return {"text": text,
            "plans": [{"name": p["name"], "target": p["target"],
                       "count": len(p["moves"]),
                       "ids": sorted({r["item_id"] for r in p["moves"] if r["item_id"]})}
                      for p in plans]}


def build_parcel_source_lua(plans_meta):
    """Parcel-tool source filters for the exported plans (see forge.js version)."""
    blocks = []
    for p in plans_meta:
        id_table = ", ".join("[%d]=true" % i for i in p["ids"])
        blocks.append(
            "    {\n"
            "        name = %s,\n"
            "        filter = function(item)\n"
            "            local ids = { %s }\n"
            "            return ids[(item.ID() or 0)] == true\n"
            "        end,\n"
            "    }," % (_lua_str("Gear Plan: %s -> %s (%d)" %
                                 (p["name"], p["target"], len(p["ids"]))), id_table))
    return ("-- Auto-generated by EQ Forge My Characters (Gear Sets) - do NOT hand-edit.\n"
            "-- Chain-loaded by config/parcel_sources.lua so the current gear plan appears\n"
            "-- as a pickable source in DerpleDude's parcel tool. Review there, then Send.\n" +
            "\n".join("--   %s -> %s (%d item(s))" % (p["name"], p["target"], p["count"])
                      for p in plans_meta) + "\n" +
            "return {\n" + "\n".join(blocks) + "\n}\n")


# --- custom-set editor: per-slot candidates over EVERYTHING owned --------------

# Effect id column -> the label the editor shows. Same four kinds as the Comp Power
# panel; names resolve through gear.spell_name (app/spell-effects.json.gz), which
# returns "" when that generated file is absent -- so effects still surface, unnamed.
EFFECT_KINDS = [("clickeffect", "Clicky"), ("proceffect", "Proc"),
                ("focuseffect", "Focus"), ("worneffect", "Worn")]


def _item_effects(dbi):
    """[{kind, name}] for an item's click/proc/focus/worn effects (empty if none)."""
    out = []
    for col, kind in EFFECT_KINDS:
        sid = dbi.get(col) or 0
        if sid > 0:
            out.append({"kind": kind, "name": gearmod.spell_name(sid) or ("#%d" % sid)})
    return out


def candidates(conn, eq_dir, class_name=None, exclude_set_id=None, db=None,
               target_char_id=None):
    """For each worn slot: every owned, class-usable, non-aug item across the whole
    roster (worn/bags/bank/shared/hoard/persona), ranked AC → HP → Mana. Carries
    owned vs free counts so the editor can gray out pieces other active sets have
    already claimed. exclude_set_id = the set being edited (its own claims don't
    count against itself). target_char_id = the set's assigned toon, so the editor
    can lead with 'already on <target>' instead of banker sources (the Earring of
    Purity confusion: one toon wore it, the Where column led with another's copy)."""
    db = db or gearmod.load_item_db()
    world = _load_world(conn, eq_dir)
    holdings = _holdings(world)
    target_char_id = int(target_char_id) if target_char_id else None
    tchar = world["by_id"].get(target_char_id) if target_char_id else None
    tacct = tchar["account_id"] if tchar else None
    # race gate: only when the assigned toon's race is on file (an Ogre SK must
    # not be offered Iksar-only Greenmist pieces). Unknown race = filter off.
    rbit = gearmod.race_bit((tchar or {}).get("race") or "") if tchar else None

    bit = gearmod.class_bit(class_name) if class_name else None
    claimed = {}                          # item_id -> copies claimed by OTHER active sets
    for s in list_sets(conn):
        if not s["active"] or s["id"] == exclude_set_id:
            continue
        for it in s["items"]:
            claimed[it["item_id"]] = claimed.get(it["item_id"], 0) + 1
    claim_names = {}                      # item_id -> [set names] (for the reserved label)
    for s in list_sets(conn):
        if not s["active"] or s["id"] == exclude_set_id:
            continue
        for it in s["items"]:
            claim_names.setdefault(it["item_id"], [])
            if s["name"] not in claim_names[it["item_id"]]:
                claim_names[it["item_id"]].append(s["name"])

    per_item = {}                         # item_id -> aggregated candidate
    for iid, entries in holdings.items():
        dbi = db.get(iid)
        if dbi is None:
            continue
        mask = dbi.get("slots", 0)
        if not mask or dbi.get("itemtype", 0) == AUG_ITEMTYPE:
            continue
        cmask = dbi.get("classes", 0)
        if bit is not None and cmask not in (0, 65535) and not (cmask & bit):
            continue
        rmask = dbi.get("races", 0)
        if rbit is not None and rmask not in (0, 65535) and not (rmask & rbit):
            continue
        owned = sum(e["count"] for e in entries)
        # banker copies listed first — they're where the planner will take from
        ranked = sorted(entries, key=lambda e: (0 if e["banker"] else 1,
                                                BUCKET_RANK.get(e["bucket"], 9)))
        best = ranked[0]
        others = len({e["holder"] for e in entries}) - 1
        # copies on someone's BACK (worn, non-banker) — taking one undresses that
        # toon even if no set claims it; idle = copies just sitting (bags/bank/hoard
        # + anything on a banker), i.e. the gear you want to bleed out first
        worn_by = sorted(
            ({"holder": e["holder"], "level": e["level"]}
             for e in entries if e["bucket"] == "worn" and not e["banker"]),
            key=lambda w: -(w["level"] or 0))
        # does the set's assigned toon already have this? (worn > held > their
        # account's shared bank — same order the planner satisfies rows in)
        t_has = ""
        if target_char_id:
            mine = [e for e in entries if e["char_id"] == target_char_id]
            if any(e["bucket"] == "worn" for e in mine):
                t_has = "worn"
            elif mine:
                t_has = "held"
            elif tacct and any(e["bucket"] == "shared" and e["account_id"] == tacct
                               for e in entries):
                t_has = "shared"
        idle = owned - sum(e["count"] for e in entries
                           if e["bucket"] == "worn" and not e["banker"])
        per_item[iid] = {
            "item_id": iid, "name": dbi["name"],
            "score": round(score_item(dbi, class_name)),
            "ac": dbi.get("ac", 0), "hp": dbi.get("hp", 0), "mana": dbi.get("mana", 0),
            "holder": best["holder"], "bucket": best["bucket"],
            "more_holders": others,
            "holders": [{"holder": e["holder"], "bucket": e["bucket"], "count": e["count"],
                         "loc": e["loc"], "banker": e["banker"], "level": e["level"]}
                        for e in ranked[:8]],
            "worn_by": worn_by[:6], "idle": idle, "target_has": t_has,
            "owned": owned, "free": owned - claimed.get(iid, 0),
            "reserved_by": claim_names.get(iid, []),
            "fvnodrop": dbi.get("fvnodrop", 0), "lore": dbi.get("loregroup", 0) == -1,
            "slots_mask": mask,
            # Full stat block + named effects so the editor can show a real item card
            # and stat deltas instead of a truncated dropdown label. Sparse (zeros
            # dropped) because this ships up to 21 slots x MAX_CANDIDATES rows.
            "stats": {k: dbi[k] for k in gearmod.STAT_KEYS if dbi.get(k)},
            "haste": dbi.get("haste", 0),
            "effects": _item_effects(dbi),
        }

    out = []
    for slot, sbits in SLOT_BITS:
        items = [c for c in per_item.values() if c["slots_mask"] & sbits]
        items.sort(key=lambda c: (-c["score"], -c["ac"], -c["hp"], c["name"]))
        out.append({"slot": slot, "paired": slot in PAIRED_SLOTS,
                    "items": [{k: v for k, v in c.items() if k != "slots_mask"}
                              for c in items[:MAX_CANDIDATES]]})
    return {"slots": out, "class_name": class_name or "",
            # "" = no target; race name = filtering; None = target has no race
            # on file (UI warns: race-locked gear is NOT being filtered out)
            "race_filter": ((tchar.get("race") or None) if tchar else "")}


# --- comp gear check: can the roster fill every live member's set at once? -----

def comp_gear_check(conn, eq_dir, char_ids):
    """For the comp's live toons: does everyone have an active set, and are there
    enough physical copies across the WHOLE roster to fill all of them at once?
    Pure counting (dumps only, no item DB) so it's fast enough to run on every
    comp edit. A toon's newest active assigned set is 'their' set."""
    char_ids = [int(x) for x in (char_ids or []) if x]
    world = _load_world(conn, eq_dir)
    holdings = _holdings(world)
    owned = {iid: sum(e["count"] for e in entries) for iid, entries in holdings.items()}
    active = [s for s in list_sets(conn) if s["active"] and s["items"]]
    by_target = {}
    for s in active:
        t = s["assigned_char_id"]
        if t and (t not in by_target
                  or (s["updated_at"] or 0) > (by_target[t]["updated_at"] or 0)):
            by_target[t] = s

    toons, no_set = [], []
    demand = {}                    # item_id -> {name, count, sets}
    for cid in char_ids:
        c = world["by_id"].get(cid)
        if c is None:
            continue
        s = by_target.get(cid)
        if s is None:
            no_set.append(c["name"])
            continue
        toons.append({"char": c["name"], "set": s["name"], "pieces": len(s["items"])})
        for it in s["items"]:
            d = demand.setdefault(it["item_id"],
                                  {"name": it["item_name"], "count": 0, "sets": []})
            d["count"] += 1
            if s["name"] not in d["sets"]:
                d["sets"].append(s["name"])

    overlaps = []                  # comp alone wants more copies than exist
    for iid, d in demand.items():
        have = owned.get(iid, 0)
        if d["count"] > have:
            overlaps.append({"item": d["name"], "need": d["count"], "owned": have,
                             "sets": d["sets"]})
    overlaps.sort(key=lambda o: (o["owned"] - o["need"], o["item"]))

    # outside pressure: sets NOT in this comp also claiming items the comp needs —
    # fine on its own, but flag when comp + outside together exceed what you own.
    comp_set_ids = {by_target[cid]["id"] for cid in char_ids if cid in by_target}
    out_claims = {}
    for s in active:
        if s["id"] in comp_set_ids:
            continue
        for it in s["items"]:
            if it["item_id"] in demand:
                oc = out_claims.setdefault(it["item_id"], {"count": 0, "sets": set()})
                oc["count"] += 1
                oc["sets"].add(s["name"])
    outside = []
    for iid, oc in out_claims.items():
        d = demand[iid]
        have = owned.get(iid, 0)
        if d["count"] <= have and d["count"] + oc["count"] > have:
            outside.append({"item": d["name"], "comp_need": d["count"],
                            "outside_need": oc["count"], "owned": have,
                            "outside_sets": sorted(oc["sets"])})
    outside.sort(key=lambda o: o["item"])
    return {"toons": toons, "no_set": no_set, "overlaps": overlaps, "outside": outside}


# --- light fit (for the sets list; no item DB needed) --------------------------

def fit_counts(conn, eq_dir):
    """set_id -> {"worn": n, "present": n, "total": n} against the target's dump."""
    world = _load_world(conn, eq_dir)
    out = {}
    for s in list_sets(conn):
        tgt = s["assigned_char_id"] or s["source_char_id"]
        tinv = world["inv"].get(tgt)
        total = len(s["items"])
        if tinv is None:
            out[s["id"]] = {"worn": None, "present": None, "total": total}
            continue
        worn_stock, all_stock = {}, {}
        for _, iid, _ in tinv["worn"]:
            worn_stock[iid] = worn_stock.get(iid, 0) + 1
            all_stock[iid] = all_stock.get(iid, 0) + 1
        for _, iid, _, n in tinv["held"]:
            all_stock[iid] = all_stock.get(iid, 0) + max(1, int(n))
        worn = present = 0
        for it in s["items"]:
            iid = it["item_id"]
            if worn_stock.get(iid, 0) > 0:
                worn_stock[iid] -= 1
                worn += 1
            if all_stock.get(iid, 0) > 0:
                all_stock[iid] -= 1
                present += 1
        out[s["id"]] = {"worn": worn, "present": present, "total": total}
    return out
