"""Lockout import: file parsing, apply/merge, expiry purge, manual-edit preservation.
Run from eqforge2/:  python -m unittest discover -s mychars/tests
"""
import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from mychars import api, db as dbm, lockouts  # noqa: E402

NOW = 1_800_000_000
FUTURE = NOW + 4 * 86400
PAST = NOW - 3600


def memdb():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    dbm.init(conn)
    conn.execute("INSERT INTO characters(name, server, class_name) VALUES"
                 " ('Rakthor','Frostreaver','Monk'), ('Belwyn','Frostreaver','Shaman')")
    conn.execute("INSERT INTO raids(name) VALUES ('Kael (King Tormax)')")
    conn.commit()
    return conn


class TestParseFile(unittest.TestCase):
    def test_parse_and_bad_rows_skipped(self):
        fd, path = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("name|server|expedition|event|expires_epoch\n"
                    "Rakthor|frostreaver|Temple of Veeshan|Vulak`Aerr|%d\n"
                    "Rakthor|frostreaver|Sleeper's Tomb||%d\n"
                    "Broken|row\n"
                    "Noexpiry|frostreaver|Kael|Tormax|soon\n" % (FUTURE, FUTURE))
        try:
            r = lockouts.parse_file(path)
        finally:
            os.unlink(path)
        self.assertTrue(r["found"])
        self.assertEqual(len(r["rows"]), 2)
        self.assertEqual(r["rows"][0]["event"], "Vulak`Aerr")
        self.assertEqual(r["rows"][1]["event"], "")

    def test_missing_file(self):
        self.assertFalse(lockouts.parse_file("Z:/nope/none.txt")["found"])

    def test_raid_display_name(self):
        self.assertEqual(lockouts.raid_display_name("Kael Drakkel", "King Tormax"),
                         "Kael Drakkel: King Tormax")
        self.assertEqual(lockouts.raid_display_name("Sleeper's Tomb", ""), "Sleeper's Tomb")
        self.assertEqual(lockouts.raid_display_name("Plane of Sky", "plane of sky"), "Plane of Sky")


class TestApply(unittest.TestCase):
    def setUp(self):
        self.conn = memdb()

    def apply(self, rows):
        return lockouts.apply(self.conn, rows, now=NOW)

    def test_apply_creates_raid_and_lockout(self):
        res = self.apply([{"name": "RAKTHOR", "server": "frostreaver",
                           "expedition": "Temple of Veeshan", "event": "Vulak`Aerr",
                           "expires": FUTURE}])
        self.assertEqual(res["applied"], 1)
        self.assertEqual(res["raids_created"], ["Temple of Veeshan: Vulak`Aerr"])
        row = self.conn.execute("SELECT * FROM raid_lockouts").fetchone()
        self.assertEqual(row["expires_at"], FUTURE)
        self.assertEqual(row["imported_at"], NOW)   # freshness stamp for the board filter

    def test_reapply_updates_expiry_not_duplicates(self):
        row = {"name": "Rakthor", "server": "Frostreaver", "expedition": "Kael (King Tormax)",
               "event": "", "expires": FUTURE}
        self.apply([row])
        row2 = dict(row, expires=FUTURE + 999)
        self.apply([row2])
        rows = self.conn.execute("SELECT * FROM raid_lockouts").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["expires_at"], FUTURE + 999)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM raids").fetchone()[0], 1)

    def test_expired_rows_skipped_and_unknown_char_reported(self):
        res = self.apply([
            {"name": "Rakthor", "server": "Frostreaver", "expedition": "Old", "event": "",
             "expires": PAST},
            {"name": "Stranger", "server": "Frostreaver", "expedition": "Kael (King Tormax)",
             "event": "", "expires": FUTURE}])
        self.assertEqual(res["applied"], 0)
        self.assertEqual(res["skipped_expired"], 1)
        self.assertEqual(res["unmatched"], ["Stranger (Frostreaver)"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM raid_lockouts").fetchone()[0], 0)

    def test_purge_expired_keeps_manual(self):
        self.conn.execute("INSERT INTO raid_lockouts(character_id, raid_id, expires_at)"
                          " VALUES (1, 1, ?)", (PAST,))
        self.conn.execute("INSERT INTO raid_lockouts(character_id, raid_id) VALUES (2, 1)")
        self.conn.commit()
        n = lockouts.purge_expired(self.conn, now=NOW)
        self.assertEqual(n, 1)
        left = self.conn.execute("SELECT character_id FROM raid_lockouts").fetchall()
        self.assertEqual([r["character_id"] for r in left], [2])   # manual row survives


class TestManualEditPreservesExpiry(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        api.DB_PATH = self.db_path
        conn = dbm.connect(self.db_path)
        conn.execute("INSERT INTO characters(name, server, class_name) VALUES"
                     " ('Rakthor','Frostreaver','Monk')")
        conn.execute("INSERT INTO raids(name) VALUES ('KT'), ('Tunare')")
        far = int(time.time()) + 5 * 86400
        conn.execute("INSERT INTO raid_lockouts(character_id, raid_id, expires_at)"
                     " VALUES (1, 1, ?)", (far,))
        conn.commit()
        conn.close()
        self.far = far

    def tearDown(self):
        api.DB_PATH = None
        os.unlink(self.db_path)

    def test_put_lockouts_keeps_expiry_for_kept_raid(self):
        code, _ = api.handle("PUT", "/characters/1/lockouts", {"raid_ids": [1, 2]})
        self.assertEqual(code, 200)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = {r["raid_id"]: r["expires_at"]
                for r in conn.execute("SELECT raid_id, expires_at FROM raid_lockouts")}
        conn.close()
        self.assertEqual(rows[1], self.far)     # imported expiry preserved
        self.assertIsNone(rows[2])              # manual check has no expiry


if __name__ == "__main__":
    unittest.main()
