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

from mychars import api, db as dbm, gear as gearmod, gearsets  # noqa: E402

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
    # weapons. The Avatar slot is defined by PROC (2434 = "Avatar"), not by mask:
    # only Primal Velium / Ancient Prismatic weapons carry it.
    305: _item("Primal Velium Fist Wraps", slots=8192 | 16384, classes=0, ac=0, hp=5,
                mana=0, itemtype=0, proceffect=gearsets.AVATAR_PROC_SPELL),
    306: _item("Great Cleaver", slots=8192, classes=0, ac=0, hp=20, mana=0,
                itemtype=1),      # 2H Slashing, no proc
    307: _item("Primal Velium Reinforced Bow", slots=2048, classes=0, ac=0, hp=5,
                itemtype=5, proceffect=gearsets.AVATAR_PROC_SPELL),   # Range, not Primary
    308: _item("Plain Shortsword", slots=8192 | 16384, classes=0, ac=0, hp=8,
                itemtype=0, proceffect=1234),      # a weapon with the WRONG proc
    # CARRIED slots. Real shapes, straight off the sodeq export:
    #   White Skystrider Whistle  = slots 4194304 (Ammo), itemtype 68, NO TRADE
    #   Trinket of the Far Frozen Wastes = slots 0, click effect, NO TRADE
    # Both are account-bound claim rewards.
    310: _item("White Skystrider Whistle", slots=gearsets.AMMO_BIT, classes=0,
                itemtype=gearsets.MOUNT_ITEMTYPE, fvnodrop=1, clickeffect=54896,
                maxcharges=-1),
    311: _item("Tan Rope Bridle", slots=gearsets.AMMO_BIT, classes=0,
                itemtype=gearsets.MOUNT_ITEMTYPE, clickeffect=2919,
                maxcharges=-1),                                        # tradeable mount
    312: _item("Trinket of the Far Frozen Wastes", slots=0, classes=0,
                itemtype=72, fvnodrop=1, clickeffect=54825, maxcharges=-1),
    313: _item("Plain Arrow", slots=gearsets.AMMO_BIT, classes=0, itemtype=27),
    314: _item("Clicky Breastplate", slots=131072, classes=0, ac=30, clickeffect=999,
                maxcharges=-1),
    # A consumable: slotless clicky like the Trinket, but FINITE charges. Real shape
    # off the user's roster (Gate Potion, itemtype 21, stack 10, 1 charge).
    315: _item("Gate Potion", slots=0, classes=0, itemtype=21, clickeffect=35,
                maxcharges=1),
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

    def gearset(self, name, target_id, item_rows, active=1, class_name=None):
        # class_name defaults to the target's class: comp_gear_map matches candidates
        # by CLASS (a loadout is a role, not a toon's property), so a set with no
        # class is offered to nobody.
        if class_name is None and target_id:
            row = self.conn.execute("SELECT class_name FROM characters WHERE id=?",
                                    (target_id,)).fetchone()
            class_name = row["class_name"] if row else ""
        code, res = gearsets.save_set(self.conn, {
            "name": name, "assigned_char_id": target_id, "active": active,
            "class_name": class_name or "",
            "items": [{"item_id": iid, "item_name": ITEMDB[iid]["name"], "slot": slot}
                      for slot, iid in item_rows]}, db=ITEMDB)
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


class TestShortfallVsMissing(_World):
    """"nobody on the roster has one" must only ever mean exactly that. The user
    2026-08-09: "it says scavo missing vel brawl stick, but he has it in his bank" —
    he owned it, in his own bags. The set had picked the same weapon into BOTH the
    2-Hander and Avatar slots (itemtype 4 AND proc 2434, so it legitimately qualifies
    for either), the first row consumed the only copy, and the second reported the one
    thing that was flatly untrue."""

    def setUp(self):
        super().setUp()
        self.t = self.char("Rakthor", "Acct1")

    def row_for(self, iid):
        rows = [r for p in self.plan()["plans"] for r in p["rows"] if r["item_id"] == iid]
        return rows[-1]                       # the row left short

    def test_owning_it_but_claiming_it_twice_is_a_shortfall_not_missing(self):
        # A plain paired slot: one ring, two Fingers rows. Nothing procs anything, so
        # there is no coverage story — the set simply asked for more than exists.
        self.dump("Rakthor", [dump_line("General1-Slot1", "Shared Ring", 204)])
        self.gearset("Kit", self.t, [("Fingers", 204), ("Fingers", 204)])
        r = self.row_for(204)
        self.assertEqual(r["status"], "shortfall")
        self.assertIn("2", r["note"])                     # claimed twice
        self.assertIn("Fingers", r["note"])               # names the slot involved
        self.assertNotIn("nobody", r["note"])

    def test_genuinely_absent_still_reads_missing(self):
        self.dump("Rakthor", [])
        self.gearset("Kit", self.t, [("Head", 207)])
        self.assertEqual(self.row_for(207)["status"], "missing")
        self.assertIn("nobody", self.row_for(207)["note"])

    def test_two_copies_owned_satisfies_both_slots(self):
        self.dump("Rakthor", [
            dump_line("General1-Slot1", "Primal Velium Fist Wraps", 305),
            dump_line("General1-Slot2", "Primal Velium Fist Wraps", 305),
        ])
        self.gearset("Kit", self.t, [("2-Hander", 305), ("Avatar", 305)])
        got = [r["status"] for p in self.plan()["plans"] for r in p["rows"]]
        self.assertEqual(got, ["have", "have"])

    def test_a_shortfall_counts_as_blocked_not_satisfied(self):
        self.dump("Rakthor", [dump_line("General1-Slot1", "Shared Ring", 204)])
        self.gearset("Kit", self.t, [("Fingers", 204), ("Fingers", 204)])
        summary = self.plan()["summary"]
        self.assertEqual(summary["satisfied"], 1)
        self.assertEqual(summary["blocked"], 1)

    def test_a_second_claim_on_the_same_PROC_is_covered_not_short(self):
        """reported 2026-08-09: "both her primary / secondary and / avatar slots all should
        have been considered ... she has 2 avatar weapons, 1 for 1hander and another
        for 2hander." Scavo wears an Ancient Prismatic Battlehammer (1H, procs Avatar)
        and keeps a Primal Velium Brawl Stick (2H, same proc) in her bags. A third
        claim on one of them buys nothing — that is redundancy, not a shortage."""
        self.dump("Rakthor", [dump_line("General1-Slot1", "Primal Velium Fist Wraps", 305)])
        self.gearset("Kit", self.t, [("2-Hander", 305), ("Avatar", 305)])
        r = self.row_for(305)
        self.assertEqual(r["status"], "covered")
        self.assertIn("2-Hander", r["note"])
        self.assertIn("clear this slot", r["note"])
        # ...and it must not sit in the blocked pile forever
        s = self.plan()["summary"]
        self.assertEqual(s["blocked"], 0)
        self.assertEqual(s["satisfied"], 2)

    def test_two_DIFFERENT_avatar_weapons_both_route(self):
        """The real Scavo shape: a 1H and a 2H that both proc Avatar, one in each
        slot. Two distinct items, so nothing is redundant and nothing is short."""
        self.dump("Rakthor", [
            dump_line("General1-Slot1", "Primal Velium Fist Wraps", 305),   # 1H, proc
            dump_line("General1-Slot2", "Primal Velium Reinforced Bow", 307),  # proc
        ])
        self.gearset("Kit", self.t, [("Avatar", 305), ("2-Hander", 306), ("Range", 307)])
        got = sorted(r["status"] for p in self.plan()["plans"] for r in p["rows"])
        self.assertEqual(got, ["have", "have", "missing"])   # 306 genuinely absent

    def test_a_redundant_avatar_slot_never_shops_a_siblings_copy(self):
        """THE ROKHAN MOVE. Beastlord Main names the same Primal Velium Brawl Stick in
        BOTH 2-Hander and Avatar. Scavo's own copy satisfied the first row; the second
        went shopping and told the user to log in Rokhan and mail his copy over for nothing
        (2026-08-10). The Avatar slot means "keep a weapon that procs Avatar" — the
        2-Hander already is one, so there is nothing to fetch.

        The old guard sat in the "nobody owns one" branch, so it only fired when the
        item was unobtainable: the moment a sibling had a copy, routing won."""
        other = self.char("Corvane", "Acct2", cls="Enchanter")
        self.dump("Rakthor", [dump_line("General1-Slot1", "Primal Velium Fist Wraps", 305)])
        self.dump("Corvane", [dump_line("General1-Slot1", "Primal Velium Fist Wraps", 305)])
        self.gearset("Kit", self.t, [("2-Hander", 305), ("Avatar", 305)])
        rows = {r["slot"]: r for p in self.plan()["plans"] for r in p["rows"]}
        self.assertEqual(rows["2-Hander"]["status"], "have")      # his own, in his bags
        self.assertEqual(rows["Avatar"]["status"], "covered")     # NOT parcel
        self.assertIn("2-Hander", rows["Avatar"]["note"])
        self.assertTrue(other)

    def test_two_copies_already_held_stay_claimed(self):
        """The other half: coverage sits BELOW the have/worn checks on purpose. A toon
        already holding two copies keeps both claimed — the user 2026-08-08 wanted the
        Avatar slot to tie up a spare weapon he owns. The rule is only "don't go
        shopping for a spare you don't need"."""
        self.dump("Rakthor", [
            dump_line("General1-Slot1", "Primal Velium Fist Wraps", 305),
            dump_line("General1-Slot2", "Primal Velium Fist Wraps", 305),
        ])
        self.gearset("Kit", self.t, [("2-Hander", 305), ("Avatar", 305)])
        got = [r["status"] for p in self.plan()["plans"] for r in p["rows"]]
        self.assertEqual(got, ["have", "have"])


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
        self.assertEqual(fits[sid],
                         {"worn": 1, "present": 2, "total": 3, "worn_total": 3})


class TestVirtualSlots(_World):
    """The Avatar slot: a weapon the toon must OWN and keep, but never wears."""

    def test_avatar_slot_offers_weapons_and_is_flagged_extra(self):
        t = self.char("Rakthor", "Acct1")
        self.dump("Rakthor", [])
        res = gearsets.candidates(self.conn, self.eq_dir, db=ITEMDB, target_char_id=t)
        by_slot = {s["slot"]: s for s in res["slots"]}
        self.assertIn("Avatar", by_slot)
        self.assertTrue(by_slot["Avatar"]["extra"])
        self.assertFalse(by_slot["Avatar"]["paired"])
        # not a worn slot, so it must never be treated as a coverage hole
        self.assertNotIn(("Avatar", 0), gearsets._expected_slot_rows())

    def test_avatar_piece_is_held_not_worn(self):
        """It sits in the bags by design: counted as on-hand, never as a missing
        worn piece - otherwise a complete set reads N-1/N worn forever."""
        t = self.char("Rakthor", "Acct1")
        self.dump("Rakthor", [
            dump_line("Waist", "Monk Belt", 200),
            dump_line("General1-Slot1", "Proc Fist", 305),
        ])
        sid = self.gearset("Kit", t, [("Waist", 200), ("Avatar", 305)])
        fits = gearsets.fit_counts(self.conn, self.eq_dir)
        self.assertEqual(fits[sid]["worn"], 1)        # belt only
        self.assertEqual(fits[sid]["worn_total"], 1)  # Avatar excluded -> set reads FULL
        self.assertEqual(fits[sid]["present"], 2)     # but both are on hand
        self.assertEqual(fits[sid]["total"], 2)

    def test_resnapshot_preserves_the_avatar_claim(self):
        """A same-name snapshot overwrites the set and save_set deletes every row
        first. The dump has no 'Avatar' slot to restore it from, so without the
        carry-forward the claim vanishes silently and the router re-gifts the weapon."""
        t = self.char("Rakthor", "Acct1")
        self.dump("Rakthor", [dump_line("Waist", "Monk Belt", 200)])
        code, res = gearsets.snapshot(self.conn, self.eq_dir, t)
        self.assertEqual(code, 200)
        sid = res["id"]
        s = [x for x in gearsets.list_sets(self.conn) if x["id"] == sid][0]
        gearsets.save_set(self.conn, {
            "items": list(s["items"]) + [{"item_id": 305, "item_name": "Proc Fist",
                                          "slot": "Avatar", "slot_index": 0}],
        }, set_id=sid)
        code, _ = gearsets.snapshot(self.conn, self.eq_dir, t)   # re-snapshot, same name
        self.assertEqual(code, 200)
        after = [x for x in gearsets.list_sets(self.conn) if x["id"] == sid][0]
        avatar = [i for i in after["items"] if i["slot"] == "Avatar"]
        self.assertEqual([i["item_id"] for i in avatar], [305])

    def test_avatar_slot_is_defined_by_the_proc_not_the_weapon_mask(self):
        """reported 2026-08-09: "the only things that can give avatar are primal velium
        weapons or ancient prismatic weapons". Confirmed against the real item DB —
        proc 2434 resolves to "Avatar" (app/spell-effects.json.gz) and exactly 24
        items carry it, all Primal Velium / Ancient Prismatic, nothing else. The old
        Primary|Secondary mask offered every weapon on the roster instead."""
        t = self.char("Rakthor", "Acct1")
        self.dump("Rakthor", [
            dump_line("General1-Slot1", "Primal Velium Fist Wraps", 305),
            dump_line("General1-Slot2", "Great Cleaver", 306),
            dump_line("General1-Slot3", "Plain Shortsword", 308),
        ])
        res = gearsets.candidates(self.conn, self.eq_dir, db=ITEMDB, target_char_id=t)
        ids = [c["item_id"] for c in {s["slot"]: s for s in res["slots"]}["Avatar"]["items"]]
        self.assertEqual(ids, [305], "only the Avatar proc belongs in this slot")

    def test_avatar_slot_keeps_weapons_the_old_mask_would_have_hidden(self):
        """Avatar weapons are not all one-handers: Fist Wraps/Warswords sit at
        Primary|Secondary and the Primal Velium Reinforced Bow at Range (2048), which
        a Primary|Secondary mask cut out entirely."""
        t = self.char("Rakthor", "Acct1")
        self.dump("Rakthor", [
            dump_line("General1-Slot1", "Primal Velium Reinforced Bow", 307)])
        res = gearsets.candidates(self.conn, self.eq_dir, db=ITEMDB, target_char_id=t)
        by_slot = {s["slot"]: s for s in res["slots"]}
        self.assertIn(307, [c["item_id"] for c in by_slot["Avatar"]["items"]])
        # ...and it is still a normal Range candidate, since it really is a bow
        self.assertIn(307, [c["item_id"] for c in by_slot["Range"]["items"]])

    def test_avatar_slot_is_not_account_bound(self):
        """Unlike Mount / WW Clicky, a weapon can be parcelled anywhere — the account
        cut must not leak across the virtual slots."""
        t = self.char("Rakthor", "Acct1")
        other = self.char("Corvane", "Acct2", cls="Enchanter")
        self.dump("Rakthor", [])
        self.dump("Corvane", [dump_line("General1-Slot1", "Primal Velium Fist Wraps", 305)])
        res = gearsets.candidates(self.conn, self.eq_dir, db=ITEMDB, target_char_id=t)
        by_slot = {s["slot"]: s for s in res["slots"]}
        self.assertFalse(by_slot["Avatar"]["account_bound"])
        self.assertIn(305, [c["item_id"] for c in by_slot["Avatar"]["items"]])
        self.gearset("Kit", t, [("Avatar", 305)])
        row = self.plan()["plans"][0]["rows"][0]
        self.assertEqual(row["status"], "parcel")
        self.assertEqual(row["holder"], "Corvane")

    def test_two_hander_slot_filters_by_itemtype_not_slot_mask(self):
        """The whole reason this needs itemtype: verified against the real item DB,
        a 1H (Wurmslayer, type 0) and a 2H (Jagged Blade of War, type 1) are BOTH
        slots=8192, so a mask-only filter would offer every one-hander as a 2H."""
        t = self.char("Rakthor", "Acct1")
        self.dump("Rakthor", [                       # must OWN them to be offered
            dump_line("General1-Slot1", "Proc Fist", 305),
            dump_line("General1-Slot2", "Great Cleaver", 306),
        ])
        res = gearsets.candidates(self.conn, self.eq_dir, db=ITEMDB, target_char_id=t)
        by_slot = {s["slot"]: s for s in res["slots"]}
        self.assertIn("2-Hander", by_slot)
        self.assertTrue(by_slot["2-Hander"]["extra"])
        ids = [c["item_id"] for c in by_slot["2-Hander"]["items"]]
        self.assertIn(306, ids, "type 1 (2H Slashing) belongs here")
        self.assertNotIn(305, ids, "type 0 one-hander must not be offered as a 2H")
        # ...but the one-hander is still a valid Avatar pick
        self.assertIn(305, [c["item_id"] for c in by_slot["Avatar"]["items"]])

    def test_carried_slots_never_add_to_worn_stats(self):
        """reported 2026-08-08: carried weapons must not inflate a player's totals
        anywhere. Everything that totals stats reads the dump's WORN rows, and these
        are not real EQ slots - so the guarantee is that no dump can ever name them."""
        for slot in gearsets.EXTRA_SLOT_NAMES:
            self.assertNotIn(slot, gearmod.WORN_SLOTS)
            self.assertNotIn((slot, 0), gearsets._expected_slot_rows())

    def test_avatar_claim_reserves_the_weapon_against_other_sets(self):
        """The whole point: one copy, one promise. A second set must not be able to
        treat a weapon already claimed as an Avatar piece as free."""
        t = self.char("Rakthor", "Acct1")
        o = self.char("Corvane", "Acct2")
        self.dump("Rakthor", [dump_line("General1-Slot1", "Proc Fist", 305)])
        self.dump("Corvane", [])
        self.gearset("MonkKit", t, [("Avatar", 305)])
        res = gearsets.candidates(self.conn, self.eq_dir, db=ITEMDB, target_char_id=o)
        by_slot = {s["slot"]: s for s in res["slots"]}
        pick = [c for c in by_slot["Primary"]["items"] if c["item_id"] == 305]
        self.assertTrue(pick, "the weapon should still be listable")
        self.assertEqual(pick[0]["free"], 0, "already promised to the Avatar slot")


class TestCarriedAccountBoundSlots(_World):
    """Mount / WW Clicky. reported 2026-08-09: "would be nice to have a mount section so
    I can track where these mounts are ... they have to only look within the toons in
    that same account the set is applied to because its an account bound only item"."""

    def setUp(self):
        super().setUp()
        self.rakthor = self.char("Rakthor", "Acct1")            # the target
        self.belwyn = self.char("Belwyn", "Acct1", cls="Bard")  # same account
        self.corvane = self.char("Corvane", "Acct2", cls="Enchanter")   # other account

    def slots(self, target_id=None):
        res = gearsets.candidates(self.conn, self.eq_dir, db=ITEMDB,
                                  target_char_id=target_id or self.rakthor)
        return {s["slot"]: s for s in res["slots"]}

    def test_slots_exist_and_are_carried_not_worn(self):
        self.dump("Rakthor", [])
        by_slot = self.slots()
        for slot in ("Mount", "WW Clicky"):
            self.assertIn(slot, by_slot)
            self.assertTrue(by_slot[slot]["extra"])
            self.assertTrue(by_slot[slot]["carried"])
            self.assertTrue(by_slot[slot]["account_bound"])
            # never a worn slot, so an empty one is not a coverage hole
            self.assertNotIn((slot, 0), gearsets._expected_slot_rows())

    def test_mount_slot_matches_itemtype_not_the_ammo_mask(self):
        # A mount and a plain arrow are both slots=Ammo. Only itemtype separates them.
        self.dump("Rakthor", [
            dump_line("General1-Slot1", "White Skystrider Whistle", 310),
            dump_line("General1-Slot2", "Plain Arrow", 313),
        ])
        ids = [c["item_id"] for c in self.slots()["Mount"]["items"]]
        self.assertIn(310, ids)
        self.assertNotIn(313, ids, "an arrow is not a mount")
        # ...and the mount must not pollute the real Ammo slot's list either way round
        self.assertIn(313, [c["item_id"] for c in self.slots()["Ammo"]["items"]])

    def test_slotless_clicky_is_pickable_at_all(self):
        """The regression this slot exists to fix: candidates() skipped every item
        with slots=0, so Trinket of the Far Frozen Wastes could never be picked no
        matter which slot you opened."""
        self.dump("Rakthor", [
            dump_line("General1-Slot1", "Trinket of the Far Frozen Wastes", 312)])
        ids = [c["item_id"] for c in self.slots()["WW Clicky"]["items"]]
        self.assertIn(312, ids)

    def test_consumables_stay_out_of_the_clicky_slot(self):
        """Without the maxcharges test this slot filled with ~30 potions off the real
        roster - every Distillate, Gate Potion, Blood of the Wolf - because a potion
        is also a slotless clicky. Reusable clickies carry maxcharges -1."""
        self.dump("Rakthor", [
            dump_line("General1-Slot1", "Trinket of the Far Frozen Wastes", 312),
            dump_line("General1-Slot2", "Gate Potion", 315, count=10),
        ])
        ids = [c["item_id"] for c in self.slots()["WW Clicky"]["items"]]
        self.assertIn(312, ids)
        self.assertNotIn(315, ids, "a stack of potions is not gear")

    def test_a_mount_is_not_also_offered_as_a_clicky(self):
        """The whistle is a clicky with no armour slot too - the two carried slots
        have to stay disjoint or every mount shows up twice."""
        self.dump("Rakthor", [
            dump_line("General1-Slot1", "White Skystrider Whistle", 310)])
        by_slot = self.slots()
        self.assertIn(310, [c["item_id"] for c in by_slot["Mount"]["items"]])
        self.assertNotIn(310, [c["item_id"] for c in by_slot["WW Clicky"]["items"]])

    def test_worn_gear_with_a_clicky_stays_out_of_the_clicky_slot(self):
        self.dump("Rakthor", [dump_line("Chest", "Clicky Breastplate", 314)])
        by_slot = self.slots()
        self.assertNotIn(314, [c["item_id"] for c in by_slot["WW Clicky"]["items"]],
                         "a breastplate belongs in Chest, not the carried clicky slot")
        self.assertIn(314, [c["item_id"] for c in by_slot["Chest"]["items"]])

    def test_candidates_hide_copies_held_on_another_account(self):
        self.dump("Rakthor", [])
        self.dump("Belwyn", [dump_line("General1-Slot1", "Tan Rope Bridle", 311)])
        self.dump("Corvane", [dump_line("General1-Slot1", "White Skystrider Whistle", 310)])
        ids = [c["item_id"] for c in self.slots()["Mount"]["items"]]
        self.assertIn(311, ids, "same account (Belwyn) — reachable")
        self.assertNotIn(310, ids, "Corvane is on Acct2 — it can never come across")

    def test_router_will_not_source_an_account_bound_item_across_accounts(self):
        """Without the account cut this routed as a parcel from Corvane — a step that
        is physically impossible to run."""
        self.dump("Rakthor", [])
        self.dump("Corvane", [dump_line("General1-Slot1", "Tan Rope Bridle", 311)])
        self.gearset("Kit", self.rakthor, [("Mount", 311)])
        row = self.plan()["plans"][0]["rows"][0]
        self.assertEqual(row["status"], "missing")
        self.assertIn("account-bound", row["note"])
        self.assertNotIn("Corvane", row["note"])

    def test_same_account_tradeable_mount_still_routes(self):
        self.dump("Rakthor", [])
        self.dump("Belwyn", [dump_line("General1-Slot1", "Tan Rope Bridle", 311)])
        self.gearset("Kit", self.rakthor, [("Mount", 311)])
        row = self.plan()["plans"][0]["rows"][0]
        self.assertEqual(row["status"], "swap")           # same account -> shared bank
        self.assertEqual(row["holder"], "Belwyn")

    def test_same_account_notrade_moves_through_the_shared_bank(self):
        """No Trade blocks trades and parcels, but NOT the account's shared bank.
        reported 2026-08-09: "that item cant be parceled but it can be put in the shared
        bank." Reporting a flat NO TRADE named a holder and then said nothing could be
        done with them; the swap route says exactly which toon to log in."""
        self.dump("Rakthor", [])
        self.dump("Belwyn", [dump_line("General1-Slot1", "White Skystrider Whistle", 310)])
        self.gearset("Kit", self.rakthor, [("Mount", 310)])
        row = self.plan()["plans"][0]["rows"][0]
        self.assertEqual(row["status"], "swap")
        self.assertEqual(row["holder"], "Belwyn")
        self.assertIn("shared bank", row["note"])

    def test_a_notrade_copy_already_in_the_shared_bank_is_just_a_grab(self):
        self.dump("Rakthor", [])
        self.dump("Belwyn", [dump_line("SharedBank1", "White Skystrider Whistle", 310)])
        self.gearset("Kit", self.rakthor, [("Mount", 310)])
        self.assertEqual(self.plan()["plans"][0]["rows"][0]["status"], "grab")

    def test_notrade_gear_outside_the_account_bound_slots_is_still_blocked(self):
        """The shared-bank exemption is scoped to claim gear ON PURPOSE — ordinary
        fvnodrop gear must keep reporting NO TRADE."""
        self.dump("Rakthor", [])
        self.dump("Belwyn", [dump_line("Hands", "NoTrade Fists", 202)])
        self.gearset("Kit", self.rakthor, [("Hands", 202)])
        self.assertEqual(self.plan()["plans"][0]["rows"][0]["status"], "notrade")

    def test_already_on_the_target_still_wins(self):
        self.dump("Rakthor", [
            dump_line("General1-Slot1", "Trinket of the Far Frozen Wastes", 312)])
        self.gearset("Kit", self.rakthor, [("WW Clicky", 312)])
        row = self.plan()["plans"][0]["rows"][0]
        self.assertEqual(row["status"], "have")           # No-Trade never even consulted

    def test_carried_claims_are_reserved_against_other_sets(self):
        """The point of giving them a slot: one copy, one promise."""
        self.dump("Rakthor", [])
        self.dump("Belwyn", [dump_line("General1-Slot1", "Tan Rope Bridle", 311)])
        self.gearset("Kit A", self.rakthor, [("Mount", 311)])
        cands = gearsets.candidates(self.conn, self.eq_dir, db=ITEMDB,
                                    target_char_id=self.rakthor)
        mount = [c for c in {s["slot"]: s for s in cands["slots"]}["Mount"]["items"]
                 if c["item_id"] == 311][0]
        self.assertEqual(mount["owned"], 1)
        self.assertEqual(mount["free"], 0)                # Kit A has it promised
        self.assertEqual(mount["reserved_by"], ["Kit A"])

    def test_mount_never_counts_as_a_missing_worn_piece(self):
        self.dump("Rakthor", [
            dump_line("Waist", "Monk Belt", 200),
            dump_line("General1-Slot1", "White Skystrider Whistle", 310),
        ])
        sid = self.gearset("Kit", self.rakthor, [("Waist", 200), ("Mount", 310)])
        fit = gearsets.fit_counts(self.conn, self.eq_dir)[sid]
        self.assertEqual(fit["worn"], 1)
        self.assertEqual(fit["worn_total"], 1)      # mount excluded -> set reads FULL
        self.assertEqual(fit["present"], 2)

    def test_resnapshot_preserves_the_mount_and_clicky_claims(self):
        self.dump("Rakthor", [
            dump_line("Waist", "Monk Belt", 200),
            dump_line("General1-Slot1", "White Skystrider Whistle", 310),
            dump_line("General1-Slot2", "Trinket of the Far Frozen Wastes", 312),
        ])
        code, res = gearsets.snapshot(self.conn, self.eq_dir, self.rakthor)
        self.assertEqual(code, 200)
        sid = res["id"]
        s = [x for x in gearsets.list_sets(self.conn) if x["id"] == sid][0]
        gearsets.save_set(self.conn, {"items": list(s["items"]) + [
            {"item_id": 310, "item_name": "White Skystrider Whistle",
             "slot": "Mount", "slot_index": 0},
            {"item_id": 312, "item_name": "Trinket of the Far Frozen Wastes",
             "slot": "WW Clicky", "slot_index": 0}]}, set_id=sid)
        self.assertEqual(gearsets.snapshot(self.conn, self.eq_dir, self.rakthor)[0], 200)
        after = [x for x in gearsets.list_sets(self.conn) if x["id"] == sid][0]
        kept = {i["slot"]: i["item_id"] for i in after["items"] if i["slot"] in
                gearsets.EXTRA_SLOT_NAMES}
        self.assertEqual(kept, {"Mount": 310, "WW Clicky": 312})


class TestParcelSourceQuantities(_World):
    """The parcel filter must agree with the work order. It used to be a bare id
    SET, which over-sent two ways: a holder with spare copies of a wanted item sent
    every copy (reported 2026-08-08: two Mask of War queued when one was planned), and
    ANY holder logging in matched the whole plan's ids, including other holders'
    rows. Both are quantity/ownership facts the id set could not carry."""

    def _built(self):
        t = self.char("Rakthor", "Acct1")
        holder = self.char("Corvane", "Acct2")
        self.dump("Rakthor", [])
        # Corvane holds THREE Parcel Boots; the plan wants exactly one.
        self.dump("Corvane", [
            dump_line("General1-Slot1", "Parcel Boots", 205),
            dump_line("General1-Slot2", "Parcel Boots", 205),
            dump_line("General1-Slot3", "Parcel Boots", 205),
            dump_line("General1-Slot4", "Trade Cloak", 206),
        ])
        self.gearset("Kit", t, [("Feet", 205), ("Back", 206)])
        return gearsets.build_plans_lua(self.plan()), holder

    def test_need_counts_copies_per_holder(self):
        built, _ = self._built()
        need = built["plans"][0]["need"]
        self.assertEqual(need, {"Corvane": {205: 1, 206: 1}},
                         "one copy owed, not the three Corvane happens to hold")

    def test_filter_is_scoped_to_the_holder_and_carries_counts(self):
        built, _ = self._built()
        lua = gearsets.build_parcel_source_lua(built["plans"])
        self.assertIn('["Corvane"] = { [205]=1, [206]=1 }', lua)
        # the old bare-set form must be gone, or the over-send comes straight back
        self.assertNotIn("ids[(item.ID() or 0)] == true", lua)
        self.assertIn("mq.TLO.Me.CleanName()", lua)   # holder scoping
        self.assertIn("if got >= want then return false end", lua)  # quantity cap

    def test_source_carries_its_destination(self):
        """Each source names its own target so the parcel tool can fill "Send To"
        on selection. The user kept picking a plan and forgetting to change the name -
        that is a mis-SEND, and gear delivered to the wrong toon is a manual
        mail-back. The plan already knows the answer; nobody should retype it."""
        built, _ = self._built()
        lua = gearsets.build_parcel_source_lua(built["plans"])
        self.assertIn('target = "Rakthor"', lua)

    def test_two_copies_planned_emits_two(self):
        """A genuine 2-copy need (paired slots) must still send both - the fix is a
        CAP at the planned count, not a blanket one-per-item."""
        t = self.char("Rakthor", "Acct1")
        self.char("Corvane", "Acct2")
        self.dump("Rakthor", [])
        self.dump("Corvane", [dump_line("General1-Slot1", "Plain Hoop", 304, 2)])
        self.gearset("Kit", t, [("Ear", 304), ("Ear", 304)])
        built = gearsets.build_plans_lua(self.plan())
        self.assertEqual(built["plans"][0]["need"], {"Corvane": {304: 2}})


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
        # was "[205]=true" (a bare id set) until 2026-08-08 - now holder -> id -> COUNT,
        # see TestParcelSourceQuantities for why.
        self.assertIn('["Corvane"] = { [205]=1 }', parcel)
        self.assertIn("Gear Plan: Kit -> Rakthor (1)", parcel)

    def test_no_moves_returns_none(self):
        t = self.char("Rakthor", "Acct1")
        self.dump("Rakthor", [dump_line("Waist", "Monk Belt", 200)])
        self.gearset("Kit", t, [("Waist", 200)])
        self.assertIsNone(gearsets.build_plans_lua(self.plan()))


class TestExportCarriesSelfSourcedRows(_World):
    """The export used to be swap/trade/parcel ONLY, so a piece already sitting in
    the target's own bags (status 'have') or shared bank ('grab') never reached
    mailgear and nothing equipped it. reported 2026-08-09: "even when the item is
    already on the toon it needs to be, sitting in their bag".

    Contract now: `rows` carries every routed piece with a status; `moves` keeps its
    old swap/trade/parcel-only contents so old readers and the parcel filter are
    untouched."""

    def setUp(self):
        super().setUp()
        self.t = self.char("Rakthor", "Acct1")
        self.holder = self.char("Corvane", "Acct2")
        self.dump("Rakthor", [
            dump_line("Waist", "Monk Belt", 200),            # worn
            dump_line("General1-Slot1", "Bag Mask", 208),    # have  (own bags)
            dump_line("SharedBank1", "Shared Ring", 204),    # grab  (own shared bank)
        ])
        self.dump("Corvane", [dump_line("Bank2", "Parcel Boots", 205)])
        self.gearset("Kit", self.t, [
            ("Waist", 200), ("Face", 208), ("Fingers", 204), ("Feet", 205)])
        self.built = gearsets.build_plans_lua(self.plan())
        self.text = self.built["text"]

    def test_self_sourced_rows_reach_the_export(self):
        for status in ("worn", "have", "grab"):
            self.assertIn('status = "%s"' % status, self.text,
                          "%s row missing from the export" % status)

    def test_self_sourced_rows_stay_out_of_moves(self):
        # A move status is emitted three times: the plan's `moves`, the plan's
        # `rows`, and the top-level back-compat mirror. A self-sourced status is
        # emitted ONCE - `rows` only. That count IS the contract.
        self.assertEqual(self.text.count('status = "parcel"'), 3)
        for status in ("worn", "have", "grab"):
            self.assertEqual(self.text.count('status = "%s"' % status), 1, status)

    def test_parcel_filter_still_counts_moves_only(self):
        meta = self.built["plans"][0]
        self.assertEqual(meta["count"], 1)                  # the one parcel
        self.assertEqual(meta["ids"], [205])
        self.assertEqual(meta["need"], {"Corvane": {205: 1}})

    def test_a_plan_with_no_moves_still_exports_when_something_needs_equipping(self):
        # Everything already on the target, but the bagged piece still has to be
        # put ON. Before this, the whole plan was dropped as "no moves".
        conn = self.conn
        conn.execute("DELETE FROM gear_sets")
        conn.commit()
        self.gearset("Bags only", self.t, [("Face", 208)])
        built = gearsets.build_plans_lua(self.plan())
        self.assertIsNotNone(built)
        self.assertIn('status = "have"', built["text"])
        self.assertEqual(built["plans"][0]["count"], 0)     # no transfers at all

    def test_paired_slots_carry_their_index(self):
        conn = self.conn
        conn.execute("DELETE FROM gear_sets")
        conn.commit()
        self.dump("Corvane", [dump_line("Bank2", "Lore Earring", 201)])
        # Finger 0 = the ring in Rakthor's own shared bank (grab), finger 1 = a
        # different ring Corvane has to parcel over.
        self.gearset("Rings", self.t, [("Fingers", 204), ("Fingers", 201)])
        text = gearsets.build_plans_lua(self.plan())["text"]
        # Two Fingers rows must be distinguishable, or the equip step has no way to
        # know they are two different fingers and overwrites the first with the second.
        self.assertIn("slotIndex = 0", text)
        self.assertIn("slotIndex = 1", text)


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

    def test_contested_save_leaves_the_donor_set_intact(self):
        # THE REGRESSION. Saving a set that wants the roster's only Bronze
        # Breastplate used to DELETE Monk kit's Chest row (_steal_overclaims).
        # A set records intent, so both keep the pick and the clash is reported.
        code, res = gearsets.save_set(self.conn, {
            "name": "SK kit", "assigned_char_id": self.sk, "steal": True,
            "items": [{"item_id": 300, "item_name": "Bronze Breastplate", "slot": "Chest"}],
        }, eq_dir=self.eq_dir)
        self.assertEqual(code, 200, res)
        self.assertEqual(self.items_of(self.monk_set), [300])       # monk KEEPS it
        self.assertEqual(self.items_of(res["id"]), [300])
        self.assertEqual(res["contested"], [
            {"item": "Bronze Breastplate", "want": 2, "owned": 1,
             "other_sets": ["Monk kit"]}])

    def test_legacy_steal_flag_is_inert(self):
        # Old clients still post steal:true. It must not resurrect the deletion.
        for _ in range(3):
            gearsets.save_set(self.conn, {
                "name": "SK kit", "assigned_char_id": self.sk, "steal": True,
                "items": [{"item_id": 300, "item_name": "Bronze Breastplate",
                           "slot": "Chest"}],
            }, eq_dir=self.eq_dir)
        self.assertEqual(self.items_of(self.monk_set), [300])

    def test_not_contested_when_copies_are_plentiful(self):
        self.dump("Sking", [dump_line("Bank1", "Bronze Breastplate", 300)])   # 2nd copy
        code, res = gearsets.save_set(self.conn, {
            "name": "SK kit", "assigned_char_id": self.sk, "steal": True,
            "items": [{"item_id": 300, "item_name": "Bronze Breastplate", "slot": "Chest"}],
        }, eq_dir=self.eq_dir)
        self.assertEqual(code, 200, res)
        self.assertEqual(res["contested"], [])                      # fungible: both fit
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


class TestCompReadiness(_World):
    """'Can I field this six right now' — the per-member roll-up, and the promise
    that focusing a comp narrows the REPORT without narrowing the reservation math."""

    def setUp(self):
        super().setUp()
        self.ready = self.char("Readyone", "Acct1")            # wearing their set
        self.equip = self.char("Equipone", "Acct2")            # holds it, not worn
        self.moves = self.char("Moveone", "Acct3")             # needs a parcel
        self.stuck = self.char("Stuckone", "Acct4")            # nobody has the item
        self.bare = self.char("Bareone", "Acct5")              # no set at all
        self.holder = self.char("Holderone", "Acct6")          # offline source
        self.outsider = self.char("Outsideone", "Acct7")       # active set, not in comp
        self.dump("Readyone", [dump_line("Waist", "Monk Belt", 200)])
        self.dump("Equipone", [dump_line("General1-Slot1", "Bag Mask", 208)])
        self.dump("Moveone", [])
        self.dump("Stuckone", [])
        self.dump("Bareone", [dump_line("Chest", "Swap Tunic", 203)])
        self.dump("Holderone", [dump_line("Bank2", "Parcel Boots", 205),
                                dump_line("Bank3", "Old Dagger", 305)])
        self.dump("Outsideone", [])
        self.gearset("Ready kit", self.ready, [("Waist", 200)])
        self.gearset("Equip kit", self.equip, [("Face", 208)])
        self.gearset("Move kit", self.moves, [("Feet", 205)])
        self.gearset("Stuck kit", self.stuck, [("Head", 207)])
        self.gearset("Outside kit", self.outsider, [("Primary", 305)])
        self.comp = [self.ready, self.equip, self.moves, self.stuck, self.bare]

    def readiness(self, ids=None, login=None):
        return gearsets.comp_readiness(self.conn, self.eq_dir, ids or self.comp,
                                       login, db=ITEMDB)

    def member(self, res, name):
        return next(m for m in res["members"] if m["name"] == name)

    def test_state_per_member(self):
        res = self.readiness()
        self.assertEqual(self.member(res, "Readyone")["state"], "ready")
        self.assertEqual(self.member(res, "Equipone")["state"], "onhand")
        self.assertEqual(self.member(res, "Moveone")["state"], "moves")
        self.assertEqual(self.member(res, "Stuckone")["state"], "blocked")
        self.assertEqual(self.member(res, "Bareone")["state"], "noset")
        self.assertEqual(res["summary"]["states"],
                         {"ready": 1, "onhand": 1, "moves": 1, "blocked": 1, "noset": 1})
        self.assertFalse(res["summary"]["ready"])

    def test_counts_and_routes(self):
        res = self.readiness()
        self.assertEqual(self.member(res, "Readyone")["equipped"], 1)
        self.assertEqual(self.member(res, "Equipone")["on_hand"], 1)
        self.assertEqual(self.member(res, "Moveone")["incoming"], {"parcel": 1})
        stuck = self.member(res, "Stuckone")
        self.assertEqual([b["status"] for b in stuck["blocked"]], ["missing"])
        self.assertEqual(stuck["blocked"][0]["item"], "Missing Crown")

    def test_all_ready_flag(self):
        res = self.readiness([self.ready])
        self.assertTrue(res["summary"]["ready"])

    def test_gaps_skip_the_slots_that_are_normally_empty(self):
        gaps = self.member(self.readiness(), "Readyone")["gaps"]
        self.assertIn("Chest", gaps)
        self.assertIn("Ear 2", gaps)              # paired slots count twice
        self.assertNotIn("Waist", gaps)           # the one the set covers
        for skipped in ("Charm", "Range", "Ammo", "Power"):
            self.assertNotIn(skipped, gaps)

    def test_a_retired_set_is_not_the_same_as_no_set(self):
        # Bareone owns a set, it's just switched off. Reporting that as "no set"
        # sends you to snapshot a duplicate instead of ticking Active.
        sid = self.gearset("Bare kit", self.bare, [("Chest", 203)], active=0)
        m = self.member(self.readiness(), "Bareone")
        self.assertEqual(m["state"], "noset")
        self.assertEqual(m["reason"], "retired")
        self.assertEqual([(s["id"], s["name"], s["pieces"]) for s in m["shelved_sets"]],
                         [(sid, "Bare kit", 1)])

    def test_genuinely_setless_member_says_so(self):
        m = self.member(self.readiness(), "Bareone")
        self.assertEqual(m["reason"], "none")
        self.assertEqual(m["shelved_sets"], [])

    def test_an_active_set_is_never_listed_as_shelved(self):
        self.assertEqual(self.member(self.readiness(), "Readyone")["shelved_sets"], [])

    def test_focus_narrows_the_report_not_the_reservation_math(self):
        # Outside kit is planned (and keeps its claim on the dagger) even though the
        # comp never sees it — otherwise the comp would read clean while taking it.
        res = self.readiness()
        names = [p["name"] for p in res["plan"]["plans"]]
        self.assertNotIn("Outside kit", names)
        self.assertEqual(res["plan"]["focus"]["shown_sets"], 4)
        self.assertEqual(res["plan"]["focus"]["planned_sets"], 5)
        # work order + totals cover the comp's moves only
        self.assertEqual([h["holder"] for h in res["plan"]["workorder"]], ["Holderone"])
        self.assertEqual([i["item"] for i in res["plan"]["workorder"][0]["items"]],
                         ["Parcel Boots"])
        self.assertEqual(res["plan"]["summary"]["by_status"].get("parcel"), 1)

    def test_focused_plan_still_honours_another_sets_claim(self):
        # one dagger, two sets want it; the outside set claims first (lower id)
        self.gearset("Move dagger", self.moves, [("Primary", 305)])
        res = self.readiness()
        row = next(r for p in res["plan"]["plans"] for r in p["rows"]
                   if r["item_id"] == 305)
        self.assertEqual(row["status"], "reserved")
        self.assertEqual(self.member(res, "Moveone")["state"], "blocked")


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
        moves = [("Waist", 200), ("Chest", 203), ("Primary", 305)]
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


class TestApplyCompGear(_World):
    """Switching which comp is fielded. `active` only means anything if the active
    group is ONE comp — the user had two comps' sets live at once, so single-copy pieces
    read as permanently contested (2026-08-09)."""

    def setUp(self):
        super().setUp()
        self.trix = self.char("Trixster", "acct1", cls="Monk")
        self.zyrak = self.char("Zyrak", "acct2", cls="Rogue")
        self.dump("Trixster", [])
        self.dump("Zyrak", [])
        self.monk_main = self.gearset("Monk Main", self.trix, [("Chest", 300)])
        self.sleeper_monk = self.gearset("Sleeper Monk", self.trix, [("Chest", 301)],
                                         active=0)
        self.rogue_main = self.gearset("Rogue Main", self.zyrak, [("Chest", 302)])
        self.sleeper_rog = self.gearset("Sleeper Rogue", self.zyrak, [("Chest", 303)],
                                        active=0)

    def assigned(self, set_id):
        return self.conn.execute("SELECT assigned_char_id FROM gear_sets WHERE id=?",
                                 (set_id,)).fetchone()[0]

    def comp(self, name, char_ids):
        cur = self.conn.execute("INSERT INTO compositions(name) VALUES (?)", (name,))
        cid = cur.lastrowid
        for i, ch in enumerate(char_ids):
            self.conn.execute("INSERT INTO composition_slots(composition_id, slot_index,"
                              " character_id) VALUES (?,?,?)", (cid, i, ch))
        return cid

    def active_names(self):
        return sorted(r[0] for r in self.conn.execute(
            "SELECT name FROM gear_sets WHERE active=1"))

    def test_ambiguous_member_is_asked_not_guessed(self):
        # Both of Trixster's sets are candidates and neither is stored. The old
        # comp_gear_check picked "newest updated active" silently; that is exactly
        # how a Sleeper toon ended up fielding Rogue Main.
        c = self.comp("Sleeper", [self.trix, self.zyrak])
        self.conn.execute("UPDATE gear_sets SET active=1")     # both sets live => tie
        code, res = gearsets.apply_comp_gear(self.conn, c)
        self.assertEqual(code, 409, res)
        self.assertTrue(res["needs_choice"])
        self.assertIn("Trixster", res["error"])
        self.assertEqual(self.active_names(),                  # nothing changed
                         ["Monk Main", "Rogue Main", "Sleeper Monk", "Sleeper Rogue"])

    def test_apply_activates_only_this_comps_sets(self):
        c = self.comp("Sleeper", [self.trix, self.zyrak])
        code, res = gearsets.apply_comp_gear(self.conn, c, choices={
            self.trix: self.sleeper_monk, self.zyrak: self.sleeper_rog})
        self.assertEqual(code, 200, res)
        self.assertEqual(self.active_names(), ["Sleeper Monk", "Sleeper Rogue"])
        self.assertEqual(res["activated"], ["Sleeper Monk", "Sleeper Rogue"])
        self.assertEqual(res["deactivated"], ["Monk Main", "Rogue Main"])

    def test_choice_is_remembered_so_reapply_needs_no_input(self):
        c = self.comp("Sleeper", [self.trix, self.zyrak])
        gearsets.apply_comp_gear(self.conn, c, choices={
            self.trix: self.sleeper_monk, self.zyrak: self.sleeper_rog})
        gearsets.apply_comp_gear(self.conn, self.comp("Main", [self.trix, self.zyrak]),
                                 choices={self.trix: self.monk_main,
                                          self.zyrak: self.rogue_main})
        self.assertEqual(self.active_names(), ["Monk Main", "Rogue Main"])
        code, res = gearsets.apply_comp_gear(self.conn, c)     # no choices this time
        self.assertEqual(code, 200, res)
        self.assertEqual(self.active_names(), ["Sleeper Monk", "Sleeper Rogue"])

    def test_deactivating_never_touches_the_picks(self):
        c = self.comp("Sleeper", [self.trix, self.zyrak])
        gearsets.apply_comp_gear(self.conn, c, choices={
            self.trix: self.sleeper_monk, self.zyrak: self.sleeper_rog})
        for sid in (self.monk_main, self.rogue_main):
            self.assertEqual(self.conn.execute(
                "SELECT COUNT(*) FROM gear_set_items WHERE set_id=?", (sid,)).fetchone()[0],
                1, "deactivating a set must not clear it")

    def test_a_loadout_moves_to_whichever_toon_the_comp_fields(self):
        """THE GAVRIEL CASE. "Rogue Main" is the best rogue kit you own, not Zyrak's
        property — WAR/CLR/BRD/MNK/ROG/BST fields it on Gavriel while the Sleeper
        group has Zyrak in Sleeper Rogue 3. Matching candidates by assignment could
        never offer it to Gavriel, so he read as "no gear set" (2026-08-09)."""
        gav = self.char("Gavriel", "acct9", cls="Rogue")
        self.dump("Gavriel", [])
        offered = [c["name"] for c in
                   gearsets.comp_gear_map(self.conn, self.comp("Main", [gav]))[0]["candidates"]]
        self.assertIn("Rogue Main", offered)          # pinned to Zyrak, still offered
        c = self.comp("Main2", [gav])
        code, res = gearsets.apply_comp_gear(self.conn, c,
                                             choices={gav: self.rogue_main})
        self.assertEqual(code, 200, res)
        self.assertEqual(res["retargeted"],
                         [{"set": "Rogue Main", "to": "Gavriel", "from": "Zyrak"}])
        self.assertEqual(self.conn.execute(
            "SELECT assigned_char_id FROM gear_sets WHERE id=?",
            (self.rogue_main,)).fetchone()[0], gav)
        self.assertEqual(res["no_set"], [])

    def test_the_same_loadout_cannot_dress_two_toons_at_once(self):
        """Two toons in one comp wearing "Rogue Main" would claim every piece twice
        and the plan would promise one physical item to both."""
        gav = self.char("Gavriel", "acct9", cls="Rogue")
        self.dump("Gavriel", [])
        c = self.comp("Main", [gav, self.zyrak])
        code, res = gearsets.apply_comp_gear(self.conn, c, choices={
            gav: self.rogue_main, self.zyrak: self.rogue_main})
        self.assertEqual(code, 409, res)
        self.assertIn("one loadout per toon", res["error"])

    def test_one_loadout_two_comps_two_toons(self):
        """The user 2026-08-09, the whole model in one sentence: "When I choose this comp
        I want to assign Rogue Main to Gavriel. When I choose the Sleeper group I
        should also be able to set one of the rogues there Rogue Main." One set, no
        copies, no labels — the comp's mapping decides who wears it."""
        gav = self.char("Gavriel", "acct9", cls="Rogue")
        self.dump("Gavriel", [])
        raid = self.comp("Raid", [gav])
        sleeper = self.comp("Sleeper", [self.zyrak])
        for c in (raid, sleeper):
            self.assertIn("Rogue Main",
                          [x["name"] for x in gearsets.comp_gear_map(self.conn, c)[0]["candidates"]])
        gearsets.apply_comp_gear(self.conn, raid, choices={gav: self.rogue_main})
        self.assertEqual(self.assigned(self.rogue_main), gav)
        gearsets.apply_comp_gear(self.conn, sleeper, choices={self.zyrak: self.rogue_main})
        self.assertEqual(self.assigned(self.rogue_main), self.zyrak)
        self.assertEqual(self.conn.execute(          # the raid comp keeps its mapping
            "SELECT gear_set_id FROM composition_slots WHERE composition_id=?",
            (raid,)).fetchone()[0], self.rogue_main)

    def test_candidates_say_where_a_loadout_is_already_used(self):
        """Derived from the mappings, so "why is this in my list?" is answered without
        anyone typing a label."""
        gav = self.char("Gavriel", "acct9", cls="Rogue")
        self.dump("Gavriel", [])
        raid = self.comp("Raid", [gav])
        gearsets.apply_comp_gear(self.conn, raid, choices={gav: self.rogue_main})
        sleeper = self.comp("Sleeper", [self.zyrak])
        cand = next(x for x in gearsets.comp_gear_map(self.conn, sleeper)[0]["candidates"]
                    if x["name"] == "Rogue Main")
        self.assertEqual(cand["used_by"], ["Raid"])
        self.assertEqual(cand["used_here"], "")

    def test_a_loadout_used_elsewhere_in_this_comp_is_flagged(self):
        """One loadout, one wearer — two rogues in the SAME comp cannot both field
        Rogue Main, and the picker has to say so before Apply refuses it."""
        c = self.comp("Sleeper", [self.zyrak, self.char("Kaelor", "acct9", cls="Rogue")])
        kae = self.conn.execute("SELECT id FROM characters WHERE name='Kaelor'").fetchone()[0]
        self.dump("Kaelor", [])
        gearsets.set_comp_choice(self.conn, c, self.zyrak, self.rogue_main)
        row = next(m for m in gearsets.comp_gear_map(self.conn, c)
                   if m["character"] == "Kaelor")
        cand = next(x for x in row["candidates"] if x["name"] == "Rogue Main")
        self.assertEqual(cand["used_here"], "Zyrak")

    def test_clone_makes_a_variant_that_starts_retired(self):
        code, res = gearsets.clone_set(self.conn, self.monk_main)
        self.assertEqual(code, 200, res)
        self.assertEqual(res["pieces"], 1)
        row = self.conn.execute("SELECT active, assigned_char_id FROM gear_sets"
                                " WHERE id=?", (res["id"],)).fetchone()
        self.assertEqual(row["active"], 0)          # a copy never changes what is fielded
        self.assertIsNone(row["assigned_char_id"])

    def test_clone_never_collides_on_the_unique_name(self):
        a = gearsets.clone_set(self.conn, self.monk_main)[1]
        b = gearsets.clone_set(self.conn, self.monk_main)[1]
        self.assertNotEqual(a["name"], b["name"])

    def test_a_pick_with_no_slot_gets_one_from_the_item(self):
        """A slotless pick is invisible in the editor (it draws one row per slot) but
        still claims its item — the user replaced Sleeper Monk 2's Neck and the comp check
        kept reporting the old Yelinak's Talisman. The Macro Builder import posts
        `slot: it.slot || ""`, so the slot has to be filled in on the way in."""
        code, res = gearsets.save_set(self.conn, {
            "name": "Imported", "assigned_char_id": self.trix,
            "items": [{"item_id": 300, "item_name": "Bronze Breastplate", "slot": ""}],
        }, db=ITEMDB)
        self.assertEqual(code, 200, res)
        row = self.conn.execute("SELECT slot, slot_index FROM gear_set_items"
                                " WHERE set_id=?", (res["id"],)).fetchone()
        self.assertEqual(row["slot"], "Chest")          # from the item's equip mask
        self.assertEqual(row["slot_index"], 0)

    def test_an_inferred_slot_never_displaces_a_named_pick(self):
        """The named pick is what the user chose; a repaired one must go elsewhere or
        stay blank. Silently overwriting Neck would re-create the original bug."""
        code, res = gearsets.save_set(self.conn, {
            "name": "Imported2", "assigned_char_id": self.trix,
            "items": [{"item_id": 300, "item_name": "Bronze Breastplate", "slot": "Chest"},
                      {"item_id": 301, "item_name": "Silver Breastplate", "slot": ""}],
        }, db=ITEMDB)
        self.assertEqual(code, 200, res)
        rows = {r["item_id"]: r["slot"] for r in self.conn.execute(
            "SELECT item_id, slot FROM gear_set_items WHERE set_id=?", (res["id"],))}
        self.assertEqual(rows[300], "Chest")            # the explicit pick is untouched
        self.assertEqual(rows[301], "")                 # no free Chest slot: left blank

    def test_choosing_a_loadout_does_not_activate_it_on_its_own(self):
        """Recording a choice is not fielding it. A lone activate is what let two
        comps' sets be live at once — only Apply changes what is active."""
        c = self.comp("Sleeper", [self.trix, self.zyrak])
        before = self.active_names()
        code, res = gearsets.set_comp_choice(self.conn, c, self.trix,
                                             self.sleeper_monk)
        self.assertEqual(code, 200, res)
        self.assertEqual(self.active_names(), before)          # nothing switched on
        m = next(x for x in res["mapping"] if x["character"] == "Trixster")
        self.assertTrue(m["stored"])
        self.assertFalse(m["needs_choice"])                    # answered, so no re-ask

    def test_choice_for_a_non_member_is_refused(self):
        outsider = self.char("Nobody", "acct7", cls="Monk")
        c = self.comp("Sleeper", [self.trix])
        code, res = gearsets.set_comp_choice(self.conn, c, outsider, self.monk_main)
        self.assertEqual(code, 404, res)

    def test_one_active_candidate_is_still_asked_about(self):
        """THE ZYRAK CASE. Zyrak owns Rogue Main (active), Sleeper Rogue 3 and
        Zyrak (Rogue). Inside the Sleeper comp the active one is the WRONG one, and
        "it was already active" is how it got that way — so it is offered as the
        default but never applied unseen."""
        c = self.comp("Sleeper", [self.zyrak])
        code, res = gearsets.apply_comp_gear(self.conn, c)
        self.assertEqual(code, 409, res)
        self.assertIn("Zyrak", res["error"])
        m = res["mapping"][0]
        self.assertEqual(m["gear_set_name"], "Rogue Main")     # defaulted for one click
        self.assertEqual(sorted(a["name"] for a in m["candidates"]),
                         ["Rogue Main", "Sleeper Rogue"])      # both offered
        self.assertEqual(self.active_names(), ["Monk Main", "Rogue Main"])   # unchanged

    def test_only_set_for_a_toon_needs_no_choice(self):
        # Cleric, not Rogue: candidates match by CLASS now, so a rogue would inherit
        # Zyrak's two rogue loadouts as alternatives and be ambiguous by definition.
        solo = self.char("Kaelor", "acct3", cls="Cleric")
        self.dump("Kaelor", [])
        only = self.gearset("Kaelor kit", solo, [("Chest", 300)], active=0)
        c = self.comp("Solo", [solo])
        code, res = gearsets.apply_comp_gear(self.conn, c)
        self.assertEqual(code, 200, res)
        self.assertEqual(self.active_names(), ["Kaelor kit"])
        self.assertEqual(res["no_set"], [])
        self.assertEqual(only, gearsets.comp_gear_map(self.conn, c)[0]["gear_set_id"])

    def test_bench_slots_are_mapped_but_not_activated(self):
        bench = self.char("Benchy", "acct4", cls="Rogue")
        self.dump("Benchy", [])
        self.gearset("Bench kit", bench, [("Chest", 300)], active=0)
        c = self.comp("Sleeper", [self.trix, self.zyrak] + [None] * 4 + [bench])
        code, res = gearsets.apply_comp_gear(self.conn, c, choices={
            self.trix: self.sleeper_monk, self.zyrak: self.sleeper_rog})
        self.assertEqual(code, 200, res)
        self.assertNotIn("Bench kit", self.active_names())

    def test_member_with_no_set_is_reported_not_fatal(self):
        # Druid: no druid loadout exists at all, so there is genuinely nothing to wear
        naked = self.char("Naked", "acct5", cls="Druid")
        self.dump("Naked", [])
        c = self.comp("Sleeper", [self.trix, naked])
        code, res = gearsets.apply_comp_gear(self.conn, c,
                                             choices={self.trix: self.sleeper_monk})
        self.assertEqual(code, 200, res)
        self.assertEqual(res["no_set"], ["Naked"])
        self.assertEqual(self.active_names(), ["Sleeper Monk"])


class TestCompGearMapSurvivesEdits(_World):
    def setUp(self):
        super().setUp()
        self.a = self.char("Aaa", "acct1", cls="Monk")
        self.b = self.char("Bbb", "acct2", cls="Rogue")
        self.dump("Aaa", [])
        self.dump("Bbb", [])
        self.sa = self.gearset("A kit", self.a, [("Chest", 300)])
        self.sb = self.gearset("B kit", self.b, [("Chest", 301)])

    def test_reordering_a_comp_keeps_each_toon_its_own_set(self):
        """save_composition rewrites every slot row. Keyed by slot_index the mapping
        would follow the POSITION and hand Bbb's set to Aaa; keyed by character it
        follows the toon. Same shape as the gear-set steal bug: a write path that
        destroys what it never read."""
        code, res = api.save_composition(self.conn, {
            "name": "Comp", "slots": [self.a, self.b]})
        self.assertEqual(code, 200, res)
        cid = res["id"]
        gearsets.apply_comp_gear(self.conn, cid,
                                 choices={self.a: self.sa, self.b: self.sb})
        api.save_composition(self.conn, {"name": "Comp", "slots": [self.b, self.a]},
                             cid)                                  # swap the order
        got = {m["character"]: m["gear_set_name"]
               for m in gearsets.comp_gear_map(self.conn, cid)}
        self.assertEqual(got, {"Aaa": "A kit", "Bbb": "B kit"})

    def test_plain_comp_edit_does_not_clear_the_mapping(self):
        code, res = api.save_composition(self.conn, {
            "name": "Comp", "slots": [self.a, self.b]})
        cid = res["id"]
        gearsets.apply_comp_gear(self.conn, cid,
                                 choices={self.a: self.sa, self.b: self.sb})
        api.save_composition(self.conn, {"name": "Comp", "slots": [self.a, self.b],
                                         "notes": "edited"}, cid)
        self.assertTrue(all(m["stored"] for m in gearsets.comp_gear_map(self.conn, cid)))


if __name__ == "__main__":
    unittest.main()
