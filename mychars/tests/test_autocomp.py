"""Auto comp builder: requirement solving, fallback tiers, bench slot rules.
Run from eqforge2/:  python -m unittest discover -s mychars/tests
"""
import os
import sys
import tempfile
import unittest
import time
import random

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from mychars import api, db as dbm, domain  # noqa: E402


def char(cid, name, acct, cls, level=60, auto="untested"):
    return {"id": cid, "name": name, "account_id": acct, "account_alias": "A%s" % acct,
            "class_name": cls, "level": level, "status": "active",
            "automation_status": auto, "caps": domain.resolve_capabilities(cls)}


ROSTER = [
    char(1, "War", 1, "Warrior", auto="tested"),
    char(2, "Rog1", 1, "Rogue"),
    char(3, "Clr", 2, "Cleric", auto="tested"),
    char(4, "Shm", 2, "Shaman"),                    # same acct as Clr!
    char(5, "Brd", 3, "Bard"),
    char(6, "Enc", 4, "Enchanter"),
    char(7, "Mnk", 5, "Monk"),
    char(8, "Wiz", 6, "Wizard"),
    char(9, "Low", 6, "Shaman", level=45),          # under cap, never picked
]


class TestAutoComp(unittest.TestCase):
    def test_basic_requirements_met_one_per_account(self):
        res = domain.auto_comp(ROSTER, ["tank", "healer", "cc", "puller"])
        self.assertEqual(res["unmet"], [])
        team = set(res["team"])
        self.assertIn("War", team)
        self.assertIn("Enc", team)
        self.assertIn("Mnk", team)
        self.assertTrue({"Clr", "Shm"} & team)
        # one per account, hard
        by_acct = {}
        by_id = {c["id"]: c for c in ROSTER}
        for cid in res["slots"]:
            aid = by_id[cid]["account_id"]
            self.assertNotIn(aid, by_acct, "two toons from account %s" % aid)
            by_acct[aid] = cid

    # No enchanter in this subset: shaman and bard are the only slow sources,
    # and the shaman shares an account with the cleric.
    NO_ENC = [c for c in ROSTER if c["class_name"] != "Enchanter"]

    def test_cleric_and_slow_conflict_resolved_by_bard_fallback(self):
        # Cleric REQUIRED locks account 2, benching the shaman -> slow must fall
        # back to the Bard: tier 1 with the risky-weave warning.
        res = domain.auto_comp(self.NO_ENC, ["Cleric", "slow"])
        self.assertEqual(res["unmet"], [])
        self.assertIn("Clr", res["team"])
        slow = next(a for a in res["assignments"] if a["req"] == "slow")
        self.assertEqual(slow["toon"], "Brd")
        self.assertEqual(slow["tier"], 1)
        self.assertTrue(any("FALLBACK" in w for w in res["warnings"]))

    def test_primary_slow_preferred_over_bard(self):
        # Without the Cleric requirement the solver frees account 2 for the
        # shaman: primary slow beats the bard fallback.
        res = domain.auto_comp(self.NO_ENC, ["slow"])
        slow = next(a for a in res["assignments"] if a["req"] == "slow")
        self.assertEqual(slow["toon"], "Shm")
        self.assertEqual(slow["tier"], 2)

    def test_impossible_requirement_reported(self):
        res = domain.auto_comp(ROSTER, ["coth"])    # no magician anywhere
        self.assertEqual(res["unmet"], ["coth"])

    def test_under_cap_never_picked(self):
        res = domain.auto_comp(ROSTER, ["slow"])
        self.assertNotIn("Low", res["team"])

    def test_tested_automation_breaks_ties(self):
        res = domain.auto_comp(ROSTER, ["healer"])
        healer = next(a for a in res["assignments"] if a["req"] == "healer")
        self.assertEqual(healer["toon"], "Clr")     # tested Clr over untested Shm


class TestBenchSlots(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        api.DB_PATH = self.db_path
        conn = dbm.connect(self.db_path)
        conn.execute("INSERT INTO accounts(alias) VALUES ('A1'), ('A2')")
        conn.execute("INSERT INTO characters(name, server, account_id, class_name) VALUES"
                     " ('Rakthor','Frostreaver',1,'Monk'),"
                     " ('Zimka','Frostreaver',1,'Enchanter'),"
                     " ('Belwyn','Frostreaver',2,'Shaman')")
        conn.commit()
        conn.close()

    def tearDown(self):
        api.DB_PATH = None
        os.unlink(self.db_path)

    def test_bench_exempt_from_account_rule(self):
        # Rakthor live + Zimka on BENCH (same account) = fine
        code, resp = api.handle("POST", "/compositions",
                                {"name": "RotSix", "slots": [1, 3, None, None, None, None, 2]})
        self.assertEqual(code, 200)
        boot = api.handle("GET", "/bootstrap")[1]
        comp = next(c for c in boot["compositions"] if c["name"] == "RotSix")
        self.assertEqual(len(comp["slots"]), 7)
        self.assertEqual(comp["bench"], ["Zimka"])
        # warnings judge live six only
        self.assertNotIn("account_conflict", {w["code"] for w in comp["warnings"]})

    def test_live_conflict_still_rejected(self):
        code, resp = api.handle("POST", "/compositions",
                                {"name": "Bad", "slots": [1, 2, None, None, None, None]})
        self.assertEqual(code, 400)

    def test_same_toon_live_and_bench_rejected(self):
        code, resp = api.handle("POST", "/compositions",
                                {"name": "Dup", "slots": [1, None, None, None, None, None, 1]})
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()


class TestLargeRosterScale(unittest.TestCase):
    """A 6-box is the small case. Testers run 12-24 accounts.

    The original budget bounded the product of every account's candidate list,
    which is the correct cost only at <=6 accounts; above that the search also
    walks C(n,6) account combinations and that multiplier was missing. Measured
    before the fix: 18 accounts x 2 toons = 12.7s, roughly doubling every two
    accounts, so a 24-account roster hung the Auto-Fill button for minutes.
    """

    CLASSES = ["Warrior", "Cleric", "Enchanter", "Shaman", "Wizard", "Monk",
               "Rogue", "Druid", "Paladin", "Ranger", "Necromancer", "Magician"]

    def roster(self, n_accts, per_acct):
        chars, cid = [], 0
        for acct in range(1, n_accts + 1):
            for _ in range(per_acct):
                cid += 1
                cls = self.CLASSES[(cid - 1) % len(self.CLASSES)]
                chars.append({"id": cid, "name": "C%d" % cid, "account_id": acct,
                              "class_name": cls, "level": 60, "status": "active",
                              "automation_status": "tested",
                              "caps": domain.resolve_capabilities(cls, {})})
        return chars

    def test_big_rosters_stay_fast_and_still_fill_six(self):
        for n_accts, per_acct in [(12, 4), (18, 4), (24, 8)]:
            with self.subTest(accounts=n_accts, per_account=per_acct):
                start = time.time()
                res = domain.auto_comp(self.roster(n_accts, per_acct),
                                       ["tank", "healer", "slow"])
                elapsed = time.time() - start
                self.assertLess(elapsed, 10.0,
                                "%d accounts took %.1fs" % (n_accts, elapsed))
                self.assertEqual(len(res["team"]), 6)
                self.assertEqual(len(res["unmet"]), 0)
                by_id = {c["id"]: c for c in self.roster(n_accts, per_acct)}
                accts = [by_id[cid]["account_id"] for cid in res["slots"]]
                self.assertEqual(len(set(accts)), 6, "one character per account")

    def test_greedy_never_loses_a_requirement_to_exhaustive(self):
        """The fallback may pick a different tiebreak, but it must never fail a
        requirement the exhaustive search could satisfy."""
        for seed in range(12):
            rnd = random.Random(seed)
            n_accts, per_acct = rnd.choice([(6, 3), (7, 3), (8, 2), (9, 2)])
            pool = {}
            for c in self.roster(n_accts, per_acct):
                pool.setdefault(c["account_id"], []).append(c)
            aids = sorted(pool)
            reqs = ["tank", "healer", "slow"]
            exact = domain._exhaustive_best(pool, aids, reqs)
            greedy = domain._greedy_best(pool, aids, reqs)
            self.assertIsNotNone(greedy)
            with self.subTest(seed=seed):
                # key[0] is the number of UNMET requirements - never worse
                self.assertEqual(greedy[0][0], exact[0][0])
