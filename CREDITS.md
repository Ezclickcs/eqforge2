# Credits & Licensing

**EQ Forge 2.0** is a modified fork of **EQ Auction Forge** by **wangel**
(<https://github.com/wangel/EQ_Auction_Forge>), used under the GNU Affero General
Public License v3.0 or later (AGPL-3.0-or-later). See [LICENSE](LICENSE) for the
full text.

Fork maintained by **Trixster (VibeMind)**. A personal, non-commercial project —
see the AGPL note at the bottom, and the TLP-Auctions licence terms below it.

## What this fork changes (vs. upstream EQ Auction Forge web app v1.5.0)

- **Multi-toon inventory aggregation** — load a `/outputfile inventory` dump from
  every character you want to sell across at once. They merge into one list
  (counts summed per item, per-toon breakdown kept). Account-shared bank items
  are counted once, not multiplied by the number of dumps loaded. Upstream loads
  a single character at a time.
- **Signed price-adjust slider** — one control for both undercut (post under
  market) and markup (post over it), replacing the undercut-only box.
- **Bundled local dev server** (`serve.py`) that also proxies the TLP pricing API,
  so price checks work when running locally.

The macro-building core is unchanged from upstream: DC2 clickable item links,
255-char / 5-line / 12-button packing, the idempotent `[Socials]` INI merge, TLP
median bulk pricing, krono folding, and the CHA-based vendor-trash band are all
wangel's work.

## Data sources (unchanged from upstream)

- Item data: [items.sodeq.org](https://items.sodeq.org) — shipped as `items.txt.gz`,
  or downloaded from <https://items.sodeq.org/downloads/items.txt.gz> on first run
  if it isn't bundled
- Pricing data: [TLP-Auctions](https://www.tlp-auctions.com) — **PolyForm
  Noncommercial**: personal and community use only, never monetize it

## AGPL obligation

Because upstream is AGPL-3.0-or-later, this fork — and any hosted/network version
of it — must keep its complete corresponding source available under the same
license. The full source is this project folder. If this is ever deployed as a
public web service, publish the source and keep the attribution above.
