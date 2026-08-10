"""Carve deleted gear_set_items rows out of mychars.db unallocated space.

gear_set_items is a WITHOUT-ROWID-less table with PK (set_id, slot, slot_index),
so leaf cells carry: [payload_len varint][rowid varint][header][set_id, slot,
slot_index, item_id, item_name].

We brute-force every byte offset, try to parse a record header whose column
types match the schema, and keep the ones that decode cleanly.
"""
import os
import sqlite3
import sys

DB = os.environ.get("EQFORGE_MYCHARS_DB") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mychars.db")


def varint(buf, i):
    n = 0
    for k in range(9):
        if i + k >= len(buf):
            return None, i
        b = buf[i + k]
        if k == 8:
            return (n << 8) | b, i + 9
        n = (n << 7) | (b & 0x7F)
        if not (b & 0x80):
            return n, i + k + 1
    return None, i


def int_of(buf, i, st):
    """Decode a column of serial type st at offset i -> (value, next_i)."""
    sizes = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 6, 6: 8, 8: 0, 9: 0}
    if st in (8, 9):
        return st - 8, i
    if st == 0:
        return None, i
    n = sizes.get(st)
    if n is None:
        return None, None
    if i + n > len(buf):
        return None, None
    v = int.from_bytes(buf[i:i + n], "big", signed=True)
    return v, i + n


def try_record(buf, i):
    """Attempt to read a gear_set_items record whose header starts at i."""
    hdr_len, j = varint(buf, i)
    if hdr_len is None or not (6 <= hdr_len <= 12):
        return None
    end_hdr = i + hdr_len
    if end_hdr > len(buf):
        return None
    types = []
    while j < end_hdr:
        t, j = varint(buf, j)
        if t is None:
            return None
        types.append(t)
    if j != end_hdr or len(types) != 5:
        return None
    t_set, t_slot, t_idx, t_item, t_name = types
    # set_id / slot_index / item_id must be integers (or the 0/1 constants)
    if t_set > 6 and t_set not in (8, 9):
        return None
    if t_idx > 6 and t_idx not in (8, 9):
        return None
    if t_item > 6 and t_item not in (8, 9):
        return None
    # slot and item_name must be TEXT: odd serial type >= 13
    if t_slot < 13 or t_slot % 2 == 0:
        return None
    if t_name < 13 or t_name % 2 == 0:
        return None
    p = end_hdr
    set_id, p = int_of(buf, p, t_set)
    if p is None:
        return None
    slot_len = (t_slot - 13) // 2
    if slot_len > 24 or p + slot_len > len(buf):
        return None
    slot = buf[p:p + slot_len]
    p += slot_len
    slot_index, p = int_of(buf, p, t_idx)
    if p is None:
        return None
    item_id, p = int_of(buf, p, t_item)
    if p is None:
        return None
    name_len = (t_name - 13) // 2
    if not (3 <= name_len <= 64) or p + name_len > len(buf):
        return None
    name = buf[p:p + name_len]
    try:
        slot_s = slot.decode("ascii")
        name_s = name.decode("ascii")
    except UnicodeDecodeError:
        return None
    if not all(32 <= c < 127 for c in slot):
        return None
    if not all(32 <= c < 127 for c in name):
        return None
    if set_id is None or not (0 < set_id < 500):
        return None
    if slot_index is None or not (0 <= slot_index < 12):
        return None
    if item_id is None or not (0 < item_id < 200000):
        return None
    if not name_s[0].isalpha():
        return None
    return {"set_id": set_id, "slot": slot_s, "slot_index": slot_index,
            "item_id": item_id, "item_name": name_s, "offset": i}


def main():
    data = open(DB, "rb").read()
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    live = {(r["set_id"], r["slot"], r["slot_index"], r["item_id"])
            for r in con.execute("SELECT * FROM gear_set_items")}
    names = {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM gear_sets")}
    active = {r["id"] for r in con.execute("SELECT id FROM gear_sets WHERE active=1")}

    found = {}
    for i in range(len(data)):
        rec = try_record(data, i)
        if not rec:
            continue
        key = (rec["set_id"], rec["slot"], rec["slot_index"], rec["item_id"])
        if key in live:
            continue
        if rec["set_id"] not in names:
            continue
        found.setdefault(key, rec)

    ghosts = sorted(found.values(),
                    key=lambda r: (r["set_id"] not in active, names[r["set_id"]],
                                   r["slot"], r["slot_index"]))
    print("carved %d deleted rows not present in the live table\n" % len(ghosts))
    for r in ghosts:
        mark = "ACTIVE" if r["set_id"] in active else "      "
        # is this slot currently EMPTY in that set? then it is a real loss
        occupied = con.execute(
            "SELECT 1 FROM gear_set_items WHERE set_id=? AND slot=? AND slot_index=?",
            (r["set_id"], r["slot"], r["slot_index"])).fetchone()
        state = "SLOT NOW EMPTY" if not occupied else "slot refilled"
        print("%s %-16s %-10s#%d  %-32s id=%-6d %s"
              % (mark, names[r["set_id"]], r["slot"], r["slot_index"],
                 r["item_name"], r["item_id"], state))


main()
