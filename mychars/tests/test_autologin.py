"""AutoLogin login.db reader: sanitization guarantee, class mapping, persona dedup.
Run from eqforge2/:  python -m unittest discover -s mychars/tests
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
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


# The write path needs the REAL schema — unique constraints, the sort_order
# triggers and will_load defaults are exactly what a hand-rolled replica gets
# wrong. Copied verbatim from a live MQ login.db (settings version 9).
REAL_SCHEMA = """
CREATE TABLE server_types (type text primary key, eq_path text not null);
CREATE TABLE accounts (id integer primary key, account text not null,
    password text not null, server_type text default 'import' not null,
    foreign key (server_type) references server_types(type) on delete cascade,
    unique(account, server_type));
CREATE TABLE characters (id integer primary key, character text not null,
    server text not null, account_id integer not null,
    visible INTEGER NOT NULL DEFAULT 1,
    foreign key (account_id) references accounts(id) on delete cascade,
    unique (character, server));
CREATE TABLE personas (id integer primary key, character_id integer not null,
    class text not null, level integer not null, last_seen text,
    foreign key (character_id) references characters(id) on delete cascade,
    unique (character_id, class));
CREATE TABLE profile_groups (id integer primary key, name text not null,
    eq_path text, sort_order integer,
    last_selected integer not null default (strftime('%s', 'now')), unique (name));
CREATE TABLE profiles (id integer primary key, character_id integer not null,
    group_id integer not null, eq_path text, hotkey text, end_after_select integer,
    char_select_delay integer, custom_client_ini text, sort_order integer,
    will_load integer not null default 1, additional_eqgame_args text,
    sounds integer not null default 1,
    foreign key (character_id) references characters(id) on delete cascade,
    foreign key (group_id) references profile_groups(id) on delete cascade,
    unique (character_id, group_id));
CREATE TRIGGER profile_groups_order AFTER INSERT ON profile_groups BEGIN
    UPDATE profile_groups SET sort_order = CASE WHEN s.sort_order IS NULL THEN 1
        ELSE s.sort_order + 1 END
    FROM (SELECT max(sort_order) AS sort_order FROM profile_groups) s
    WHERE id = new.id AND new.sort_order IS NULL; END;
CREATE TRIGGER profiles_order AFTER INSERT ON profiles BEGIN
    UPDATE profiles SET sort_order = CASE WHEN s.sort_order IS NULL THEN 1
        ELSE s.sort_order + 1 END
    FROM (SELECT MAX(sort_order) AS sort_order FROM profiles WHERE group_id = new.group_id) s
    WHERE id = new.id AND new.sort_order IS NULL; END;
"""


class TestProfileGroupWrite(unittest.TestCase):
    """push_profile_group: the one write path into MQ's login.db."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(self.path)
        conn.executescript(REAL_SCHEMA)
        conn.execute("INSERT INTO server_types VALUES ('live', 'C:/EQ')")
        conn.execute("INSERT INTO accounts VALUES (3, ?, ?, 'live')", (SECRET_USER, SECRET_PASS))
        conn.executescript("""
            INSERT INTO characters VALUES (1, 'trixster', 'frostreaver', 3, 1);
            INSERT INTO characters VALUES (2, 'kaelor',   'frostreaver', 3, 1);
            INSERT INTO characters VALUES (3, 'taqiyat',  'frostreaver', 3, 1);
            INSERT INTO profile_groups(id, name) VALUES (1, 'hit squad 1');
            INSERT INTO profiles(character_id, group_id) VALUES (3, 1);
        """)
        conn.commit()
        conn.close()
        self._real_running = autologin.loader_running
        autologin.loader_running = lambda: False        # loader closed unless a test says otherwise

    def tearDown(self):
        autologin.loader_running = self._real_running
        for p in (self.path, self.path + "-wal", self.path + "-shm"):
            if os.path.isfile(p):
                os.unlink(p)
        d, base = os.path.dirname(self.path), os.path.basename(self.path)
        for f in os.listdir(d):
            if f.startswith(base + ".bak-eqforge-"):
                os.unlink(os.path.join(d, f))

    def push(self, names, group="EQF Login Set", replace=False):
        return autologin.push_profile_group(
            self.path, group,
            [{"name": n, "server": "Frostreaver"} for n in names], replace=replace)

    def rows(self, group):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(
                """SELECT c.character, p.sort_order, p.will_load
                   FROM profiles p JOIN characters c ON c.id = p.character_id
                   JOIN profile_groups g ON g.id = p.group_id
                   WHERE g.name = ? ORDER BY p.sort_order""", (group,)).fetchall()
        finally:
            conn.close()

    def test_writes_the_group_in_launch_order(self):
        code, res = self.push(["Trixster", "Kaelor"])
        self.assertEqual(code, 200, res)
        self.assertEqual(res["group"], "eqf login set")        # loader looks up LOWER(name)
        self.assertEqual(res["added"], ["Trixster", "Kaelor"])
        self.assertEqual(self.rows("eqf login set"),
                         [("trixster", 1, 1), ("kaelor", 2, 1)])

    def test_refuses_while_the_loader_is_running(self):
        autologin.loader_running = lambda: True
        code, res = self.push(["Trixster"])
        self.assertEqual(code, 409)
        self.assertTrue(res["loader"])
        self.assertEqual(self.rows("eqf login set"), [])

    def test_refuses_when_it_cannot_tell(self):
        # unknown is not the same as "not running" — never write on a guess
        autologin.loader_running = lambda: None
        code, res = self.push(["Trixster"])
        self.assertEqual(code, 409)
        self.assertEqual(self.rows("eqf login set"), [])

    def test_existing_group_needs_explicit_replace(self):
        code, res = self.push(["Trixster"], group="hit squad 1")
        self.assertEqual(code, 409)
        self.assertTrue(res["exists"])
        self.assertEqual([c["name"] for c in res["current"]], ["Taqiyat"])
        self.assertEqual([r[0] for r in self.rows("hit squad 1")], ["taqiyat"])   # untouched

    def test_replace_swaps_the_membership_wholesale(self):
        code, res = self.push(["Trixster", "Kaelor"], group="hit squad 1", replace=True)
        self.assertEqual(code, 200, res)
        self.assertEqual([r[0] for r in self.rows("hit squad 1")], ["trixster", "kaelor"])

    def test_unknown_character_is_reported_not_invented(self):
        code, res = self.push(["Trixster", "Ghostwho"])
        self.assertEqual(code, 200, res)
        self.assertEqual(res["added"], ["Trixster"])
        self.assertEqual([m["name"] for m in res["missing"]], ["Ghostwho"])

    def test_all_unknown_writes_nothing(self):
        code, res = self.push(["Ghostwho", "Nobody"])
        self.assertEqual(code, 400)
        self.assertEqual(self.rows("eqf login set"), [])

    def test_credentials_are_never_touched(self):
        self.push(["Trixster", "Kaelor"])
        conn = sqlite3.connect(self.path)
        try:
            self.assertEqual(conn.execute("SELECT account, password FROM accounts").fetchall(),
                             [(SECRET_USER, SECRET_PASS)])
        finally:
            conn.close()

    def test_backup_is_taken_before_the_write(self):
        code, res = self.push(["Trixster"])
        self.assertEqual(code, 200, res)
        self.assertTrue(os.path.isfile(res["backup"]))
        conn = sqlite3.connect(res["backup"])
        try:    # the backup predates the write
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM profile_groups WHERE name='eqf login set'").fetchone()[0], 0)
        finally:
            conn.close()

    def test_backup_carries_the_wal(self):
        with open(self.path + "-wal", "wb") as f:
            f.write(b"not a real wal, but it must come along")
        code, res = self.push(["Trixster"])
        self.assertEqual(code, 200, res)
        self.assertTrue(os.path.isfile(res["backup"] + "-wal"))

    def test_backups_are_pruned_to_the_newest_few(self):
        # each set is db + wal + shm; the whole set ages out together
        with open(self.path + "-wal", "wb") as f:
            f.write(b"wal")
        for i in range(5):
            autologin.backup_login_db(self.path, keep=2)
            time.sleep(1.05)                     # stamps are per-second
        folder, prefix = os.path.dirname(self.path), os.path.basename(self.path) + autologin.BACKUP_TAG
        kept = [f for f in os.listdir(folder) if f.startswith(prefix)]
        stamps = {f[len(prefix):len(prefix) + 15] for f in kept}
        self.assertEqual(len(stamps), 2)
        self.assertEqual(len(kept), 4)           # 2 stamps x (.db + -wal)

    def test_missing_db_is_a_clean_404(self):
        code, res = autologin.push_profile_group(self.path + ".nope", "x",
                                                 [{"name": "Trixster", "server": "Frostreaver"}])
        self.assertEqual(code, 404)
        self.assertFalse(res["ok"])

    def test_read_profile_group_returns_none_for_unknown(self):
        self.assertIsNone(autologin.read_profile_group(self.path, "no such group"))


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
