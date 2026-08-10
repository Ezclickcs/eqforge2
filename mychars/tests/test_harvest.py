"""Harvest coverage report: dump census, status classification, run-log merge.

The point of this report is that it must never say "covered" when it isn't, so most
of these tests are about the FAULT statuses, not the happy path.
Run from eqforge2/:  python -m unittest discover -s mychars/tests
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from mychars import harvest  # noqa: E402

HEADER = "Location\tName\tID\tCount\tSlots\n"
NOW = 1_800_000_000
OLD = NOW - 20 * 24 * 3600      # past DEFAULT_STALE_H, so only the run log decides


def line(loc, name="Something", iid=100, count=1):
    return "%s\t%s\t%d\t%d\t0\r\n" % (loc, name, iid, count)


def full_dump(extra=(), bank_slots=24, shared_slots=8):
    """A structurally complete dump: every top-level bank/shared slot listed."""
    rows = [line("Chest", "Bronze BP", 300), line("General1", "Bag", 208)]
    for i in range(1, bank_slots + 1):
        rows.append(line("Bank%d" % i, "Empty", 0, 0))
    for i in range(1, shared_slots + 1):
        rows.append(line("SharedBank%d" % i, "Empty", 0, 0))
    rows.extend(extra)
    return rows


class _World(unittest.TestCase):
    def setUp(self):
        self.eq_dir = tempfile.mkdtemp()
        self.mq_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.eq_dir, ignore_errors=True)
        shutil.rmtree(self.mq_dir, ignore_errors=True)

    def dump(self, name, rows, mtime=None):
        p = os.path.join(self.eq_dir, "%s_frostreaver-Inventory.txt" % name)
        with open(p, "w", encoding="latin-1", newline="") as f:
            f.write(HEADER + "".join(rows))
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    def char(self, name, alias="Acct1", tags=""):
        return {"id": abs(hash(name)) % 10000, "name": name, "server": "Frostreaver",
                "account_id": 1, "account_alias": alias, "class_name": "Monk",
                "level": 60, "group_tags": tags}

    def run_file(self, account, started=None, done=(), errors=(), current=None,
                 finished=True, queue=None):
        """Write the append-only event log (+ optional armed queue) the Lua produces."""
        lines = ["armed|%d|%d\n" % (started or NOW - 600, len(done) + len(errors))]
        if current:
            lines.append("current|%s|%d\n" % (current, NOW - 30))
        for d in done:
            lines.append("done|%s|%d|%s|%s\n" % (
                d["name"], d.get("at", NOW - 300), d.get("result", "dumped"),
                d.get("note", "")))
        for e in errors:
            lines.append("error|%s|%d|%s\n" % (
                e["name"], e.get("at", NOW - 100), e.get("error", "failed")))
        if finished:
            lines.append("finish|%d\n" % (NOW - 60))
        with open(os.path.join(self.mq_dir, "harvest_%s.log" % account),
                  "w", encoding="utf-8") as f:
            f.writelines(lines)
        if queue is not None:
            harvest.write_queue(self.mq_dir, account,
                                [{"name": n, "hoard": False} for n in queue])
            # write_queue truncates the log; restore the events under it
            with open(os.path.join(self.mq_dir, "harvest_%s.log" % account),
                      "w", encoding="utf-8") as f:
                f.writelines(lines)

    def hoard_stamp(self, name, at):
        """The sidecar the dump guards write when Hoard rows are captured LIVE."""
        p = os.path.join(self.eq_dir, "%s_frostreaver%s" % (name, harvest.HOARD_STAMP_SUFFIX))
        with open(p, "w", encoding="latin-1") as f:
            f.write("%d\n" % at)
        return p

    def cover(self, chars, **kw):
        run = harvest.read_runs(self.mq_dir)
        return harvest.coverage(self.eq_dir, chars, run=run, now=NOW, **kw)

    def status_of(self, result, name):
        return next(r["status"] for r in result["rows"] if r["name"] == name)


class ScanTests(_World):
    def test_counts_items_per_bucket_ignoring_empty(self):
        self.dump("Rakthor", full_dump(extra=[
            line("Bank3", "Sword", 209), line("SharedBank1", "Ring", 204),
            line("Hoard1", "Ore", 500), line("Personal-Depot1", "Clay", 501)]))
        s = harvest.scan_dump(os.path.join(self.eq_dir, "Rakthor_frostreaver-Inventory.txt"))
        self.assertTrue(s["ok"])
        self.assertEqual(s["items"]["worn"], 1)
        self.assertEqual(s["items"]["bags"], 1)
        self.assertEqual(s["items"]["bank"], 1)      # the 24 "Empty" rows don't count
        self.assertEqual(s["items"]["shared"], 1)
        self.assertEqual(s["items"]["hoard"], 1)
        self.assertEqual(s["items"]["depot"], 1)

    def test_empty_bank_still_counts_its_slots(self):
        """The whole truncation detector rests on this distinction."""
        self.dump("Empty", full_dump())
        s = harvest.scan_dump(os.path.join(self.eq_dir, "Empty_frostreaver-Inventory.txt"))
        self.assertEqual(s["items"]["bank"], 0)
        self.assertEqual(s["bank_slots"], 24)
        self.assertEqual(s["shared_slots"], 8)

    def test_non_dump_file_is_flagged_not_crashed(self):
        p = os.path.join(self.eq_dir, "Junk_frostreaver-Inventory.txt")
        with open(p, "w", encoding="latin-1") as f:
            f.write("this is not a dump\n")
        s = harvest.scan_dump(p)
        self.assertFalse(s["ok"])
        self.assertIn("Location", s["error"])


class StatusTests(_World):
    def test_complete_recent_dump_is_ok(self):
        self.dump("Rakthor", full_dump(), mtime=NOW - 3600)
        self.assertEqual(self.status_of(self.cover([self.char("Rakthor")]), "Rakthor"), "ok")

    def test_no_dump_at_all_is_missing(self):
        r = self.cover([self.char("Ghost")])
        self.assertEqual(self.status_of(r, "Ghost"), "missing")

    def test_short_bank_listing_is_truncated(self):
        """Dumped before inventory finished loading — the fast-dump bug."""
        self.dump("Speedy", full_dump(bank_slots=6), mtime=NOW - 60)
        r = self.cover([self.char("Speedy")])
        self.assertEqual(self.status_of(r, "Speedy"), "truncated")
        self.assertIn("6/24", next(x["note"] for x in r["rows"] if x["name"] == "Speedy"))

    def test_missing_shared_slots_also_truncated(self):
        self.dump("Speedy", full_dump(shared_slots=0), mtime=NOW - 60)
        self.assertEqual(self.status_of(self.cover([self.char("Speedy")]), "Speedy"), "truncated")

    def test_old_dump_is_stale(self):
        self.dump("Dusty", full_dump(), mtime=NOW - 40 * 24 * 3600)
        self.assertEqual(self.status_of(self.cover([self.char("Dusty")]), "Dusty"), "stale")

    def test_stale_window_is_configurable(self):
        self.dump("Dusty", full_dump(), mtime=NOW - 10 * 3600)
        self.assertEqual(self.status_of(self.cover([self.char("Dusty")]), "Dusty"), "ok")
        self.assertEqual(
            self.status_of(self.cover([self.char("Dusty")], stale_h=5), "Dusty"), "stale")

    def test_hoard_tagged_toon_without_hoard_rows(self):
        self.dump("Banker", full_dump(), mtime=NOW - 60)
        r = self.cover([self.char("Banker", tags="banker,hoard")])
        self.assertEqual(self.status_of(r, "Banker"), "hoard-missed")

    def test_hoard_tagged_toon_with_hoard_rows_is_ok(self):
        self.dump("Banker", full_dump(extra=[line("Hoard1", "Ore", 500)]), mtime=NOW - 60)
        r = self.cover([self.char("Banker", tags="banker,hoard")])
        self.assertEqual(self.status_of(r, "Banker"), "ok")

    def test_untagged_toon_without_hoard_rows_is_fine(self):
        """Most toons have no Hoard at all — absence must not be a fault."""
        self.dump("Plain", full_dump(), mtime=NOW - 60)
        self.assertEqual(self.status_of(self.cover([self.char("Plain")]), "Plain"), "ok")


class HoardFreshnessTests(_World):
    """A dump whose Hoard rows were SPLICED forward by the dump guards has a fresh
    mtime but stale hoard data. Without the sidecar stamp the report would call that
    "ok" — the one thing this report must never do (reported 2026-08-09)."""

    def hoarder(self, name="Vaypur"):
        return self.char(name, tags="banker,hoard")

    def test_live_hoard_capture_reads_ok(self):
        self.dump("Vaypur", full_dump(extra=[line("Hoard 1", "Sarnak Prayer Beads", 5773)]),
                  mtime=NOW - 600)
        self.hoard_stamp("Vaypur", NOW - 600)
        self.assertEqual(self.status_of(self.cover([self.hoarder()]), "Vaypur"), "ok")

    def test_carried_forward_hoard_is_reported_stale_even_though_the_dump_is_fresh(self):
        self.dump("Vaypur", full_dump(extra=[line("Hoard 1", "Sarnak Prayer Beads", 5773)]),
                  mtime=NOW - 600)                      # dumped 10 minutes ago...
        self.hoard_stamp("Vaypur", NOW - 30 * 24 * 3600)   # ...hoard last SEEN a month ago
        res = self.cover([self.hoarder()])
        row = next(r for r in res["rows"] if r["name"] == "Vaypur")
        self.assertEqual(row["status"], "stale")
        self.assertIn("carried forward", row["note"])
        self.assertLess(row["dump_age_h"], 2)           # the dump really is fresh
        self.assertGreater(row["hoard_age_h"], 600)     # the hoard rows are not

    def test_no_stamp_means_no_claim_either_way(self):
        """Dumps written before the guards existed have no sidecar. Absence must not
        invent staleness — it just leaves hoard age unknown."""
        self.dump("Vaypur", full_dump(extra=[line("Hoard 1", "Sarnak Prayer Beads", 5773)]),
                  mtime=NOW - 600)
        row = next(r for r in self.cover([self.hoarder()])["rows"] if r["name"] == "Vaypur")
        self.assertIsNone(row["hoard_asof"])
        self.assertIsNone(row["hoard_age_h"])
        self.assertEqual(row["status"], "ok")

    def test_an_old_stamp_does_not_downgrade_a_toon_with_no_hoard_rows(self):
        """Only rows actually PRESENT in the dump can be stale. A toon whose hoard is
        genuinely empty already has its own status and must not be double-flagged."""
        self.dump("Vaypur", full_dump(), mtime=NOW - 600)
        self.hoard_stamp("Vaypur", NOW - 30 * 24 * 3600)
        # tagged `hoard` with zero Hoard rows is the pre-existing, more specific fault
        self.assertEqual(self.status_of(self.cover([self.hoarder()]), "Vaypur"),
                         "hoard-missed")
        # ...and an untagged toon stays ok
        self.assertEqual(self.status_of(self.cover([self.char("Vaypur")]), "Vaypur"), "ok")

    def test_unreadable_stamp_is_ignored(self):
        self.dump("Vaypur", full_dump(extra=[line("Hoard 1", "Beads", 5773)]), mtime=NOW - 600)
        p = os.path.join(self.eq_dir, "Vaypur_frostreaver" + harvest.HOARD_STAMP_SUFFIX)
        with open(p, "w", encoding="latin-1") as f:
            f.write("not a number\n")
        row = next(r for r in self.cover([self.hoarder()])["rows"] if r["name"] == "Vaypur")
        self.assertIsNone(row["hoard_asof"])
        self.assertEqual(row["status"], "ok")


class RunLogTests(_World):
    def test_attempted_but_file_not_rewritten_is_failed(self):
        """The case the dumps alone can NEVER show."""
        self.dump("Zimkin", full_dump(), mtime=OLD)              # predates the run
        self.run_file("Acct1", started=NOW - 600,
                      done=[{"name": "Zimkin", "at": NOW - 300, "result": "dumped"}])
        r = self.cover([self.char("Zimkin")])
        self.assertEqual(self.status_of(r, "Zimkin"), "failed")

    def test_stale_but_never_attempted_stays_stale_not_failed(self):
        """Same old file; the difference is only in the run log."""
        self.dump("Zimkin", full_dump(), mtime=OLD)
        self.run_file("Acct1", started=NOW - 600, done=[])
        self.assertEqual(self.status_of(self.cover([self.char("Zimkin")]), "Zimkin"), "stale")

    def test_run_reported_error_is_failed_with_its_note(self):
        self.dump("Stuck", full_dump(), mtime=NOW - 30)
        self.run_file("Acct1", started=NOW - 600,
                      errors=[{"name": "Stuck", "at": NOW - 100, "error": "camp timeout"}])
        r = self.cover([self.char("Stuck")])
        self.assertEqual(self.status_of(r, "Stuck"), "failed")
        self.assertIn("camp timeout", next(x["note"] for x in r["rows"] if x["name"] == "Stuck"))

    def test_current_toon_is_in_progress(self):
        self.run_file("Acct1", started=NOW - 60, current="Mulgrim",
                      finished=False, queue=["Mulgrim"])
        r = self.cover([self.char("Mulgrim")])
        self.assertEqual(self.status_of(r, "Mulgrim"), "in-progress")
        self.assertTrue(r["run"]["running"])

    def test_armed_queue_without_finish_reads_as_running(self):
        """`running` means armed and not finished — that is what stops the report
        calling a mid-run toon `failed`."""
        self.run_file("Acct1", finished=False, queue=["A", "B"])
        r = self.cover([self.char("A")])
        acct = r["run"]["accounts"][0]
        self.assertEqual(acct["state"], "running")
        self.assertTrue(acct["armed"])
        self.assertEqual(acct["queue"], ["A", "B"])

    def test_disarmed_queue_is_not_running(self):
        self.run_file("Acct1", finished=False, queue=["A"])
        self.assertEqual(harvest.disarm(self.mq_dir, "Acct1"), ["Acct1"])
        r = self.cover([self.char("A")])
        self.assertFalse(r["run"]["running"])
        self.assertFalse(r["run"]["accounts"][0]["armed"])

    def test_half_written_log_line_is_ignored_not_fatal(self):
        """The Lua appends between logins — a truncated tail must never break this."""
        with open(os.path.join(self.mq_dir, "harvest_Acct1.log"), "w", encoding="utf-8") as f:
            f.write("armed|%d|2\ndone|Rakthor|%d|dumped|\ndone|Gun" % (NOW - 600, NOW - 300))
        self.dump("Rakthor", full_dump(), mtime=NOW - 60)
        r = self.cover([self.char("Rakthor")])
        self.assertEqual(self.status_of(r, "Rakthor"), "ok")
        self.assertEqual([d["name"] for d in r["run"]["accounts"][0]["done"]],
                         ["Rakthor", "Gun"])

    def test_no_run_log_at_all_still_reports(self):
        self.dump("Rakthor", full_dump(), mtime=NOW - 60)
        r = self.cover([self.char("Rakthor")])
        self.assertEqual(self.status_of(r, "Rakthor"), "ok")
        self.assertFalse(r["run"]["running"])


class QueueTests(_World):
    """The queue file is the arming mechanism — both Lua scripts fire on every login
    and must no-op without it, so its format is a contract."""

    def test_round_trip_preserves_order_and_hoard_flag(self):
        harvest.write_queue(self.mq_dir, "coldflame", [
            {"name": "Torvin", "hoard": True},
            {"name": "Rakthor", "hoard": False},
            {"name": "Corvane", "hoard": True}])
        q = harvest.read_queue(harvest.queue_path(self.mq_dir, "coldflame"))
        self.assertEqual(q["account"], "coldflame")
        self.assertEqual([(e["name"], e["hoard"]) for e in q["entries"]],
                         [("Torvin", True), ("Rakthor", False), ("Corvane", True)])

    def test_comments_and_blank_lines_ignored(self):
        p = harvest.queue_path(self.mq_dir, "x")
        with open(p, "w", encoding="utf-8") as f:
            f.write("# a comment\n\naccount=x\nTorvin|hoard\n\n# trailing\n")
        q = harvest.read_queue(p)
        self.assertEqual([e["name"] for e in q["entries"]], ["Torvin"])

    def test_missing_queue_file_reads_empty_not_error(self):
        q = harvest.read_queue(harvest.queue_path(self.mq_dir, "nope"))
        self.assertEqual(q["entries"], [])

    def test_arming_truncates_the_previous_run_log(self):
        """A new run must not inherit the last run's done/errors."""
        self.run_file("acct", done=[{"name": "Old"}])
        harvest.write_queue(self.mq_dir, "acct", [{"name": "New"}])
        run = harvest.read_runs(self.mq_dir)
        self.assertEqual(run["accounts"][0]["done"], [])
        self.assertEqual(run["accounts"][0]["queue"], ["New"])

    def test_disarm_all_accounts(self):
        harvest.write_queue(self.mq_dir, "a", [{"name": "X"}])
        harvest.write_queue(self.mq_dir, "b", [{"name": "Y"}])
        self.assertEqual(sorted(harvest.disarm(self.mq_dir)), ["a", "b"])
        self.assertFalse(harvest.read_runs(self.mq_dir)["running"])


class SummaryTests(_World):
    def test_problems_sort_above_healthy_rows(self):
        self.dump("Fine", full_dump(), mtime=NOW - 60)
        self.dump("Short", full_dump(bank_slots=2), mtime=NOW - 60)
        r = self.cover([self.char("Fine"), self.char("Ghost"), self.char("Short")])
        self.assertEqual([x["name"] for x in r["rows"]], ["Ghost", "Short", "Fine"])

    def test_needs_action_excludes_ok(self):
        self.dump("Fine", full_dump(), mtime=NOW - 60)
        r = self.cover([self.char("Fine"), self.char("Ghost")])
        self.assertEqual(r["summary"]["needs_action"], 1)
        self.assertEqual(r["summary"]["total"], 2)

    def test_dump_with_no_roster_row_is_an_orphan(self):
        """A rename or a never-imported toon must not vanish from the count."""
        self.dump("Nobody", full_dump(), mtime=NOW - 60)
        r = self.cover([self.char("Ghost")])
        self.assertEqual(r["orphan_dumps"], ["nobody (frostreaver)"])

    def test_per_account_rollup(self):
        self.dump("A1", full_dump(), mtime=NOW - 60)
        r = self.cover([self.char("A1", "Acct1"), self.char("A2", "Acct1"),
                        self.char("B1", "Acct2")])
        by = {a["account"]: a for a in r["summary"]["by_account"]}
        self.assertEqual((by["Acct1"]["ok"], by["Acct1"]["total"]), (1, 2))
        self.assertEqual((by["Acct2"]["ok"], by["Acct2"]["total"]), (0, 1))


if __name__ == "__main__":
    unittest.main()
