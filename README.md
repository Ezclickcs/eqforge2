# EQ Forge 2.0

A browser tool for running an EverQuest box crew. Sell your loot across **all your toons at once** —
load every inventory dump, pull live TLP-Auctions prices, spot what's mispriced, build a WTS macro of
clickable item links and write it straight into a character's INI. Then the other half: a roster that
knows your accounts, your six-boxes, who's wearing what, and what's locked out.

Everything runs locally. Your inventory, INI, roster and logs never leave your machine — only item-id
price lookups go to TLP-Auctions.

A multi-toon fork of [EQ Auction Forge](https://github.com/wangel/EQ_Auction_Forge) by wangel —
see [CREDITS.md](CREDITS.md).

> **Start here:** [GETTING-STARTED.md](GETTING-STARTED.md) — what to download, how to run it, the full
> workflow, every tab, and how to read the price flags.
> **Running MacroQuest?** [MQ-SETUP.md](MQ-SETUP.md) — the optional in-game addon and gear delivery.

**Pricing needs Frostreaver**, the only server TLP-Auctions has data for. Everything else — inventory,
gear, roster, lockouts, macros — works on any server.

## What it does

**Selling**
- **Multi-toon inventory** — merge a `/outputfile inventory` dump from every toon into one list.
  Shared-bank items counted once, not per dump.
- **Filters** — Toon, Type, gear Slot, Class/Race, a stat filter (AC/HP/haste/resists…), and Location
  toggles (Bags / Worn / Bank / Shared / KeyRing / Depot / Hoard), plus live search.
  **Exclude forever** blacklists junk and storage bags for good.
- **Pricing** — TLP median check, signed price-adjust slider (undercut ⟷ markup, live), clean
  rounding, automatic krono for high-value items.
- **Market flags** — 🔥 *flooded* (price-aware: clears when you undercut to the pack) · 📈 *in demand*
  (WTB spam = hold) · 🟢 *thin* · gold *sells higher* (recent asks above your post — the full-history
  median lags badly on spells) · blue *overpriced*. **⚠ Review** filters to just the flagged ones.
- **Macros** — packs the list into `[Socials]` buttons of clickable item links, split Spell / Gear /
  Misc, and writes them into the INI of the one toon you post from.
- **Vendor list** and **Sold log** — a running "vendor these for ~X plat" tally, and sales tracking
  with today / all-time totals.
- **Live Monitor** — tails your EQ log for buyers who want something you're holding, one click to copy
  `/tell Buyer I have <item> for <your price>`.
- **🔍 Upgrades** — your equipped gear vs BIS and quest-armor data, slot by slot, with where to farm it.

**Roster** (My Characters)
- **Accounts + Comp Builder** — six-boxes with one-character-per-account enforced, and role-gap warnings.
- **Gear** and **Gear Sets** — worn stat sheets, plus snapshot a loadout, assign it to a toon, and get a
  move plan routed by real cost: own shared bank → same-account swap → trade → parcel.
- **Harvest** — dump coverage: who has one, how old, and who the last pass missed.
- **Lockout Board** — per raid × account, which accounts are free to field a run right now.

**Setup and MacroQuest**
- **⚙ Setup** — finds your EverQuest and MacroQuest folders automatically and shows what's arriving
  from the game, so an empty tab tells you *why* it's empty.
- **In-game addon** (optional, `extras/eqforge`) — `/camp` and your roster, lockouts and inventory
  export themselves. Nothing else in the app needs MacroQuest.

## Version history

**1.8.0** — ⚙ Setup tab (auto-detects both folders; no more environment variables, no more features
silently returning nothing because a path was wrong) · **MacroQuest addon** that exports roster,
expedition lockouts and inventory automatically on `/camp` · fresh installs start with an empty roster
instead of a sample one · item database downloads itself when it isn't bundled.

**1.6.0** — macros split into Spell / Gear / Misc buttons, all clickable links · price-aware
*flooded* / *in demand* / *thin* market flags · "no sales" auto-fix by exact name · per-item pricing
slider and ❄ freeze · item clickouts to TLP-Auctions and AG/ZAM · gear stat filter · krono-aware
grand total.

## License

GNU **AGPL-3.0-or-later**, inherited from the EQAF fork — see [LICENSE](LICENSE) and
[CREDITS.md](CREDITS.md). Keep the source available if you deploy it.

The TLP-Auctions API is **PolyForm Noncommercial**: personal and community use only, never monetize it.
