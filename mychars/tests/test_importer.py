"""Importer: duplicate detection, merge-by-(server,name), secret stripping.
Run from eqforge2/:  python -m unittest discover -s mychars/tests
"""
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from mychars import db as dbm, importer  # noqa: E402


def memdb():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    dbm.init(conn)
    return conn


class TestParsing(unittest.TestCase):
    def test_csv_and_json_equivalent(self):
        csv_rows = importer.parse_text("name,server,class,level\nRakthor,Frostreaver,Monk,60\n")
        json_rows = importer.parse_text('[{"name":"Rakthor","server":"Frostreaver","class":"Monk","level":60}]')
        a, _, _ = importer.normalize_rows(csv_rows)
        b, _, _ = importer.normalize_rows(json_rows)
        self.assertEqual(a, b)

    def test_header_aliases(self):
        rows, _, _ = importer.normalize_rows(
            [{"Character": "Fenwick", "Class": "Druid", "Lvl": "44", "Epic": "none"}])
        self.assertEqual(rows[0]["name"], "Fenwick")
        self.assertEqual(rows[0]["class_name"], "Druid")
        self.assertEqual(rows[0]["level"], 44)
        self.assertEqual(rows[0]["server"], "Frostreaver")   # default server

    def test_secrets_are_stripped(self):
        rows, stripped, _ = importer.normalize_rows(
            [{"name": "Rakthor", "password": "hunter2", "AuthToken": "abc",
              "account_password": "x", "class": "Monk"}])
        self.assertEqual(sorted(stripped), ["AuthToken", "account_password", "password"])
        self.assertNotIn("password", rows[0])
        self.assertEqual(set(rows[0]), {"name", "class_name", "server"})

    def test_secrets_never_reach_db(self):
        conn = memdb()
        rows, _, _ = importer.normalize_rows([{"name": "Rakthor", "password": "hunter2"}])
        importer.commit(conn, rows)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(characters)")]
        self.assertFalse(any("pass" in c or "token" in c for c in cols))
        row = conn.execute("SELECT * FROM characters").fetchone()
        self.assertNotIn("hunter2", " ".join(str(v) for v in tuple(row)))


class TestPreviewAndCommit(unittest.TestCase):
    def setUp(self):
        self.conn = memdb()
        self.conn.execute("INSERT INTO accounts(alias) VALUES ('Acct1')")
        self.conn.execute(
            "INSERT INTO characters(name, server, account_id, class_name, level)"
            " VALUES ('Rakthor', 'Frostreaver', 1, 'Monk', 59)")
        self.conn.commit()

    def rows(self, raw):
        rows, _, _ = importer.normalize_rows(raw)
        return rows

    def test_in_batch_duplicates_flagged_once(self):
        plan = importer.preview(self.conn, self.rows(
            [{"name": "Newguy", "class": "Rogue"},
             {"name": "newguy", "server": "frostreaver", "class": "Rogue"}]))
        self.assertEqual(len(plan["create"]), 1)
        self.assertEqual(len(plan["duplicate"]), 1)

    def test_existing_char_is_update_not_create(self):
        plan = importer.preview(self.conn, self.rows(
            [{"name": "RAKTHOR", "server": "Frostreaver", "level": "60"}]))
        self.assertEqual(len(plan["create"]), 0)
        self.assertEqual(len(plan["update"]), 1)
        self.assertEqual(plan["update"][0]["diff"]["level"]["new"], 60)

    def test_identical_reimport_is_noop_duplicate(self):
        plan = importer.preview(self.conn, self.rows(
            [{"name": "Rakthor", "class": "Monk", "level": "59"}]))
        self.assertEqual(len(plan["update"]), 0)
        self.assertEqual(len(plan["duplicate"]), 1)

    def test_blank_cells_never_clobber(self):
        plan = importer.preview(self.conn, self.rows(
            [{"name": "Rakthor", "class": "", "level": ""}]))
        self.assertEqual(len(plan["update"]), 0)      # nothing to change

    def test_missing_account_flagged_but_importable(self):
        plan = importer.preview(self.conn, self.rows(
            [{"name": "Orphan", "account": "Acct9", "class": "Bard"}]))
        self.assertEqual(plan["missing_accounts"], ["Acct9"])
        result = importer.commit(self.conn, self.rows(
            [{"name": "Orphan", "account": "Acct9", "class": "Bard"}]))
        self.assertEqual(result["created"], 1)
        row = self.conn.execute("SELECT account_id FROM characters WHERE name='Orphan'").fetchone()
        self.assertIsNone(row["account_id"])          # left unmapped, not guessed

    def test_commit_counts_and_merge(self):
        result = importer.commit(self.conn, self.rows(
            [{"name": "Rakthor", "level": "60"},                    # update
             {"name": "Rakthor", "level": "60"},                    # in-batch dup
             {"name": "Newgal", "class": "Cleric", "account": "Acct1"}]))  # create
        self.assertEqual(result, {"created": 1, "updated": 1, "skipped_duplicates": 1,
                                  "missing_accounts": []})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM characters").fetchone()[0], 2)
        lvl = self.conn.execute("SELECT level FROM characters WHERE name='Rakthor'").fetchone()[0]
        self.assertEqual(lvl, 60)
        acct = self.conn.execute("SELECT account_id FROM characters WHERE name='Newgal'").fetchone()[0]
        self.assertEqual(acct, 1)


if __name__ == "__main__":
    unittest.main()
