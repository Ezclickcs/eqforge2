"""Harvest coverage: what each roster toon's inventory dump ACTUALLY contains.

Answers "did the last dump run miss anyone, and did it get everything?" without
trusting the run to report on itself. Two independent sources, deliberately:

  1. **The dumps on disk** (`<EQ_DIR>/<Name>_<server>-Inventory.txt`) — ground truth
     for WHAT was captured.
  2. **The run log** (`<MQ config>/harvest_<account>.json`, written by the rotation
     script) — the ONLY source for "we tried this toon and nothing was written".
     The dumps alone can never show that: `/outputfile` rewrites the file even when
     the contents are identical, so an untouched mtime after an attempt is the only
     evidence of a silent failure, and a stale file with no attempt just means
     "not in this run's queue". Same fact, two very different fixes.

Counted RAW here rather than through `gear.parse_dump` — that one filters to
gear-relevant rows (and silently drops `Personal-Depot`), which is right for the
gear engine and wrong for a census.

Truths verified against 36 real dumps (2026-08-04):
  - Bank AND SharedBank arrive in the login packet — they appear with no banker in
    sight. Only Hoard and Depot need their window open, so those are the only
    buckets a bank visit adds.
  - A complete dump always lists **24 top-level `Bank<n>` rows and 8 `SharedBank<n>`
    rows**, "Empty" included. Fewer than that means the dump fired before the
    inventory finished loading — the truncation case worth catching.
"""
import os
import re
import time

from .gear import loc_bucket

DUMP_SUFFIX = "-Inventory.txt"
RUN_PREFIX = "harvest_"
QUEUE_SUFFIX = ".queue"     # armed work list, written here, popped by the Lua
LOG_SUFFIX = ".log"         # append-only event log, written by the Lua

# The queue file is the ARMING mechanism. ingame.cfg / charselect.cfg fire for every
# character on every login, so both Lua scripts exit instantly when no queue file
# exists for that station name. Deleting the file is the hard stop.
QUEUE_HEADER = (
    "# EQ Forge harvest queue - DELETE THIS FILE TO DISARM.\n"
    "# One toon per line: <Name>|plain  or  <Name>|hoard (hoard = visit a banker first).\n"
    "# The Lua pops the top line as each toon finishes; when empty the run ends.\n")

# Top-level container slots (bag CONTENTS look like "Bank5-Slot2" and are excluded
# by the trailing $ — only the outer slots are a fixed-size, always-listed set).
BANK_SLOT_RE = re.compile(r"^Bank\d+$")
SHARED_SLOT_RE = re.compile(r"^SharedBank\d+$")
BANK_SLOTS_EXPECTED = 24
SHARED_SLOTS_EXPECTED = 8

# Buckets reported per toon, in display order. loc_bucket() supplies the values.
BUCKETS = ["worn", "bags", "bank", "shared", "hoard", "depot", "keyring", "persona"]

# A toon only counts as "should have Hoard rows" when tagged — mirrors the
# `banker` group_tags convention in gearsets._is_banker. Nothing in an EQ dump
# says "this character owns a Dragon's Hoard", so it has to be declared.
HOARD_TAG = "hoard"

DEFAULT_STALE_H = 24 * 7        # a week — mules change rarely; players re-dump on camp

# Worst first — the table sorts by this so the problems are always on top.
STATUS_ORDER = {"missing": 0, "truncated": 1, "failed": 2, "unreadable": 3,
                "hoard-missed": 4, "stale": 5, "in-progress": 6, "ok": 7}

# Statuses that mean "a toon's data is wrong or absent right now".
ACTIONABLE = {"missing", "truncated", "failed", "unreadable", "hoard-missed", "stale"}

STATUS_HELP = {
    "ok": "Dump is complete and recent.",
    "stale": "Dump is complete but older than the freshness window.",
    "missing": "Roster toon with no dump file at all — never harvested.",
    "truncated": "Dump fired before inventory finished loading; bank/shared rows are short.",
    "failed": "The run reached this toon but no dump was written.",
    "unreadable": "The file exists but isn't a readable inventory dump.",
    "hoard-missed": "Tagged `hoard`, but the dump has no Hoard rows — needs a bank window.",
    "in-progress": "Being harvested right now.",
}


def scan_dump(path):
    """Raw per-bucket census of one dump file.

    -> {"ok", "error", "total_rows", "items": {bucket: n}, "slots": {bucket: n},
        "bank_slots", "shared_slots"}

    `items` counts real items; `slots` counts every listed row including "Empty".
    Both matter: 0 items + 24 slots = an empty bank (fine); 0 items + 0 slots =
    a bank that never arrived (broken dump).
    """
    out = {"ok": False, "error": None, "total_rows": 0,
           "items": {b: 0 for b in BUCKETS}, "slots": {b: 0 for b in BUCKETS},
           "bank_slots": 0, "shared_slots": 0}
    try:
        with open(path, encoding="latin-1") as f:
            header = f.readline()
            if not header.startswith("Location"):
                out["error"] = "no Location header (not an /outputfile inventory dump)"
                return out
            for line in f:
                parts = line.rstrip("\r\n").split("\t")
                if len(parts) < 2:
                    continue
                loc, name = parts[0], parts[1]
                if not loc:
                    continue
                out["total_rows"] += 1
                bucket = loc_bucket(loc)
                if bucket in out["slots"]:
                    out["slots"][bucket] += 1
                    if name and name != "Empty":
                        out["items"][bucket] += 1
                if BANK_SLOT_RE.match(loc):
                    out["bank_slots"] += 1
                elif SHARED_SLOT_RE.match(loc):
                    out["shared_slots"] += 1
    except OSError as e:
        out["error"] = str(e)
        return out
    out["ok"] = True
    return out


def queue_path(mq_config_dir, account):
    return os.path.join(mq_config_dir, RUN_PREFIX + account + QUEUE_SUFFIX)


def log_path(mq_config_dir, account):
    return os.path.join(mq_config_dir, RUN_PREFIX + account + LOG_SUFFIX)


def write_queue(mq_config_dir, account, entries):
    """Arm a harvest for one account. entries: [{"name", "hoard": bool}].

    One file per account — six clients must never share one (2026-07-25 write race).
    Truncates the previous log so a new run's status can't inherit the last one's.
    """
    os.makedirs(mq_config_dir, exist_ok=True)
    lines = [QUEUE_HEADER, "account=%s\n" % account]
    for e in entries:
        lines.append("%s|%s\n" % (e["name"], "hoard" if e.get("hoard") else "plain"))
    with open(queue_path(mq_config_dir, account), "w", encoding="utf-8") as f:
        f.writelines(lines)
    with open(log_path(mq_config_dir, account), "w", encoding="utf-8") as f:
        f.write("armed|%d|%d\n" % (int(time.time()), len(entries)))
    return queue_path(mq_config_dir, account)


def disarm(mq_config_dir, account=None):
    """Delete queue files — the hard stop. Logs are kept so the report can still
    explain what happened on the toons the run already touched."""
    removed = []
    if not os.path.isdir(mq_config_dir):
        return removed
    for fn in sorted(os.listdir(mq_config_dir)):
        if not (fn.startswith(RUN_PREFIX) and fn.endswith(QUEUE_SUFFIX)):
            continue
        acct = fn[len(RUN_PREFIX):-len(QUEUE_SUFFIX)]
        if account and acct.lower() != account.lower():
            continue
        try:
            os.remove(os.path.join(mq_config_dir, fn))
            removed.append(acct)
        except OSError:
            pass
    return removed


def read_queue(path):
    """-> {"account": str, "entries": [{"name", "hoard"}]}. Tolerant by design: this
    file is rewritten by the Lua between logins and may be read mid-write."""
    account, entries = "", []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.lower().startswith("account="):
                    account = line.split("=", 1)[1].strip()
                    continue
                name, _, flag = line.partition("|")
                if name.strip():
                    entries.append({"name": name.strip(),
                                    "hoard": flag.strip().lower() == "hoard"})
    except OSError:
        pass
    return {"account": account, "entries": entries}


def read_runs(mq_config_dir):
    """Merge each account's queue + append-only event log into one run picture.

    Pipe-delimited rather than JSON because the writer is MQ Lua: appending one line
    with io.open(path,'a') can't half-write a structure the way rewriting a JSON blob
    can, and it needs no Lua JSON library.

      armed|<epoch>|<total>
      current|<name>|<epoch>
      done|<name>|<epoch>|<result>|<note>
      error|<name>|<epoch>|<message>
      finish|<epoch>

    -> {"accounts", "attempted": {name_lower: attempt}, "started", "updated",
        "running", "errors": [str]}
    """
    runs, attempted, bad = [], {}, []
    started = updated = None
    if not os.path.isdir(mq_config_dir):
        return {"accounts": [], "attempted": {}, "started": None,
                "updated": None, "running": False, "errors": []}

    accounts = set()
    for fn in os.listdir(mq_config_dir):
        if fn.startswith(RUN_PREFIX) and fn.endswith((QUEUE_SUFFIX, LOG_SUFFIX)):
            suffix = QUEUE_SUFFIX if fn.endswith(QUEUE_SUFFIX) else LOG_SUFFIX
            accounts.add(fn[len(RUN_PREFIX):-len(suffix)])

    for acct in sorted(accounts):
        qpath = queue_path(mq_config_dir, acct)
        armed = os.path.isfile(qpath)
        queue = read_queue(qpath)["entries"] if armed else []
        run = {"account": acct, "file": os.path.basename(qpath), "armed": armed,
               "queue": [e["name"] for e in queue], "current": None,
               "done": [], "errors": [], "state": "idle",
               "started": None, "updated": None, "total": len(queue)}
        try:
            with open(log_path(mq_config_dir, acct), encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            lines = []
        finished = False
        for raw in lines:
            p = [x.strip() for x in raw.rstrip("\n").split("|")]
            if not p or not p[0]:
                continue
            kind = p[0]
            at = int(p[2]) if kind in ("done", "error") and len(p) > 2 and p[2].isdigit() \
                else (int(p[1]) if len(p) > 1 and p[1].isdigit() else None)
            if at is not None:
                run["updated"] = at if run["updated"] is None else max(run["updated"], at)
            if kind == "armed":
                run["started"] = at
                if len(p) > 2 and p[2].isdigit():
                    run["total"] = max(run["total"], int(p[2]))
            elif kind == "current" and len(p) > 1:
                run["current"] = p[1]
            elif kind == "done" and len(p) > 1:
                run["done"].append({"name": p[1], "at": at,
                                    "result": p[3] if len(p) > 3 else "dumped",
                                    "note": p[4] if len(p) > 4 else ""})
                if run["current"] == p[1]:
                    run["current"] = None
            elif kind == "error" and len(p) > 1:
                run["errors"].append({"name": p[1], "at": at,
                                      "error": p[3] if len(p) > 3 else "failed"})
                if run["current"] == p[1]:
                    run["current"] = None
            elif kind == "finish":
                finished = True
            elif kind not in ("armed", "current", "done", "error", "finish"):
                bad.append("%s: unknown event %r" % (os.path.basename(
                    log_path(mq_config_dir, acct)), kind))

        run["state"] = "running" if (armed and not finished) else \
                       "done" if finished else "idle"
        runs.append(run)
        if run["started"] is not None:
            started = run["started"] if started is None else min(started, run["started"])
        if run["updated"] is not None:
            updated = run["updated"] if updated is None else max(updated, run["updated"])
        for d in run["done"]:
            attempted[d["name"].lower()] = {"account": acct, "at": d["at"],
                                            "result": d["result"], "note": d["note"]}
        for e in run["errors"]:
            attempted[e["name"].lower()] = {"account": acct, "at": e["at"],
                                            "result": "error", "note": e["error"]}
        if run["current"]:
            attempted.setdefault(run["current"].lower(), {
                "account": acct, "at": None, "result": "in-progress", "note": ""})

    return {"accounts": runs, "attempted": attempted, "started": started,
            "updated": updated,
            "running": any(r["state"] == "running" for r in runs), "errors": bad}


def _status(scan, mtime, attempt, run_started, now, stale_h, wants_hoard):
    """-> (status, note). Priority: the most specific actionable fault wins."""
    if scan is None:
        if attempt and attempt.get("result") == "in-progress":
            return "in-progress", "harvesting now"
        return "missing", "no dump file has ever been written for this toon"
    if not scan["ok"]:
        return "unreadable", scan["error"] or "could not read the dump"

    if scan["bank_slots"] < BANK_SLOTS_EXPECTED or scan["shared_slots"] < SHARED_SLOTS_EXPECTED:
        return "truncated", ("only %d/%d bank and %d/%d shared slots listed — the dump "
                             "fired before inventory finished loading"
                             % (scan["bank_slots"], BANK_SLOTS_EXPECTED,
                                scan["shared_slots"], SHARED_SLOTS_EXPECTED))

    # Attempted this run but the file predates the run = /outputfile never wrote.
    if attempt and run_started and mtime is not None and mtime < run_started:
        if attempt.get("result") == "in-progress":
            return "in-progress", "harvesting now"
        return "failed", ("the run reached this toon but the dump was never rewritten"
                          + (" (%s)" % attempt["note"] if attempt.get("note") else ""))
    if attempt and attempt.get("result") == "error":
        return "failed", attempt.get("note") or "the run reported an error"

    if wants_hoard and scan["items"]["hoard"] == 0:
        return "hoard-missed", ("tagged `hoard` but the dump has no Hoard rows — needs a "
                               "banker window open (mychars_bankrun.lua)")

    age_h = None if mtime is None else (now - mtime) // 3600
    if age_h is not None and age_h >= stale_h:
        return "stale", "dump is %s old" % _age_label(age_h)
    return "ok", ""


def _age_label(hours):
    if hours < 48:
        return "%d hour%s" % (hours, "" if hours == 1 else "s")
    days = hours // 24
    return "%d day%s" % (days, "" if days == 1 else "s")


def coverage(eq_dir, chars, run=None, now=None, stale_h=DEFAULT_STALE_H):
    """Join roster characters to their dumps and the run log.

    chars: dicts with at least name/server; account_alias, class_name, level,
    group_tags used for display and the hoard tag. Pure — no DB, no HTTP.
    """
    now = int(time.time()) if now is None else now
    run = run or {"attempted": {}, "started": None, "updated": None,
                  "running": False, "accounts": [], "errors": []}
    attempted = run.get("attempted") or {}
    run_started = run.get("started")

    dumps = {}
    if os.path.isdir(eq_dir):
        for fn in os.listdir(eq_dir):
            if not fn.endswith(DUMP_SUFFIX):
                continue
            base = fn[:-len(DUMP_SUFFIX)]
            name, _, server = base.partition("_")
            if name:
                dumps[(name.lower(), server.lower())] = os.path.join(eq_dir, fn)

    rows, matched = [], set()
    for c in chars:
        key = ((c.get("name") or "").lower(), (c.get("server") or "").lower())
        path = dumps.get(key)
        if path:
            matched.add(key)
        scan = scan_dump(path) if path else None
        try:
            mtime = int(os.path.getmtime(path)) if path else None
        except OSError:
            mtime = None
        attempt = attempted.get(key[0])
        tags = (c.get("group_tags") or "").lower()
        wants_hoard = HOARD_TAG in tags
        status, note = _status(scan, mtime, attempt, run_started, now, stale_h, wants_hoard)
        rows.append({
            "character_id": c.get("id"), "name": c.get("name"), "server": c.get("server"),
            "account_id": c.get("account_id"), "account_alias": c.get("account_alias"),
            "class_name": c.get("class_name"), "level": c.get("level"),
            "status": status, "note": note, "wants_hoard": wants_hoard,
            "dump_mtime": mtime,
            "dump_age_h": None if mtime is None else (now - mtime) // 3600,
            "attempted": bool(attempt),
            "attempt_result": (attempt or {}).get("result"),
            "total_rows": (scan or {}).get("total_rows", 0),
            "bank_slots": (scan or {}).get("bank_slots", 0),
            "shared_slots": (scan or {}).get("shared_slots", 0),
            "items": (scan or {}).get("items") or {b: 0 for b in BUCKETS},
        })

    rows.sort(key=lambda r: (STATUS_ORDER.get(r["status"], 99),
                             (r["account_alias"] or ""), (r["name"] or "")))

    # Dumps on disk with no roster row: either a toon you never imported or a
    # rename. Surfaced so the report can't quietly under-count the roster.
    orphans = sorted("%s (%s)" % (k[0], k[1]) for k in dumps if k not in matched)

    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    by_account = {}
    for r in rows:
        a = by_account.setdefault(r["account_alias"] or "—",
                                  {"account": r["account_alias"] or "—", "total": 0, "ok": 0})
        a["total"] += 1
        if r["status"] == "ok":
            a["ok"] += 1

    return {
        "rows": rows,
        "orphan_dumps": orphans,
        "buckets": BUCKETS,
        "summary": {
            "total": len(rows), "counts": counts,
            "needs_action": sum(n for s, n in counts.items() if s in ACTIONABLE),
            "by_account": sorted(by_account.values(), key=lambda a: a["account"]),
            "stale_h": stale_h,
        },
        "run": {"started": run.get("started"), "updated": run.get("updated"),
                "running": run.get("running"), "accounts": run.get("accounts") or [],
                "errors": run.get("errors") or []},
        "status_help": STATUS_HELP,
    }
