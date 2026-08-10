"""Give a real slot to gear_set_items rows that have slot = ''.

The Macro Builder import wrote picks with no slot label. The set editor renders one
row per SLOT, so those picks are invisible in the UI -- but the router still counts
them as claims. The user changed Sleeper Monk 2's Neck to Zlandicar's Talisman and the
comp check kept reporting Yelinak's Talisman reserved, because a second, unslotted
row still claimed it (2026-08-09).

Infers the slot from the item's own equip mask (gearsets.SLOT_BITS), fills paired
slots (Ear/Wrist/Fingers) index 0 then 1, and refuses to overwrite a slot the set
already fills. Anything it cannot place is listed, not guessed.

Run with --apply to write; default is a dry run.
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mychars import gear as gearmod          # noqa: E402
from mychars import gearsets                 # noqa: E402

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "mychars.db")


def slots_for(mask):
    """Every worn slot this item can occupy, in SLOT_BITS order."""
    return [name for name, bits in gearsets.SLOT_BITS if mask & bits]


def main(apply_it):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    db = gearmod.load_item_db()
    ghosts = con.execute(
        "SELECT i.rowid AS rid, i.set_id, i.slot_index, i.item_id, i.item_name,"
        "       g.name AS set_name FROM gear_set_items i"
        " JOIN gear_sets g ON g.id = i.set_id"
        " WHERE i.slot = '' ORDER BY g.name, i.slot_index").fetchall()
    if not ghosts:
        print("no unslotted rows -- nothing to do")
        return

    # what each set already fills, so a repair can never clobber a real pick
    taken = {}
    for r in con.execute("SELECT set_id, slot, slot_index FROM gear_set_items"
                         " WHERE slot != ''"):
        taken.setdefault(r["set_id"], set()).add((r["slot"], r["slot_index"]))

    fixed, stuck = [], []
    for g in ghosts:
        info = db.get(g["item_id"]) or {}
        mask = int(info.get("slots", 0) or 0)
        placed = None
        for slot in slots_for(mask):
            span = 2 if slot in gearsets.PAIRED_SLOTS else 1
            for k in range(span):
                if (slot, k) not in taken.setdefault(g["set_id"], set()):
                    placed = (slot, k)
                    break
            if placed:
                break
        if placed is None:
            stuck.append((g, slots_for(mask)))
            continue
        taken[g["set_id"]].add(placed)
        fixed.append((g, placed))

    print("%-18s %-30s %-10s %s" % ("SET", "ITEM", "SLOT", "was"))
    for g, (slot, k) in fixed:
        print("%-18s %-30s %-10s ''#%s" % (g["set_name"][:18], g["item_name"][:30],
                                           "%s#%d" % (slot, k), g["slot_index"]))
    if stuck:
        print("\nCOULD NOT PLACE (left alone -- delete them by hand in the editor):")
        for g, cand in stuck:
            print("  %-18s %-30s mask slots=%s"
                  % (g["set_name"][:18], g["item_name"][:30], ", ".join(cand) or "none"))

    print("\n%d placeable, %d stuck" % (len(fixed), len(stuck)))
    if not apply_it:
        print("dry run -- re-run with --apply to write")
        return
    for g, (slot, k) in fixed:
        con.execute("UPDATE gear_set_items SET slot=?, slot_index=? WHERE rowid=?",
                    (slot, k, g["rid"]))
    con.commit()
    left = con.execute("SELECT COUNT(*) FROM gear_set_items WHERE slot=''").fetchone()[0]
    print("written. unslotted rows remaining: %d" % left)


main("--apply" in sys.argv)
