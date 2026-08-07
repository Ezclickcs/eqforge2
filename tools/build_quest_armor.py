#!/usr/bin/env python3
"""Build app/quest-armor.json — the Velious class-armor planner map (GENERIC).

Your "Unadorned <material>" pieces are 0-stat MOLDS for the Skyshrine (East ToV) class
armor. Per class/slot this maps: mold you own -> the FINISHED piece it builds (real stats
via its id) + the gems the turn-in needs. The app injects the finished piece as an upgrade
candidate whenever you own that slot's mold.

Now data-driven, NOT hardcoded per class:
  - finished pieces come from raidloot-bis.json rows tagged "Quest: Skyshrine ..." (works
    for every class we've scraped — Monk=White Lotus, Warrior=Myrmidon, Cleric=Akkirus, ...)
  - molds are matched from items.txt.gz: an "Unadorned ..." (or caster "Tattered Silk")
    item in that slot, usable by the class.
  - gems from eqprogression's per-class-group table.
Pure stdlib. Re-run after scraping a new class into raidloot-bis.json.
"""
import gzip, json, os

CLASS_BIT = {"Warrior": 1, "Cleric": 2, "Paladin": 4, "Ranger": 8, "Shadowknight": 16,
             "Druid": 32, "Monk": 64, "Bard": 128, "Rogue": 256, "Shaman": 512,
             "Necromancer": 1024, "Wizard": 2048, "Magician": 4096, "Enchanter": 8192,
             "Beastlord": 16384, "Berserker": 32768}
# gem group (eqprogression): A=melee/hybrid plate·chain·leather, B=cloth casters, C=priests
GROUP = {c: "A" for c in ["Warrior", "Monk", "Rogue", "Berserker", "Paladin", "Ranger", "Shadowknight", "Bard", "Beastlord"]}
GROUP.update({c: "B" for c in ["Enchanter", "Magician", "Necromancer", "Wizard"]})
GROUP.update({c: "C" for c in ["Cleric", "Druid", "Shaman"]})
GEMS = {
    "A": {"Chest": "3x Flawless Diamond", "Legs": "3x Flawed Sea Sapphire", "Arms": "3x Flawed Emerald",
          "Head": "3x Crushed Coral", "Feet": "3x Crushed Black Marble", "Hands": "3x Crushed Topaz",
          "Wrist": "3x Crushed Flame Emerald"},
    "B": {"Chest": "3x Pristine Emerald", "Legs": "3x Nephrite", "Arms": "3x Flawed Topaz",
          "Head": "3x Crushed Flame Opal", "Feet": "3x Crushed Jaundice Gem", "Hands": "3x Crushed Topaz",
          "Wrist": "3x Crushed Onyx Sapphire"},
    "C": {"Chest": "3x Black Marble", "Legs": "3x Chipped Onyx Sapphire", "Arms": "3x Jaundice Gem",
          "Head": "3x Crushed Onyx Sapphire", "Feet": "3x Crushed Flame Emerald", "Hands": "3x Crushed Lava Ruby",
          "Wrist": "3x Crushed Opal"},
}
SLOT_BITS = [(4, "Head"), (128, "Arms"), (512 | 1024, "Wrist"), (4096, "Hands"),
             (131072, "Chest"), (262144, "Legs"), (524288, "Feet")]
# The three mold tiers (eqprogression Table 0). Each builds a different faction set,
# which raidloot tags in its Source column. molds = name prefixes (plate+chain share the
# tier word; leather/cloth have their own). Real molds are 0-stat blanks — a stat/AC filter
# excludes look-alikes ("Unadorned Slayer", "Corroded Infantry", "Unadorned Tabard").
TIERS = [
    {"label": "Ancient (Kael)",    "src": "quest: kael",
     "molds": ("ancient tarnished", "ancient leather", "ancient silk")},
    {"label": "Unadorned (Skyshrine)", "src": "quest: skyshrine",
     "molds": ("unadorned", "tattered silk")},
    {"label": "Corroded (Thurgadin)",  "src": "quest: thurgadin",
     "molds": ("corroded", "eroded leather", "torn enchanted silk")},
]
# Explicit fills for holes in raidloot's Quest tagging (piece exists in-game but raidloot
# didn't list it for that class/slot/tier). Verified ids. Add here when the coverage audit
# (tools note) finds a gap. (class, slot, tier-src) -> (finishedId, name).
OVERRIDES = {
    ("Cleric", "Arms", "quest: kael"): (25393, "Templar's Vambraces"),
}

HERE = os.path.dirname(os.path.abspath(__file__))
RAID = os.path.join(HERE, "..", "app", "raidloot-bis.json")
DB = os.path.join(HERE, "..", "items.txt.gz")
OUT = os.path.join(HERE, "..", "app", "quest-armor.json")


def slot_of(mask):
    return next((n for b, n in SLOT_BITS if mask & b), None)


def load_molds():
    """Every 0-stat class-armor mold: (name, id, classes_mask, slot). 0-stat gate (AC/HP 0)
    drops the statted look-alikes that share a prefix."""
    out = []
    with gzip.open(DB, "rt", encoding="utf-8", errors="replace") as f:
        head = f.readline().rstrip().split("|"); I = {c: i for i, c in enumerate(head)}
        for line in f:
            p = line.split("|")
            if len(p) <= I["classes"]:
                continue
            low = p[I["name"]].lower()
            if not any(low.startswith(pre) for t in TIERS for pre in t["molds"]):
                continue
            if int(p[I["ac"]] or 0) != 0 or int(p[I["hp"]] or 0) != 0:   # real molds are blank
                continue
            s = slot_of(int(p[I["slots"]] or 0))
            if s:
                out.append((p[I["name"]], int(p[I["id"]]), int(p[I["classes"]] or 0), s))
    return out


def mold_for(molds, prefixes, slot, bit):
    """Lowest-id 0-stat mold matching one of `prefixes`, in `slot`, usable by the class."""
    cands = [(i, n) for (n, i, cl, s) in molds
             if s == slot and (cl & bit) and any(n.lower().startswith(pre) for pre in prefixes)]
    return min(cands) if cands else None   # min id = the canonical mold (beats e.g. Unadorned Tabard)


def main():
    raid = json.load(open(RAID, encoding="utf-8"))
    molds = load_molds()
    out = {"_meta": {"source": "raidloot 'Quest: Kael/Skyshrine/Thurgadin' rows + eqprogression gems + items.txt.gz",
                     "tiers": [t["label"] for t in TIERS],
                     "note": "each slot lists up to 3 buildable options (one per mold tier). app injects a finished piece as an upgrade candidate for each option whose moldId you own"}}
    for cls, byslot in raid.items():
        if cls.startswith("_") or not isinstance(byslot, dict):
            continue
        bit = CLASS_BIT.get(cls); grp = GROUP.get(cls)
        if not bit or not grp:
            continue
        slots = {}
        for slot, rows in byslot.items():
            if not isinstance(rows, list):
                continue
            opts = []
            for tier in TIERS:
                fin = next((r for r in rows if str(r.get("source", "")).lower().startswith(tier["src"])), None)
                if not fin:
                    ov = OVERRIDES.get((cls, slot, tier["src"]))     # fill a raidloot tagging gap
                    if ov:
                        fin = {"id": ov[0], "name": ov[1]}
                    else:
                        continue
                mold = mold_for(molds, tier["molds"], slot, bit)
                if not mold:
                    continue   # can't build without the mold; the finished piece still appears via bis-sets
                mid, mname = mold
                opts.append({"tier": tier["label"], "finishedId": fin["id"], "finishedName": fin["name"],
                             "moldId": mid, "moldName": mname, "gems": GEMS[grp].get(slot)})
            if opts:
                slots[slot] = opts
        if slots:
            out[cls] = {"slots": slots}
            tot = sum(len(v) for v in slots.values())
            print(f"  {cls:8s} slots={len(slots)}  build-options={tot}")

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("wrote", os.path.normpath(OUT))


if __name__ == "__main__":
    main()
