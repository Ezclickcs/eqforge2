# MacroQuest setup (optional)

**None of this is required to use EQ Forge.** Inventory, pricing, macros, INI writing, the Live
Monitor, Upgrades, My Characters — all of it works with no MacroQuest and nothing installed.

This page covers the two things MQ adds:

1. **The EQ Forge addon** — gets your roster, your expedition lockouts and your inventory out of the
   game automatically, so the app always has fresh data. *(Included.)*
2. **Gear delivery** — hands a planned gear set to an in-game tool so you're not reading a list off a
   second monitor while shuffling gear between toons. *(Partly third-party.)*

> ⚠️ **MacroQuest is third-party automation and is against EverQuest's Terms of Service.** Running it
> on a live or TLP server risks your account. That's your call to make — this page documents how the
> pieces fit together if you already run MQ. Ignore the whole file otherwise.

## Downloads referenced on this page

| | Where |
| --- | --- |
| MacroQuest — docs | <https://docs.macroquest.org/> |
| MacroQuest — source | <https://github.com/macroquest/macroquest> |
| MacroQuest — builds | <https://www.redguides.com/> (a working *Live* build in practice needs their paid tier; the public source targets emu) |
| DerpleDude's `parcel` — free | <https://github.com/DerpleDude/parcel> |
| DerpleDude's `parcel` — RedGuides | <https://www.redguides.com/community/resources/parcel-helper.2998/> (needs Level 2) |

The EQ Forge addon and MailGear are **in this package** — `extras\eqforge` and `extras\mailgear.lua`.
Nothing to download for those.

---

# Part 1 — the EQ Forge addon

## What it does

| | |
| --- | --- |
| **roster** | name, server, class, level, race, membership → My Characters |
| **lockouts** | your DynamicZone / expedition timers → Keys & Access |
| **inventory** | `/outputfile inventory`, and then **checks the file is complete** |
| **beacon** | tells the EQ Forge server where MacroQuest and EverQuest live |

The beacon is why there's nothing to configure: the addon knows both paths for certain, so it writes
them somewhere the server can always find. Run it once and **Setup** stops asking.

## Install

Copy the whole `extras\eqforge` folder into MacroQuest's `lua` folder, so you end up with:

```
<MacroQuest>\lua\eqforge\init.lua
```

The folder name matters — MQ runs a script by folder, loading `lua\<name>\init.lua`. Open
**⚙ Setup** in EQ Forge and it prints the exact path for your install and confirms when it's there.

Then, in game:

```
/lua run eqforge
```

Whole crew at once: `/dgae /lua run eqforge` (MQ2DanNet) or `/bcaa //lua run eqforge` (EQBC).

### Then do this — it's part of the install, not an extra

Add this line to MacroQuest's `config\ingame.cfg`:

```
/timed 50 /lua run eqforge
```

(`/timed` is in tenths of a second, so `50` = a 5-second pause to let the world finish loading.)

**Why it's required:** the addon **does not survive camping to character select** — measured, not
assumed. The camp export itself still works, because it fires about 30 seconds before you actually
leave. But the script is gone once you're at the character-select screen, so a hand-started addon
gives you **exactly one camp export and then silently stops.** That line restarts it on every
character that enters the world, on every client, and the problem disappears permanently.

Both the addon and EQ Forge's **⚙ Setup** page check for this line and tell you if it's missing —
`/eqf status` shows it as `autostart`.

## Automatic exports

Out of the box it exports **when you `/camp`** and **shortly after you log in**. Camping is the useful
one: you're done playing, your inventory is settled, and the app gets a complete picture of that toon
with no extra step.

Change what fires either in game or from EQ Forge → **⚙ Setup** (both write the same file):

```
/eqf on camp        /eqf off camp         export everything on /camp        (default ON)
/eqf on login       /eqf off login        roster + lockouts after login     (default ON)
/eqf on logindump   /eqf off logindump    ...and inventory too              (default off)
/eqf on zone        /eqf off zone         roster + lockouts on every zone   (default off)
/eqf on quiet       /eqf off quiet        stop the routine chatter          (default off)
/eqf every 60       /eqf every 0          full export on a timer, in minutes
```

Settings live in `<MQ config>\eqforge_addon.lua`. The addon reads it **at start**, so after changing
it from the web page, restart the addon in game: `/eqf stop` then `/lua run eqforge`.

`logindump` is off by default on purpose — logging in is exactly when inventory is still streaming
from the server, which is the one moment a dump can come back plausible and wrong.

## Commands

| Command | What it does |
| --- | --- |
| `/eqf` | Help. |
| `/eqf status` | What's enabled, when each export last ran, and the resolved paths. |
| `/eqf roster` | Export this character for My Characters. |
| `/eqf lockouts` | Export this character's expedition lockouts. |
| `/eqf dump` | `/outputfile inventory`, then verify the file is complete. |
| `/eqf all` | All three, plus refresh the paths beacon. |
| `/eqf camp` | All three, **then** `/camp`. |
| `/eqf beacon` | Rewrite the paths file EQ Forge reads. |
| `/eqf stop` | Stop the addon. |
| `/eqf bank` | Walk to a **nearby** banker, open the bank + Dragon's Hoard + Tradeskill Depot, then dump. Never automatic. |
| `/eqf crew <cmd>` | Run any of the above on **every box at once**. |

## Every box at once

There's no in-game window and no buttons — it's commands, like most things you'd box with. For the
whole crew:

```
/eqf crew all
```

That broadcasts through **MQ2DanNet** or **MQ2EQBC**, whichever you have loaded, so you don't have to
remember that one wants `/dgae /eqf all` and the other wants `/bcaa //eqf all` (that second slash is
easy to miss, and getting it wrong just quietly does nothing on the other clients). Anything works:
`/eqf crew dump`, `/eqf crew lockouts`, `/eqf crew on camp`.

Make it a hotkey — a normal EQ social, or a Button Master button — with that one line in it.

> **You mostly won't need it.** With the `ingame.cfg` line above, every box runs the addon and
> exports on its own `/camp` already. `/eqf crew all` is for "refresh everything *right now*" —
> before planning a gear move, say, without camping the crew.

You can also run one job without the resident addon at all:

```
/lua run eqforge roster
/lua run eqforge lockouts
/lua run eqforge dump
/lua run eqforge all
```

## Getting the data into the app

The addon writes files; the app reads them on demand. Nothing is watched or pushed.

| Data | Where to load it |
| --- | --- |
| Inventory | Macro Builder → **⟳ Reload dumps from EQ** |
| Roster | My Characters → **Import** → **📄 Load in-game exports** |
| Lockouts | My Characters → **Keys & Access** → **⚡ Load lockouts from MQ** |

**⚙ Setup** shows how many of each file it can see and how old the newest one is — that's the first
place to look if a tab is emptier than you expect.

## The Dragon's Hoard and the Tradeskill Depot

Your **bank and shared bank arrive in the login packet**, so a dump from anywhere captures them.
The **Dragon's Hoard** and the **Personal Tradeskill Depot** do not — they only export while their
window is **open**. That's an EverQuest limitation, not something the addon can work around: there
is nothing to read until the client has been sent the contents.

Because `/outputfile` rewrites the *whole* file, a routine dump taken away from a banker would
otherwise **delete** hoard and depot rows a previous dump had captured. So the addon refuses:

```
[eqforge] KEPT the older dump: it had 211 Hoard/Depot row(s) this one would have wiped.
```

Your existing dump is left intact and nothing is lost. To actually refresh those rows:

```
/eqf bank
```

That walks to a banker **already nearby**, opens the bank, clicks open both the **Dragon's Hoard**
and the **Tradeskill Depot**, waits for their contents to stream in, dumps, verifies, and closes the
bank window again. It reports which containers it actually opened rather than assuming the clicks
worked, and skips whichever you don't own. Whole crew at once: `/eqf crew bank`.

It is deliberately **never automatic** — not on `/camp`, not on login. Moving cancels a camp in
progress, and nothing should take the controls off a character you might be driving. You ask, it goes.

It refuses rather than doing anything surprising:

| Situation | What it does |
| --- | --- |
| In combat | Refuses to move you |
| No banker in the zone | Says so, dumps nothing |
| Banker more than 300 units away | Refuses — walk closer yourself, it will never trek |
| Banker out of range, zone has no navmesh | Refuses, tells you to walk into range |
| Character has no Dragon's Hoard | Dumps normally, reports "no hoard window" |

If you'd rather do it by hand, park at a banker, open the Hoard and Depot yourself and run
`/eqf dump` — same result.

> The Tradeskill Depot is a paid/membership perk. If you don't have one the button simply does
> nothing, and the bank run carries on normally.

If you genuinely emptied the hoard and want the new, smaller dump to stick, override it with
`/eqf dump force`.

Two more things worth knowing: hoard contents **stream in after the window opens**, so a dump taken
immediately can capture a partial hoard — the addon warns if the count drops by more than half, and
re-running fixes it. And if you want this fully unattended across dozens of characters, that's what
the harvest rotation is for ([docs/HARVEST-ROTATION.md](docs/HARVEST-ROTATION.md)) — it walks each
toon to a banker and opens the hoard itself.

## Lockouts, honestly

`/eqf lockouts` reads the DynamicZone timer list. That covers **instanced / expedition** content —
raid zone lockouts, per-event lockouts, loot lockouts and replay timers. Verified on a Velious-era
TLP: it returns real rows like `The Western Wastes / Harla Dar`, `Dragon Necropolis / Loot Lockout`,
and the shorter replay timers alongside them.

What it does **not** cover is open-world spawn timers — a contested dragon nobody instanced isn't a
lockout, so nothing in the game exposes it and nothing here can read it. Those stay a manual entry on
the Lockout Board.

An export with no lockouts still **writes the file** — that's how a lockout you've since cleared stops
showing up in the app.

---

# Part 2 — gear delivery

The **Gear Sets** tab (My Characters) builds a named loadout, assigns it to a toon, and works out who
is holding each piece and how it should travel — own shared bank, same-account swap, trade, or parcel.

**That's already useful with nothing installed.** It's a read-only worklist: log in the holder,
dequip, hand it over. Most people should stop there. The export below only saves you re-typing it.

## Export routes

**Send plan** writes these into your MacroQuest `config\` folder:

| File | Consumed by | Needs |
| --- | --- | --- |
| `mailgearplan.lua` | **MailGear** (`extras/mailgear.lua`, included) | MQ only — **dequip / bank / equip** |
| `parcel_gearplan.lua` | DerpleDude's **parcel** Lua | MQ + the parcel script — **delivery** |
| `trixbox_gearplan.lua` | **TrixBox** | MQ + TrixBox (private package — not included) |

`mailgearplan.lua` and `trixbox_gearplan.lua` are the same bytes under two names — the old name is
still written so an existing TrixBox setup keeps working. All are written every time; whichever tools
you have pick up their own file and ignore the rest, so having only one installed is fine.

The two included routes are **complementary** and together cover the whole job:

```
mailgear.lua           parcel                  mailgear.lua
  dequip / getbank    ->     send it over    ->      equip
  (on the holder)          (to the target)        (on the target)
```

### Route A — parcel (recommended, standalone)

You get a **"Gear Plan: \<set\> → \<toon\> (n)"** entry in the parcel window's source dropdown,
containing exactly the items in your plan. You review the list and hit Send yourself — the app never
drives the parcel window.

**You need:**

1. **MacroQuest** with Lua support (`MQ2Lua` — built into any modern MQ build).
2. **DerpleDude's `parcel` script** at `<MacroQuest>\lua\parcel\`. Third-party; **not bundled** with
   EQ Forge. Get it from the MacroQuest Lua community and install it yourself.
3. **`config\parcel_sources.lua`** containing the chain-load block.
   - Don't have one? Copy `extras\parcel_sources.lua` → `<MacroQuest>\config\parcel_sources.lua`.
   - Already have one? **Don't overwrite it.** Open `extras\parcel_sources.lua`, copy just the
     **CHAIN-LOAD BLOCK** at the bottom, and paste it into your file above its `return sources`.

The chain-load is `pcall`-guarded — if the generated file isn't there yet it silently skips, so you
can install it before ever running an export.

**In game:** `/lua run parcel` → pick the **Gear Plan: …** source → eyeball the list → Send.

⚙ Setup tells you whether it can see the parcel script and `parcel_sources.lua`.

### Route B — MailGear (included)

`extras\mailgear.lua` does the parts the parcel tool can't: taking gear **off** a toon, pulling it
**out of the bank**, and putting it **on** the receiving toon. It reads the same exported plan and
needs **nothing but MacroQuest** — no TrixBox, no rgmercs.

**Install** — same folder rule as the addon: the file has to be **renamed to `init.lua` inside a
folder named `mailgear`**, not dropped in loose:

```
<MacroQuest>\lua\mailgear\init.lua      <- copy extras\mailgear.lua here AND rename it to init.lua
```

> ❗ If `/lua run mailgear` says **"no lua script matching mailgear"**, the file isn't at
> `lua\mailgear\init.lua`. Either the folder name is wrong, or you left the file named `mailgear.lua`.

**Run:** `/lua run mailgear`   ·   **Stop:** `/lua stop mailgear`

**It starts in DRY-RUN.** Nothing moves until you type `/mailgear live on`. Run it dry first and read
the `WOULD …` lines.

| Command | What it does |
| --- | --- |
| `/mailgear` | Help. |
| `/mailgear plans` | List the exported plans; marks which pieces are *from you* and *to you*. |
| `/mailgear useplan <n>` | Make plan #n active. |
| `/mailgear status` | Loaded plans, live/dry-run, queue progress. |
| `/mailgear dequip` | **On the holder** — moves that toon's planned worn pieces into bags. |
| `/mailgear getbank` | **At a banker, bank window open** — pulls planned pieces out of bank / shared bank. |
| `/mailgear hoard` | Lists pieces needing a **manual** pull and opens the checklist window. |
| `/mailgear equip` | **On the receiving toon** — equips the pieces meant for it. |
| `/mailgear live on\|off` | Arm / disarm real movement. |
| `/mailgear stop` | **Emergency stop** — clears any running queue immediately. |
| `/mailgear resume` | Clear the stop. |
| `/mailgear reload` | Re-read the plan file after a fresh export. |

**Typical run:**

```
(on each holder)   /mailgear dequip        ... then /mailgear getbank at a banker
(deliver)          /lua run parcel         ... pick the "Gear Plan: …" source, Send
(on the target)    /mailgear equip
```

**The Hoard cannot be automated.** MacroQuest has no addressable slot names for it, so those pieces
can only be retrieved by hand — open the Hoard, search the name, Retrieve. `/mailgear hoard` gives
you the list in a window (rather than chat, where it scrolls away), and `/mailgear dequip` pops that
window automatically if any planned piece is stuck there. Persona-closet gear is the same story:
switch to that persona, then dequip.

**Safety:** dry-run by default; `/mailgear stop` clears the queue between items; every pickup verifies
the cursor holds *exactly* the intended item before doing anything with it; a piece that fails is
skipped and the run continues; the only thing that halts a batch is genuinely having zero empty bag
slots.

### Route C — TrixBox

Only relevant if you have TrixBox (not part of this package):

```
/trix plans          -- list the exported plans
/trix useplan <n>    -- choose one
/trix sendgear       -- run on the toon HOLDING the gear
/trix getgear        -- run on the TARGET toon
/trix gearlive on    -- leave OFF to dry-run first
```

It stays in dry-run until you explicitly `/trix gearlive on`.

---

# Where files get written

EQ Forge finds both folders on its own — saved override, then `EQFORGE_MQ_CONFIG` /
`EQFORGE_EQ_DIR`, then the addon's beacon, then a scan of the usual install locations.

**Open ⚙ Setup to see what it resolved to and where each path came from.** You can correct either one
there; it takes effect immediately, no restart. The server also prints both paths on startup and flags
any it had to guess.

> **Heads up:** the server creates the MQ config directory if it doesn't exist. If the path is wrong
> *and* you press Send plan, you'll get an empty junk folder. Harmless, but Setup is the place to fix
> it first.

If the app isn't being served by `serve.py`, plan exports fall back to a normal browser download.

---

# Quick reference

| Want | Need |
| --- | --- |
| Everything except the MQ features | Nothing — Python 3 + Chrome/Edge |
| Automatic roster / lockout / inventory exports | MQ + `extras\eqforge` (included) |
| Gear Sets as a read-only worklist | Nothing |
| Dequip / bank-pull / equip + hoard list | MQ + `extras\mailgear.lua` (included) |
| Plan → parcel window for delivery | MQ + `lua\parcel\` (third-party) + `extras\parcel_sources.lua` |
| Plan → `/trix` commands | MQ + TrixBox (not included) |

# Status

`eqforge` is **v1.0.0**. Its dump verification and its export formats are the ones that have been
running against a real 40-character roster; the command layer and the automatic triggers are new.

`mailgear.lua` is **v1.0.0**, extracted from TrixBox. Its item-moving primitives are the ones TrixBox
has been running against a live crew — including the fixes for lifting worn items, the two-slot
ear/wrist/finger swap, and the zero-based bank index. The standalone command layer, plan filtering,
and queue around them are new.

Run both in dry-run / read-only first and confirm the output matches what you expect.
