"""Membership load: CSV parsing (incl. old 5-col rows), account mapping, expiry recs.
Run from eqforge2/:  python -m unittest discover -s mychars/tests
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from mychars import api, db as dbm, domain  # noqa: E402

NOW = 1_800_000_000


class TestLoadMembership(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        api.DB_PATH = self.db_path
        self.cfg_dir = tempfile.mkdtemp()
        self.old_cfg = api.MQ_CONFIG_DIR
        api.MQ_CONFIG_DIR = self.cfg_dir
        conn = dbm.connect(self.db_path)
        conn.execute("INSERT INTO accounts(alias) VALUES ('acct-a'), ('acct-b')")
        conn.execute("INSERT INTO characters(name, server, account_id, class_name) VALUES"
                     " ('Rakthor','Frostreaver',1,'Monk'),"
                     " ('Belwyn','Frostreaver',2,'Shaman')")
        conn.commit()
        conn.close()

    def tearDown(self):
        api.DB_PATH = None
        api.MQ_CONFIG_DIR = self.old_cfg
        os.unlink(self.db_path)

    def write_csv(self, body, fname="mychars_export.csv"):
        with open(os.path.join(self.cfg_dir, fname), "w", encoding="utf-8") as f:
            f.write(body)

    def test_apply_and_freshest_asof_wins(self):
        # rows spread across per-toon files AND a legacy shared file — all merged
        header = "name,server,class,level,race,membership,subdays,asof\n"
        self.write_csv(header + "Rakthor,frostreaver,Monk,60,Human,GOLD,26,%d\n" % (NOW - 86400),
                       "mychars_export_Rakthor.csv")               # older, agrees with below
        self.write_csv(header + "rakthor,Frostreaver,Monk,60,Human,GOLD,25,%d\n" % NOW)  # fresher, legacy file
        self.write_csv(header + "Belwyn,frostreaver,Shaman,60,Barbarian,FREE,-1,%d\n" % NOW,
                       "mychars_export_Belwyn.csv")
        code, res = api.handle("POST", "/membership/load", {})
        self.assertEqual(code, 200)
        self.assertEqual(res["rows_matched"], 2)
        self.assertEqual(res["applied"]["acct-a"]["membership"], "GOLD")
        self.assertEqual(res["applied"]["acct-a"]["expires"], NOW + 25 * 86400)
        self.assertEqual(res["applied"]["acct-b"]["membership"], "FREE")
        self.assertIsNone(res["applied"]["acct-b"]["expires"])     # -1 days = unknown

    def test_window_narrowed_by_intersecting_readings(self):
        """Two toons on one account, read a day apart, pin expiry tighter than either.

        This is the real kurakoo case: the freshest reading alone said "lapsed now",
        the intersection says "later today".
        """
        header = "name,server,class,level,race,membership,subdays,asof\n"
        self.write_csv(header + "Rakthor,Frostreaver,Monk,60,Human,GOLD,1,%d\n" % (NOW - 86400),
                       "mychars_export_Rakthor.csv")   # [NOW+0h, NOW+24h)
        conn = dbm.connect(self.db_path)
        conn.execute("UPDATE characters SET account_id=1 WHERE name='Belwyn'")
        conn.commit()
        conn.close()
        self.write_csv(header + "Belwyn,Frostreaver,Shaman,60,Barbarian,GOLD,0,%d\n" % (NOW - 6 * 3600),
                       "mychars_export_Belwyn.csv")    # [NOW-6h, NOW+18h)
        code, res = api.handle("POST", "/membership/load", {})
        self.assertEqual(code, 200)
        got = res["applied"]["acct-a"]
        self.assertEqual(got["expires"], NOW)                    # max of the two lows
        self.assertEqual(got["expires_max"], NOW + 18 * 3600)    # min of the two highs
        self.assertEqual(got["readings"], 2)

    def test_contradicting_readings_fall_back_to_freshest(self):
        """A krono applied mid-series makes old readings wrong — don't average them in."""
        header = "name,server,class,level,race,membership,subdays,asof\n"
        self.write_csv(header + "Rakthor,Frostreaver,Monk,60,Human,GOLD,1,%d\n" % (NOW - 86400),
                       "mychars_export_Rakthor.csv")   # pre-krono: about to lapse
        conn = dbm.connect(self.db_path)
        conn.execute("UPDATE characters SET account_id=1 WHERE name='Belwyn'")
        conn.commit()
        conn.close()
        self.write_csv(header + "Belwyn,Frostreaver,Shaman,60,Barbarian,GOLD,30,%d\n" % NOW,
                       "mychars_export_Belwyn.csv")    # post-krono: 30 days
        code, res = api.handle("POST", "/membership/load", {})
        got = res["applied"]["acct-a"]
        self.assertEqual(got["expires"], NOW + 30 * 86400)
        self.assertEqual(got["expires_max"], NOW + 31 * 86400)
        self.assertEqual(got["readings"], 1)      # only the post-krono one counts
        self.assertEqual(got["stale"], 1)

    def test_stale_reading_does_not_block_later_narrowing(self):
        """Regression: a pre-krono CSV must not pin the account at a 24h window.

        Dropping the whole set on contradiction (the first cut of this code) left
        kurakoo permanently 24h wide, because Ysolde's week-old export sits on disk
        until he next logs in. Stale readings must be dropped one at a time.
        """
        stale = (NOW - 3 * 86400, NOW - 86400, NOW)                  # pre-krono, contradicts
        fresh = (NOW, NOW + 30 * 86400, NOW + 31 * 86400)            # post-krono anchor
        later = (NOW + 86400, NOW + 30 * 86400 + 3600, NOW + 31 * 86400 + 3600)
        lo, hi, used = api._expiry_window([stale, fresh, later])
        self.assertEqual((lo, hi), (NOW + 30 * 86400 + 3600, NOW + 31 * 86400))
        self.assertEqual(hi - lo, 23 * 3600)      # narrowed below a full day
        self.assertEqual(used, 2)                 # stale dropped, both good ones kept

    def test_window_of_single_reading_is_one_day(self):
        lo, hi, used = api._expiry_window([(NOW, NOW + 5 * 86400, NOW + 6 * 86400)])
        self.assertEqual((hi - lo, used), (86400, 1))

    def test_no_readings(self):
        self.assertEqual(api._expiry_window([]), (None, None, 0))

    def test_old_five_column_rows_ignored(self):
        self.write_csv("name,server,class,level,race\nRakthor,frostreaver,Monk,60,Human\n")
        code, res = api.handle("POST", "/membership/load", {})
        self.assertEqual(code, 200)
        self.assertEqual(res["rows_matched"], 0)

    def test_missing_file_graceful(self):
        code, res = api.handle("POST", "/membership/load", {})
        self.assertEqual(code, 200)
        self.assertFalse(res["found"])


class TestMembershipRecs(unittest.TestCase):
    def acct(self, alias, member, expires, expires_max=None):
        return {"id": 1, "alias": alias, "membership": member, "sub_expires": expires,
                "sub_expires_max": expires_max, "launch_order": 1}

    def recs(self, a):
        return [r["message"] for r in
                domain.recommendations([a], [], [], [], now=NOW) if r["kind"] == "membership"]

    def test_expiring_soon_warns(self):
        msgs = self.recs(self.acct("main", "GOLD", NOW + 3 * 86400, NOW + 4 * 86400))
        self.assertEqual(len(msgs), 1)
        self.assertIn("expires in 3–4 days", msgs[0])

    def test_lapsed_only_once_past_the_LATEST_bound(self):
        """The old bug: floor'd days made this fire up to 24h early."""
        # past the earliest but not the latest — still live, must NOT say LAPSED
        msg = self.recs(self.acct("main", "GOLD", NOW - 3600, NOW + 5 * 3600))[0]
        self.assertNotIn("LAPSED", msg)
        self.assertIn("within the next 5h", msg)
        # past the latest — genuinely gone
        self.assertIn("LAPSED", self.recs(self.acct("main", "GOLD", NOW - 2 * 86400, NOW - 60))[0])

    def test_hours_shown_inside_two_days(self):
        msg = self.recs(self.acct("main", "GOLD", NOW + 6 * 3600, NOW + 30 * 3600))[0]
        self.assertIn("expires in 6h–30h", msg)
        self.assertIn("krono today", msg)

    def test_free_flagged(self):
        self.assertIn("FREE", self.recs(self.acct("main", "FREE", None))[0])

    def test_healthy_and_unknown_silent(self):
        self.assertEqual(self.recs(self.acct("main", "GOLD", NOW + 30 * 86400, NOW + 31 * 86400)), [])
        self.assertEqual(self.recs(self.acct("main", "", None)), [])

    def test_legacy_row_without_max_still_works(self):
        """Accounts written before the migration have no sub_expires_max."""
        self.assertIn("LAPSED", self.recs(self.acct("main", "GOLD", NOW - 60))[0])
        self.assertIn("expires in 3 days", self.recs(self.acct("main", "GOLD", NOW + 3 * 86400))[0])

    def test_no_now_skips_membership_checks(self):
        recs = domain.recommendations([self.acct("main", "FREE", None)], [], [], [])
        self.assertFalse([r for r in recs if r["kind"] == "membership"])


if __name__ == "__main__":
    unittest.main()
