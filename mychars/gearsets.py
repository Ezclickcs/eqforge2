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
# "covered" = a proc-defined slot (Avatar) whose proc is already carried by another
# slot of the same set. Nothing to fetch and nothing to do in game, so it counts as
# satisfied rather than sitting in the blocked pile forever.
SATISFIED_STATUSES = ("worn", "have", "grab", "covered")
# Everything the Lua export carries. "Satisfied" means no OTHER toon has to hand the
# piece over — it does NOT mean there is nothing to do: `have` still has to be equipped
# out of the target's own bags and `grab` still has to be pulled from its own shared
# bank. Exporting moves only (the pre-2026-08-09 behaviour) made those invisible in
# game, so a set whose pieces were already sitting on the right toon equipped nothing.
EXPORT_STATUSES = tuple(MOVE_STATUSES) + tuple(SATISFIED_STATUSES)
# Rows that still need a hand in game. `worn` is exported for slot reservation only.
ACTIONABLE_STATUSES = frozenset(MOVE_STATUSES) | {"have", "grab"}

# EQ wearable-slot bitmask (items.txt "slots" column) — same table forge.js uses.
# Labels match the DUMP's worn-slot names (so set items line up with snapshots);
# paired slots (both ears/wrists/fingers) get two editor rows via PAIRED_SLOTS.
SLOT_BITS = [("Charm", 1), ("Ear", 2 | 16), ("Head", 4), ("Face", 8), ("Neck", 32),
             ("Shoulders", 64), ("Arms", 128), ("Back", 256), ("Wrist", 512 | 1024),
             ("Range", 2048), ("Hands", 4096), ("Primary", 8192), ("Secondary", 16384),
             ("Fingers", 32768 | 65536), ("Chest", 131072), ("Legs", 262144),
             ("Feet", 524288), ("Waist", 1048576), ("Power", 2097152), ("Ammo", 4194304)]
PAIRED_SLOTS = {"Ear", "Wrist", "Fingers"}
# VIRTUAL slots — gear a toon must OWN and keep, but never wears. reported 2026-08-08:
# "i need to add an avatar weapon slot incase i have one I can assign to that toon to
# keep them reserved, effectivly tieing up 3 weapons per toon possibly." A monk wears
# Primary+Secondary (the Fists bandolier) and keeps a third weapon in his bags purely to
# proc Avatar; because it is not WORN, a snapshot never captured it, so nothing claimed
# it and the router was free to hand it to somebody else. Giving it a slot makes it a
# normal claim: reserved across sets, routed to the toon, counted by compcheck/steal.
# These deliberately live OUTSIDE SLOT_BITS so they never count as a worn slot or as a
# coverage hole — see _expected_slot_rows and fit_counts.
EXTRA_SLOTS = [("Avatar", 8192 | 16384), ("2-Hander", 8192),
               ("Mount", 0), ("WW Clicky", 0)]
EXTRA_SLOT_NAMES = {s for s, _ in EXTRA_SLOTS}
# 2H weapons are NOT identifiable from the slots bitmask: verified against the real item
# DB, Wurmslayer (1H Slashing) and Jagged Blade of War (2H Slashing) are BOTH slots=8192,
# because plenty of one-handers are primary-only too. itemtype is the discriminator -
# 1 = 2H Slashing, 4 = 2H Blunt, 35 = 2H Piercing (Ebon Scythe / Bronzewood Staff /
# Narandi's Lance all land correctly; Wurmslayer is type 0 and stays out).
# Caveat worth knowing: ~1960 primary-capable items in the sodeq export carry NO itemtype
# at all, so they cannot appear here. This slot is a deliberate manual pick, so a short
# list is better than a wrong one.
TWO_HAND_ITEMTYPES = {1, 4, 35}
EXTRA_SLOT_ITEMTYPES = {"2-Hander": TWO_HAND_ITEMTYPES}

# CARRIED slots — Mount and WW Clicky. reported 2026-08-09: "these are in bag things
# really even though the mount can fit in the ammo slot". Unlike Avatar/2-Hander
# (weapons, which the slots bitmask CAN see), these cannot be found by mask at all:
# White Skystrider Whistle is slots=4194304 (Ammo) while Trinket of the Far Frozen
# Wastes is slots=0, and an item with slots=0 is dropped outright by the candidate
# scan. So they match on a PREDICATE instead, and candidates() has to stop skipping
# slotless items for them.
AMMO_BIT = 4194304
MOUNT_ITEMTYPE = 68            # bridles/whistles/saddles. Verified: 294 in the sodeq
                               # export, incl. White Skystrider Whistle (106958).


def _is_mount(dbi):
    return (dbi.get("itemtype", -1) or -1) == MOUNT_ITEMTYPE


def _is_bag_clicky(dbi):
    """A REUSABLE clicky you carry rather than wear — the Trinket of the Far Frozen
    Wastes / Token of the Magus shape.

    Three conditions, each earning its place against the real item DB:
      clickeffect > 0   it is a clicky at all
      maxcharges == -1  unlimited uses. This is the one that matters: without it the
                        slot filled up with ~30 consumables off the user's real roster
                        (Gate Potion, every Distillate, Blood of the Wolf), because a
                        potion is also a slotless clicky. Consumables carry a finite
                        charge count (1/3/10/20); reusable clickies carry -1.
      no armour slot    slots=0, or Ammo — where EQ files mounts and bag oddments.
                        Worn gear with a clicky belongs in its own real slot.
    Deliberately NOT keyed on itemtype: the Trinket is type 72, but so are Arx Key and
    Tolan's Darkwood Breastplate — that column is a grab-bag and would drag a
    breastplate in here. Mounts are excluded so the two carried slots stay disjoint."""
    if (dbi.get("clickeffect") or 0) <= 0:
        return False
    if _is_mount(dbi):
        return False                       # it has its own slot
    if (dbi.get("maxcharges", 0) or 0) != -1:
        return False                       # consumable, not kit
    mask = dbi.get("slots", 0) or 0
    return mask == 0 or mask == AMMO_BIT


CARRIED_SLOT_MATCH = {"Mount": _is_mount, "WW Clicky": _is_bag_clicky}
# Gear that belongs to the ACCOUNT, not the character: claim rewards and mounts.
# It can never cross to another account, so a holder on a different one is not a
# source — offering one produces a plan step that is physically impossible to run.
ACCOUNT_BOUND_SLOTS = frozenset({"Mount", "WW Clicky"})

# PROC-DEFINED slots: the effect IS the definition of the slot, so the slots bitmask
# is ignored entirely. reported 2026-08-09: "the only things that can give avatar are
# primal velium weapons or ancient prismatic weapons" — confirmed against the item DB
# and app/spell-effects.json.gz: proc 2434 resolves to "Avatar" and is carried by
# exactly 24 items, every one of them a Primal Velium or Ancient Prismatic weapon,
# and NOTHING else in the whole DB carries it.
#
# This replaces a Primary|Secondary mask that offered every weapon on the roster —
# hundreds of picks, all but a handful useless, and the old note here ("nothing in the
# item data marks that, so it must stay a manual pick") was simply wrong: proceffect
# marks it exactly. Dropping the mask also matters because these are NOT all one-hand
# slots — the Fist Wraps/Warsword sit at 24576 (Primary|Secondary) and the Primal
# Velium Reinforced Bow at 2048 (Range), which a Primary|Secondary mask would have hidden.
AVATAR_PROC_SPELL = 2434
EXTRA_SLOT_PROCS = {"Avatar": AVATAR_PROC_SPELL}
# Slots a set is expected to cover before we call a hole a hole. Charm/Range/Ammo/
# Power sit empty on most Velious-era toons, so "no pick" there is normal — same
# skip list the editor's "Best available" uses.
GAP_SKIP_SLOTS = {"Charm", "Range", "Ammo", "Power"}
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


# --- a comp's gear: which set each member fields, and switching to it ---------
#
# `active` decides three things: which sets contend for copies, which get routed
# into the move plan, and which comp_gear_check counts. So the active group is only
# meaningful when it is ONE fieldable comp. Before 2026-08-09 the user had two comps'
# sets active at once (Monk Main + Bard Main alongside four Sleeper sets), which is
# why single-copy pieces looked permanently contested.

def comp_gear_map(conn, comp_id):
    """Which gear set this comp fields per member, plus the alternatives.

    A set is a ROLE LOADOUT, not a toon's property: "Rogue Main" is the best rogue
    kit you own, and which rogue wears it depends on the comp — Gavriel in
    WAR/CLR/BRD/MNK/ROG/BST, while Zyrak wears Sleeper Rogue 3 in the Sleeper group.
    So candidates are matched by CLASS, not by assigned_char_id. Matching on the
    assignment (the first cut of this, 2026-08-09) could never offer Rogue Main to
    Gavriel, which is exactly why he read as "no gear set".

    Returns one row per occupied slot: the stored choice, else a PROPOSAL (the set
    already pinned to this toon wins; failing that a lone class match).
    `needs_choice` marks a slot the caller must ask about rather than guess.
    """
    sets = [s for s in list_sets(conn) if s["items"]]
    by_class = {}
    for s in sets:
        by_class.setdefault((s["class_name"] or "").lower(), []).append(s)
    # WHO ELSE FIELDS EACH LOADOUT, derived from the mappings — no hand-typed label.
    # A set is shared on purpose (Rogue Main goes on Gavriel in one comp and Zyrak in
    # another), so this never hides anything; it just says where a set is already in
    # use so "why is this in my list?" is answered on the row itself.
    used = {}
    for r2 in conn.execute(
            "SELECT s.gear_set_id AS sid, s.composition_id AS cid, c.name AS comp,"
            "       ch.name AS toon FROM composition_slots s"
            " JOIN compositions c ON c.id = s.composition_id"
            " LEFT JOIN characters ch ON ch.id = s.character_id"
            " WHERE s.gear_set_id IS NOT NULL"):
        used.setdefault(r2["sid"], []).append(dict(r2))
    out = []
    for r in conn.execute(
            "SELECT s.slot_index, s.character_id, s.gear_set_id, c.name,"
            "       c.class_name"
            " FROM composition_slots s LEFT JOIN characters c ON c.id = s.character_id"
            " WHERE s.composition_id=? AND s.character_id IS NOT NULL"
            " ORDER BY s.slot_index", (comp_id,)):
        # Every loadout this toon's CLASS can wear. Nothing else filters it: a set is
        # shared freely across comps, so the only wrong answer is hiding one.
        cands = list(by_class.get((r["class_name"] or "").lower(), []))
        cands.sort(key=lambda s: (s["id"] != r["gear_set_id"],          # this comp's pick
                                  s["assigned_char_id"] != r["character_id"],
                                  not s["active"], s["name"].lower()))
        stored = next((s for s in cands if s["id"] == r["gear_set_id"]), None)
        pinned = [s for s in cands if s["assigned_char_id"] == r["character_id"]]
        proposed, needs = stored, False
        if proposed is None:
            if len(cands) <= 1:
                # 0 candidates = no set of this class exists; reported as no_set, it
                # must not block the apply. 1 = no alternative to weigh up.
                proposed = cands[0] if cands else None
            else:
                # Several sets fit this class: ALWAYS ask. The one already pinned to
                # this toon is offered as the default so confirming is one click, but
                # it is never applied unseen — "it was already active/pinned" is not
                # evidence of intent, it is how Zyrak ended up on Rogue Main inside
                # the Sleeper group.
                live = [s for s in pinned if s["active"]]
                proposed = (pinned[0] if len(pinned) == 1 else
                            live[0] if len(live) == 1 else None)
                needs = True
        out.append({
            "slot_index": r["slot_index"], "character_id": r["character_id"],
            "character": r["name"], "class_name": r["class_name"],
            "bench": r["slot_index"] >= 6,
            "gear_set_id": proposed["id"] if proposed else None,
            "gear_set_name": proposed["name"] if proposed else "",
            "stored": stored is not None,
            # exactly what apply_comp_gear refuses on — one definition, so the panel
            # can never show "fine" for a row the Apply button then rejects
            "needs_choice": needs,
            "candidates": [{"id": s["id"], "name": s["name"], "pieces": len(s["items"]),
                            "active": bool(s["active"]),
                            # who wears it TODAY: picking a set pinned to someone else
                            # moves it, so the picker has to say so out loud
                            "assigned_to": s["assigned_name"] or "",
                            # comps already fielding it, and — separately — whether
                            # THIS comp has it on someone else, which is the only case
                            # that is actually unusable (one loadout, one wearer).
                            "used_by": sorted({u["comp"] for u in used.get(s["id"], [])
                                               if u["cid"] != comp_id}),
                            "used_here": next((u["toon"] for u in used.get(s["id"], [])
                                               if u["cid"] == comp_id
                                               and u["toon"] != r["name"]), ""),
                            "moves": bool(s["assigned_char_id"]
                                          and s["assigned_char_id"] != r["character_id"])}
                           for s in cands],
        })
    return out


def _infer_slots(items, db=None):
    """Give a slot to any pick that arrives without one, from the item's equip mask.

    The Macro Builder import posts `slot: it.slot || ""` and its old storage does not
    always carry one. A slotless row is INVISIBLE in the set editor (which draws one
    row per known slot) yet still claims its item, so the user edited Sleeper Monk 2's
    Neck and the comp check went on reporting the pick he had replaced (2026-08-09).
    Anything still unplaceable is left blank and shown by the editor as "no slot" —
    guessing a wrong slot would be worse than saying so.
    """
    blanks = [it for it in items if not (it.get("slot") or "").strip()]
    if not blanks:
        return {}
    taken = set()
    for it in items:
        slot = (it.get("slot") or "").strip()
        if slot:
            taken.add((slot, int(it.get("slot_index") or 0)))
    db = db if db is not None else gearmod.load_item_db()
    out = {}
    for it in blanks:
        mask = int((db.get(int(it.get("item_id") or 0)) or {}).get("slots", 0) or 0)
        for name, bits in SLOT_BITS:
            if not (mask & bits):
                continue
            for k in range(2 if name in PAIRED_SLOTS else 1):
                if (name, k) not in taken:
                    taken.add((name, k))
                    out[id(it)] = name
                    it["slot_index"] = k
                    break
            if id(it) in out:
                break
    return out


def set_comp_choice(conn, comp_id, character_id, gear_set_id):
    """Record which loadout a comp fields for one member, WITHOUT activating anything.

    Separate from apply_comp_gear because choosing is per-slot and applying is
    all-or-nothing: you should be able to answer one row at a time and press Apply
    when the six are settled. Activating a single set on its own is what produced
    the two-comps-live-at-once state, so no UI should offer that any more.
    """
    row = conn.execute("SELECT 1 FROM composition_slots WHERE composition_id=?"
                       " AND character_id=?", (comp_id, character_id)).fetchone()
    if row is None:
        return 404, {"ok": False, "error": "That character is not in this composition."}
    conn.execute("UPDATE composition_slots SET gear_set_id=?"
                 " WHERE composition_id=? AND character_id=?",
                 (int(gear_set_id) if gear_set_id else None, comp_id, character_id))
    conn.commit()
    return 200, {"ok": True, "mapping": comp_gear_map(conn, comp_id)}


def clone_set(conn, set_id, name=None):
    """Duplicate a loadout, pieces and all — for a VARIANT, not for sharing.

    Sharing needs no copy at all: map the same set in both comps and it is fielded by
    whichever toon each one seats. Use this only when a comp wants a divergent version
    ("Monk Main" -> "Monk Main (tank)"), knowing the copy is maintained separately.
    """
    src = conn.execute("SELECT * FROM gear_sets WHERE id=?", (set_id,)).fetchone()
    if src is None:
        return 404, {"ok": False, "error": "No such gear set."}
    base = (name or "").strip() or "%s (copy)" % src["name"]
    candidate, n = base, 2
    while conn.execute("SELECT 1 FROM gear_sets WHERE name=?", (candidate,)).fetchone():
        candidate, n = "%s %d" % (base, n), n + 1      # name is UNIQUE
    now = int(time.time())
    cur = conn.execute(
        "INSERT INTO gear_sets(name, class_name, source_char_id, assigned_char_id,"
        " active, notes, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (candidate, src["class_name"], src["source_char_id"], None, 0,
         src["notes"], now, now))
    new_id = cur.lastrowid
    conn.execute(
        "INSERT INTO gear_set_items(set_id, slot, slot_index, item_id, item_name)"
        " SELECT ?, slot, slot_index, item_id, item_name FROM gear_set_items"
        " WHERE set_id=?", (new_id, set_id))
    conn.commit()
    pieces = conn.execute("SELECT COUNT(*) FROM gear_set_items WHERE set_id=?",
                          (new_id,)).fetchone()[0]
    # active=0 and unassigned on purpose: a fresh copy claims nothing until the comp
    # that owns it is applied, so cloning can never change what is being fielded.
    return 200, {"ok": True, "id": new_id, "name": candidate, "pieces": pieces}


def apply_comp_gear(conn, comp_id, choices=None):
    """Make this comp's sets the ONLY active ones.

    choices: {character_id: gear_set_id} from the picker, stored on the comp slot
    so the next Apply needs no input. Bench slots (6+) are mapped but never
    activated — a bench toon is a swap candidate, not someone you are gearing.

    Deactivating is not destructive: it clears no picks, and re-applying the other
    comp puts it straight back. That is the whole reason this is a flag flip rather
    than anything that touches gear_set_items.
    """
    row = conn.execute("SELECT name FROM compositions WHERE id=?", (comp_id,)).fetchone()
    if row is None:
        return 404, {"ok": False, "error": "No such composition."}
    for cid, sid in (choices or {}).items():
        conn.execute("UPDATE composition_slots SET gear_set_id=?"
                     " WHERE composition_id=? AND character_id=?",
                     (int(sid) if sid else None, comp_id, int(cid)))
    mapping = comp_gear_map(conn, comp_id)
    unresolved = [m["character"] for m in mapping
                  if not m["bench"] and m["needs_choice"]]
    if unresolved:
        return 409, {"ok": False, "needs_choice": True, "mapping": mapping,
                     "error": "Pick a gear set for: " + ", ".join(unresolved)}
    # One loadout cannot be worn by two toons at once — it would claim every piece
    # twice and the move plan would promise the same physical item to both.
    seen = {}
    for m in mapping:
        if m["bench"] or not m["gear_set_id"]:
            continue
        if m["gear_set_id"] in seen:
            return 409, {"ok": False, "needs_choice": True, "mapping": mapping,
                         "error": "%s is picked for both %s and %s — one loadout per "
                                  "toon." % (m["gear_set_name"],
                                             seen[m["gear_set_id"]], m["character"])}
        seen[m["gear_set_id"]] = m["character"]
    # persist whatever we proposed, so Apply is a one-click repeat from now on
    for m in mapping:
        if m["gear_set_id"] and not m["stored"]:
            conn.execute("UPDATE composition_slots SET gear_set_id=?"
                         " WHERE composition_id=? AND character_id=?",
                         (m["gear_set_id"], comp_id, m["character_id"]))
    keep = {m["gear_set_id"] for m in mapping if m["gear_set_id"] and not m["bench"]}
    was = {r["id"]: (r["name"], bool(r["active"]), r["assigned_char_id"])
           for r in conn.execute(
               "SELECT id, name, active, assigned_char_id FROM gear_sets")}
    # RETARGET. assigned_char_id is "who wears this loadout now", not ownership, so
    # fielding a comp points its sets at that comp's toons: Rogue Main goes to Gavriel
    # in WAR/CLR/BRD/MNK/ROG/BST and back to Zyrak in whatever comp maps it to him.
    # Everything downstream (build_plans, comp_gear_check) routes off this column, so
    # retargeting here is what makes the move plan address the right character.
    moved = []
    for m in mapping:
        if m["bench"] or not m["gear_set_id"]:
            continue
        prev = was.get(m["gear_set_id"], (None, None, None))[2]
        if prev != m["character_id"]:
            conn.execute("UPDATE gear_sets SET assigned_char_id=? WHERE id=?",
                         (m["character_id"], m["gear_set_id"]))
            moved.append({"set": m["gear_set_name"], "to": m["character"],
                          "from": (conn.execute("SELECT name FROM characters WHERE id=?",
                                                (prev,)).fetchone() or [""])[0]
                                  if prev else ""})
    conn.execute("UPDATE gear_sets SET active = CASE WHEN id IN (%s) THEN 1 ELSE 0 END"
                 % (",".join("?" * len(keep)) or "NULL"), list(keep))
    conn.commit()
    activated = sorted(was[i][0] for i in keep if i in was and not was[i][1])
    deactivated = sorted(n for i, (n, a, _) in was.items() if a and i not in keep)
    no_set = [m["character"] for m in mapping
              if not m["bench"] and not m["gear_set_id"]]
    return 200, {"ok": True, "comp": row["name"], "active": len(keep),
                 "activated": activated, "deactivated": deactivated,
                 "retargeted": moved, "no_set": no_set, "mapping": mapping}


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


def save_set(conn, payload, set_id=None, eq_dir=None, db=None):
    """Create/update a set. payload: name, class_name, source_char_id,
    assigned_char_id, active, notes, items:[{item_id,item_name,slot,slot_index}].
    Items are replaced only when the payload carries an "items" key.

    A save NEVER touches another set's rows. A set records what you want that toon
    to wear, so the same piece may appear in as many sets as you like — contention
    is REPORTED, never resolved by deletion. Returns payload["contested"]: the
    items active sets collectively want more copies of than the roster owns, and
    who else wants them. Needs eq_dir for owned counts; without it, no report.

    (Until 2026-08-09 this ran _steal_overclaims, which DELETED the shortfall from
    other active sets. It silently destroyed Monk Main's Arms/Back/Head/Ear picks.
    The routing layer already degrades gracefully on contention — it emits a
    "reserved" row naming the owners — so the deletion bought nothing and cost
    The user's stated intent.)"""
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
        filled = _infer_slots(payload.get("items") or [], db)
        for it in payload.get("items") or []:
            iid = int(it.get("item_id") or 0)
            if iid <= 0:
                continue
            slot = (it.get("slot") or "").strip() or filled.get(id(it), "")
            k = it.get("slot_index")
            if k is None:
                k = seen.get(slot, 0)
            seen[slot] = max(seen.get(slot, 0), int(k) + 1)
            conn.execute(
                "INSERT OR REPLACE INTO gear_set_items(set_id, slot, slot_index,"
                " item_id, item_name) VALUES (?,?,?,?,?)",
                (set_id, slot, int(k), iid, it.get("item_name") or ""))
    contested = _contested(conn, set_id, eq_dir) if "items" in payload and eq_dir else []
    conn.commit()
    return 200, {"ok": True, "id": set_id, "contested": contested}


def _contested(conn, set_id, eq_dir):
    """Items THIS set picks that all active sets together want more copies of than
    the roster owns. Read-only — nothing is deleted or reassigned. Returns
    [{"item", "want", "owned", "other_sets"}], worst shortfall first."""
    holdings = _holdings(_load_world(conn, eq_dir))
    owned = {iid: sum(e["count"] for e in entries) for iid, entries in holdings.items()}
    mine = {}
    for r in conn.execute("SELECT item_id, item_name FROM gear_set_items WHERE set_id=?",
                          (set_id,)):
        mine.setdefault(r["item_id"], r["item_name"])
    out = []
    for iid, name in mine.items():
        rows = conn.execute("""
            SELECT g.id AS gid, g.name AS set_name FROM gear_set_items gi
            JOIN gear_sets g ON g.id = gi.set_id
            WHERE gi.item_id=? AND g.active=1""", (iid,)).fetchall()
        have = owned.get(iid, 0)
        if len(rows) <= have:
            continue                # spares to go round — nobody is short
        others = sorted({r["set_name"] for r in rows if r["gid"] != set_id})
        out.append({"item": name, "want": len(rows), "owned": have,
                    "other_sets": others})
    out.sort(key=lambda c: (c["owned"] - c["want"], c["item"]))
    return out


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
    # CARRY THE VIRTUAL SLOTS FORWARD. A same-name save overwrites the existing set and
    # save_set DELETEs every row before re-inserting, so without this a re-snapshot would
    # silently drop the Avatar weapon claim - the dump has no "Avatar" slot to restore it
    # from. Re-snapshotting is routine (any gear change), so the loss would be invisible
    # until the router quietly gave the weapon to somebody else. Same shape as the
    # 2026-07-02 override-corruption bug: a write path that destroys data it never read.
    prior = conn.execute(
        "SELECT i.slot, i.slot_index, i.item_id, i.item_name FROM gear_set_items i"
        " JOIN gear_sets s ON s.id = i.set_id WHERE s.name = ?", (name,)).fetchall()
    for r in prior:
        if r["slot"] in EXTRA_SLOT_NAMES:
            items.append({"item_id": r["item_id"], "item_name": r["item_name"],
                          "slot": r["slot"], "slot_index": r["slot_index"]})
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

def build_plans(conn, eq_dir, login_ids=None, db=None, stale_h=None,
                focus_ids=None, world=None):
    """Route every active set's pieces. All active sets are planned TOGETHER so the
    one-copy-one-promise reservation math holds across sets (the '33 rows for 24
    items' bug class). login_ids = char ids currently online (the login set) —
    holders in it get the trade route (no extra login needed).

    focus_ids: narrow what comes BACK to the sets targeting these toons (a comp).
    Every active set is still PLANNED — focus only filters the result — otherwise a
    comp would read clean while quietly taking a piece another set is promised.

    stale_h: dumps older than this mark their rows `stale_source`. The planner still
    routes them — it does NOT refuse — because a stale dump is usually still right;
    it just stops being something to trust silently. Same window as the Harvest tab.
    """
    db = db or gearmod.load_item_db()
    stale_h = int(stale_h) if stale_h else harvestmod.DEFAULT_STALE_H
    world = world or _load_world(conn, eq_dir)
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

    # Name the rival on every "reserved" row. _route_item can only list holders that
    # were routed THROUGH it, and the commonest rival wins the copy on the worn fast
    # path (line ~630), which never records a claim — so the note came out as a bare
    # "every copy is promised elsewhere". Since 2026-08-09 contention is the ONLY
    # signal that a set can't be fielded (saves no longer delete the loser's pick),
    # an anonymous one is not good enough.
    want = {}
    for s in sets:
        for it in s["items"]:
            want.setdefault(it["item_id"], []).append(s["name"])
    for p in plans:
        for r in p["rows"]:
            if r["status"] != "reserved":
                continue
            rivals = sorted({n for n in want.get(r["item_id"], []) if n != p["name"]})
            if rivals:
                r["note"] = ("every copy is promised elsewhere: "
                             + ", ".join(rivals[:4])
                             + (" +%d more" % (len(rivals) - 4) if len(rivals) > 4 else ""))

    focus = {int(x) for x in (focus_ids or []) if x}
    if focus:
        # Work order + totals are recomputed over the focused sets only: "who do I
        # log in to gear THIS comp", not the whole roster.
        shown = [p for p in plans if p.get("target") and p["target"]["id"] in focus]
        return {"plans": shown, "workorder": _workorder(shown, world),
                "summary": _summary(shown, stale_h),
                "focus": {"char_ids": sorted(focus), "planned_sets": len(plans),
                          "shown_sets": len(shown)}}
    return {"plans": plans, "workorder": _workorder(plans, world),
            "summary": _summary(plans, stale_h)}


def _age_of(row, mtime, now, stale_h):
    """Stamp the row with the age of the dump this routing decision came from."""
    row["source_age_h"] = None if mtime is None else (now - mtime) // 3600
    row["stale_source"] = (row["source_age_h"] is not None
                           and row["source_age_h"] >= stale_h)
    return row


def _proc_covered(it, s, db):
    """Other rows of this set that already carry the proc this virtual slot exists for.

    The Avatar slot means "keep a weapon that procs Avatar". If another slot of the
    same set already holds one, the slot is satisfied — including the case where it
    names the very same item. Returns [] for ordinary slots.
    """
    slot_proc = EXTRA_SLOT_PROCS.get(it["slot"])
    if not slot_proc:
        return []
    out = []
    for i in s["items"]:
        if i["slot"] == it["slot"] and i["slot_index"] == it["slot_index"]:
            continue
        other = db.get(i["item_id"]) or {}
        if int(other.get("proceffect", 0) or 0) == slot_proc:
            out.append(i)
    return out


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

    # PROC-DEFINED slot (Avatar) already covered by another row of this same set. Past
    # this point every path SOURCES FROM ANOTHER TOON, and that is the one thing a
    # redundant Avatar claim must never do: Beastlord Main names the same Primal Velium
    # Brawl Stick in both 2-Hander and Avatar, Scavo's own copy satisfied the first
    # row, and the second told the user to log in Rokhan and mail his copy across for
    # nothing (2026-08-10). Deliberately BELOW the have/worn checks — a toon that
    # already holds two copies keeps both claimed ("effectivly tieing up 3 weapons per
    # toon", the user 2026-08-08); the rule is only "don't go shopping for a spare you
    # don't need". It used to live in the "nobody owns one" branch, so it fired only
    # when the item was unobtainable — the moment a sibling had one, routing won.
    covers = _proc_covered(it, s, db)
    if covers:
        row["status"] = "covered"
        row["note"] = ("already covered — %s procs this too; clear this slot"
                       % ", ".join("%s (%s)" % (i["item_name"], i["slot"])
                                   for i in covers[:3]))
        return row

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

    # ACCOUNT-BOUND slots (Mount / WW Clicky). A claim reward or mount belongs to the
    # account, so a copy on another account is not a source at all — it cannot be
    # traded, parcelled or banked across. Cutting those candidates here (rather than
    # letting the ladder pick one and blocking later) is what makes the note useful:
    # otherwise the row read "NO TRADE — Gunkrat has one", naming a toon on an
    # account that physically cannot hand it over.
    acct_bound = it["slot"] in ACCOUNT_BOUND_SLOTS
    if acct_bound:
        tacct = target["account_id"]
        cands = [e for e in cands
                 if tacct is not None and e["account_id"] == tacct]
        if not cands:
            row["status"] = "missing"
            row["note"] = ("account-bound — nobody on %s has one"
                           % (target["account_alias"] or "this account"))
            return row

    # Account-bound claim gear (mounts, claim clickies) is NO TRADE, so it can never be
    # traded or parcelled — but it CAN move through the account's SHARED BANK. The user
    # 2026-08-09: "that item cant be parceled but it can be put in the shared bank."
    # cands are already cut to this account, so letting the normal ladder run yields
    # exactly the right instruction — grab it out of the shared bank, or log the
    # sibling holding it and drop it in there first — instead of a dead "NO TRADE"
    # that named a holder and then told you nothing could be done with them.
    if not movable and not acct_bound:
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
            owned = sum(e["count"] for e in holdings.get(iid, []))
            if owned > 0:
                # You DO own it. Every copy is on the TARGET and an earlier row of THIS
                # SAME SET already consumed it, so cands came back empty and the old
                # message said "nobody on the roster has one" — flatly wrong, and it
                # sent the user hunting for an item sitting in Scavo's own bags
                # (2026-08-09). Scavo's Primal Velium Brawl Stick is itemtype 4 AND
                # procs Avatar, so it is a legitimate pick for the 2-Hander slot or the
                # Avatar slot — just not both at once on one copy.
                want = sum(1 for i in s["items"] if i["item_id"] == iid)
                slots = sorted({i["slot"] for i in s["items"] if i["item_id"] == iid})
                row["status"] = "shortfall"
                row["note"] = ("this set claims it %d× (%s) but you only own %d — free a "
                               "slot or find another copy"
                               % (want, ", ".join(slots), owned))
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
    first plan mirrored at top level for older builds. Returns None if there is
    nothing to do in game.

    Each plan carries TWO row lists:
      moves — swap/trade/parcel only. Unchanged shape, unchanged contents: this is
              what old TrixBox/mailgear builds read, and what the parcel filter is
              counted from. Never widen it.
      rows  — EVERY routed piece, `worn`/`have`/`grab` included, each stamped with
              status/bucket/slotIndex. New mailgear reads this and falls back to
              `moves` when an older export has no `rows`.

    Why `rows` had to exist (reported 2026-08-09: "doesnt equip items that are already
    on the toon, sitting in their bag"): a piece already in the target's own bags
    routes as `have` — the plan literally says "just equip it" — but `have` is not
    in MOVE_STATUSES, so it never reached the Lua and /mailgear equip could not see
    it. Same for `grab` (the target's own shared bank). Exporting only the moves
    silently made the cheapest half of every plan un-executable.

    `worn` rows carry no work, but they are exported anyway: they are how the equip
    step knows a paired slot is ALREADY holding a piece this set wants, so it must
    not be overwritten (see doEquipItem's slot reservation)."""
    plans = []
    for p in plan_result["plans"]:
        if not p.get("target"):
            continue
        rows = [r for r in p["rows"] if r["status"] in EXPORT_STATUSES]
        moves = [r for r in p["rows"] if r["status"] in MOVE_STATUSES]
        if any(r["status"] in ACTIONABLE_STATUSES for r in rows):
            plans.append({"name": p["name"], "target": p["target"]["name"],
                          "moves": moves, "rows": rows})
    if not plans:
        return None

    def move_lua(r, target, indent):
        # from = the OTHER toon you take it from. Deliberately empty for worn/have/
        # grab: nobody hands those over, they are already on the target, so mailgear's
        # mineFrom() must not put them in a holder's dequeue queue. status/bucket are
        # what the equip side branches on instead.
        return ("%s{ id = %d, name = %s, from = %s, to = %s, slot = %s, slotIndex = %d, "
                "status = %s, bucket = %s, fromLoc = %s, attuneRisk = %s },"
                % (indent, r["item_id"] or 0, _lua_str(r["item_name"]),
                   _lua_str(r["holder"]), _lua_str(target), _lua_str(r["slot"]),
                   int(r.get("slot_index") or 0),
                   _lua_str(r["status"]), _lua_str(r.get("bucket") or ""),
                   _lua_str(r["from_loc"]),
                   "true" if r["attune_risk"] else "false"))

    plan_blocks = []
    for p in plans:
        moves = "\n".join(move_lua(r, p["target"], "      ") for r in p["moves"])
        rows = "\n".join(move_lua(r, p["target"], "      ") for r in p["rows"])
        plan_blocks.append(
            "    { name = %s, set = %s, target = %s,\n"
            "      moves = {\n%s\n      },\n"
            "      rows = {\n%s\n      } }," %
            (_lua_str(p["name"]), _lua_str(p["name"]), _lua_str(p["target"]),
             moves, rows))
    first = plans[0]
    first_moves = "\n".join(move_lua(r, first["target"], "    ") for r in first["moves"])
    header = ("-- Gear plans - generated by EQ Forge My Characters (Gear Sets)\n" +
              "\n".join("--   %s -> %s (%d move(s), %d already on the target)"
                        % (p["name"], p["target"], len(p["moves"]),
                           sum(1 for r in p["rows"] if r["status"] in ("have", "grab")))
                        for p in plans) + "\n" +
              "-- In game (mailgear): /mailgear plans, /mailgear dequip (on a holder),\n"
              "-- /mailgear getbank (at a banker), /mailgear equip (on the target).\n"
              "-- Dry-run until /mailgear live on.  TrixBox users: /trix plans, /trix sendgear.\n")
    text = (header + "return {\n  plans = {\n" + "\n".join(plan_blocks) + "\n  },\n"
            "  -- back-compat: first plan mirrored for older TrixBox builds\n"
            "  name = %s, set = %s, target = %s,\n  moves = {\n%s\n  },\n}\n" %
            (_lua_str(first["name"]), _lua_str(first["name"]),
             _lua_str(first["target"]), first_moves))
    def _need(moves):
        """holder -> item id -> HOW MANY copies that holder owes this plan.
        The parcel filter used to be a bare id set, which silently over-sent two ways:
        a holder with 2 spare copies of a wanted item sent BOTH (reported 2026-08-08:
        Damaris queued two Mask of War when the work order said one), and any holder
        who logged in matched the WHOLE plan's ids, including rows assigned to a
        different holder. Counting per holder is what makes the parcel window agree
        with the work order."""
        need = {}
        for r in moves:
            iid, holder = r["item_id"], r["holder"]
            if not iid or not holder:
                continue
            need.setdefault(holder, {})
            need[holder][iid] = need[holder].get(iid, 0) + 1
        return need

    return {"text": text,
            "plans": [{"name": p["name"], "target": p["target"],
                       "count": len(p["moves"]),
                       "ids": sorted({r["item_id"] for r in p["moves"] if r["item_id"]}),
                       "need": _need(p["moves"])}
                      for p in plans]}


def build_parcel_source_lua(plans_meta):
    """Parcel-tool source filters for the exported plans (see forge.js version)."""
    blocks = []
    for p in plans_meta:
        need = p.get("need") or {}
        # holder -> { [itemid] = copies owed }. Emitted per holder so the filter can
        # scope itself to whoever is logged in.
        need_rows = "\n".join(
            "                [%s] = { %s },"
            % (_lua_str(holder), ", ".join("[%d]=%d" % (i, n)
                                           for i, n in sorted(items.items())))
            for holder, items in sorted(need.items()))
        blocks.append(
            "    {\n"
            "        name = %s,\n"
            # The plan already knows who it is for, so the parcel tool fills "Send To"
            # from this the moment the source is picked. reported 2026-08-08: "i cant tell
            # you how many times i select the gearplan and i forgot to change the send
            # to" - retyping a name you already told the app is a mis-SEND waiting to
            # happen, not a typo, and gear that goes to the wrong toon has to be mailed
            # back by hand.
            "        target = %s,\n"
            "        filter = (function()\n"
            "            local mq = require('mq')\n"
            "            local need = {\n%s\n            }\n"
            "            -- getFilteredItems() re-runs this filter from scratch on every\n"
            "            -- scan (source pick, Recheck) but gives us no start-of-scan hook,\n"
            "            -- so the per-scan tally is reset by a TIME GAP: a whole scan runs\n"
            "            -- inside one frame, while any two scans are far more than 500ms\n"
            "            -- apart. Without the reset the counters stay exhausted and the\n"
            "            -- second scan would show nothing.\n"
            "            local taken, lastT = {}, -99999\n"
            "            local function nowMs()\n"
            "                if mq and mq.gettime then return mq.gettime() end\n"
            "                return (os.clock() or 0) * 1000\n"
            "            end\n"
            "            return function(item)\n"
            "                local t = nowMs()\n"
            "                if (t - lastT) > 500 then taken = {} end\n"
            "                lastT = t\n"
            "                local me = mq.TLO.Me.CleanName() or ''\n"
            "                local mine = need[me]\n"
            "                if not mine then return false end\n"
            "                local id = item.ID() or 0\n"
            "                local want = mine[id] or 0\n"
            "                if want == 0 then return false end\n"
            "                local got = taken[id] or 0\n"
            "                if got >= want then return false end\n"
            "                taken[id] = got + 1\n"
            "                return true\n"
            "            end\n"
            "        end)(),\n"
            "    }," % (_lua_str("Gear Plan: %s -> %s (%d)" %
                                 (p["name"], p["target"], len(p["ids"]))),
                        _lua_str(p["target"]), need_rows))
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
        if dbi.get("itemtype", 0) == AUG_ITEMTYPE:
            continue
        # A slotless item is normally junk to a gear set — EXCEPT for the carried
        # slots, whose whole point is gear with no slot of its own. Before this,
        # `if not mask: continue` silently made Trinket of the Far Frozen Wastes
        # (slots=0) unpickable no matter what the editor offered.
        carried = [s for s, ok in CARRIED_SLOT_MATCH.items() if ok(dbi)]
        if not mask and not carried:
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
            "itemtype": dbi.get("itemtype", -1),
            "proceffect": dbi.get("proceffect", 0) or 0,
            "carried": carried,
            # Which accounts hold a copy — the account-bound slots filter on this so
            # the editor never offers a mount that lives on an account the target
            # cannot reach.
            "account_ids": sorted({e["account_id"] for e in entries if e["account_id"]}),
            # Full stat block + named effects so the editor can show a real item card
            # and stat deltas instead of a truncated dropdown label. Sparse (zeros
            # dropped) because this ships up to 21 slots x MAX_CANDIDATES rows.
            "stats": {k: dbi[k] for k in gearmod.STAT_KEYS if dbi.get(k)},
            "haste": dbi.get("haste", 0),
            "effects": _item_effects(dbi),
        }

    out = []
    for slot, sbits in SLOT_BITS + EXTRA_SLOTS:
        match = CARRIED_SLOT_MATCH.get(slot)
        proc = EXTRA_SLOT_PROCS.get(slot)
        if match:
            # Carried slots ignore the bitmask entirely (that is the point) and are
            # ranked by NAME: score_item is a stat sum, and a mount or a port clicky
            # has no stats, so ranking them by score would be arbitrary noise.
            items = [c for c in per_item.values() if slot in c["carried"]]
            items.sort(key=lambda c: c["name"])
        elif proc:
            # The proc IS the slot. Mask ignored on purpose — Avatar weapons span
            # Primary, Primary|Secondary and Range.
            items = [c for c in per_item.values() if c["proceffect"] == proc]
            items.sort(key=lambda c: (-c["score"], -c["ac"], -c["hp"], c["name"]))
        else:
            items = [c for c in per_item.values() if c["slots_mask"] & sbits]
            types = EXTRA_SLOT_ITEMTYPES.get(slot)
            if types:
                items = [c for c in items if c["itemtype"] in types]
            items.sort(key=lambda c: (-c["score"], -c["ac"], -c["hp"], c["name"]))
        if slot in ACCOUNT_BOUND_SLOTS and tacct:
            items = [c for c in items if tacct in c["account_ids"]]
        out.append({"slot": slot, "paired": slot in PAIRED_SLOTS,
                    "extra": slot in EXTRA_SLOT_NAMES,
                    "carried": bool(match),
                    "account_bound": slot in ACCOUNT_BOUND_SLOTS,
                    "items": [{k: v for k, v in c.items()
                               if k not in ("slots_mask", "itemtype", "carried",
                                            "account_ids", "proceffect")}
                              for c in items[:MAX_CANDIDATES]]})
    return {"slots": out, "class_name": class_name or "",
            # "" = no target; race name = filtering; None = target has no race
            # on file (UI warns: race-locked gear is NOT being filtered out)
            "race_filter": ((tchar.get("race") or None) if tchar else "")}


# --- comp gear check: can the roster fill every live member's set at once? -----

def comp_gear_check(conn, eq_dir, char_ids, world=None):
    """For the comp's live toons: does everyone have an active set, and are there
    enough physical copies across the WHOLE roster to fill all of them at once?
    Pure counting (dumps only, no item DB) so it's fast enough to run on every
    comp edit. A toon's newest active assigned set is 'their' set."""
    char_ids = [int(x) for x in (char_ids or []) if x]
    world = world or _load_world(conn, eq_dir)
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


# --- comp readiness: one row per live member, straight off the routed plan -----

def _expected_slot_rows():
    """Every slot row a complete set covers (paired slots twice), minus the ones
    that are legitimately empty in this era."""
    rows = []
    for slot, _ in SLOT_BITS:
        if slot in GAP_SKIP_SLOTS:
            continue
        rows.append((slot, 0))
        if slot in PAIRED_SLOTS:
            rows.append((slot, 1))
    return rows


# Worst first — the order the UI sorts and colours by.
READINESS_STATES = ("noset", "blocked", "moves", "onhand", "ready")


def comp_readiness(conn, eq_dir, char_ids, login_ids=None, stale_h=None, db=None):
    """'Can I field this comp right now?' — one row per live member, derived from
    the SAME routed plan the Move Plan renders, so the two can never disagree.

    Per member: their active set, how much of it is already on their body, what is
    still inbound (and by which route), what is blocked outright, and which slots
    the set does not cover at all. States, worst first: no set assigned → blocked
    (a piece nobody can deliver) → moves pending → on hand but not worn → ready.
    """
    char_ids = [int(x) for x in (char_ids or []) if x]
    stale_h = int(stale_h) if stale_h else harvestmod.DEFAULT_STALE_H
    world = _load_world(conn, eq_dir)
    plan = build_plans(conn, eq_dir, login_ids, db=db, stale_h=stale_h,
                       focus_ids=char_ids, world=world)
    check = comp_gear_check(conn, eq_dir, char_ids, world=world)
    by_target = {p["target"]["id"]: p for p in plan["plans"] if p.get("target")}
    expected = _expected_slot_rows()
    now = int(time.time())
    # Sets a member owns that the plan ignored (retired, or emptied). Without this a
    # toon with a perfectly good retired set reads "no set", and the obvious fix —
    # snapshot them — quietly builds a duplicate instead of ticking Active.
    shelved = {}
    for s in list_sets(conn):
        tgt = s["assigned_char_id"] or s["source_char_id"]
        if tgt and not (s["active"] and s["items"]):
            shelved.setdefault(tgt, []).append(
                {"id": s["id"], "name": s["name"], "pieces": len(s["items"]),
                 "active": bool(s["active"])})

    members = []
    for cid in char_ids:
        c = world["by_id"].get(cid)
        if c is None:
            continue
        m = {"char_id": cid, "name": c["name"], "class_name": c["class_name"] or "",
             "account_alias": c["account_alias"] or "",
             "dumped": cid in world["inv"],
             "dump_age_h": ((now - world["mtimes"][cid]) // 3600
                            if cid in world["mtimes"] else None),
             "set_id": None, "set_name": "", "pieces": 0, "gaps": [],
             "equipped": 0, "on_hand": 0, "incoming": {}, "blocked": [],
             "stale_moves": 0, "state": "noset",
             "shelved_sets": shelved.get(cid, [])}
        p = by_target.get(cid)
        if p is None:
            # "retired" = they have one, it just isn't switched on. Different fix.
            m["reason"] = "retired" if m["shelved_sets"] else "none"
            members.append(m)
            continue
        m["set_id"], m["set_name"] = p["set_id"], p["name"]
        m["pieces"] = len(p["rows"])
        filled = {(r["slot"], r["slot_index"]) for r in p["rows"]}
        m["gaps"] = [s if i == 0 else s + " 2"
                     for s, i in expected if (s, i) not in filled]
        for r in p["rows"]:
            st = r["status"]
            if st == "worn":
                m["equipped"] += 1
            elif st in SATISFIED_STATUSES:      # in their bags / own shared bank
                m["on_hand"] += 1
            elif st in MOVE_STATUSES:
                m["incoming"][st] = m["incoming"].get(st, 0) + 1
                if r["stale_source"]:
                    m["stale_moves"] += 1
            else:
                m["blocked"].append({"slot": r["slot"], "item": r["item_name"],
                                     "status": st, "note": r["note"]})
        m["moves"] = sum(m["incoming"].values())
        m["state"] = ("blocked" if m["blocked"] else
                      "moves" if m["moves"] else
                      "onhand" if m["on_hand"] else "ready")
        members.append(m)

    states = {k: 0 for k in READINESS_STATES}
    for m in members:
        states[m["state"]] += 1
    return {"members": members, "plan": plan, "check": check,
            "summary": {"members": len(members), "states": states,
                        "logins": len(plan["workorder"]), "stale_h": stale_h,
                        "ready": states["ready"] == len(members) and bool(members)}}


# --- light fit (for the sets list; no item DB needed) --------------------------

def fit_counts(conn, eq_dir):
    """set_id -> {"worn": n, "present": n, "total": n, "worn_total": n} against the
    target's dump. worn_total excludes virtual slots (Avatar): that weapon lives in the
    bags on purpose, so counting it as a missing worn piece would leave every monk set
    reading "18/19 worn" forever and make a complete set look broken."""
    world = _load_world(conn, eq_dir)
    out = {}
    for s in list_sets(conn):
        tgt = s["assigned_char_id"] or s["source_char_id"]
        tinv = world["inv"].get(tgt)
        total = len(s["items"])
        worn_total = sum(1 for it in s["items"] if it["slot"] not in EXTRA_SLOT_NAMES)
        if tinv is None:
            out[s["id"]] = {"worn": None, "present": None, "total": total,
                            "worn_total": worn_total}
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
            # Virtual slots never consume a WORN copy - the Avatar weapon is in the bags
            # by design - but they still consume a held copy, so "on hand" stays honest.
            if it["slot"] not in EXTRA_SLOT_NAMES and worn_stock.get(iid, 0) > 0:
                worn_stock[iid] -= 1
                worn += 1
            if all_stock.get(iid, 0) > 0:
                all_stock[iid] -= 1
                present += 1
        out[s["id"]] = {"worn": worn, "present": present, "total": total,
                        "worn_total": worn_total}
    return out
