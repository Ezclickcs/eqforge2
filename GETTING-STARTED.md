# EQ Forge 2.0 — Getting Started

A browser tool for running an EverQuest box crew: sell loot across **all your toons at once**, and
keep track of who they are, what they're wearing, and what they're locked out of.

Multi-toon fork of [EQ Auction Forge](https://github.com/wangel/EQ_Auction_Forge) by wangel.

**Pricing needs the Frostreaver TLP** — it's the only server TLP-Auctions has data for. Everything
else (inventory, gear, roster, lockouts, macros) works on any server.

---

## 0. What you need to download

Only the first one is required.

| | Where | Notes |
| --- | --- | --- |
| **Python 3** *(required)* | <https://www.python.org/downloads/> | **Tick “Add python.exe to PATH”** in the installer. That checkbox is the #1 reason this won't start. |
| **Chrome or Edge** *(recommended)* | <https://www.google.com/chrome/> — Edge ships with Windows | Needed for one-click "Write to INI" and the Live Monitor. Any browser works otherwise. |
| **Item database** *(automatic)* | <https://items.sodeq.org/downloads/items.txt.gz> | ~8 MB. The server downloads it on first run if it isn't already next to `serve.py`. Listed here only so you know what it's fetching. |
| **MacroQuest** *(optional)* | docs <https://docs.macroquest.org/> · source <https://github.com/macroquest/macroquest> · builds <https://www.redguides.com/> | Only for the automatic exports and gear delivery. **Against EverQuest's ToS — your account, your call.** A working *Live* build in practice comes from RedGuides' paid tier; the public source builds for emu. |
| **DerpleDude's `parcel`** *(optional)* | free: <https://github.com/DerpleDude/parcel> · RedGuides: <https://www.redguides.com/community/resources/parcel-helper.2998/> | Only if you want gear plans delivered in game. The GitHub copy is public and free; the RedGuides page needs Level 2. Install to `<MacroQuest>\lua\parcel\`. |

Not a download, but this is where the prices come from: [TLP-Auctions](https://tlp-auctions.com).
Item data is from [items.sodeq.org](https://items.sodeq.org). This app is a fork of
[EQ Auction Forge](https://github.com/wangel/EQ_Auction_Forge) — see [CREDITS.md](CREDITS.md).

---

## 1. Install & run (Windows)

1. Install **Python 3** — check with `python --version`. Nothing else (stdlib only, no pip, no npm,
   no build step).
2. Double-click **`run.bat`**. It starts the server and opens the app for you at
   <http://localhost:8000/app/>. (Or run `python serve.py` and open that yourself.)
3. Use **Chrome or Edge**. They support the File System Access API, which is what lets the app write
   into your INI and tail your EQ log. Other browsers work, you just copy/paste macros by hand.

First load caches the item database in IndexedDB — a few seconds once, instant after that. If
`items.txt.gz` isn't next to `serve.py`, the server downloads it before starting; you'll see the
progress in the console window.

**If `run.bat` says `'python' is not recognized`,** Python isn't on your PATH. Reinstall it from
<https://www.python.org/downloads/> and tick **"Add python.exe to PATH"**.

**If Windows warns before running `run.bat`** ("Windows protected your PC", or an
*Open File – Security Warning*), that's Mark-of-the-Web: anything downloaded from a browser or
Discord gets flagged, and the flag survives unzipping. It isn't a virus warning. Either click
**More info → Run anyway**, or clear it once up front: right-click the **zip** →
**Properties** → tick **Unblock** → OK, *then* extract.

**Why the server?** The app is static HTML/JS and could open from `file://` — except the TLP-Auctions
API only allows browser calls from wangel's site. `serve.py` proxies `/api/*` so pricing works from
localhost. Automatic, nothing to configure.

---

## 2. Check ⚙ Setup

Open the **⚙ Setup** tab once. It shows the two folders everything else depends on:

- your **EverQuest** folder (where `/outputfile inventory` writes dumps)
- your **MacroQuest `config`** folder (only if you use MQ)

Both are detected automatically on a normal install. If either shows a warning, type the right path
and hit Save — it takes effect immediately, no restart.

**Why bother:** a wrong folder here doesn't produce an error. It produces an *empty result*, because
scanning an empty folder succeeds. Setup is where "why is this tab blank?" gets answered.

---

## 3. Get your inventory out of EQ

In game, on each toon:

```
/outputfile inventory
```

That writes a `<Toon>_<server>-Inventory.txt` into your EQ folder. Do it for every toon — mules and
bank toons included. The app merges them all into one list.

Then either **Choose dumps…** (pick files by hand) or **⟳ Reload dumps from EQ** (one click, straight
out of your EQ folder).

Running MacroQuest? The included addon does this for you, and checks the file is actually complete —
see [MQ-SETUP.md](MQ-SETUP.md). Set it up once and camping out on a toon refreshes everything the app
knows about it.

---

## 4. The selling workflow

1. **Load** — drop in every inventory dump. Toon chips appear at the top; click one to add/remove it
   from the view. Shared-bank items are counted once, not per-toon.
2. **Filter / search** — by Toon, Type (gear / spell / tradeskill / food / bag), gear Slot,
   Class/Race, a **stat filter** (AC, HP, haste, resists…), and Location toggles (Bags / Worn / Bank /
   Shared / KeyRing / **Depot** / Hoard). "Depot" is the Personal Tradeskill Depot — mats only, but it
   stacks deep and most of it sells. Junk you never want to see again → select it and
   **Exclude forever**.
3. **Add** rows to the sell list (right pane).
4. **PC All** — price-checks everything against TLP-Auctions in one request. Set the **price-adjust
   slider** first (undercut ⟷ markup); prices round to clean "nice numbers" and auto-convert to krono
   when they're high enough.
5. **⚠ Review** — filters to the items the app thinks are mispriced. Gold "sells higher" means recent
   asks or live buyer bids are above your post (the API median lags badly on spells). Blue means
   you're probably overpriced. "— no sales" means no market data.
6. **Generate** — packs the list into `[Socials]` macro buttons, split into **Spell / Gear / Misc**,
   all clickable item links, 255-char lines. Vendor-trash gets auto-moved to the Vendor list.
7. **Write to INI** — pick the one toon you actually post from. **Close EQ first** (it rewrites the
   INI on logout and will clobber your changes otherwise). The previous INI is stashed in
   localStorage as a backup.
8. **Sold** — mark items as they sell; tracks price, quantity, time, with today / all-time totals.

---

## 5. The tabs

### Macro Builder

| Tab | What it does |
| --- | --- |
| **Sell list** | The main flow above. |
| **Vendor list** | Junk you're vendoring, with a running "these are worth ~X plat" tally. |
| **Sold** | Sales log — today and all-time totals, krono-aware. |
| **🔍 Upgrades** | The *market* half of gear: compares a toon's equipped gear against BIS / quest-armor data and shows what's worth chasing, by slot, with where to farm it. |

**Live Monitor** (top of the page) tails your EQ log and watches EC-tunnel chatter for buyers who want
something you're holding, then one-click copies a `/tell Buyer I have <item> for <your price>`.

### My Characters

The roster half. Start on **Import** — you can't do much until the app knows your characters.

| Tab | What it does |
| --- | --- |
| **Import** | Get your roster in: paste CSV/JSON, **⚡ Load from MQ AutoLogin**, or **📄 Load in-game exports** (from the addon). Preview before anything is written. |
| **Roster** | Every character by account, with a Current Login Set — one pick per account, which is what you're actually going to log in. |
| **Comp Builder** | Build six-boxes with the hard rule enforced: one character per EQ account. Warns on role gaps. |
| **Gear** | Worn stat sheet per toon, and the best item you own for any stat. |
| **Gear Sets** | Snapshot a loadout, assign it to a toon, and get a routed move plan — own shared bank, same-account swap, trade, or parcel. See [MQ-SETUP.md](MQ-SETUP.md) to hand it to an in-game tool. |
| **Harvest** | Dump coverage: who has a dump, how old, and who the last pass missed. |
| **Keys & Access** | Keys, flags, and the **Lockout Board** — per raid × account, who's free right now. **⚡ Load lockouts from MQ** pulls expedition timers in from the addon. |
| **Optimizer** | What your roster is missing and what to level next. |

---

## 6. Reading the price flags

Hover any price cell for the full read.

| Flag | Meaning |
| --- | --- |
| 🔥 **flooded** | Lots of recent asks **and** you're priced above the pack. Undercut to the recent median and it clears. |
| 📈 **in demand** | Frequent WTB spam relative to asks — seller's market. Hold or price up; don't undercut. |
| 🟢 **thin** | Very few recent asks. Scarce; you can hold for your number. |
| gold **sells higher** | Recent asks/bids are above what you posted. Reprice up. |
| blue | Probably overpriced. |
| — **no sales** | No market data for this item. |

The **"Recent pricing"** checkbox (on by default) is the important one: when several recent asks
diverge meaningfully from the full-history median, the app prices off the *recent* asks instead. The
API's median covers all of history and lags hard on volatile items — spells especially.

---

## 7. Notes & gotchas

- **Close EQ before writing the INI.** EQ overwrites the INI on logout.
- **Chrome/Edge** for one-click INI writing and log monitoring; other browsers → "Copy macros".
- Your inventory, INI, roster and logs **never leave your machine**. Only item-id price lookups go out
  to TLP-Auctions.
- **A fresh install starts with an empty roster.** That's deliberate — import your own. If you want to
  see the tabs populated first, Import → load the sample roster.
- **Shared bank across multiple accounts** can over-merge — the dumps don't say which account a shared
  slot belongs to. Per-toon carried/bank items are always correct.
- **MacroQuest is against EverQuest's ToS** and every MQ feature here is opt-in. Nothing in the app
  requires it.
- Pricing data and market flags are **advisory**. They're a read on the market, not a guarantee.
- **Spell and effect names in tooltips are optional extra data** you generate from your own EQ
  client — it isn't distributed, because the text is Daybreak's. Without it an item's proc shows as
  "Combat Proc" instead of by name. To turn it on:
  ```
  python tools/build_spells.py
  ```
  It reads `spells_us.txt` and `dbstr_us.txt` out of your EverQuest folder and writes
  `app/spell-effects.json.gz`. Re-run it after a patch that changes spell data.
  Until you do, the browser console shows two harmless 404s for `spell-effects.json.gz`
  and `focus-families.json` — that's the app checking for them and carrying on without.

---

## 8. Files

```
eqforge2/
├── run.bat / serve.py     ← local server + TLP API proxy
├── items.txt.gz           ← bundled item DB (items.sodeq.org)
├── app/
│   ├── index.html         ← Macro Builder
│   ├── mychars.html       ← My Characters
│   ├── setup.html         ← ⚙ Setup
│   ├── forge.js           ← parsing, pricing, macros, INI, UI
│   ├── watchlist.js       ← Live Monitor match engine
│   └── *.json             ← BIS sets, quest armor, upgrade sources
├── mychars/               ← roster backend (SQLite, stdlib only)
├── extras/
│   ├── eqforge/           ← the MacroQuest addon  → MQ-SETUP.md
│   ├── mailgear.lua       ← dequip / bank-pull / equip
│   └── parcel_sources.lua ← makes a gear plan show up in DerpleDude's parcel tool
├── tools/                 ← Python scrapers that regenerate the JSON data files
└── docs/API.md            ← TLP-Auctions API reference
```

No framework, no build step, no dependencies. Edit the files and refresh the page.

---

## 9. License

**AGPL-3.0-or-later** (inherited from the EQAF fork) — see [LICENSE](LICENSE) and
[CREDITS.md](CREDITS.md). If you deploy it, keep the source available.

The TLP-Auctions API is **PolyForm Noncommercial** — personal and community use only. Don't monetize
it.
