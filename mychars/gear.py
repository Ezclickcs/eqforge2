"""Gear engine: worn stat sheets and best-in-inventory stat search.

Data sources (both already on disk):
  - items.txt.gz (sodeq item DB, pipe-delimited, 319 cols) — full item stats.
    Parsed lazily ONCE per process into a trimmed {id: dict}; only NEEDED_COLS kept.
  - <EQ_DIR>/<Name>_<server>-Inventory.txt dumps — worn vs held per toon.

Known truths encoded here:
  - Worn haste does NOT stack: only the single best worn item applies.
  - Stats/resists sum linearly (era stat caps are display concerns, not math).
  - Dump rows with "-SlotN" are aug/bag sub-slots — never worn themselves.
"""
import gzip
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ITEMS_PATH = os.path.join(os.path.dirname(HERE), "items.txt.gz")

# (db column, short label) in display order
STAT_FIELDS = [
    ("ac", "AC"), ("hp", "HP"), ("mana", "Mana"),
    ("regen", "Regen"), ("manaregen", "MRegen"), ("attack", "ATK"),
    ("astr", "STR"), ("asta", "STA"), ("aagi", "AGI"), ("adex", "DEX"),
    ("awis", "WIS"), ("aint", "INT"), ("acha", "CHA"),
    ("mr", "SvM"), ("fr", "SvF"), ("cr", "SvC"), ("dr", "SvD"), ("pr", "SvP"),
]
STAT_KEYS = [k for k, _ in STAT_FIELDS]
SEARCHABLE_STATS = STAT_FIELDS + [("haste", "Haste %")]

# Worn special effects collected per toon (deduped, in slot order).
# NOTE (fixed 2026-08-05): these read the *effect id* columns, NOT the sodeq
# `focusname`/`clickname`/`wornname`/`procname` columns -- those are EMPTY in this
# export, so every one of these four lists silently came back [] for every toon.
# Names are resolved through spell_name() (app/spell-effects.json.gz).
EFFECT_COLS = [("focuseffect", "focuses"), ("clickeffect", "clickies"),
               ("worneffect", "worneffects"), ("proceffect", "procs")]

NEEDED_COLS = ["id", "name", "classes", "races", "reqlevel", "slots", "haste",
               "nodrop", "fvnodrop", "loregroup", "itemtype"] + STAT_KEYS + [c for c, _ in EFFECT_COLS]

# EQ class bitmask order (bit 0 = WAR ... bit 15 = BER)
CLASS_BITS = ["Warrior", "Cleric", "Paladin", "Ranger", "Shadow Knight", "Druid",
              "Monk", "Bard", "Rogue", "Shaman", "Necromancer", "Wizard",
              "Magician", "Enchanter", "Beastlord", "Berserker"]
CLASS_SHORT = ["WAR", "CLR", "PAL", "RNG", "SHD", "DRU", "MNK", "BRD",
               "ROG", "SHM", "NEC", "WIZ", "MAG", "ENC", "BST", "BER"]
ALL_CLASSES_MASK = (1 << 16) - 1

WORN_SLOTS = {"Charm", "Ear", "Head", "Face", "Neck", "Shoulders", "Arms", "Back",
              "Wrist", "Range", "Hands", "Primary", "Secondary", "Fingers",
              "Chest", "Legs", "Feet", "Waist", "Ammo", "Power"}

_itemdb = None
_itemdb_path = None

# app/spell-effects.json.gz -- {spell_id: [name] | [name, description]}, built by
# tools/build_spells.py from the EQ client. GENERATED + gitignored, so it may be
# absent: every consumer must tolerate spell_name() returning "".
DEFAULT_SPELLS_PATH = os.path.join(os.path.dirname(HERE), "app", "spell-effects.json.gz")
_spelldb = None


def load_spell_db(path=None):
    """Lazy singleton: {spell_id(str): name}. Missing file -> {} (never raises)."""
    global _spelldb
    if _spelldb is not None:
        return _spelldb
    _spelldb = {}
    try:
        with gzip.open(path or DEFAULT_SPELLS_PATH, "rt", encoding="utf-8") as f:
            for sid, entry in json.load(f).items():
                if entry:
                    _spelldb[sid] = entry[0]
    except (OSError, ValueError):
        pass       # not built yet -- effects degrade to "#<id>", nothing breaks
    return _spelldb


def spell_name(spell_id):
    """Effect spell name, or "" when unknown/unbuilt."""
    if not spell_id or spell_id <= 0:
        return ""
    return load_spell_db().get(str(spell_id), "")


def load_item_db(path=None):
    """Lazy singleton: {item_id: {col: int|str}}. First call parses the whole DB."""
    global _itemdb, _itemdb_path
    path = path or DEFAULT_ITEMS_PATH
    if _itemdb is not None and _itemdb_path == path:
        return _itemdb
    db = {}
    with gzip.open(path, "rt", encoding="latin-1") as f:
        header = f.readline().rstrip("\n").split("|")
        idx = {c: header.index(c) for c in NEEDED_COLS if c in header}
        for line in f:
            parts = line.rstrip("\n").split("|")
            if len(parts) < len(header):
                continue
            try:
                iid = int(parts[idx["id"]])
            except (ValueError, KeyError):
                continue
            item = {"name": parts[idx["name"]]}
            for col in NEEDED_COLS:
                if col in ("id", "name") or col not in idx:
                    continue
                v = parts[idx[col]]
                try:
                    item[col] = int(v)
                except ValueError:
                    item[col] = 0
            db[iid] = item
    _itemdb, _itemdb_path = db, path
    return db


def classes_label(mask):
    if mask in (0, ALL_CLASSES_MASK, 65535):
        return "ALL"
    names = [CLASS_SHORT[i] for i in range(16) if mask & (1 << i)]
    return " ".join(names) if len(names) <= 4 else " ".join(names[:4]) + " +%d" % (len(names) - 4)


def class_bit(class_name):
    try:
        return 1 << CLASS_BITS.index(class_name)
    except ValueError:
        return None


# EQ race-restriction bitmask order (items.txt "races" column, bit 0 = Human ...
# bit 15 = Drakkin) — mirrors forge.js RACE_BITS. 0 or 65535 = unrestricted.
RACE_BITS = ["Human", "Barbarian", "Erudite", "Wood Elf", "High Elf", "Dark Elf",
             "Half Elf", "Dwarf", "Troll", "Ogre", "Halfling", "Gnome",
             "Iksar", "Vah Shir", "Froglok", "Drakkin"]


def race_bit(race_name):
    try:
        return 1 << RACE_BITS.index(race_name)
    except ValueError:
        return None


def race_guess(worn, db):
    """Infer a toon's possible races from race-LOCKED worn items: AND together the
    race masks of every restricted worn piece (a toon wearing an Iksar-only mask
    and a TRL/OGR-only belt is impossible -> possible = []). Unrestricted gear is
    no evidence. -> {"possible": [race names], "evidence": [{name, races}]}."""
    mask = 65535
    evidence = []
    for _slot, iid, _nm in worn:
        dbi = db.get(iid)
        rm = dbi.get("races", 0) if dbi else 0
        if rm in (0, 65535):
            continue
        evidence.append({"name": dbi["name"],
                         "races": [RACE_BITS[i] for i in range(16) if rm & (1 << i)]})
        mask &= rm
    possible = [RACE_BITS[i] for i in range(16) if mask & (1 << i)] if evidence else []
    return {"possible": possible, "evidence": evidence}


def parse_dump(path):
    """-> {"worn": [(slot, id, name)], "held": [(loc, id, name, count)]}"""
    worn, held = [], []
    with open(path, encoding="latin-1") as f:
        header = f.readline()
        if not header.startswith("Location"):
            return {"worn": [], "held": []}
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 4:
                continue
            loc, name, iid, count = parts[0], parts[1], parts[2], parts[3]
            try:
                iid = int(iid)
            except ValueError:
                continue
            if iid <= 0 or name == "Empty":
                continue
            if loc in WORN_SLOTS:
                worn.append((loc, iid, name))
            elif (loc.startswith(("General", "Bank", "SharedBank", "Held", "Dragon",
                                  "Hoard", "KeyRing"))
                  or loc == "Equipment"):
                # "Equipment" (exact) = an inactive PERSONA's closet — owned but awkward
                # to reach (needs a persona switch), so the planner ranks it last.
                try:
                    n = int(count)
                except ValueError:
                    n = 1
                held.append((loc, iid, name, n))
            # worn "-SlotN" rows are augs; era has none worth counting — skipped
    return {"worn": worn, "held": held}


def list_dumps(eq_dir):
    """-> {(name_lower, server_lower): (path, mtime)}"""
    out = {}
    if not os.path.isdir(eq_dir):
        return out
    for fn in os.listdir(eq_dir):
        if not fn.endswith("-Inventory.txt"):
            continue
        base = fn[:-len("-Inventory.txt")]
        name, _, server = base.partition("_")
        if not name:
            continue
        fp = os.path.join(eq_dir, fn)
        out[(name.lower(), server.lower())] = (fp, int(os.path.getmtime(fp)))
    return out


def worn_stats(worn, db):
    """Sum worn stats; haste = best single item; collect worn special effects."""
    totals = {k: 0 for k in STAT_KEYS}
    haste_val, haste_item = 0, ""
    unknown = []
    effects = {out: [] for _, out in EFFECT_COLS}
    empty_slots = sorted(WORN_SLOTS - {s for s, _, _ in worn} - {"Power", "Ammo", "Charm"})
    for slot, iid, name in worn:
        item = db.get(iid)
        if item is None:
            unknown.append(name)
            continue
        for k in STAT_KEYS:
            totals[k] += item.get(k, 0)
        if item.get("haste", 0) > haste_val:
            haste_val, haste_item = item["haste"], item["name"]
        for col, out in EFFECT_COLS:
            sid = item.get(col) or 0
            if sid > 0:
                # Unknown ids still surface (as "#1234") so a missing spell file
                # under-labels the effect rather than hiding that it exists.
                label = "%s (%s)" % (spell_name(sid) or ("#%d" % sid), item["name"])
                if label not in effects[out]:
                    effects[out].append(label)
    return {"totals": totals, "haste": haste_val, "haste_item": haste_item,
            "worn_count": len(worn), "empty_slots": empty_slots, "unknown_items": unknown,
            **effects}


def loc_bucket(loc):
    """Full location classifier, mirroring forge.js locBucket. Rank order for the
    gear-set planner lives in gearsets.BUCKET_RANK (bags cheapest, persona worst)."""
    if loc in WORN_SLOTS:
        return "worn"
    if loc.startswith("SharedBank"):
        return "shared"
    if loc.startswith("Bank"):
        return "bank"
    if loc.startswith(("Hoard", "Dragon")):          # Dragon's Hoard storage
        return "hoard"
    if loc.startswith(("Personal-Depot", "Personal Depot", "Depot")):
        return "depot"                                # Personal Tradeskill Depot (mats only)
    if loc == "Equipment":                            # inactive persona's closet
        return "persona"
    if loc.startswith("KeyRing"):
        return "keyring"
    return "bags"


def where_of(loc):
    return loc_bucket(loc)


def best_stat(stat, dumps, db, class_name=None, limit=25):
    """Rank every held+worn item across toons by a stat.

    dumps: [(toon_name, parsed_dump)] ; class_name filters to items that class can use.
    Returns rows: {item, id, value, holder, where, usable}
    """
    bit = class_bit(class_name) if class_name else None
    seen_best = []
    for toon, parsed in dumps:
        rows = [(loc, iid, nm) for loc, iid, nm in parsed["worn"]] + \
               [(loc, iid, nm) for loc, iid, nm, _ in parsed["held"]]
        for loc, iid, nm in rows:
            item = db.get(iid)
            if item is None:
                continue
            val = item.get(stat, 0)
            if val <= 0:
                continue
            mask = item.get("classes", 0)
            if bit is not None and mask not in (0, 65535) and not (mask & bit):
                continue
            seen_best.append({"item": item["name"], "id": iid, "value": val,
                              "holder": toon, "where": where_of(loc),
                              "usable": classes_label(mask),
                              "fvnodrop": item.get("fvnodrop", 0)})
    seen_best.sort(key=lambda r: (-r["value"], r["item"], r["holder"]))
    return seen_best[:limit]
