"""Account-conflict rule: only one character per EQ account in a live composition.
Run from eqforge2/:  python -m unittest discover -s mychars/tests
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from mychars import api, db as dbm, domain  # noqa: E402


def char(cid, name, acct, cls, auto="tested", overrides=None):
    return {"id": cid, "name": name, "account_id": acct, "account_alias": "Acct%s" % acct,
            "class_name": cls, "automation_status": auto,
            "caps": domain.resolve_capabilities(cls, overrides)}


class TestValidateComposition(unittest.TestCase):
    def codes(self, warns):
        return {w["code"] for w in warns}

    def test_same_account_is_error(self):
        members = [char(1, "Rakthor", 1, "Monk"), char(2, "Zimka", 1, "Enchanter")]
        warns = domain.validate_composition(members)
        conflicts = [w for w in warns if w["code"] == "account_conflict"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["level"], "error")
        self.assertIn("Rakthor", conflicts[0]["message"])
        self.assertIn("Zimka", conflicts[0]["message"])

    def test_distinct_accounts_no_conflict(self):
        members = [char(1, "Rakthor", 1, "Monk"), char(2, "Belwyn", 2, "Shaman")]
        self.assertNotIn("account_conflict", self.codes(domain.validate_composition(members)))

    def test_unassigned_accounts_never_conflict(self):
        members = [char(1, "A", None, "Monk"), char(2, "B", None, "Monk")]
        self.assertNotIn("account_conflict", self.codes(domain.validate_composition(members)))

    def test_role_gap_warnings(self):
        # Monk + Rogue: no tank/heal/slow/haste/cc/rez/ports; puller covered by monk.
        members = [char(1, "A", 1, "Monk"), char(2, "B", 2, "Rogue")]
        codes = self.codes(domain.validate_composition(members))
        for expected in ("no_tank", "no_healer", "no_slow", "no_haste", "no_cc",
                         "no_rez", "no_ports"):
            self.assertIn(expected, codes)
        self.assertNotIn("no_puller", codes)

    def test_full_group_is_clean(self):
        members = [char(1, "War", 1, "Warrior"), char(2, "Shm", 2, "Shaman"),
                   char(3, "Enc", 3, "Enchanter"), char(4, "Mnk", 4, "Monk"),
                   char(5, "Clr", 5, "Cleric"), char(6, "Wiz", 6, "Wizard")]
        codes = self.codes(domain.validate_composition(members))
        self.assertFalse(codes & {"no_tank", "no_healer", "no_slow", "no_haste", "no_cc",
                                  "no_rez", "no_ports", "no_puller", "account_conflict"})

    def test_capability_override_changes_warnings(self):
        # Force the wizard's ports OFF (e.g. spell not scribed) -> no_ports fires.
        members = [char(1, "Wiz", 1, "Wizard", overrides={"ports": False, "evac": False})]
        self.assertIn("no_ports", self.codes(domain.validate_composition(members)))

    def test_untested_automation_listed(self):
        members = [char(1, "A", 1, "Monk", auto="untested")]
        warns = [w for w in domain.validate_composition(members)
                 if w["code"] == "untested_automation"]
        self.assertEqual(len(warns), 1)
        self.assertIn("A", warns[0]["message"])

    def test_required_unlock_missing(self):
        members = [char(1, "A", 1, "Monk"), char(2, "B", 2, "Shaman")]
        warns = domain.validate_composition(
            members, ["Sebilis Key"],
            {1: {"Sebilis Key": "Complete"}, 2: {"Sebilis Key": "Missing"}})
        access = [w for w in warns if w["code"] == "missing_access"]
        self.assertEqual(len(access), 1)
        self.assertIn("B", access[0]["message"])
        self.assertNotIn("A (", access[0]["message"])


class TestCompositionApi(unittest.TestCase):
    """The save endpoint hard-rejects two characters from one account."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        api.DB_PATH = self.db_path
        conn = dbm.connect(self.db_path)
        conn.execute("INSERT INTO accounts(alias) VALUES ('Acct1'), ('Acct2')")
        conn.execute("INSERT INTO characters(name, server, account_id, class_name) VALUES"
                     " ('Rakthor','Frostreaver',1,'Monk'),"
                     " ('Zimka','Frostreaver',1,'Enchanter'),"
                     " ('Belwyn','Frostreaver',2,'Shaman')")
        conn.commit()
        conn.close()

    def tearDown(self):
        api.DB_PATH = None
        os.unlink(self.db_path)

    def test_save_rejects_account_conflict(self):
        code, resp = api.handle("POST", "/compositions",
                                {"name": "Bad Six", "slots": [1, 2, None, None, None, None]})
        self.assertEqual(code, 400)
        self.assertIn("Account conflict", resp["error"])
        self.assertIn("Rakthor", resp["error"])

    def test_save_accepts_one_per_account(self):
        code, resp = api.handle("POST", "/compositions",
                                {"name": "Good Six", "slots": [1, 3, None, None, None, None]})
        self.assertEqual(code, 200)
        self.assertTrue(resp["ok"])

    def test_save_rejects_same_character_twice(self):
        code, resp = api.handle("POST", "/compositions",
                                {"name": "Dupe", "slots": [1, 1, None, None, None, None]})
        self.assertEqual(code, 400)

    def test_validate_endpoint_reports_conflict(self):
        code, resp = api.handle("POST", "/compositions/validate", {"slots": [1, 2]})
        self.assertEqual(code, 200)
        self.assertIn("account_conflict", {w["code"] for w in resp["warnings"]})


if __name__ == "__main__":
    unittest.main()
