"""GET /roster/exports: per-toon CSV merge, freshest-asof dedup, character fields only.
Run from eqforge2/:  python -m unittest discover -s mychars/tests
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from mychars import api  # noqa: E402

NOW = 1_800_000_000
HEADER = "name,server,class,level,race,membership,subdays,asof\n"


class TestLoadExports(unittest.TestCase):
    def setUp(self):
        self.cfg_dir = tempfile.mkdtemp()
        self.old_cfg = api.MQ_CONFIG_DIR
        api.MQ_CONFIG_DIR = self.cfg_dir

    def tearDown(self):
        api.MQ_CONFIG_DIR = self.old_cfg

    def write_csv(self, body, fname):
        with open(os.path.join(self.cfg_dir, fname), "w", encoding="utf-8") as f:
            f.write(body)

    def test_merges_per_toon_files_with_character_fields_only(self):
        self.write_csv(HEADER + "peryn,frostreaver,Rogue,49,Wood Elf,GOLD,20,%d\n" % NOW,
                       "mychars_export_Peryn.csv")
        self.write_csv(HEADER + "Rakthor,frostreaver,Monk,60,Human,GOLD,25,%d\n" % NOW,
                       "mychars_export_Rakthor.csv")
        code, res = api.handle("GET", "/exports")
        self.assertEqual(code, 200)
        self.assertTrue(res["found"])
        self.assertEqual(len(res["files"]), 2)
        self.assertEqual([r["name"] for r in res["rows"]], ["Peryn", "Rakthor"])
        gav = res["rows"][0]
        # lowercased dump values come back EQ-cased; membership/asof never leak into rows
        self.assertEqual(gav["server"], "Frostreaver")
        self.assertEqual(gav["level"], "49")
        self.assertEqual(sorted(gav.keys()), ["class", "level", "name", "race", "server"])

    def test_freshest_asof_wins_across_legacy_shared_file(self):
        self.write_csv(HEADER + "Rakthor,Frostreaver,Monk,58,Human,GOLD,25,%d\n" % (NOW - 86400),
                       "mychars_export.csv")                      # legacy shared, older
        self.write_csv(HEADER + "Rakthor,Frostreaver,Monk,60,Human,GOLD,25,%d\n" % NOW,
                       "mychars_export_Rakthor.csv")             # fresher
        code, res = api.handle("GET", "/exports")
        self.assertEqual(code, 200)
        self.assertEqual(len(res["rows"]), 1)
        self.assertEqual(res["rows"][0]["level"], "60")

    def test_missing_dir_graceful(self):
        api.MQ_CONFIG_DIR = os.path.join(self.cfg_dir, "nope")
        code, res = api.handle("GET", "/exports")
        self.assertEqual(code, 200)
        self.assertFalse(res["found"])
        self.assertIn("mychars_export_*.csv", res["path"])


if __name__ == "__main__":
    unittest.main()
