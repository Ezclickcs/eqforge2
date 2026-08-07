# Harvest rotation

Unattended pass that logs each queued toon in turn, dumps its inventory (visiting a
banker first if it owns a Dragon's Hoard), and camps — so the roster's gear data can be
refreshed without hand-driving 40 logins.

> **This is the advanced option, and it is not what most people want.** For keeping one
> crew's data fresh, install the **EQ Forge addon** instead ([MQ-SETUP.md](../MQ-SETUP.md)) and
> let it export on `/camp` — no queue, no rotation, nothing to arm. The rotation below
> exists for the case the addon can't cover: refreshing dozens of toons you are *not*
> going to play, unattended. The two coexist; installing one does not affect the other.

## How to run one

1. **EQ Forge → My Characters → Harvest.** Tick toons (or *Select problems* /
   *Select hoard toons*) → **⚡ Arm harvest**.
   That writes `<MQ config>/harvest_<LoginName>.queue`, one file per account.
2. **Camp each of those accounts' clients to character select.** The rotation takes over:
   log in → dump → camp → next.
3. Watch progress on the Harvest tab (hit Refresh) — the banner shows each account's
   current toon, remaining count, and failures.
4. It disarms itself when the queue drains. **⛔ Disarm** (or deleting the `.queue`
   files) is the hard stop at any point.

## Why the login is unavoidable

There is no way to read a character's inventory without entering the world on it.
Character select carries name/level/class/zone plus visible armor *material* IDs — MQ
AutoLogin's `login.db` harvest is the ceiling of what that screen knows. Frostreaver is
a **Daybreak** server: no server DB, no character API. The only lever is making the
login cheap.

## Architecture — cfg hooks, not a long-lived script

| Hook | Script | Job |
|---|---|---|
| `config/ingame.cfg` (fires for **every** character entering the world) | `harvest_step.lua` | settle → optional banker + Hoard → `/outputfile inventory` → **verify the dump** → pop the queue → `/camp` |
| `config/CharSelect.cfg` (fires at character select) | `harvest_next.lua` | read the queue, take the top name, `/switchchar <name>`; delete the file + log `finish` when empty |

Neither script is long-lived, neither participates in stopping itself, neither has to
survive a gamestate flip — this satisfies the 2026-07-16 and 2026-07-17 TrixBox rules by
construction. `ingame.cfg` means **no per-character cfg files needed**, so TrixBox's
auto-generated `frostreaver_<Name>.cfg` files are untouched.

`/switchchar <name>` and `/switchserver <server> <char>` are documented AutoLogin
commands. AutoLogin has **no built-in cycling** — the queue is ours.

Sources live in `extras/`; deployed copies in `<mq>/lua/`.

## Arming is the safety model

Both scripts fire on **every** login for **every** character. They are safe because:

- No `harvest_<LoginName>.queue` → instant silent exit.
- `harvest_step` also checks that **this toon is the top of the queue**; a hand-logged
  toon is left completely alone.
- The queue file is re-checked between steps, so deleting it aborts mid-run.

> **Known and accepted:** if a queue is armed and you manually log in the toon that is
> on top of it, it *will* dump and camp that toon. That is the same code path the
> rotation uses; there is no way to distinguish the two logins. Disarm first if you want
> to play one of the queued toons.

## Files

`<MQ config>/harvest_<account>.queue` — the work list, popped by the Lua.
`<MQ config>/harvest_<account>.log` — append-only event log.

One file per account, never shared: six clients broadcast-writing one file silently
dropped rows before (2026-07-25).

```
armed|<epoch>|<total>
current|<name>|<epoch>
done|<name>|<epoch>|<result>|<note>
error|<name>|<epoch>|<message>
finish|<epoch>
```

Pipe-delimited rather than JSON **because the writer is MQ Lua**: appending one line
with `io.open(path,'a')` cannot half-write a structure the way rewriting a JSON blob
can, and it needs no Lua JSON library. Read by `mychars/harvest.py::read_runs`.

### Why the log is required, not optional

`/outputfile` rewrites the dump even when contents are identical, so an untouched mtime
after an attempt is the *only* evidence of a silent failure. Without the log, "the run
reached this toon and wrote nothing" (`failed`) is indistinguishable from "this toon
wasn't in the queue" (`stale`). Same file on disk, completely different fix.

## The dump is verified, not assumed

`harvest_step` reads its own output back and requires **24 top-level `Bank<n>` rows and
8 `SharedBank<n>` rows** (verified across 36 real dumps; "Empty" rows count). Short means
the dump fired before the server finished sending inventory — the one failure mode that
produces a plausible-looking but wrong file. It re-dumps up to 3 times before recording
an error. Tested against real, truncated, and missing files.

## The hoard leg

Bank and SharedBank arrive in the login packet and need **no banker**. Only the Dragon's
Hoard (and the Tradeskill Depot) need their window open, so the banker trip runs **only
for toons flagged `hoard`** — set automatically by *Select hoard toons* / the
`POST /roster/harvest/taghoard` endpoint, which tags any toon whose dump already proves
it owns one — tag each hoard owner with the `hoard` group tag.

**Same-zone only.** Velious era has no Plane of Knowledge, so a banker means an old-world
city; there is deliberately **no cross-zone travel**. No banker in the zone → it logs a
warning and dumps without the hoard, and the report flags `hoard-missed`. Park hoard
toons in a bank city.

## Gotchas proven in the field (first full run, 2026-08-04 — 34 toons, 0 errors)

**A client already sitting at character select when you arm will never start.**
`charselect.cfg` is a *transition* hook — it fires on arrival, not continuously. Kick it
by hand on that client: `/lua run harvest_next`. (Cost us one account's whole queue on
the first full run.)

**The Dragon's Hoard streams in AFTER the window opens.** At a 500 ms wait, a test toon's
hoard captured 26 of 81 items — a partial dump that looked structurally perfect (24 bank
/ 8 shared all present). `HOARD_POPULATE` is now 1500 ms; the next run brought all 81
back. If a hoard count drops sharply between runs, suspect this before believing the
items moved.

**"The Hoard window opened" is NOT evidence anything was captured.** That window opens on
every toon, hoard or not, so 26 of the 34 toons logged a hoard success while their dumps
contained zero hoard rows. `dumpIsComplete()` now counts hoard ITEMS out of the file and
reports the real number, and a toon flagged `hoard` that captures 0 is logged as an
**error** — because that case means a good dump was just overwritten with a hoardless one.

Hoard sizes seen in testing ranged from 18 to 125 items.

## Known interactions

- **rgmercs** is paused at the start of the step and again before camping. On toons whose
  `frostreaver_<Name>.cfg` launches rgmercs on a `/timed` delay, rgmercs may load *after*
  the first pause — harmless for town-parked bank toons, but don't queue a toon parked
  somewhere hostile.
- **Camp is 30s and restarts if the toon is hit.** A toon in a dangerous spot will stall
  its account's queue; it shows as `in-progress` on the report. `BUDGET` (180s) caps the
  work before the camp, not the camp itself.
- A toon **in combat on login** is skipped with an error and popped, so the rotation
  moves on rather than looping.
