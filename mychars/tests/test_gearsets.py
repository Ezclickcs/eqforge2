"""Gear-set planner: snapshot, route ladder (worn/have/grab/swap/trade/parcel),
reservations across sets, target protection, Lua export format.
Run from eqforge2/:  python -m unittest discover -s mychars/tests
"""
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from mychars import api, db as dbm, gearsets  # noqa: E402

# synthetic item DB — only the columns the planner reads
def _item(name, fvnodrop=0, loregroup=0, attunable=0, **extra):
    d = {"name": name, "fvnodrop": fvnodrop, "loregroup": loregroup,
         "attunable": attunable}
    d.update(extra)
    return d

ITEMDB = {
    200: _item("Monk Belt"),
    201: _item("Lore Earring", loregroup=-1),
    202: _item("NoTrade Fists", fvnodrop=1),
    203: _item("Swap Tunic"),
    204: _item("Shared Ring"),
    205: _item("Parcel Boots"),
    206: _item("Trade Cloak"),
    207: _item("Missing Crown"),
    208: _item("Bag Mask"),
    209: _item("Old Dagger"),
    # editor-candidate items (slots bitmask: Chest=131072, Ear=2|16, Primary=8192)
    300: _item("Bronze Breastplate", slots=131072, classes=0, ac=20, hp=10, mana=0),
    301: _item("Silver Breastplate", slots=131072, classes=0, ac=35, hp=0, mana=0),
    302: _item("Caster Vest", slots=131072, classes=1 << 13, ac=5, hp=0, mana=50),  # ENC only
    303: _item("Chest Aug", slots=131072, classes=0, ac=99, hp=0, mana=0, itemtype=54),
    304: _item("Plain Hoop", slots=2 | 16, classes=0, ac=4, hp=10, mana=0),
}

HEADER = "Location\tName\tID\tCount\tSlots\n"


def dump_line(loc, name, iid, count=1):
    return "%s\t%s\t%d\t%d\t0\n" % (loc, name, iid, count)


class _World(unittest.TestCase):
    """Temp DB + temp EQ dir; helpers to add accounts/chars/dumps/sets."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.eq_dir = tempfile.mkdtemp()
        self.conn = dbm.connect(self.db_path)
        self._acct = {}

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)
        shutil.rmtree(self.eq_dir, ignore_errors=True)

    def acct(self, alias):
        if alias not in self._acct:
            cur = self.conn.execute("INSERT INTO accounts(alias) VALUES (?)", (alias,))
            self._acct[alias] = cur.lastrowid
        return self._acct[alias]

    def char(self, name, acct_alias, cls="Monk", level=60, tags=""):
        cur = self.conn.execute(
            "INSERT INTO characters(name, server, account_id, class_name, level, group_tags)"
            " VALUES (?,?,?,?,?,?)",
            (name, "Frostreaver", self.acct(acct_alias), cls, level, tags))
        return cur.lastrowid

    def dump(self, name, lines):
        with open(os.path.join(self.eq_dir, "%s_frostreaver-Inventory.txt" % name),
                  "w", encoding="latin-1") as f:
            f.write(HEADER + "".join(lines))

    def gearset(self, name, target_id, item_rows, active=1):
        code, res = gearsets.save_set(self.conn, {
            "name": name, "assigned_char_id": target_id, "active": active,
            "items": [{"item_id": iid, "item_name": ITEMDB[iid]["name"], "slot": slot}
                      for slot, iid in item_rows]})
        self.assertEqual(code, 200, res)
        return res["id"]

    def plan(self, login_ids=None):
        return gearsets.build_plans(self.conn, self.eq_dir, login_ids, db=ITEMDB)


class TestRouteLadder(_World):
    def setUp(self):
        super().setUp()
        # Acct1: Rakthor (target) + Belwyn (same-account holder)
        # Acct2: Torvin (in the login set -> trade) ; Acct3: Corvane (offline -> parcel)
        self.rakthor = self.char("Rakthor", "Acct1")
        self.belwyn = self.char("Belwyn", "Acct1", cls="Bard")
        self.torvin = self.char("Torvin", "Acct2", cls="Shaman")
        self.corvane = self.char("Corvane", "Acct3", cls="Enchanter")
        self.dump("Rakthor", [
            dump_line("Waist", "Monk Belt", 200),
            dump_line("General1-Slot1", "Bag Mask", 208),
            dump_line("SharedBank1", "Shared Ring", 204),
        ])
        self.dump("Belwyn", [
            dump_line("Chest", "Swap Tunic", 203),
            dump_line("SharedBank1", "Shared Ring", 204),   # same account bank, must dedupe
        ])
        self.dump("Torvin", [dump_line("General2-Slot3", "Trade Cloak", 206)])
        self.dump("Corvane", [
            dump_line("Bank2", "Parcel Boots", 205),
            dump_line("Hands", "NoTrade Fists", 202),
            dump_line("General1-Slot1", "Lore Earring", 201),
        ])
        self.set_id = self.gearset("Rakthor kit", self.rakthor, [
            ("Waist", 200), ("Face", 208), ("Fingers", 204), ("Chest", 203),
            ("Back", 206), ("Feet", 205), ("Hands", 202), ("Ear", 201),
            ("Ear", 201), ("Head", 207),
        ])

    def by_item(self, result, iid, k=0):
        rows = [r for p in result["plans"] for r in p["rows"] if r["item_id"] == iid]
        return rows[k]

    def test_full_ladder(self):
        res = self.plan(login_ids=[self.rakthor, self.torvin])
        self.assertEqual(self.by_item(res, 200)["status"], "worn")
        self.assertEqual(self.by_item(res, 208)["status"], "have")
        grab = self.by_item(res, 204)
        self.assertEqual(grab["status"], "grab")            # own shared bank, 0 logins
        swap = self.by_item(res, 203)
        self.assertEqual(swap["status"], "swap")
        self.assertEqual(swap["holder"], "Belwyn")
        trade = self.by_item(res, 206)
        self.assertEqual(trade["status"], "trade")          # Torvin is online
        self.assertEqual(self.by_item(res, 205)["status"], "parcel")
        self.assertEqual(self.by_item(res, 202)["status"], "notrade")
        self.assertEqual(self.by_item(res, 207)["status"], "missing")

    def test_trade_needs_login_set(self):
        res = self.plan(login_ids=None)                     # nobody online
        self.assertEqual(self.by_item(res, 206)["status"], "parcel")

    def test_lore_second_copy_blocked(self):
        res = self.plan()
        self.assertEqual(self.by_item(res, 201, 0)["status"], "parcel")
        second = self.by_item(res, 201, 1)
        self.assertEqual(second["status"], "lore")

    def test_workorder_excludes_grab_and_orders_by_size(self):
        res = self.plan(login_ids=[self.torvin])
        holders = [h["holder"] for h in res["workorder"]]
        self.assertNotIn("Rakthor", holders)               # grab needs no login
        corvane = next(h for h in res["workorder"] if h["holder"] == "Corvane")
        self.assertEqual(corvane["total"], 2)                # boots + 1 lore earring
        self.assertEqual(res["workorder"][0]["holder"], "Corvane")  # biggest first

    def test_reservation_across_sets(self):
        sythe = self.char("Sythe", "Acct4", cls="Rogue")
        self.gearset("Second kit", sythe, [("Chest", 203)])   # only 1 Swap Tunic exists
        res = self.plan()
        first = self.by_item(res, 203, 0)
        second = self.by_item(res, 203, 1)
        self.assertEqual(first["status"], "swap")           # set 1 claimed it
        self.assertEqual(second["status"], "reserved")
        self.assertIn("promised", second["note"])

    def test_retired_set_releases_claims(self):
        sythe = self.char("Sythe", "Acct4", cls="Rogue")
        self.gearset("Second kit", sythe, [("Chest", 203)])
        # retire set 1 -> its tunic claim disappears, second set gets it
        self.conn.execute("UPDATE gear_sets SET active=0 WHERE id=?", (self.set_id,))
        self.conn.commit()
        res = self.plan()
        self.assertEqual(len(res["plans"]), 1)
        row = res["plans"][0]["rows"][0]
        self.assertEqual(row["status"], "parcel")           # Belwyn -> Sythe, cross-acct
        self.assertEqual(row["holder"], "Belwyn")

    def test_summary_counts(self):
        res = self.plan(login_ids=[self.torvin])
        s = res["summary"]
        self.assertEqual(s["pieces"], 10)
        self.assertEqual(s["satisfied"], 3)                 # worn + have + grab
        self.assertEqual(s["moves"], 4)                     # swap + trade + parcel + lore-1st
        self.assertEqual(s["blocked"], 3)                   # notrade + lore-2nd + missing


class TestBankerPriority(_World):
    """Gear should be TAKEN from bankers/mules before any playing toon — a banker
    parcel beats even a same-account swap. Banker = 'banker' tag OR level <= 5."""

    def setUp(self):
        super().setUp()
        self.target = self.char("Rakthor", "Acct1")
        self.samacct = self.char("Belwyn", "Acct1", cls="Bard")     # swap candidate
        self.main = self.char("Torvin", "Acct2", cls="Shaman")       # cross-acct main
        self.mule = self.char("Mulgrim", "Acct3", level=1)           # auto-banker
        self.dump("Rakthor", [])
        self.dump("Belwyn", [dump_line("Waist", "Monk Belt", 200)])
        self.dump("Torvin", [dump_line("General1-Slot1", "Monk Belt", 200)])

    def row(self, res):
        return res["plans"][0]["rows"][0]

    def test_level1_mule_beats_swap_and_main(self):
        self.dump("Mulgrim", [dump_line("Bank1", "Monk Belt", 200)])
        self.gearset("Kit", self.target, [("Waist", 200)])
        r = self.row(self.plan())
        self.assertEqual(r["holder"], "Mulgrim")            # banker wins
        self.assertEqual(r["status"], "parcel")             # even over a swap
        self.assertTrue(r["holder_banker"])

    def test_tagged_banker_counts_at_any_level(self):
        banker60 = self.char("Bankwyn", "Acct4", cls="Enchanter", tags="core, banker")
        self.dump("Bankwyn", [dump_line("General2-Slot1", "Monk Belt", 200)])
        self.gearset("Kit", self.target, [("Waist", 200)])
        r = self.row(self.plan())
        self.assertEqual(r["holder"], "Bankwyn")
        self.assertTrue(r["holder_banker"])

    def test_without_banker_swap_still_wins(self):
        self.gearset("Kit", self.target, [("Waist", 200)])  # no banker holds it
        r = self.row(self.plan())
        self.assertEqual(r["status"], "swap")
        self.assertEqual(r["holder"], "Belwyn")

    def test_own_shared_bank_still_beats_banker(self):
        self.dump("Mulgrim", [dump_line("Bank1", "Monk Belt", 200)])
        self.dump("Rakthor", [dump_line("SharedBank1", "Monk Belt", 200)])
        self.gearset("Kit", self.target, [("Waist", 200)])
        r = self.row(self.plan())
        self.assertEqual(r["status"], "grab")               # zero effort wins


class TestTargetProtection(_World):
    def test_other_set_cannot_strip_a_targets_needed_gear(self):
        aldreth = self.char("Aldreth", "Acct1", cls="Rogue")
        sythe = self.char("Sythe", "Acct2", cls="Rogue")
        self.dump("Aldreth", [dump_line("Primary", "Old Dagger", 209)])
        self.dump("Sythe", [])
        # set A (older id) wants to take Aldreth's dagger for Sythe;
        # set B says Aldreth's own loadout needs that dagger.
        self.gearset("Sythe kit", sythe, [("Primary", 209)])
        self.gearset("Aldreth kit", aldreth, [("Primary", 209)])
        res = self.plan()
        sythe_row = res["plans"][0]["rows"][0]
        aldreth_row = res["plans"][1]["rows"][0]
        self.assertEqual(aldreth_row["status"], "worn")      # keeps his own gear
        self.assertEqual(sythe_row["status"], "reserved")  # may not strip the target


class TestSnapshotAndFit(_World):
    def test_snapshot_and_paired_slots(self):
        t = self.char("Rakthor", "Acct1")
        self.dump("Rakthor", [
            dump_line("Waist", "Monk Belt", 200),
            dump_line("Fingers", "Shared Ring", 204),
            dump_line("Fingers", "Shared Ring", 204),
        ])
        code, res = gearsets.snapshot(self.conn, self.eq_dir, t)
        self.assertEqual(code, 200, res)
        self.assertEqual(res["pieces"], 3)
        sets = gearsets.list_sets(self.conn)
        self.assertEqual(len(sets), 1)
        fingers = sorted(i["slot_index"] for i in sets[0]["items"] if i["slot"] == "Fingers")
        self.assertEqual(fingers, [0, 1])                   # paired slot got 0 and 1
        # re-snapshot same name updates, not duplicates
        code, _ = gearsets.snapshot(self.conn, self.eq_dir, t)
        self.assertEqual(code, 200)
        self.assertEqual(len(gearsets.list_sets(self.conn)), 1)

    def test_snapshot_without_dump_fails_cleanly(self):
        t = self.char("NoDump", "Acct1")
        code, res = gearsets.snapshot(self.conn, self.eq_dir, t)
        self.assertEqual(code, 400)
        self.assertIn("outputfile", res["error"])

    def test_fit_counts(self):
        t = self.char("Rakthor", "Acct1")
        self.dump("Rakthor", [
            dump_line("Waist", "Monk Belt", 200),
            dump_line("General1-Slot1", "Bag Mask", 208),
        ])
        sid = self.gearset("Kit", t, [("Waist", 200), ("Face", 208), ("Head", 207)])
        fits = gearsets.fit_counts(self.conn, self.eq_dir)
        self.assertEqual(fits[sid], {"worn": 1, "present": 2, "total": 3})


class TestLuaExport(_World):
    def test_format_matches_forge_contract(self):
        t = self.char("Rakthor", "Acct1")
        v = self.char("Corvane", "Acct2")
        self.dump("Rakthor", [])
        self.dump("Corvane", [dump_line("Bank2", "Parcel Boots", 205)])
        self.gearset("Kit", t, [("Feet", 205)])
        result = self.plan()
        built = gearsets.build_plans_lua(result)
        self.assertIsNotNone(built)
        text = built["text"]
        self.assertIn("plans = {", text)
        self.assertIn('name = "Kit"', text)                 # back-compat mirror
        self.assertIn('from = "Corvane"', text)
        self.assertIn('to = "Rakthor"', text)
        self.assertIn('fromLoc = "Bank2"', text)
        self.assertIn('slot = "Feet"', text)
        self.assertIn("attuneRisk = false", text)
        parcel = gearsets.build_parcel_source_lua(built["plans"])
        self.assertIn("[205]=true", parcel)
        self.assertIn("Gear Plan: Kit -> Rakthor (1)", parcel)

    def test_no_moves_returns_none(self):
        t = self.char("Rakthor", "Acct1")
        self.dump("Rakthor", [dump_line("Waist", "Monk Belt", 200)])
        self.gearset("Kit", t, [("Waist", 200)])
        self.assertIsNone(gearsets.build_plans_lua(self.plan()))


class TestSourceFreshness(_World):
    """A plan is only as true as the dump it was routed from (2026-07-22: a plan
    promised gear Corvane no longer had). Every row carries the age of the dump that
    justified it, and move rows past the window are called out."""

    HOUR = 3600

    def setUp(self):
        super().setUp()
        self.rakthor = self.char("Rakthor", "Acct1")
        self.marnok = self.char("Marnok", "Acct2", cls="Bard")
        self.dump("Rakthor", [dump_line("Waist", "Monk Belt", 200)])
        self.dump("Marnok", [dump_line("Bank2", "Parcel Boots", 205)])
        self.set_id = self.gearset("Kit", self.rakthor,
                                   [("Waist", 200), ("Feet", 205)])

    def age(self, name, hours):
        p = os.path.join(self.eq_dir, "%s_frostreaver-Inventory.txt" % name)
        t = time.time() - hours * self.HOUR
        os.utime(p, (t, t))

    def plan(self, login_ids=None, stale_h=None):
        return gearsets.build_plans(self.conn, self.eq_dir, login_ids,
                                    db=ITEMDB, stale_h=stale_h)

    def row(self, res, iid):
        return next(r for p in res["plans"] for r in p["rows"] if r["item_id"] == iid)

    def test_fresh_holder_is_not_flagged(self):
        self.age("Rakthor", 1)
        self.age("Marnok", 2)
        r = self.row(self.plan(), 205)
        self.assertEqual(r["status"], "parcel")
        self.assertFalse(r["stale_source"])
        self.assertLessEqual(r["source_age_h"], 3)

    def test_old_holder_dump_flags_the_move_row(self):
        self.age("Rakthor", 1)
        self.age("Marnok", 30 * 24)
        res = self.plan()
        r = self.row(res, 205)
        self.assertEqual(r["status"], "parcel")      # still routed, never refused
        self.assertTrue(r["stale_source"])
        self.assertIn("Marnok", r["note"])
        self.assertIn("30d", r["note"].replace("30 d", "30d"))
        self.assertEqual(res["summary"]["stale_moves"], 1)

    def test_stale_worn_row_does_not_count_as_a_stale_move(self):
        """The target wearing it already is not a promise about anything."""
        self.age("Rakthor", 30 * 24)
        self.age("Marnok", 1)
        res = self.plan()
        self.assertEqual(self.row(res, 200)["status"], "worn")
        self.assertTrue(self.row(res, 200)["stale_source"])
        self.assertEqual(res["summary"]["stale_moves"], 0)

    def test_window_is_configurable(self):
        self.age("Rakthor", 1)
        self.age("Marnok", 50)
        self.assertFalse(self.row(self.plan(), 205)["stale_source"])
        self.assertTrue(self.row(self.plan(stale_h=24), 205)["stale_source"])

    def test_workorder_card_carries_holder_dump_age(self):
        self.age("Rakthor", 1)
        self.age("Marnok", 30 * 24)
        card = next(h for h in self.plan()["workorder"] if h["holder"] == "Marnok")
        self.assertTrue(card["stale"])
        self.assertGreaterEqual(card["dump_age_h"], 30 * 24 - 1)

    def test_target_dump_age_flagged_on_the_plan(self):
        self.age("Rakthor", 30 * 24)
        self.age("Marnok", 1)
        p = self.plan()["plans"][0]
        self.assertTrue(p["target"]["stale"])
        self.assertEqual(p["stale_rows"], 1)

    def test_oldest_source_is_reported(self):
        self.age("Rakthor", 1)
        self.age("Marnok", 100)
        self.assertGreaterEqual(self.plan()["summary"]["oldest_source_h"], 99)

    def test_undumped_holder_has_no_false_freshness(self):
        """No dump = no age; must never read as 'fresh'."""
        self.char("Ghost", "Acct3")
        self.age("Rakthor", 1)
        self.age("Marnok", 1)
        r = self.row(self.plan(), 200)
        self.assertIsNotNone(r["source_age_h"])
        blocked = [x for p in self.plan()["plans"] for x in p["rows"]
                   if x["status"] in ("missing", "notrade", "lore", "reserved")]
        for b in blocked:
            self.assertIsNone(b["source_age_h"])
            self.assertFalse(b["stale_source"])


class TestCandidates(_World):
    def setUp(self):
        super().setUp()
        self.trix = self.char("Rakthor", "Acct1", cls="Rogue")
        self.vay = self.char("Corvane", "Acct2", cls="Enchanter")
        self.dump("Rakthor", [
            dump_line("Chest", "Bronze Breastplate", 300),
            dump_line("General1-Slot1", "Chest Aug", 303),
            dump_line("Ear", "Plain Hoop", 304),
        ])
        self.dump("Corvane", [
            dump_line("Bank1", "Silver Breastplate", 301),
            dump_line("General2-Slot2", "Caster Vest", 302),
        ])

    def cands(self, **kw):
        return gearsets.candidates(self.conn, self.eq_dir, db=ITEMDB, **kw)

    def slot(self, result, name):
        return next(s for s in result["slots"] if s["slot"] == name)

    def test_slot_mapping_ranking_and_aug_exclusion(self):
        chest = self.slot(self.cands(), "Chest")
        names = [c["name"] for c in chest["items"]]
        self.assertEqual(names, ["Silver Breastplate", "Bronze Breastplate", "Caster Vest"])
        self.assertNotIn("Chest Aug", names)                # itemtype 54 never offered
        ear = self.slot(self.cands(), "Ear")
        self.assertTrue(ear["paired"])
        self.assertEqual([c["name"] for c in ear["items"]], ["Plain Hoop"])
        self.assertFalse(self.slot(self.cands(), "Legs")["items"])

    def test_class_filter(self):
        chest = self.slot(self.cands(class_name="Rogue"), "Chest")
        names = [c["name"] for c in chest["items"]]
        self.assertNotIn("Caster Vest", names)              # ENC-only
        self.assertIn("Silver Breastplate", names)          # unrestricted
        chest = self.slot(self.cands(class_name="Enchanter"), "Chest")
        self.assertIn("Caster Vest", [c["name"] for c in chest["items"]])

    def test_reservation_and_exclude_self(self):
        sid = self.gearset("Tank kit", self.trix, [("Chest", 301)])
        chest = self.slot(self.cands(), "Chest")
        silver = next(c for c in chest["items"] if c["item_id"] == 301)
        self.assertEqual(silver["free"], 0)                 # claimed by Tank kit
        self.assertIn("Tank kit", silver["reserved_by"])
        # editing Tank kit itself: its own claim doesn't lock the piece
        chest = self.slot(self.cands(exclude_set_id=sid), "Chest")
        silver = next(c for c in chest["items"] if c["item_id"] == 301)
        self.assertEqual(silver["free"], 1)

    def test_holder_and_owned(self):
        chest = self.slot(self.cands(), "Chest")
        bronze = next(c for c in chest["items"] if c["item_id"] == 300)
        self.assertEqual(bronze["holder"], "Rakthor")
        self.assertEqual(bronze["bucket"], "worn")
        self.assertEqual(bronze["owned"], 1)
        self.assertEqual(bronze["holders"][0]["holder"], "Rakthor")   # full list carried

    def test_class_weighted_score_reorders(self):
        # Enchanter (caster weights): Caster Vest's 50 mana beats Silver's raw AC.
        chest = self.slot(self.cands(class_name="Enchanter"), "Chest")
        self.assertEqual(chest["items"][0]["name"], "Caster Vest")
        # Rogue (melee weights): AC dominates, Silver first, Caster Vest filtered out.
        chest = self.slot(self.cands(class_name="Rogue"), "Chest")
        self.assertEqual(chest["items"][0]["name"], "Silver Breastplate")
        self.assertTrue(all(c["score"] >= 0 for c in chest["items"]))


class TestStealAndClear(_World):
    def setUp(self):
        super().setUp()
        self.monk = self.char("Monka", "Acct1", cls="Monk")
        self.sk = self.char("Sking", "Acct2", cls="Shadow Knight")
        self.dump("Monka", [dump_line("Chest", "Bronze Breastplate", 300)])
        self.dump("Sking", [])
        self.monk_set = self.gearset("Monk kit", self.monk, [("Chest", 300)])

    def items_of(self, sid):
        return [r["item_id"] for r in self.conn.execute(
            "SELECT item_id FROM gear_set_items WHERE set_id=?", (sid,))]

    def test_steal_moves_claim_from_donor_set(self):
        code, res = gearsets.save_set(self.conn, {
            "name": "SK kit", "assigned_char_id": self.sk, "steal": True,
            "items": [{"item_id": 300, "item_name": "Bronze Breastplate", "slot": "Chest"}],
        }, eq_dir=self.eq_dir)
        self.assertEqual(code, 200, res)
        self.assertEqual(res["taken"], [{"item": "Bronze Breastplate", "from_set": "Monk kit"}])
        self.assertEqual(self.items_of(self.monk_set), [])         # monk lost the claim
        self.assertEqual(self.items_of(res["id"]), [300])

    def test_no_steal_when_copies_are_plentiful(self):
        self.dump("Sking", [dump_line("Bank1", "Bronze Breastplate", 300)])   # 2nd copy
        code, res = gearsets.save_set(self.conn, {
            "name": "SK kit", "assigned_char_id": self.sk, "steal": True,
            "items": [{"item_id": 300, "item_name": "Bronze Breastplate", "slot": "Chest"}],
        }, eq_dir=self.eq_dir)
        self.assertEqual(code, 200, res)
        self.assertEqual(res["taken"], [])                          # fungible: both fit
        self.assertEqual(self.items_of(self.monk_set), [300])       # monk untouched

    def test_empty_save_frees_claims_keeps_set(self):
        code, res = gearsets.save_set(self.conn, {"items": []}, self.monk_set,
                                      eq_dir=self.eq_dir)
        self.assertEqual(code, 200, res)
        self.assertEqual(self.items_of(self.monk_set), [])
        self.assertEqual(len(gearsets.list_sets(self.conn)), 1)     # set shell kept


class TestCompCheck(_World):
    def setUp(self):
        super().setUp()
        self.m1 = self.char("Monka", "Acct1", cls="Monk")
        self.m2 = self.char("Monkb", "Acct2", cls="Monk")
        self.m3 = self.char("Nosets", "Acct3", cls="Cleric")
        self.m4 = self.char("Outside", "Acct4", cls="Rogue")
        self.dump("Monka", [dump_line("Waist", "Monk Belt", 200),
                            dump_line("SharedBank1", "Shared Ring", 204)])
        self.dump("Monkb", [])

    def check(self, ids):
        return gearsets.comp_gear_check(self.conn, self.eq_dir, ids)

    def test_overlap_when_two_sets_want_the_only_copy(self):
        self.gearset("Monka kit", self.m1, [("Waist", 200)])
        self.gearset("Monkb kit", self.m2, [("Waist", 200)])
        gc = self.check([self.m1, self.m2, self.m3])
        self.assertEqual(len(gc.get("overlaps")), 1)
        o = gc["overlaps"][0]
        self.assertEqual((o["item"], o["need"], o["owned"]), ("Monk Belt", 2, 1))
        self.assertEqual(sorted(o["sets"]), ["Monka kit", "Monkb kit"])
        self.assertEqual(gc["no_set"], ["Nosets"])
        self.assertEqual(len(gc["toons"]), 2)

    def test_enough_copies_is_clean_and_fungible(self):
        # 2 copies exist -> both sets fine; nobody cares WHICH copy is whose
        self.dump("Monkb", [dump_line("Waist", "Monk Belt", 200)])
        self.gearset("Monka kit", self.m1, [("Waist", 200)])
        self.gearset("Monkb kit", self.m2, [("Waist", 200)])
        gc = self.check([self.m1, self.m2])
        self.assertEqual(gc["overlaps"], [])
        self.assertEqual(gc["no_set"], [])

    def test_outside_set_pressure_is_a_soft_warning(self):
        self.gearset("Monka kit", self.m1, [("Fingers", 204)])
        self.gearset("Outside kit", self.m4, [("Fingers", 204)])   # not in the comp
        gc = self.check([self.m1])
        self.assertEqual(gc["overlaps"], [])                       # comp alone is fine
        self.assertEqual(len(gc["outside"]), 1)
        o = gc["outside"][0]
        self.assertEqual((o["item"], o["comp_need"], o["outside_need"], o["owned"]),
                         ("Shared Ring", 1, 1, 1))
        self.assertEqual(o["outside_sets"], ["Outside kit"])

    def test_newest_active_set_wins_per_toon(self):
        a = self.gearset("Old kit", self.m1, [("Waist", 200)])
        b = self.gearset("New kit", self.m1, [("Fingers", 204)])
        self.conn.execute("UPDATE gear_sets SET updated_at=100 WHERE id=?", (a,))
        self.conn.execute("UPDATE gear_sets SET updated_at=200 WHERE id=?", (b,))
        self.conn.commit()
        gc = self.check([self.m1])
        self.assertEqual(gc["toons"][0]["set"], "New kit")


class TestApiRoundtrip(unittest.TestCase):
    """CRUD through api.handle (no item DB / plan calls — those parse items.txt.gz)."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.old_db, api.DB_PATH = api.DB_PATH, self.db_path
        self.old_eq, api.EQ_DIR = api.EQ_DIR, tempfile.mkdtemp()

    def tearDown(self):
        api.DB_PATH = self.old_db
        shutil.rmtree(api.EQ_DIR, ignore_errors=True)
        api.EQ_DIR = self.old_eq
        os.unlink(self.db_path)

    def test_crud(self):
        code, res = api.handle("POST", "/gearsets", {
            "name": "Imported", "items": [
                {"item_id": 200, "item_name": "Monk Belt", "slot": "Waist"}]})
        self.assertEqual(code, 200, res)
        sid = res["id"]
        code, res = api.handle("GET", "/gearsets")
        self.assertEqual(code, 200)
        mine = [s for s in res["sets"] if s["id"] == sid]
        self.assertEqual(len(mine), 1)
        self.assertEqual(len(mine[0]["items"]), 1)
        self.assertIn("fit", mine[0])
        code, _ = api.handle("PUT", "/gearsets/%d" % sid, {"active": 0, "name": "Renamed"})
        self.assertEqual(code, 200)
        code, res = api.handle("GET", "/gearsets")
        row = [s for s in res["sets"] if s["id"] == sid][0]
        self.assertEqual(row["active"], 0)
        self.assertEqual(row["name"], "Renamed")
        code, _ = api.handle("DELETE", "/gearsets/%d" % sid, {})
        self.assertEqual(code, 200)
        code, res = api.handle("GET", "/gearsets")
        self.assertFalse([s for s in res["sets"] if s["id"] == sid])


class TestSharedBankCapacity(_World):
    """The shared bank is 8 top-level slots (item OR bag). A swap parks one item
    there per hand-off, so free space is a hard per-round ceiling — the planner has
    to say when a plan needs more rounds than one."""

    def _shared(self, entries):
        """entries: list of (name, bag_contents|None). Empty top slots are padded
        out to 8 the way a real dump always lists them."""
        lines = []
        for i in range(1, 9):
            name, kids = entries[i - 1] if i <= len(entries) else ("Empty", None)
            lines.append(dump_line("SharedBank%d" % i, name, 0))
            for j, kid in enumerate(kids or [], start=1):
                lines.append(dump_line("SharedBank%d-Slot%d" % (i, j), kid, 0))
        return lines

    def test_counts_empty_slots_and_bag_interiors(self):
        self.dump("Solo", self._shared([
            ("Dreamweave Satchel", ["Ore", "Empty", "Empty", "Empty"]),   # 3 free of 4
            ("Loose Ring", None),                                          # occupied
        ]))
        cap = gearsets.shared_capacity(
            os.path.join(self.eq_dir, "Solo_frostreaver-Inventory.txt"))
        self.assertTrue(cap["ok"])
        self.assertEqual(cap["free_direct"], 6)     # slots 3..8
        self.assertEqual(cap["free_bag"], 3)
        self.assertEqual(cap["free_total"], 9)

    def test_a_full_bank_reports_no_space(self):
        self.dump("Solo", self._shared([(n, None) for n in
                                        ["a", "b", "c", "d", "e", "f", "g", "h"]]))
        cap = gearsets.shared_capacity(
            os.path.join(self.eq_dir, "Solo_frostreaver-Inventory.txt"))
        self.assertEqual(cap["free_total"], 0)

    def test_missing_or_unreadable_dump_is_not_fatal(self):
        cap = gearsets.shared_capacity(os.path.join(self.eq_dir, "nope.txt"))
        self.assertFalse(cap["ok"])
        self.assertEqual(cap["free_total"], 0)

    def test_plan_flags_overflow_and_round_count(self):
        tgt = self.char("Target", "acct1", cls="Cleric")
        self.char("Holder", "acct1", cls="Cleric")       # same account -> swap route
        # Two free shared slots only: seven full top slots + a bag with 2 free.
        nearly_full = self._shared([(n, None) for n in "abcdefg"] +
                                   [("Bag", ["Empty", "Empty"])])
        self.dump("Target", nearly_full)
        moves = [("Waist", 200), ("Chest", 203), ("Primary", 209)]
        self.dump("Holder", nearly_full +
                  [dump_line("General1-Slot%d" % (i + 1), ITEMDB[iid]["name"], iid)
                   for i, (_, iid) in enumerate(moves)])
        self.gearset("Set", tgt, moves)
        res = self.plan()
        plan = res["plans"][0]
        sb = plan["shared_bank"]
        self.assertEqual(plan["counts"].get("swap"), 3)
        self.assertEqual(sb["free_total"], 2)
        self.assertTrue(sb["overflow"])
        self.assertEqual(sb["rounds"], 2)            # 3 pieces / 2 slots

    def test_freshest_dump_on_the_account_wins(self):
        """Shared bank is account storage every toon's dump repeats — a stale copy
        disagrees with a fresh one, and believing the stale one over-promises space."""
        a = self.acct("acct1")
        self.char("Old", "acct1", cls="Cleric")
        self.char("New", "acct1", cls="Cleric")
        self.dump("Old", self._shared([("Bag", ["Empty"] * 6)]))          # looks roomy
        self.dump("New", self._shared([("Bag", ["x"] * 6)]))              # actually full
        old = os.path.join(self.eq_dir, "Old_frostreaver-Inventory.txt")
        os.utime(old, (time.time() - 7200, time.time() - 7200))
        world = gearsets._load_world(self.conn, self.eq_dir)
        cap = gearsets._shared_by_account(world)[a]
        self.assertEqual(cap["observed_from"], "New")
        self.assertEqual(cap["free_bag"], 0)


if __name__ == "__main__":
    unittest.main()
