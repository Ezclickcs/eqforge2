"""Gear engine: dump parsing, worn stat totals, haste-max rule, best-stat search.
Run from eqforge2/:  python -m unittest discover -s mychars/tests
"""
import gzip
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from mychars import gear  # noqa: E402

# tiny synthetic item DB: FBSS-alike (21% haste, ALL), CoF-alike (36%, melee-ish),
# caster ring (no haste, +50 mana, ENC-only), fv-nodrop cloak
ITEMDB = {
    100: {"name": "Swift Belt", "haste": 21, "ac": 5, "hp": 0, "mana": 0, "astr": 0,
          "asta": 5, "aagi": 0, "adex": 0, "awis": 0, "aint": 0, "acha": 0,
          "mr": 0, "fr": 0, "cr": 0, "dr": 0, "pr": 0, "classes": 65535, "fvnodrop": 0,
          # effects are stored as spell IDS (the sodeq *name* columns are empty);
          # SPELLDB below stands in for app/spell-effects.json.gz
          "regen": 2, "clickeffect": 36, "worneffect": 40},
    101: {"name": "Flowing Cloak", "haste": 36, "ac": 10, "hp": 25, "mana": 0, "astr": 5,
          "asta": 0, "aagi": 0, "adex": 0, "awis": 0, "aint": 0, "acha": 0,
          "mr": 5, "fr": 0, "cr": 0, "dr": 0, "pr": 0,
          "classes": (1 << 6) | (1 << 8), "fvnodrop": 1},          # MNK|ROG
    102: {"name": "Sage Ring", "haste": 0, "ac": 2, "hp": 0, "mana": 50, "astr": 0,
          "asta": 0, "aagi": 0, "adex": 0, "awis": 0, "aint": 5, "acha": 0,
          "mr": 0, "fr": 0, "cr": 0, "dr": 0, "pr": 0, "classes": 1 << 13, "fvnodrop": 0},  # ENC
}

DUMP = """Location\tName\tID\tCount\tSlots
Charm\tEmpty\t0\t0\t0
Waist\tSwift Belt\t100\t1\t6
Back\tFlowing Cloak\t101\t1\t6
Back-Slot1\tEmpty\t0\t0\t0
Fingers\tSage Ring\t102\t1\t6
Fingers\tEmpty\t0\t0\t0
General1\tBackpack\t17969\t1\t0
General1-Slot1\tSwift Belt\t100\t1\t6
Bank1\tSage Ring\t102\t1\t6
SharedBank1\tEmpty\t0\t0\t0
"""


def write_dump(text):
    fd, path = tempfile.mkstemp(suffix="-Inventory.txt")
    with os.fdopen(fd, "w", encoding="latin-1") as f:
        f.write(text)
    return path


class TestParseDump(unittest.TestCase):
    def setUp(self):
        self.path = write_dump(DUMP)
        self.parsed = gear.parse_dump(self.path)

    def tearDown(self):
        os.unlink(self.path)

    def test_worn_vs_held_split(self):
        worn_ids = [iid for _, iid, _ in self.parsed["worn"]]
        self.assertEqual(sorted(worn_ids), [100, 101, 102])
        held_ids = sorted(iid for _, iid, _, _ in self.parsed["held"])
        self.assertEqual(held_ids, [100, 102, 17969])     # bag + bag content + bank

    def test_aug_subslots_and_empty_skipped(self):
        locs = [loc for loc, _, _ in self.parsed["worn"]]
        self.assertNotIn("Back-Slot1", locs)
        self.assertNotIn("Charm", locs)                   # empty


class TestWornStats(unittest.TestCase):
    def setUp(self):
        # Stub the spell lookup so these tests never depend on the generated
        # (gitignored) app/spell-effects.json.gz being present.
        self._saved_spelldb = gear._spelldb
        gear._spelldb = {"36": "Gate", "40": "Inferno Shield"}

    def tearDown(self):
        gear._spelldb = self._saved_spelldb

    def test_totals_and_haste_max_not_sum(self):
        path = write_dump(DUMP)
        try:
            worn = gear.parse_dump(path)["worn"]
        finally:
            os.unlink(path)
        s = gear.worn_stats(worn, ITEMDB)
        self.assertEqual(s["totals"]["ac"], 17)
        self.assertEqual(s["totals"]["hp"], 25)
        self.assertEqual(s["totals"]["mana"], 50)
        self.assertEqual(s["haste"], 36)                   # best single item, NOT 57
        self.assertEqual(s["haste_item"], "Flowing Cloak")
        self.assertIn("Head", s["empty_slots"])
        self.assertNotIn("Waist", s["empty_slots"])
        self.assertEqual(s["totals"]["regen"], 2)          # worn regen summed
        self.assertEqual(s["clickies"], ["Gate (Swift Belt)"])
        self.assertEqual(s["worneffects"], ["Inferno Shield (Swift Belt)"])
        self.assertEqual(s["focuses"], [])

    def test_unknown_item_reported_not_crashed(self):
        s = gear.worn_stats([("Head", 99999, "Mystery Hat")], ITEMDB)
        self.assertEqual(s["unknown_items"], ["Mystery Hat"])
        self.assertEqual(s["totals"]["ac"], 0)


class TestBestStat(unittest.TestCase):
    def dumps(self):
        path = write_dump(DUMP)
        try:
            parsed = gear.parse_dump(path)
        finally:
            os.unlink(path)
        return [("Rakthor", parsed)]

    def test_haste_ranked_desc_worn_and_held(self):
        rows = gear.best_stat("haste", self.dumps(), ITEMDB)
        self.assertEqual(rows[0]["item"], "Flowing Cloak")
        self.assertEqual(rows[0]["value"], 36)
        self.assertEqual([r["value"] for r in rows], [36, 21, 21])   # cloak, worn belt, bagged belt
        self.assertEqual({r["where"] for r in rows}, {"worn", "bags"})

    def test_class_filter_uses_bitmask(self):
        rows = gear.best_stat("haste", self.dumps(), ITEMDB, class_name="Enchanter")
        self.assertEqual([r["item"] for r in rows], ["Swift Belt", "Swift Belt"])  # ALL-class only
        rows = gear.best_stat("haste", self.dumps(), ITEMDB, class_name="Monk")
        self.assertEqual(rows[0]["item"], "Flowing Cloak")             # MNK bit set

    def test_mana_search_finds_bank(self):
        rows = gear.best_stat("mana", self.dumps(), ITEMDB, class_name="Enchanter")
        self.assertEqual(rows[0]["item"], "Sage Ring")
        self.assertEqual({r["where"] for r in rows}, {"worn", "bank"})

    def test_fvnodrop_flagged(self):
        rows = gear.best_stat("haste", self.dumps(), ITEMDB)
        self.assertEqual(rows[0]["fvnodrop"], 1)


class TestItemDbLoader(unittest.TestCase):
    def test_parses_pipe_gz(self):
        fd, path = tempfile.mkstemp(suffix=".txt.gz")
        os.close(fd)
        cols = ["id", "name", "ac", "hp", "mana", "astr", "asta", "aagi", "adex", "acha",
                "aint", "awis", "mr", "fr", "cr", "dr", "pr", "haste", "classes", "races",
                "reqlevel", "slots", "nodrop", "fvnodrop", "loregroup"]
        with gzip.open(path, "wt", encoding="latin-1") as f:
            f.write("|".join(cols) + "\n")
            f.write("|".join(["5000", "Test Blade"] + ["3"] * (len(cols) - 2)) + "\n")
            f.write("bad|row\n")
        try:
            db = gear.load_item_db(path)
        finally:
            gear._itemdb = None
            gear._itemdb_path = None
            os.unlink(path)
        self.assertEqual(db[5000]["name"], "Test Blade")
        self.assertEqual(db[5000]["haste"], 3)
        self.assertEqual(len(db), 1)


if __name__ == "__main__":
    unittest.main()
