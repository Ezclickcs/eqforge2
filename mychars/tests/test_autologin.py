"""AutoLogin login.db reader: sanitization guarantee, class mapping, persona dedup.
Run from eqforge2/:  python -m unittest discover -s mychars/tests
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from mychars import autologin  # noqa: E402

SECRET_USER = "mysecretlogin@example.com"
SECRET_PASS = "hunter2-encrypted-blob"


def make_login_db(path):
    """Minimal replica of MQ AutoLogin's login.db schema, with real secrets planted."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE accounts (id integer primary key, account text not null,
            password text not null, server_type text default 'import' not null);
        CREATE TABLE characters (id integer primary key, character text not null,
            server text not null, account_id integer not null,
            visible INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE personas (id integer primary key, character_id integer not null,
            class text not null, level integer not null, last_seen text);
        CREATE TABLE profile_groups (id integer primary key, name text not null);
        CREATE TABLE profiles (id integer primary key, character_id integer not null,
            group_id integer not null);
    """)
    conn.execute("INSERT INTO accounts VALUES (3, ?, ?, 'live')", (SECRET_USER, SECRET_PASS))
    conn.execute("INSERT INTO accounts VALUES (4, 'other@x.com', 'pw2', 'live')")
    conn.executescript("""
        INSERT INTO characters VALUES (1, 'Rakthor', 'frostreaver', 3, 1);
        INSERT INTO characters VALUES (2, 'Zimka', 'frostreaver', 3, 1);
        INSERT INTO characters VALUES (3, 'Belwyn',  'frostreaver', 4, 1);
        INSERT INTO characters VALUES (4, 'Deleted',  'frostreaver', 4, 0);
        INSERT INTO personas VALUES (1, 1, 'MNK', 60, '2026-07-01');
        INSERT INTO personas VALUES (2, 1, 'MNK', 58, '2026-06-01');  -- stale duplicate
        INSERT INTO personas VALUES (3, 2, 'ENC', 60, '2026-07-01');
        INSERT INTO profile_groups VALUES (1, 'trix');
        INSERT INTO profiles VALUES (1, 1, 1);
        INSERT INTO profiles VALUES (2, 2, 1);
    """)
    conn.commit()
    conn.close()


class TestAutologinReader(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        make_login_db(self.path)
        self.result = autologin.read_roster(self.path)

    def tearDown(self):
        os.unlink(self.path)

    def test_credentials_never_leak(self):
        dump = json.dumps(self.result)
        self.assertNotIn(SECRET_PASS, dump)
        self.assertNotIn(SECRET_USER, dump)
        self.assertNotIn("hunter2", dump)
        self.assertNotIn("@", dump)                    # no email-shaped usernames anywhere

    def test_accounts_are_opaque_keys(self):
        self.assertEqual({r["account"] for r in self.result["rows"]}, {"MQAcct3", "MQAcct4"})

    def test_rows_and_class_mapping(self):
        rows = {r["name"]: r for r in self.result["rows"]}
        self.assertEqual(rows["Rakthor"]["class"], "Monk")     # MNK -> Monk
        self.assertEqual(rows["Rakthor"]["level"], 60)         # highest persona wins
        self.assertEqual(rows["Rakthor"]["server"], "Frostreaver")
        self.assertEqual(rows["Zimka"]["class"], "Enchanter")
        self.assertEqual(rows["Belwyn"]["class"], "")          # never seen in game: no persona

    def test_hidden_characters_excluded(self):
        self.assertNotIn("Deleted", [r["name"] for r in self.result["rows"]])
        self.assertEqual(len(self.result["rows"]), 3)

    def test_account_summaries_carry_groups(self):
        accts = {a["key"]: a for a in self.result["accounts"]}
        self.assertEqual(accts["MQAcct3"]["char_count"], 2)
        self.assertEqual(accts["MQAcct3"]["groups"], ["trix"])
        self.assertEqual(accts["MQAcct4"]["groups"], [])

    def test_missing_db_is_graceful(self):
        r = autologin.read_roster(self.path + ".nope")
        self.assertFalse(r["found"])
        self.assertEqual(r["rows"], [])

    def test_read_account_names_usernames_only(self):
        names = autologin.read_account_names(self.path)
        self.assertEqual(names, {3: SECRET_USER, 4: "other@x.com"})
        # the password must never appear, even in this explicit opt-in reader
        self.assertNotIn(SECRET_PASS, json.dumps(names))
        self.assertNotIn("hunter2", json.dumps(names))

    def test_read_account_names_missing_db(self):
        self.assertEqual(autologin.read_account_names(self.path + ".nope"), {})

    def test_rows_feed_importer_cleanly(self):
        from mychars import importer
        rows, stripped, ignored = importer.normalize_rows(self.result["rows"])
        self.assertEqual(len(rows), 3)
        self.assertEqual(stripped, [])
        self.assertEqual(ignored, [])
        by_name = {r["name"]: r for r in rows}
        self.assertEqual(by_name["Rakthor"]["class_name"], "Monk")


if __name__ == "__main__":
    unittest.main()
