// SPDX-License-Identifier: AGPL-3.0-or-later
// EQ Auction Forge — Copyright (C) 2026 wangel. GNU AGPLv3 or later (see LICENSE).
// Hosted/modified versions must make their complete source available.
//
// MODIFIED 2026 — "EQ Forge 2.0" fork by Trixster (VibeMind). Changes: multi-toon
// inventory aggregation (load many /outputfile dumps, merge into one sell list,
// shared-bank dedup), a signed price-adjust slider (undercut OR markup), and a
// bundled local dev-server proxy for the TLP pricing API. See CREDITS.md. This
// remains AGPL-3.0-or-later; complete source ships alongside this file.
"use strict";
/*
 * EQ Auction Forge — browser app (originally wangel.github.io/EQ_Auction_Forge/app/).
 *
 * Pure client-side: files are read in the browser, processed in JS, and the INI
 * is written back locally. Nothing is uploaded. A faithful port of the desktop
 * app's core (EQ-Auction_Forge.py) — same DC2 link format, items.txt columns,
 * inventory-dump parsing, 255-char / 5-line / 12-button packing, idempotent
 * [Socials] merge, bulk pricing + undercut + recent-asks divergence, live krono
 * folding, the CHA vendor-trash band, and typed link macros (Spell/Gear/Misc).
 *
 * Pricing calls tlp-auctions directly (CORS is enabled for the Pages origin);
 * localhost dev uses a same-origin proxy. Desktop-only: log monitor, watchlist, the
 * Settings dialog, EQ auto-install detection, the update check.
 */

// ----- constants (mirror the Python module globals) -----
const DC2 = "\x12";              // EQ item-link delimiter (hex 0x12)
const BUTTONS_PER_PAGE = 12;
const MAX_PAGE = 10;
const BULK_PRICE_LIMIT = 10;     // max item ids per /prices/bulk request
const DEFAULT_KRONO_RATE = 4000; // fallback fold rate if the API reports none
const RECENT_SALES_LIMIT = 8;    // recent postings pulled for the Recent Postings modal
const REVIEW_FETCH = 20;         // recent postings per item during a price check (sales/bulk perItemLimit max)
const BULK_SALES_LIMIT = 200;    // max item ids per /sales/bulk request
// Recent-market pricing (the #useRecent toggle): the bulk medianPlatPrice is a
// full-history median that LAGS on volatile items (spells especially). When enough
// recent asks disagree with it hard, price from the recent market instead.
const ADOPT_MIN_N = 3;           // recent priced asks needed before trusting the recent median as the base
const ADOPT_PCT = 15;            // adopt when the recent ask median diverges >= this % from the history median
const DIVERGE_PCT = 14.7;        // recent asks running this % UNDER your post → "recent asks lower" (maybe overpriced)
const UNDER_PCT = 15;            // recent WTS asks running this % ABOVE your post → "sells higher" (underpriced)
const BID_MULT = 1.3;            // a fresh WTB bid at ≥ this × your post → active demand above your price (underpriced)
const BID_WINDOW_H = 36;         // how recent a WTB bid must be to count as live demand
// Supply-pressure (listing velocity) flags: from the recent WTS asks already
// fetched for the review flags, how fast are competitors listing this item?
const SAT_HI = 12;               // ≥ this many WTS asks/day → "saturated" (flooded market, undercut to move)
const SAT_LO = 5;                // ≤ this many WTS asks/day → "thin" (scarce, hold your price)
const SAT_MIN_N = 6;             // need at least this many recent asks before calling a market "saturated"
const SAT_MIN_SPAN_D = 0.5;      // floor the age-span (days) so a burst of asks in minutes doesn't divide-by-~0
// WTB demand: buyers rarely post WTB in EQ, so FREQUENT WTB (rivaling the WTS count)
// is a real seller's-market signal → hold or price up (krono, gems, WTB-spammed rares).
const DEMAND_MIN_BIDS = 5;       // need at least this many recent WTB bids to trust the read
const DEMAND_RATIO = 0.35;       // and WTB/WTS at or above this → "in demand" (noise items sit <0.1)
// NPC vendor buyback estimate (CHA-based). Port of vendor_multiplier/value_pp.
const VENDOR_SLOPE = 0.004, VENDOR_INTERCEPT = 0.584, VENDOR_CAP = 1 / 1.05;
// Apex host (valid cert). "/api" routes to a same-origin proxy for local dev.
const API_HOST = "https://tlp-auctions.com/api";
const SERVER = "Frostreaver";    // only TLP with tlp-auctions data; no server picker needed
// Bump on any user-visible change — the header badge is how you confirm the browser
// actually loaded the new build instead of a cached one (see serve.py NO_CACHE_EXT).
const APP_VERSION = "1.8.0";
// Identify our traffic to the API owner: every request carries this so they can
// see/measure our usage and reach out if needed.
const CLIENT_TAG = `EQ-Auction-Forge/${APP_VERSION}`;

// Anonymous visit beacon -> our own Cloudflare Worker (records standard web-visit
// metadata only: page/referrer/event + server-side IP/country/UA — never any
// inventory/INI data). Set the deployed Worker subdomain below; until then the
// placeholder check keeps it inert. Fires on the production origin only.
const ANALYTICS_URL = "https://eqforge-analytics.wangel.workers.dev/collect";
function track(event) {
  try {
    if (location.hostname !== "wangel.github.io") return;     // production only
    if (ANALYTICS_URL.includes("YOUR-SUBDOMAIN")) return;     // not configured yet
    if (!navigator.sendBeacon) return;
    const body = JSON.stringify({
      event: event || "view",
      path: location.pathname,
      ref: document.referrer || "",
    });
    navigator.sendBeacon(ANALYTICS_URL, body);   // text/plain -> no CORS preflight
  } catch { /* analytics must never break the app */ }
}

// Built-in newbie/starter junk dropped from inventory loads (exact, lowercase).
const EXCLUDED_ITEMS = new Set([
  "backpack", "small box", "dagger", "skin of milk", "bread cakes",
  "gloomingdeep lantern", "ethereal dreamweave satchel", "dreamweave satchel",
]);

// ----- app state -----
const state = {
  db: null,          // { byId: Map<int,{link,price,name}>, byName: Map<name,link> }
  spells: null,      // spell-effects.json.gz: { "<spellId>": [name] | [name, description] }
                     // — resolves item click/proc/worn/focus/scroll ids to real names.
  focus: null,       // focus-families.json: { families:[{name,ranked,ranks,items}], spells:{id:[famIdx,rank]} }
  toons: [],         // loaded characters: [{name, filename, items:[parseInventory rows]}]
  inventory: [],     // left pane: AGGREGATE across all toons [{name, location, count, id, bagCount, bagLocation, sources}]
  auction: [],       // right pane (curated "to post"): [{name, location, count, id, price, _priceInput}]
  invSel: new Set(), // selected inventory row indices
  aucSel: new Set(), // selected auction row indices
  invAnchor: null,   // last-clicked inventory row index (shift-range anchor)
  aucAnchor: null,   // last-clicked auction row index (shift-range anchor)
  invSort: { col: null, desc: false },   // inventory column sort
  invSrcOpen: new Set(),    // inventory rows whose "who holds it" panel is expanded
  invCols: ["ac", "hp", "mana", "effects"],   // stat columns shown in the inventory table (persisted)
  invCompare: "",    // toon name to score every row against (their WORN gear), "" = off
  invUpgradesOnly: false,   // with invCompare on: hide anything that isn't an upgrade
  invExpanded: false,       // fold the sell pane away, item browser takes the full window
  invColFilters: {},        // per-column filters: {statKey: minNumber | substring} (persisted)
  _invHeadSig: null,        // last-rendered column signature — guards <thead> rebuilds
  aucSort: { col: null, desc: false },   // auction column sort
  filters: { toon: "", type: "", slot: "", class: "", race: "", stat: "", statMin: 0, locs: new Set(["bags"]) },   // inventory filters
  savedLists: {},    // name -> [{name,id,price,count}] priced sell lists you can reload
  vendor: [],        // vendor list (items to sell to an NPC): [{name, id, count}]
  vendorSel: new Set(),
  vendorAnchor: null,
  excluded: new Set(),   // persisted blacklist keys — never show/add these again
  sales: [],         // sold-item log (persisted): [{name, id, price, count, at}]
  rightTab: "sell",  // which right-pane tab is active: "sell" | "vendor" | "sold"
  reviewOnly: false, // sell list filtered to items flagged for price review
  kronoRate: 0,      // last krono->plat rate seen (for the Recent Postings hint)
  watchlist: [],     // item names to alert on when seen WTS in EC tunnel
  logHandle: null,   // FileSystemFileHandle for the EQ log (persisted in IndexedDB)
  logSize: 0,        // last byte offset read (tail-from-end)
  logTimer: null,    // setInterval id while monitoring
  monitoring: false, // tailing the log right now?
  lastCheckAt: 0,    // ms timestamp of the last successful read (for the banner)
  idf: null,         // IDF map for the fuzzy SELL matcher (built from the DB once)
  aliasPats: null,   // compiled alias patterns for SELL expansion
  silenced: new Set(), // lowercased auctioneer names muted from toasts
  gearSets: {},      // Gear Planner: setName -> [{id,name}] wanted gear (named, persisted)
  gearSetTargets: {},// setName -> target toon ("apply to"), remembered per set
  gearSetFree: {},   // setName -> true = ignore other sets' claims (free-for-all set)
  gearSetInactive: {},// setName -> true = retired; claims nothing, frees its pieces
  gearSetName: "",   // active named gear set
  distTarget: "",    // target toon the active set is applied/scanned against
};

// ----- tiny DOM helpers -----
const $ = (id) => (typeof document !== "undefined" ? document.getElementById(id) : null);
// The always-visible status bar shows the latest line; tint green on success,
// red on trouble so completion/errors are unmissable.
function setStatus(msg) {
  const el = $("statusMsg");
  if (!el) return;
  el.textContent = msg;
  const m = msg.toLowerCase();
  el.className = /error|failed|fail|couldn|blocked|no item|nothing|no auction|no recent/.test(m) ? "err"
    : /complete|generated|added|saved|wrote|downloaded|cleared|removed|in place|priced at/.test(m) ? "ok" : "";
}
function log(msg) {
  const el = $("log");
  if (el) { el.textContent += msg + "\n"; el.scrollTop = el.scrollHeight; }   // hidden store
  setStatus(msg);   // mirror the latest line to the status bar
}

// =====================================================================
// Faithful ports of the desktop logic
// =====================================================================

// make_link: DC2 + hash + SPACE + name + DC2. Split hash from name using the
// known name (NOT hex detection — names starting A-F would break that). The
// space between hash and name is CRITICAL or the link won't render in-game.
function makeLink(itemlink, itemName) {
  if (itemlink.endsWith(itemName)) {
    const hashPart = itemlink.slice(0, itemlink.length - itemName.length);
    return `${DC2}${hashPart} ${itemName}${DC2}`;
  }
  return `${DC2}${itemlink}${DC2}`;
}

// --- Self-generated item links ---------------------------------------------
// We compute the EC-tunnel item-link hash ourselves (reverse-engineered from the
// EQ client's ItemBase::CreateItemTagString) instead of relying on the DB's
// precomputed 'itemlink' column, which items.sodeq.org dropped. The hash is
// java.lang.String.hashCode (h = h*31 + ch) over a per-item "hash string" built
// from a handful of stat columns with every char folded to UPPER-CASE. The
// layout is chosen by `itemclass` (client type byte) and, for normal items,
// whether it's a charm (slots == 1). Validated byte-exact vs 136,522 sodeq
// itemlinks. Books (itemclass 2: quest text, never sold) use an undecoded packed
// format, so we skip them. MUST stay in parity with the Python build_itemlink().
const HASH_MULT = 31; // java.lang.String.hashCode multiplier; the EQ item hash

const _hx = (n, w) => (n >>> 0).toString(16).toUpperCase().padStart(w, "0");
const _int = (s) => { const n = parseInt(s, 10); return Number.isFinite(n) ? n : 0; };

// g(col) -> already-trimmed column value. Returns null for books.
function itemHashString(g) {
  const itemclass = g("itemclass"), slots = g("slots");
  const iid = String(_int(g("id"))), name = g("name");
  if (itemclass === "2") return null;              // book/note: not sold, undecoded
  if (itemclass === "1")                            // bag/container: "%x%d%09X%d"
    return iid + name + _int(g("bagslots")).toString(16).toUpperCase()
         + g("bagwr") + _hx(_int(g("price")), 9) + g("weight");
  let cols;
  if (itemclass === "0" && slots === "1")           // charm (8 fields)
    cols = ["light", "icon", "price", "size", null, "itemtype", "favor", "guildfavor"];
  else                                              // normal item (13 fields)
    cols = ["mana", "hp", "favor", "light", "icon", "price", "weight",
            "reqlevel", "size", null, "itemtype", "ac", "guildfavor"];
  const body = cols.map((c) => (c === null ? "0" : g(c))).join(" ");
  return iid + name + body;
}

function eqStringHash(s) {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (Math.imul(h, HASH_MULT) + s.charCodeAt(i)) | 0;
  return h >>> 0;
}

// Compute the DB-equivalent itemlink (hex body + name); "" for books.
// NOTE: hash string is upper-cased BEFORE hashing (matches toupper in-client).
function buildItemlink(g) {
  const s = itemHashString(g);
  if (s === null) return "";
  const h = eqStringHash(s.toUpperCase());
  const evolving = ["0", "", "-1"].includes(g("evoitem")) ? 0 : 1;
  const evoGroup = _int(g("evoid")) & 0xffff;
  const evoLevel = _int(g("evolvl")) & 0xff;
  // Live/TLP body (tag layout rev 9c2f1b): action(0)+id(5)+12 empty aug slots(60)
  // +evolving(1)+evolveGroup(4)+evolveLevel(2)+2 empty ornament slots(10)+hash(8).
  const body = "0" + _hx(_int(g("id")), 5) + "0".repeat(60)
    + _hx(evolving, 1) + _hx(evoGroup, 4) + _hx(evoLevel, 2)
    + "0".repeat(10) + _hx(h, 8);
  return body + g("name");
}

// Split one CSV line into fields, honoring RFC quoting (delimiter '|', quote
// '"', doubled "" -> literal "). Matches Python csv.reader on single-line
// records — needed because some item names contain '"' or even a literal '|'
// (e.g. `Iksar Right Hand '=|-'`) and a naive split('|') would corrupt the name
// and therefore the computed hash.
function splitCsvLine(line, delim = "|") {
  const out = [];
  let cur = "", q = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (q) {
      if (ch === '"') { if (line[i + 1] === '"') { cur += '"'; i++; } else q = false; }
      else cur += ch;
    } else if (ch === '"') { q = true; }
    else if (ch === delim) { out.push(cur); cur = ""; }
    else cur += ch;
  }
  out.push(cur);
  return out;
}

// Parse items.txt (pipe-delimited). We compute the itemlink ourselves from stat
// columns (sodeq removed the precomputed 'itemlink' column). Builds the id-keyed
// map (unambiguous) plus a first-row-wins name fallback.
function parseItemDb(text) {
  const byId = new Map();
  const byName = new Map();
  const byNameIds = new Map();   // lowercase name -> [all db ids with that name] (for the sibling-id price fallback)
  let dupNames = 0;
  const lines = text.split(/\r?\n/);
  if (!lines.length) return { byId, byName, byNameIds };
  const header = splitCsvLine(lines[0]).map((h) => h.trim().toLowerCase());
  const idx = {};
  header.forEach((h, i) => { if (!(h in idx)) idx[h] = i; });
  if (!("name" in idx) || !("id" in idx))
    throw new Error("items.txt missing name/id columns");
  for (let r = 1; r < lines.length; r++) {
    if (!lines[r]) continue;
    const p = splitCsvLine(lines[r]);
    const g = (c) => (c in idx ? (p[idx[c]] || "").trim() : "");
    const name = g("name");
    if (!name) continue;
    const idStr = g("id");
    const id = /^\d+$/.test(idStr) ? parseInt(idStr, 10) : null;
    const link = id !== null ? buildItemlink(g) : "";
    const prStr = g("price");
    const price = /^\d+$/.test(prStr) ? parseInt(prStr, 10) : null;
    // Extra fields for the Type/Slot filters: slots bitmask, item class
    // (1=container), food duration (>0=food/drink), tradeskill flag.
    const num = (c) => { const v = g(c); return /^\d+$/.test(v) ? parseInt(v, 10) : 0; };
    const snum = (c) => { const v = g(c); return /^-?\d+$/.test(v) ? parseInt(v, 10) : 0; };
    if (id !== null) byId.set(id, {
      link, price, name,
      slots: num("slots"), itemclass: num("itemclass"),
      foodduration: num("foodduration"), tradeskills: num("tradeskills"),
      classes: num("classes"),   // class-restriction bitmask (Spell/Gear class filter)
      races: num("races"),       // race-restriction bitmask (Gear "usable by race" filter)
      // trade flags (Who-Has-It / gear distributor). Frostreaver is FREE-TRADE, so
      // the tradability test is fvnodrop (NOT the standard nodrop, which is overridden
      // there). attunable items become No-Trade once worn -> flag when found equipped.
      nodrop: num("nodrop"), fvnodrop: num("fvnodrop"), attunable: num("attunable"),
      tip: buildTipFields(num, snum),   // full stat block for the hover tooltip
    });
    if (byName.has(name)) dupNames++;
    else byName.set(name, link);
    if (id !== null) { const lk = name.toLowerCase(); const a = byNameIds.get(lk); if (a) a.push(id); else byNameIds.set(lk, [id]); }
  }
  if (dupNames) log(`  (${dupNames} duplicate item names in DB — id matching disambiguates)`);
  return { byId, byName, byNameIds };
}

// ----- location buckets, item type, and equip slots (2.0 filter support) -----
// Bare equipment-slot names as they appear in a dump's Location column.
const EQUIP_SLOTS = new Set(["Charm", "Ear", "Head", "Face", "Neck", "Shoulders",
  "Arms", "Back", "Wrist", "Range", "Hands", "Primary", "Secondary", "Fingers",
  "Chest", "Legs", "Feet", "Waist", "Ammo", "Power Source", "Held"]);

// Which storage area a dump Location belongs to → drives the location filter.
function locBucket(loc) {
  const l = (loc || "").trim();
  const low = l.toLowerCase();
  if (low.startsWith("sharedbank")) return "shared";
  if (low.startsWith("bank")) return "bank";
  if (low.startsWith("general")) return "bags";
  if (low.startsWith("hoard")) return "hoard";   // Hoard storage (up to 125 slots)
  // "Personal-Depot" = the Personal Tradeskill Depot (marketplace storage that only
  // accepts tradeskill items). Its own bucket, not "bags": it's a big pile of very
  // sellable mats, and you withdraw from the depot window rather than from a bag.
  if (/^personal[ _-]?depot/.test(low) || low.startsWith("depot")) return "depot";
  // "Equipment" = a PERSONA's closet: gear worn by one of this character's inactive
  // personas. Reachable only by switching to that persona and unequipping, so it is not
  // ordinary spare inventory (2026-07-22).
  if (low === "equipment") return "persona";
  if (low === "keyring") return "keyring";
  return EQUIP_SLOTS.has(l.split("-")[0].trim()) ? "equipped" : "other";
}
const LOC_BUCKETS = ["bags", "equipped", "bank", "shared", "keyring", "depot", "hoard", "persona", "other"];

// EQ wearable-slot bitmask (items.txt "slots" column). Paired slots (both ears,
// both wrists, both fingers) collapse to one label. Order = a sensible top→bottom.
const SLOT_BITS = [
  ["Charm", 1], ["Ear", 2 | 16], ["Head", 4], ["Face", 8], ["Neck", 32],
  ["Shoulders", 64], ["Arms", 128], ["Back", 256], ["Wrist", 512 | 1024],
  ["Range", 2048], ["Hands", 4096], ["Primary", 8192], ["Secondary", 16384],
  ["Fingers", 32768 | 65536], ["Chest", 131072], ["Legs", 262144], ["Feet", 524288],
  ["Waist", 1048576], ["Power Source", 2097152], ["Ammo", 4194304],
];
const SLOT_ORDER = SLOT_BITS.map(([n]) => n);

// EQ class-restriction bitmask (items.txt "classes" column). Drives the Spell
// class filter. A spell can list several classes (e.g. Circle of Winter =
// Ranger|Druid); 0 means unrestricted (all classes). Order = the canonical
// EQ class order, so the dropdown reads naturally.
const CLASS_BITS = [
  ["Warrior", 1, "WAR"], ["Cleric", 2, "CLR"], ["Paladin", 4, "PAL"], ["Ranger", 8, "RNG"],
  ["Shadowknight", 16, "SHD"], ["Druid", 32, "DRU"], ["Monk", 64, "MNK"], ["Bard", 128, "BRD"],
  ["Rogue", 256, "ROG"], ["Shaman", 512, "SHM"], ["Necromancer", 1024, "NEC"], ["Wizard", 2048, "WIZ"],
  ["Magician", 4096, "MAG"], ["Enchanter", 8192, "ENC"], ["Beastlord", 16384, "BST"], ["Berserker", 32768, "BER"],
];
const CLASS_ORDER = CLASS_BITS.map(([n]) => n);

// Class names an item is restricted to, from its DB classes bitmask. Empty for
// unrestricted (0) or unknown items. Populated for spells (caster class) and gear
// (wearer class) — the class filter treats both as "usable by class X".
function itemClasses(item) {
  const rec = item.id && state.db ? state.db.byId.get(item.id) : null;
  const mask = rec ? (rec.classes || 0) : 0;
  if (!mask) return [];
  return CLASS_BITS.filter(([, bit]) => (mask & bit) !== 0).map(([n]) => n);
}

// EQ race-restriction bitmask (items.txt "races" column). Drives the Gear "usable
// by race" filter. 0 = unrestricted; 65535 = all 16 races — BOTH mean "any race",
// so we return [] (empty === no race restriction) and only list the actually-
// restricted races otherwise (e.g. a bard horn limited to HUM/HEF/…).
const RACE_BITS = [
  ["Human", 1, "HUM"], ["Barbarian", 2, "BAR"], ["Erudite", 4, "ERU"], ["Wood Elf", 8, "ELF"],
  ["High Elf", 16, "HIE"], ["Dark Elf", 32, "DEF"], ["Half Elf", 64, "HEF"], ["Dwarf", 128, "DWF"],
  ["Troll", 256, "TRL"], ["Ogre", 512, "OGR"], ["Halfling", 1024, "HFL"], ["Gnome", 2048, "GNM"],
  ["Iksar", 4096, "IKS"], ["Vah Shir", 8192, "VAH"], ["Froglok", 16384, "FRG"], ["Drakkin", 32768, "DRK"],
];
const RACE_ORDER = RACE_BITS.map(([n]) => n);

function itemRaces(item) {
  const rec = item.id && state.db ? state.db.byId.get(item.id) : null;
  const mask = rec ? (rec.races || 0) : 0;
  if (!mask || mask === 65535) return [];   // unrestricted / all races
  return RACE_BITS.filter(([, bit]) => (mask & bit) !== 0).map(([n]) => n);
}

// ----- item hover tooltip (rendered locally from items.txt — no network) -------
// The DB carries the full in-game stat block; we reconstruct the item window from
// it. NOTE (verified 2026-07-05): the nodrop/norent columns are NOT trustworthy in
// this DB — Fungus Tunic (real NO DROP) and Short Sword (droppable) both read
// nodrop=1 — so we deliberately DO NOT render NO DROP / NO RENT. magic + loregroup
// (LORE) do vary correctly and are shown. Effect spell IDs show as "#id" until a
// spell-name file is bundled (planned step 2).
const SIZE_NAMES = ["TINY", "SMALL", "MEDIUM", "LARGE", "GIANT", "GIGANTIC",
                    "COLOSSAL", "GARGANTUAN", "IMMENSE"];
const WEAPON_SKILL = { 0: "1H Slashing", 1: "2H Slashing", 2: "Piercing",
  3: "1H Blunt", 4: "2H Blunt", 5: "Archery", 7: "Throwing", 8: "Shield",
  35: "1H Blunt", 36: "Hand to Hand", 45: "2H Piercing" };

// Pull the tooltip-relevant columns into a compact record during DB parse. `num`
// is unsigned, `snum` signed (stats can be negative; loregroup is -1 for LORE).
function buildTipFields(num, snum) {
  let augs = 0;
  for (let i = 1; i <= 6; i++) if (num("augslot" + i + "type") > 0) augs++;
  return {
    magic: num("magic"), lore: snum("loregroup") !== 0 ? 1 : 0, attunable: num("attunable"),
    ac: snum("ac"), dmg: snum("damage"), delay: num("delay"), wtype: num("itemtype"),
    str: snum("astr"), sta: snum("asta"), agi: snum("aagi"), dex: snum("adex"),
    wis: snum("awis"), int: snum("aint"), cha: snum("acha"),
    hp: snum("hp"), mana: snum("mana"), end: snum("endurance"),
    svf: snum("fr"), svc: snum("cr"), svm: snum("mr"), svd: snum("dr"), svp: snum("pr"), svcor: snum("svcorruption"),
    atk: snum("attack"), haste: snum("haste"), regen: snum("regen"),
    mregen: snum("manaregen"), heal: snum("healamt"), sdmg: snum("spelldmg"),
    clair: snum("clairvoyance"), bs: snum("backstabdmg"),
    hstr: snum("heroic_str"), hsta: snum("heroic_sta"), hagi: snum("heroic_agi"), hdex: snum("heroic_dex"),
    hwis: snum("heroic_wis"), hint: snum("heroic_int"), hcha: snum("heroic_cha"),
    click: num("clickeffect"), casttime: num("casttime"), charges: snum("maxcharges"),
    proc: num("proceffect"), worn: num("worneffect"), focus: num("focuseffect"), scroll: num("scrolleffect"),
    weight: num("weight"), size: num("size"), reqlvl: num("reqlevel"), reclvl: num("reclevel"),
    stack: num("stacksize"), augs,
  };
}

// Build the tooltip HTML for a DB record (rec.tip + rec.slots/classes/races).
function itemTipHtml(rec) {
  const t = rec.tip; if (!t) return "";
  const L = [`<div class="tip-name">${escapeHtml(rec.name)}</div>`];
  const flags = [];
  if (t.magic) flags.push("MAGIC");
  if (t.lore) flags.push("LORE");
  if (t.attunable) flags.push("ATTUNABLE");
  if (flags.length) L.push(`<div class="tip-flags">${flags.join(" · ")}</div>`);

  if (rec.slots) {
    const names = SLOT_BITS.filter(([, bit]) => (rec.slots & bit)).map(([n]) => n);
    if (names.length) L.push(`<div class="tip-line">Slot: ${names.join(" ")}</div>`);
  }
  if (t.dmg > 0) {
    const skill = WEAPON_SKILL[t.wtype] || "Weapon";
    L.push(`<div class="tip-line">Skill: ${skill}${t.delay ? ` &nbsp; Atk Delay: ${t.delay}` : ""}</div>`);
    L.push(`<div class="tip-line">DMG: ${t.dmg}${t.delay ? ` &nbsp; Ratio: ${(t.dmg / t.delay).toFixed(2)}` : ""}</div>`);
  }
  if (t.ac) L.push(`<div class="tip-line">AC: ${t.ac}</div>`);

  const chip = (lbl, v) => v ? `<span class="tip-stat">${lbl} ${v > 0 ? "+" : ""}${v}</span>` : "";
  const chips = (pairs, cls) => {
    const s = pairs.map(([l, v]) => chip(l, v)).filter(Boolean).join("");
    if (s) L.push(`<div class="tip-stats${cls ? " " + cls : ""}">${s}</div>`);
  };
  chips([["STR", t.str], ["STA", t.sta], ["AGI", t.agi], ["DEX", t.dex], ["WIS", t.wis],
         ["INT", t.int], ["CHA", t.cha], ["HP", t.hp], ["MANA", t.mana], ["END", t.end]]);
  chips([["HSTR", t.hstr], ["HSTA", t.hsta], ["HAGI", t.hagi], ["HDEX", t.hdex],
         ["HWIS", t.hwis], ["HINT", t.hint], ["HCHA", t.hcha]], "tip-hero");
  chips([["SV FIRE", t.svf], ["SV COLD", t.svc], ["SV MAGIC", t.svm],
         ["SV DISEASE", t.svd], ["SV POISON", t.svp], ["SV CORRUPT", t.svcor]]);

  const mods = [];
  const m = (l, v, suf = "") => { if (v) mods.push(`${l} ${v > 0 ? "+" : ""}${v}${suf}`); };
  m("Atk", t.atk); m("Haste", t.haste, "%"); m("HP Regen", t.regen); m("Mana Regen", t.mregen);
  m("Heal", t.heal); m("Spell Dmg", t.sdmg); m("Clairvoyance", t.clair);
  if (t.bs) mods.push(`Backstab DMG ${t.bs}`);
  if (mods.length) L.push(`<div class="tip-line dim">${mods.join(" · ")}</div>`);

  // The item DB stores effects as bare spell IDs and its own name columns are empty,
  // so the NAME + what-it-does text come from spell-effects.json.gz (build_spells.py).
  // Without that file every line degrades to the old type-only label.
  const clickQual = () => {
    const b = [];
    if (t.casttime) b.push(`cast ${(t.casttime / 1000).toFixed(1)}s`);
    if (t.charges > 0) b.push(`${t.charges} charge${t.charges > 1 ? "s" : ""}`);
    return b.length ? ` (${b.join(", ")})` : "";
  };
  const eff = [
    ["Clicky", t.click, clickQual()],
    ["Proc", t.proc, ""],
    ["Worn", t.worn, ""],
    ["Focus", t.focus, ""],
    ["Scroll", t.scroll, ""],
  ];
  for (const [label, id, qual] of eff) {
    if (!id) continue;
    const info = spellInfo(id);
    // Focus names already encode family + rank ("Improved Damage III"); showing the
    // parsed rank again would be noise, so only the name is added for those.
    const head = info ? `${label}: ${escapeHtml(info.name)}${qual}` : `${label} Effect${qual}`;
    L.push(`<div class="tip-line tip-eff">${head}</div>`);
    if (info && info.desc) L.push(`<div class="tip-line tip-effdesc">${escapeHtml(info.desc)}</div>`);
  }

  const sz = SIZE_NAMES[t.size] || "";
  L.push(`<div class="tip-line dim">WT: ${(t.weight / 10).toFixed(1)}` +
         `${sz ? ` &nbsp; Size: ${sz}` : ""}${t.stack > 1 ? ` &nbsp; Stack: ${t.stack}` : ""}</div>`);

  const allCls = rec.classes === 0 || rec.classes === 65535;
  const cls = allCls ? "ALL" : CLASS_BITS.filter(([, b]) => rec.classes & b).map(([, , a]) => a).join(" ");
  L.push(`<div class="tip-line">Class: ${cls || "NONE"}</div>`);
  const raceAll = !rec.races || rec.races === 65535;
  const race = raceAll ? "ALL" : RACE_BITS.filter(([, b]) => rec.races & b).map(([, , a]) => a).join(" ");
  L.push(`<div class="tip-line">Race: ${race}</div>`);

  if (t.reqlvl) L.push(`<div class="tip-line dim">Required level ${t.reqlvl}</div>`);
  if (t.reclvl) L.push(`<div class="tip-line dim">Recommended level ${t.reclvl}</div>`);
  if (t.augs) L.push(`<div class="tip-line dim">${t.augs} augment slot${t.augs > 1 ? "s" : ""}</div>`);
  return L.join("");
}

// Single reusable floating tooltip element (created lazily, follows the cursor).
let _itemTipEl = null;
function itemTipEl() {
  if (!_itemTipEl) {
    _itemTipEl = document.createElement("div");
    _itemTipEl.className = "item-tip";
    _itemTipEl.style.display = "none";
    document.body.appendChild(_itemTipEl);
  }
  return _itemTipEl;
}
function showItemTip(rec) { const el = itemTipEl(); el.innerHTML = itemTipHtml(rec); el.style.display = "block"; }
function hideItemTip() { if (_itemTipEl) _itemTipEl.style.display = "none"; }
function positionItemTip(x, y) {
  const el = itemTipEl(); const pad = 14;
  let left = x + pad, top = y + pad;
  if (left + el.offsetWidth > window.innerWidth - 8) left = x - el.offsetWidth - pad;
  if (top + el.offsetHeight > window.innerHeight - 8) top = Math.max(8, window.innerHeight - el.offsetHeight - 8);
  el.style.left = left + "px"; el.style.top = top + "px";
}

// All equip slots an item can go in, from its DB slots bitmask (handles items
// valid in multiple slots, e.g. rings/earrings). Empty for non-equippable items.
function itemSlots(item) {
  const rec = item.id && state.db ? state.db.byId.get(item.id) : null;
  const mask = rec ? (rec.slots || 0) : 0;
  if (!mask) return [];
  const out = [];
  for (const [name, bits] of SLOT_BITS) if (mask & bits) out.push(name);
  return out;
}

// Coarse item type for the Type filter. DB fields first (itemclass = container,
// foodduration = food, slots = gear, tradeskills = mat), name prefix for spells.
function classifyType(item) {
  const n = (item.name || "").toLowerCase();
  if (n.startsWith("spell:") || n.startsWith("song:")) return "spell";
  const rec = item.id && state.db ? state.db.byId.get(item.id) : null;
  if (rec) {
    if (rec.itemclass === 1) return "container";
    if ((rec.foodduration || 0) > 0) return "food";
    if ((rec.slots || 0) > 0) return "gear";
    if (rec.tradeskills === 1) return "tradeskill";
  }
  return "other";
}

// Parse an EQ /outputfile inventory dump (tab-separated). Combine stacks /
// duplicate slots by id (fall back to name when there's no id column), drop
// excluded junk and the phantom empty/KeyRing rows. Each combined entry also
// carries per-location-bucket counts so the location filter can slice by area.
function parseInventory(text) {
  const combined = new Map();
  const order = [];
  const rows = text.split(/\r?\n/);
  let header = null;
  let ni = 1, li = 0, ci = null, ii = null, si = null;
  for (const raw of rows) {
    const line = raw.replace(/[\r\n]+$/, "");
    if (!line) continue;
    const parts = line.split("\t");
    if (header === null) {
      header = parts.map((p) => p.trim().toLowerCase());
      ni = header.indexOf("name"); if (ni < 0) ni = 1;
      li = header.indexOf("location"); if (li < 0) li = 0;
      ci = header.indexOf("count"); if (ci < 0) ci = null;
      ii = header.indexOf("id"); if (ii < 0) ii = null;
      si = header.indexOf("slots"); if (si < 0) si = null;
      continue;
    }
    if (parts.length < 3) continue;
    const name = (parts[ni] || "").trim().replace(/\*+$/, "");
    const loc = (parts[li] || "").trim();
    const lower = name.toLowerCase();
    if (lower === "" || lower === "empty" || lower === "name") continue;
    if (EXCLUDED_ITEMS.has(lower)) continue;
    // Drop bags you're CARRYING: a container (Slots>0, i.e. capacity for general
    // inventory) sitting directly in a top-level General slot ("General 3", not a
    // nested "General 3-SlotN") is storage holding your wares, not merchandise. A
    // bag you'd actually sell lives nested inside another bag, so it keeps a
    // "-Slot" location and survives this. Scoped to "General N" so equipped gear
    // (whose Slots column counts AUGMENT slots, e.g. raid gear = 6) is never hit.
    // No hardcoded bag list needed.
    const slots = si !== null && si < parts.length ? (parseInt((parts[si] || "").trim(), 10) || 0) : 0;
    if (slots > 0 && /^general \d+$/i.test(loc)) continue;
    let count = 1;
    if (ci !== null && ci < parts.length) {
      const n = parseInt((parts[ci] || "").trim(), 10);
      count = Number.isFinite(n) ? Math.max(n, 1) : 1;
    }
    let id = 0;
    if (ii !== null && ii < parts.length) {
      const n = parseInt((parts[ii] || "").trim(), 10);
      id = Number.isFinite(n) ? Math.max(n, 0) : 0;
    }
    // Track per-location-bucket quantities so the location filter can show just
    // bags, just worn, bank+shared, etc. bagCount/bagLocation kept for compat.
    const bucket = locBucket(loc);
    const inBag = bucket === "bags";
    const key = id ? `#${id}` : name;
    if (combined.has(key)) {
      const e = combined.get(key);
      e.count += count;
      e.buckets[bucket] += count;
      if (inBag) { e.bagCount += count; if (!e.bagLocation) e.bagLocation = loc; }
    } else {
      const buckets = { bags: 0, equipped: 0, bank: 0, shared: 0, keyring: 0, depot: 0, hoard: 0, persona: 0, other: 0};
      buckets[bucket] = count;
      combined.set(key, {
        name, location: loc, count, id, price: "",
        bagCount: inBag ? count : 0,
        bagLocation: inBag ? loc : "",
        buckets,
      });
      order.push(key);
    }
  }
  return order.map((k) => combined.get(k));
}

// ----- multi-toon aggregation (the 2.0 addition over EQ Auction Forge) --------
// Derive a character name from the dump filename, e.g.
// "Rakthor_frostreaver-Inventory.txt" -> "Rakthor". Falls back to the whole base.
function charNameFromFilename(fname) {
  const base = (fname || "").replace(/\.[^.]+$/, "");   // strip extension
  const m = base.match(/^([A-Za-z0-9]+)[_-]/);
  return m ? m[1] : (base || "toon");
}

// "Rakthor_frostreaver-Inventory.txt" -> "frostreaver". "" when the filename
// doesn't carry one (hand-renamed dumps).
function serverFromFilename(fname) {
  const base = (fname || "").replace(/\.[^.]+$/, "");
  const m = base.match(/^[A-Za-z0-9]+_([A-Za-z]+)-Inventory$/i);
  return m ? m[1].toLowerCase() : "";
}

// TLP-Auctions accepts six server names but only Frostreaver actually has sales
// (verified 2026-08-07: 5,917 items with sales vs 0 on Teek/Mischief/Oakwynd/
// Thornblade/Yelinak). Someone boxing another TLP therefore gets "— no sales" on
// EVERY row, which reads as a broken app rather than an empty market. Say it once,
// plainly, the moment their dumps show a different server.
let warnedForeignServer = false;
function warnIfForeignServer() {
  if (warnedForeignServer) return;
  const servers = new Set();
  for (const t of state.toons || []) {
    const s = serverFromFilename(t.filename || "");
    if (s) servers.add(s);
  }
  if (!servers.size || servers.has(SERVER.toLowerCase())) return;
  warnedForeignServer = true;
  const names = [...servers].map((s) => s.charAt(0).toUpperCase() + s.slice(1)).join(", ");
  log(`Heads up: these dumps are from ${names}, and TLP-Auctions only has sales data ` +
      `for ${SERVER}. Price checks will come back "no sales" for everything — that's ` +
      `the market data missing, not a fault. Everything else (inventory, gear, ` +
      `roster, lockouts, macros) works normally.`);
}

// Fold every loaded toon's parsed inventory into ONE aggregate list for the left
// pane. Same item (by id, else name) across toons is merged: counts and bag
// counts summed, and a `sources` list records which toon holds how many + where.
// Account-shared bank items (location starts "SharedBank") are physically one
// item visible from every boxed toon on that account, so they're counted ONCE
// (by location+id+name) instead of multiplied by the number of dumps loaded.
function rebuildInventory() {
  // Open "who holds it" panels are keyed by index into state.inventory, and this
  // rebuilds that array from scratch — stale indices would expand the wrong items.
  state.invSrcOpen.clear();
  warnIfForeignServer();      // both load paths funnel through here
  const combined = new Map();
  const order = [];
  const sharedSeen = new Set();   // location|id|name already counted from a shared bank
  let sharedDupes = 0;
  for (const toon of state.toons) {
    for (const it of toon.items) {
      const key = it.id ? `#${it.id}` : it.name.toLowerCase();
      const isShared = /^sharedbank/i.test(it.location || "");
      let dupSkip = false;
      if (isShared) {
        const sk = `${it.location}|${it.id}|${it.name}`;
        if (sharedSeen.has(sk)) { dupSkip = true; sharedDupes++; }
        else sharedSeen.add(sk);
      }
      let e = combined.get(key);
      if (!e) {
        e = { name: it.name, id: it.id, price: "", count: 0, bagCount: 0,
              bagLocation: "", location: "", sources: [],
              buckets: { bags: 0, equipped: 0, bank: 0, shared: 0, keyring: 0, depot: 0, hoard: 0, persona: 0, other: 0},
              toons: new Set() };
        combined.set(key, e);
        order.push(key);
      }
      if (dupSkip) continue;   // same physical shared-bank item, already counted
      e.count += it.count;
      e.bagCount += it.bagCount;
      if (!e.bagLocation && it.bagLocation) e.bagLocation = it.bagLocation;
      for (const b of LOC_BUCKETS) e.buckets[b] += (it.buckets ? it.buckets[b] : 0);
      e.toons.add(toon.name);
      e.sources.push({ toon: toon.name, count: it.count, location: it.location });
    }
  }
  // Finalize each aggregate: display location, type + valid slots (for filters).
  for (const e of combined.values()) {
    if (e.sources.length === 1) e.location = `${e.sources[0].toon} · ${e.sources[0].location}`;
    else if (e.sources.length > 1) e.location = `${e.toons.size} toon${e.toons.size > 1 ? "s" : ""}`;
    else e.location = "shared";
    e.type = classifyType(e);
    e.slots = itemSlots(e);
    // class filter: spells (caster) + gear (wearer); race filter: gear only ([] = any race)
    e.classes = (e.type === "spell" || e.type === "gear") ? itemClasses(e) : [];
    e.races = e.type === "gear" ? itemRaces(e) : [];
  }
  state.inventory = order.map((k) => combined.get(k));
  return { sharedDupes };
}

// The DB link for an inventory item: prefer the exact id, fall back to name.
function linkFor(item) {
  if (item.id && state.db.byId.has(item.id)) return state.db.byId.get(item.id).link;
  return state.db.byName.get(item.name) || null;
}

// One auction token: "<link> <price>" (no xN — tlp-auctions reads x2 as 2-for-price).
function linkToken(item) {
  const link = makeLink(linkFor(item), item.name);
  return item.price ? `${link} ${item.price}` : link;
}

// Pack tokens into <=255-char lines, each led by prefix (and optional suffix).
function packToLines(tokens, prefix, suffix, sep) {
  const lines = [];
  let cur = [];
  const base = prefix.length + 1;
  const suffixLen = suffix ? ` ${suffix}`.length : 0;
  let curLen = base;
  for (const tok of tokens) {
    const add = (cur.length ? sep.length : 0) + tok.length;
    if (cur.length && curLen + add + suffixLen > 255) {
      lines.push(`${prefix} ` + cur.join(sep) + (suffix ? ` ${suffix}` : ""));
      cur = [tok];
      curLen = base + tok.length;
    } else {
      cur.push(tok);
      curLen += add;
    }
  }
  if (cur.length) lines.push(`${prefix} ` + cur.join(sep) + (suffix ? ` ${suffix}` : ""));
  return lines;
}

// Lay packed lines into social buttons (5 lines/button, 12 buttons/page, from
// startPage up; page 1 is never touched). Returns {entries, preview, overflow,
// endPage} — endPage is the last page that got a button (startPage-1 if none),
// so a second group (links) can begin on a fresh page after this one.
function buttonsFromLines(lines, btnName, startPage, maxLinesBtn = 5) {
  const entries = [];     // [key, val] pairs in order
  const preview = [];
  let page = startPage, btn = 1, written = 0, overflow = 0, endPage = startPage - 1;
  const chunks = [];
  for (let i = 0; i < lines.length; i += maxLinesBtn) chunks.push(i);
  for (const bs of chunks) {
    if (btn > BUTTONS_PER_PAGE) { page++; btn = 1; }
    if (page > MAX_PAGE) { overflow = chunks.length - written; break; }
    const bl = lines.slice(bs, bs + maxLinesBtn);
    const label = `${btnName}${written + 1}`;
    entries.push([`Page${page}Button${btn}Name`, label]);
    entries.push([`Page${page}Button${btn}Color`, "0"]);
    bl.forEach((line, idx) => entries.push([`Page${page}Button${btn}Line${idx + 1}`, line]));
    preview.push([label, bl]);
    endPage = page;
    btn++; written++;
  }
  return { entries, preview, overflow, endPage };
}

// Lay several TYPED groups (Spell / Gear / Misc) into social buttons on ONE
// continuous run of pages, starting at startPage. Groups flow one after another
// (spells, then gear, then misc) sharing the same page block — but every button
// holds lines from a SINGLE group only, so a spell and a piece of gear never end
// up in the same macro. Each group numbers its own buttons (Spell1, Spell2, …,
// Gear1, …). Returns {entries, preview, overflow}. groups = [{name, lines}, …].
function layoutGroups(groups, startPage, maxLinesBtn = 5) {
  const entries = [], preview = [];
  let page = startPage, btn = 1, overflow = 0, full = false;
  for (const g of groups) {
    if (full) { overflow += Math.ceil(g.lines.length / maxLinesBtn); continue; }
    let written = 0;
    for (let i = 0; i < g.lines.length; i += maxLinesBtn) {
      if (btn > BUTTONS_PER_PAGE) { page++; btn = 1; }
      if (page > MAX_PAGE) { overflow += Math.ceil((g.lines.length - i) / maxLinesBtn); full = true; break; }
      const bl = g.lines.slice(i, i + maxLinesBtn);
      const label = `${g.name}${written + 1}`;
      entries.push([`Page${page}Button${btn}Name`, label]);
      entries.push([`Page${page}Button${btn}Color`, "0"]);
      bl.forEach((line, idx) => entries.push([`Page${page}Button${btn}Line${idx + 1}`, line]));
      preview.push([label, bl]);
      btn++; written++;
    }
  }
  return { entries, preview, overflow };
}

// Idempotent merge into [Socials]: drop buttons we previously auto-wrote
// (Name matches ^(WTS|Rare)\d+$), then update/insert the new entries. Hand-made
// socials and every non-[Socials] section are left untouched. Faithful port of
// the desktop _write_ini merge.
const WIPE_FROM_PAGE = 3;   // "Overwrite sell pages" clears app macros on this page and up (1–2 = personal)
// App-generated social button names. Spell/Gear/Misc are the current typed groups;
// WTS/Rare are legacy names kept here so old buttons from earlier versions still get
// cleaned up on write. Any button whose Name matches this is ours to replace.
const AUTO_NAME_RE = /^(?:Spell|Gear|Misc|WTS|Rare)\d+$/;
function mergeIntoIni(existing, entries, clearFromPage) {
  const newMap = new Map(entries);
  if (!existing.includes("[Socials]")) {
    existing = existing.replace(/\s+$/, "") + "\n\n[Socials]\n";
  }
  const autoNameRe = AUTO_NAME_RE;
  // Only clear stale auto (WTS#/Rare#) buttons on the PAGES this write targets, so a
  // second batch on a different page (gear pg 2 vs spells pg 5) doesn't wipe the
  // first. Within a page it still fully overwrites. Page 1 is never a macro target.
  const targetPages = new Set();
  for (const [k] of entries) { const m = /^Page(\d+)Button/.exec(k); if (m) targetPages.add(m[1]); }
  const pageOf = (key) => { const m = /^Page(\d+)Button/.exec(key); return m ? m[1] : null; };
  const dropPrefixes = new Set();
  let inSocials = false;
  for (const raw of existing.split("\n")) {
    const st = raw.trim();
    if (st === "[Socials]") inSocials = true;
    else if (st.startsWith("[") && st.endsWith("]")) inSocials = false;
    else if (inSocials && st.includes("=")) {
      const eq = st.indexOf("=");
      const k = st.slice(0, eq).trim();
      const v = st.slice(eq + 1);
      const prefix = k.slice(0, -4);
      if (k.endsWith("Name") && autoNameRe.test(v.trim())) {
        const pg = pageOf(prefix);
        // drop a stale macro button if it's on a page THIS write targets, or (when
        // "Overwrite pages N+" is on) anywhere in the clear zone.
        if (targetPages.has(pg) || (clearFromPage && pg != null && parseInt(pg, 10) >= clearFromPage)) dropPrefixes.add(prefix);
      }
    }
  }
  const isAuto = (key) => {
    for (const p of dropPrefixes) {
      if (key === p + "Name" || key === p + "Color") return true;
      if (key.startsWith(p + "Line") && /^\d+$/.test(key.slice(p.length + 4))) return true;
    }
    return false;
  };
  const out = [];
  const written = new Set();
  inSocials = false;
  for (const line of existing.split("\n")) {
    const stripped = line.trim();
    if (stripped === "[Socials]") { inSocials = true; out.push(line); continue; }
    if (stripped.startsWith("[") && stripped.endsWith("]")) {
      if (inSocials) {
        for (const [k, v] of entries) if (!written.has(k)) { out.push(`${k}=${v}`); written.add(k); }
      }
      inSocials = false; out.push(line); continue;
    }
    if (inSocials && stripped.includes("=")) {
      const key = stripped.slice(0, stripped.indexOf("=")).trim();
      if (newMap.has(key)) { out.push(`${key}=${newMap.get(key)}`); written.add(key); continue; }
      if (isAuto(key)) continue;
    }
    out.push(line);
  }
  if (inSocials) {
    for (const [k, v] of entries) if (!written.has(k)) { out.push(`${k}=${v}`); written.add(k); }
  }
  return out.join("\n");
}

// ----- latin-1 byte helpers (the encoding gotcha, solved cleanly in JS) -----
// latin-1 is a 1:1 codepoint->byte map for 0-255, so DC2 (0x12) stays 0x12.
function latin1Bytes(str) {
  const bytes = new Uint8Array(str.length);
  for (let i = 0; i < str.length; i++) bytes[i] = str.charCodeAt(i) & 0xff;
  return bytes;
}
function latin1Decode(bytes) {
  let s = "";
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return s;
}

// ----- gzip decompress in the browser (native, no library) -----
async function gunzipToText(arrayBuffer) {
  if (typeof DecompressionStream === "undefined") {
    throw new Error("This browser lacks DecompressionStream — use a recent Chrome/Edge/Firefox.");
  }
  const ds = new DecompressionStream("gzip");
  const stream = new Blob([arrayBuffer]).stream().pipeThrough(ds);
  return await new Response(stream).text();
}

// ----- IndexedDB cache so the 11.6 MB gz is downloaded only once -----
const IDB_NAME = "eqaf", IDB_STORE = "kv", DB_KEY = "items-gz", DB_META_KEY = "items-meta";
function idbOpen() {
  return new Promise((res, rej) => {
    const r = indexedDB.open(IDB_NAME, 1);
    r.onupgradeneeded = () => r.result.createObjectStore(IDB_STORE);
    r.onsuccess = () => res(r.result);
    r.onerror = () => rej(r.error);
  });
}
async function idbGet(key) {
  try {
    const db = await idbOpen();
    return await new Promise((res, rej) => {
      const q = db.transaction(IDB_STORE, "readonly").objectStore(IDB_STORE).get(key);
      q.onsuccess = () => res(q.result || null);
      q.onerror = () => rej(q.error);
    });
  } catch { return null; }
}
async function idbPut(key, val) {
  try {
    const db = await idbOpen();
    await new Promise((res, rej) => {
      const q = db.transaction(IDB_STORE, "readwrite").objectStore(IDB_STORE).put(val, key);
      q.onsuccess = () => res();
      q.onerror = () => rej(q.error);
    });
  } catch { /* best effort */ }
}
async function idbDel(key) {
  try {
    const db = await idbOpen();
    await new Promise((res) => {
      const q = db.transaction(IDB_STORE, "readwrite").objectStore(IDB_STORE).delete(key);
      q.onsuccess = () => res(); q.onerror = () => res();
    });
  } catch { /* best effort */ }
}

// Auto-load the bundled item DB from ../items.txt.gz (one level up — i.e.
// docs/items.txt.gz, served at the Pages root). Cached in IndexedDB so it's
// downloaded only once, but REVALIDATED every load so a shipped DB update is
// picked up automatically: send a conditional request with the cached copy's
// validator (ETag on Pages, Last-Modified when served locally) — unchanged → 304, use
// cache; changed → download the new one. Only works when SERVED (localhost /
// Pages); under file:// fetch is blocked.
async function autoLoadDb({ forceNetwork = false } = {}) {
  $("dbStatus").textContent = "loading…";
  try {
    let buf = forceNetwork ? null : await idbGet(DB_KEY);
    let meta = forceNetwork ? null : await idbGet(DB_META_KEY);   // {etag, lastModified}

    const store = async (resp) => {
      buf = await resp.arrayBuffer();
      meta = { etag: resp.headers.get("ETag"), lastModified: resp.headers.get("Last-Modified") };
      await idbPut(DB_KEY, buf);
      await idbPut(DB_META_KEY, meta);
    };

    if (buf) {
      // Revalidate with exactly ONE validator (ETag preferred). Sending both
      // trips SimpleHTTPRequestHandler, which ignores If-Modified-Since when
      // If-None-Match is present and would then re-send the whole file.
      const headers = {};
      if (meta && meta.etag) headers["If-None-Match"] = meta.etag;
      else if (meta && meta.lastModified) headers["If-Modified-Since"] = meta.lastModified;
      try {
        const resp = await fetch("../items.txt.gz", { headers, cache: "no-store" });
        if (resp.status === 304) {
          log("Item DB: cached copy is current (304, not re-downloaded).");
        } else if (resp.ok) {
          await store(resp);
          log("Item DB: server copy changed — downloaded the update.");
        } else {
          log(`Item DB: revalidation HTTP ${resp.status} — using cached copy.`);
        }
      } catch {
        log("Item DB: offline — using cached copy.");
      }
    } else {
      log("Item DB: first visit, downloading items.txt.gz…");
      const resp = await fetch("../items.txt.gz", { cache: "no-store" });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      await store(resp);
    }

    state.db = parseItemDb(await gunzipToText(buf));
    $("dbStatus").textContent = `${state.db.byName.size} items loaded`;
    $("dbCount").textContent = `DB: ${state.db.byName.size.toLocaleString()} items`;
    log(`Item DB: ${state.db.byName.size} names, ${state.db.byId.size} by id.`);
    // If dumps were loaded before the DB finished, re-aggregate so Type/Slot
    // classification (which needs the DB) is filled in now.
    if (state.toons.length) refreshInventoryFromToons();
  } catch (err) {
    $("dbStatus").textContent = "auto-load failed — serve via localhost";
    log("DB auto-load failed (" + (err && err.message ? err.message : err) +
        "). Under file:// fetch is blocked — open the served app instead.");
  }
}

// ----- spell effect names (app/spell-effects.json.gz, built by tools/build_spells.py) -----
// items.txt.gz stores effects as bare spell IDs and its own *name* columns are empty,
// so without this file the tooltip can only say "Combat Proc". Same download-once +
// revalidate + IndexedDB pattern as the item DB (516 KB gz).
const SPELL_KEY = "spells-gz", SPELL_META_KEY = "spells-meta";

async function loadSpellData() {
  try {
    let buf = await idbGet(SPELL_KEY);
    let meta = await idbGet(SPELL_META_KEY);
    const store = async (resp) => {
      buf = await resp.arrayBuffer();
      meta = { etag: resp.headers.get("ETag"), lastModified: resp.headers.get("Last-Modified") };
      await idbPut(SPELL_KEY, buf);
      await idbPut(SPELL_META_KEY, meta);
    };
    if (buf) {
      // ONE validator only — see autoLoadDb for why sending both breaks the dev server.
      const headers = {};
      if (meta && meta.etag) headers["If-None-Match"] = meta.etag;
      else if (meta && meta.lastModified) headers["If-Modified-Since"] = meta.lastModified;
      const resp = await fetch("spell-effects.json.gz", { headers, cache: "no-store" });
      if (resp.ok && resp.status !== 304) await store(resp);
    } else {
      const resp = await fetch("spell-effects.json.gz", { cache: "no-store" });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      await store(resp);
    }
    state.spells = JSON.parse(await gunzipToText(buf));
    log(`Spell effects: ${Object.keys(state.spells).length} names loaded.`);
  } catch (err) {
    // Non-fatal by design: every effect line falls back to the old type-only label
    // ("Combat Proc"), so a missing/failed spell file degrades instead of breaking.
    state.spells = null;
    log("Spell effect names unavailable (" + (err && err.message ? err.message : err) +
        ") — effects will show as types only. Run tools/build_spells.py to generate them.");
  }
  try {
    const r = await fetch("focus-families.json", { cache: "no-store" });
    if (r.ok) state.focus = await r.json();
  } catch { /* focus filtering just stays unavailable */ }
}

// Effect spell name / description lookup. Returns null when the spell file is absent
// or the id is unknown, so callers keep their type-only fallback.
function spellInfo(id) {
  if (!state.spells || !id) return null;
  const e = state.spells[String(id)];
  return e ? { name: e[0], desc: e.length > 1 ? e[1] : "" } : null;
}

// Focus family + rank for a focus spell id, e.g. {family:"Improved Damage", rank:3}.
// Ranks come from the spell name's trailing Roman numeral (see build_spells.py).
function focusInfo(id) {
  if (!state.focus || !id) return null;
  const e = state.focus.spells[String(id)];
  if (!e) return null;
  const fam = state.focus.families[e[0]];
  return fam ? { family: fam.name, rank: e[1], ranked: fam.ranked } : null;
}

// =====================================================================
// Pricing — TLP-Auctions bulk API (mirrors probe.html / the desktop app)
// =====================================================================

// Are we running from a local dev server? The proxy is a local-only crutch; on
// GitHub Pages we always go direct. **Private LAN addresses count as local**: with
// serve.py bound to 0.0.0.0 (phone on the house wifi) the page is still served by
// serve.py, so it must still use /api — going direct from 192.168.x.x would be
// CORS-blocked exactly like localhost is, and pricing would silently fail.
function isLocalhost() {
  const h = location.hostname;
  return ["localhost", "127.0.0.1", "[::1]"].includes(h) ||
         h.endsWith(".local") ||
         /^10\./.test(h) ||
         /^192\.168\./.test(h) ||
         /^172\.(1[6-9]|2\d|3[01])\./.test(h) ||
         // 100.64/10 — carrier-grade NAT. Some home routers hand these out, and
         // Tailscale uses the same block, so it is a LAN address here in practice.
         /^100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\./.test(h);
}

// Direct = the apex host (valid cert). Proxy = same-origin /api for local dev.
// The TLP API only allows CORS from wangel's own origin, so a browser served from
// a local/forked origin can't call it directly — always route through the bundled
// dev-server proxy (serve.py) on localhost. A deployed origin would go direct.
function apiBase() {
  return isLocalhost() ? "/api" : API_HOST;
}

// Shared request headers. X-Client-App tags our traffic for the API owner; merge
// in any per-call extras (e.g. Content-Type for the POST).
function apiHeaders(extra) {
  return Object.assign(
    { "Accept": "application/json", "X-Client-App": CLIENT_TAG },
    extra || {});
}

// Signed price adjustment % from the slider: negative = undercut (post under the
// median), positive = markup (post over it), 0 = post the median as-is. Clamped
// to a sane band. Blank/invalid = 0.
function adjustPct() {
  const n = parseFloat(($("adjust") || {}).value);
  return Number.isFinite(n) ? Math.max(-90, Math.min(n, 500)) : 0;
}
// "Nice number" rounding for a postable plat price. Items 100p and up snap to the
// nearest 100p (510→500, 780→800, 990→1000, 2990→3000, 12,650→12,700); cheap items
// under 100p snap to the nearest 5p (so 20–80p drops aren't crushed to 0/100).
// Applied as the FINAL step in every price path (batch check, global slider, and
// "apply % to selected"), so posted numbers stay tidy. Krono prices keep their own
// ½-krono rounding.
function niceRound(v) {
  v = Math.round(v);
  if (v <= 0) return 0;
  if (v < 100) return Math.max(Math.round(v / 5) * 5, 5);   // cheap items: nearest 5p
  return Math.round(v / 100) * 100;                         // 100p+: nearest 100p
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// One /prices/bulk call with a single retry on transient failure (the upstream
// occasionally resets a connection mid-run). Returns parsed JSON or throws.
async function fetchBulk(server, itemIds) {
  for (let attempt = 1; attempt <= 2; attempt++) {
    try {
      const resp = await fetch(`${apiBase()}/prices/bulk`, {
        method: "POST",
        headers: apiHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ serverName: server, itemIds }),
      });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      return await resp.json();
    } catch (e) {
      if (attempt === 2) throw e;
      await sleep(500);   // brief backoff, then one retry
    }
  }
}

// Recent postings for MANY items in ONE request: POST /sales/bulk (≤200 ids,
// perItemLimit ≤20, upstream-cached ~5min). Each sale row has the same shape as
// GET /sales (transactionType/platPrice/kronoPrice/datetime), so recentMarket /
// topRecentBid / askVelocity / bidDemand consume it unchanged. Returns
// Map<itemId, sales[]>. Never throws: a failed chunk (after one retry) is logged
// and skipped — its items just get no recent-market read this pass.
async function fetchBulkSales(server, itemIds, perItemLimit = REVIEW_FETCH) {
  const out = new Map();
  for (let i = 0; i < itemIds.length; i += BULK_SALES_LIMIT) {
    const chunk = itemIds.slice(i, i + BULK_SALES_LIMIT);
    for (let attempt = 1; attempt <= 2; attempt++) {
      try {
        const resp = await fetch(`${apiBase()}/sales/bulk`, {
          method: "POST",
          headers: apiHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ serverName: server, itemIds: chunk, perItemLimit }),
        });
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        const data = await resp.json();
        for (const it of data.items || []) out.set(it.itemId, it.sales || []);
        break;
      } catch (e) {
        if (attempt === 2) { log(`  recent-postings batch failed (${e.message}) — review flags skipped for ${chunk.length} item(s)`); break; }
        await sleep(500);
      }
    }
  }
  return out;
}

// Median of a numeric list (avg of the two middles for even length), or null.
function median(nums) {
  if (!nums.length) return null;
  const s = [...nums].sort((a, b) => a - b);
  const m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

// Live krono->plat rate: the 1-day window average (EC krono moves fast, longer
// windows lag). Returns an int, or null on failure. Port of fetch_krono_rate.
async function fetchKronoRate(server) {
  try {
    const resp = await fetch(`${apiBase()}/krono-prices/${encodeURIComponent(server)}/windows`,
      { headers: apiHeaders() });
    if (!resp.ok) return null;
    const data = await resp.json();
    const byDays = {};
    for (const w of data.windows || []) byDays[w.days] = w;
    for (const d of [1, 2, 3, 7]) {            // freshest window that has data
      const w = byDays[d];
      if (w && w.sampleSize > 0 && w.averagePrice > 0) return Math.round(w.averagePrice);
    }
  } catch { /* keep fallback */ }
  return null;
}

// Resolve an EXACT item name to the live-API item id that actually has sales — for
// rows whose dump id isn't in our local DB (items newer than the items.txt snapshot)
// or that had no id at all. `hasSales` in the search result tells us which id to
// take (and skips "Fabled …"-style near-matches). Returns an itemId or null.
async function resolveIdByName(name, server) {
  const qs = new URLSearchParams({ q: name, serverName: server });
  const resp = await fetch(`${apiBase()}/items/search?${qs}`, { headers: apiHeaders() });
  if (!resp.ok) return null;
  const data = await resp.json();
  const exact = (data.items || []).filter((x) => (x.item || "").toLowerCase() === name.toLowerCase());
  const pick = exact.find((x) => x.hasSales) || exact[0];
  return pick && pick.itemId ? pick.itemId : null;
}

// Recent individual postings for ONE item (exact-name match), newest first.
// Port of fetch_recent_sales.
async function fetchRecentSales(name, server, limit = RECENT_SALES_LIMIT) {
  const qs = new URLSearchParams({ searchTerm: name, exactMatch: "true",
    serverName: server, pageSize: String(limit) });
  const resp = await fetch(`${apiBase()}/sales?${qs}`, { headers: apiHeaders() });
  if (!resp.ok) throw new Error("HTTP " + resp.status);
  const data = await resp.json();
  return (data.items || []).slice(0, limit);
}

// Summarize recent WTS asks into a denomination-aware read, or null if <2 priced
// asks. EC combines currencies, so each ask's effective plat is platPrice +
// kronoPrice*rate; krono-dominant when more asks name krono than not. Port of
// _recent_market.
function recentMarket(sales, rate) {
  const wts = sales.filter((s) => !s.transactionType);   // transactionType false = WTS
  const eff = []; let krCount = 0, platCount = 0;
  for (const s of wts) {
    const p = s.platPrice || 0, k = s.kronoPrice || 0;
    if (k > 0) { eff.push(p + k * rate); krCount++; }
    else if (p > 0) { eff.push(p); platCount++; }
  }
  const n = eff.length;
  const effMed = median(eff);
  if (effMed === null || n < 2) return null;
  const isKrono = krCount > platCount;
  const priceStr = isKrono
    ? `${Math.max(Math.round((effMed / rate) * 2) / 2, 0.5)}kr`   // nearest 0.5 krono
    : `${niceRound(effMed)}p`;
  return { effMed, n, isKrono, priceStr };
}

// Age of an ISO timestamp in hours (big number if unparseable).
function ageHours(iso) {
  const t = Date.parse(iso || "");
  return Number.isFinite(t) ? (Date.now() - t) / 3.6e6 : 1e9;
}
// Highest recent WTB (buy) bid within the window, in effective plat. Real demand
// sitting above your post is the strongest "you're underpriced" signal.
function topRecentBid(sales, rate) {
  let top = 0;
  for (const s of sales) {
    if (!s.transactionType) continue;            // WTB only (transactionType true)
    if (ageHours(s.datetime) > BID_WINDOW_H) continue;
    const eff = (s.platPrice || 0) + (s.kronoPrice || 0) * rate;
    if (eff > top) top = eff;
  }
  return Math.round(top);
}

// Supply pressure from the recent WTS asks: how many competing listings, over
// how many days, → asks/day. High = the market is flooded (undercut to move);
// low = scarce (hold your price). Uses the same 25 asks already fetched for the
// review flags, so it costs no extra API call. Returns {askN, askSpanD, askPerDay}.
function askVelocity(sales) {
  const now = Date.now();
  const ages = [];
  for (const s of sales) {
    if (s.transactionType) continue;               // WTS only (false = WTS)
    const t = Date.parse(s.datetime || "");
    if (Number.isFinite(t)) ages.push((now - t) / 8.64e7);   // days old
  }
  const n = ages.length;
  if (!n) return { askN: 0, askSpanD: 0, askPerDay: 0 };
  const span = Math.max(...ages);                   // oldest of the recent asks (newest ≈ 0)
  const perDay = n / Math.max(span, SAT_MIN_SPAN_D);
  return { askN: n, askSpanD: span, askPerDay: perDay };
}

// WTB demand from the SAME recent postings: how many buy bids and how fast. Frequent
// WTB (see DEMAND_* — buyers rarely bother, so it means real demand) → a seller's
// market. Returns {bidN, bidPerDay}.
function bidDemand(sales) {
  const now = Date.now(); const ages = [];
  for (const s of sales) {
    if (!s.transactionType) continue;            // WTB only (true = WTB)
    const t = Date.parse(s.datetime || "");
    if (Number.isFinite(t)) ages.push((now - t) / 8.64e7);
  }
  const n = ages.length;
  if (!n) return { bidN: 0, bidPerDay: 0 };
  return { bidN: n, bidPerDay: n / Math.max(Math.max(...ages), SAT_MIN_SPAN_D) };
}

// Read the "Recent pricing" toggle (default ON when the box is absent).
function useRecentPricing() {
  const el = $("useRecent");
  return el ? el.checked : true;
}

// Resolve one bulk result into a postable price from the prefetched recent sales
// (ONE /sales/bulk call covers the whole check — no per-item fetch), and gather
// recent-market signals (recent WTS ask median + top live WTB bid) so the row can
// flag under/over-pricing vs whatever price ends up posted. High-value items
// trading in krono swap to a krono price. When "Recent pricing" is on and enough
// recent asks (≥ADOPT_MIN_N) diverge ≥ADOPT_PCT% from the lagging full-history
// median, the recent median becomes the price base instead (adoptedBase set, so
// the caller can point the live slider at it).
function resolvePrice(r, rate, pct, sales) {
  const med = Math.round(r.medianPlatPrice);
  // pct is SIGNED: +10 = mark up 10%, -5 = undercut 5%, 0 = post the median as-is.
  const platStr = `${Math.max(niceRound(pct ? med * (1 + pct / 100) : med), 5)}p`;
  const flat = { priceStr: platStr, krono: false, recentMed: null, recentN: 0, topBid: 0, adoptedBase: null };
  if (!sales || !sales.length) return flat;
  const mk = recentMarket(sales, rate);
  const topBid = topRecentBid(sales, rate);
  const vel = askVelocity(sales);          // supply pressure (asks/day) — same fetch
  const dem = bidDemand(sales);            // WTB demand (bids/day) — same fetch
  if (!mk) return { ...flat, topBid, ...vel, ...dem };
  if (mk.isKrono) return { priceStr: mk.priceStr, krono: true, recentMed: null, recentN: mk.n, topBid, adoptedBase: null, ...vel, ...dem };
  const recentMed = Math.round(mk.effMed);
  // Divergence adoption: price from the live market, not the stale median.
  if (useRecentPricing() && mk.n >= ADOPT_MIN_N && med > 0 &&
      Math.abs(recentMed - med) / med >= ADOPT_PCT / 100) {
    const adoptedStr = `${Math.max(niceRound(pct ? recentMed * (1 + pct / 100) : recentMed), 5)}p`;
    return { priceStr: adoptedStr, krono: false, recentMed, recentN: mk.n, topBid, adoptedBase: recentMed, ...vel, ...dem };
  }
  return { priceStr: platStr, krono: false, recentMed, recentN: mk.n, topBid, adoptedBase: null, ...vel, ...dem };
}

// Price-check every inventory item that has an id: ONE /sales/bulk call for the
// recent postings (review flags + recent-market pricing for the WHOLE list), then
// POST /prices/bulk in batches of <=10 for the history medians, then resolve each
// (median or adopted recent base, %-adjusted; krono swap when recent asks trade in
// krono). id-keyed, so items with no id are skipped. A failed batch is retried
// once then skipped (not fatal). Port of _price_check_all + _resolve_price.
async function priceItems(items) {
  const server = SERVER;

  const rowsById = new Map();   // itemId -> [auction rows sharing that id]
  for (const item of items) {
    if (!item.id) continue;
    if (!rowsById.has(item.id)) rowsById.set(item.id, []);
    rowsById.get(item.id).push(item);
  }
  const ids = [...rowsById.keys()];
  if (!ids.length) { log("Price check: no auction items have an id to look up (type prices by hand)."); return; }

  const pct = adjustPct();
  const adjLbl = pct ? `, ${pct > 0 ? "markup" : "undercut"} ${Math.abs(pct)}%` : "";
  const btns = [$("pcBtn"), $("pcSelBtn")];
  btns.forEach((b) => b && (b.disabled = true));
  setStatus("Price check: starting…");
  const batches = Math.ceil(ids.length / BULK_PRICE_LIMIT);
  log(`Price check: ${ids.length} item(s) on ${server} in ${batches} request(s)` +
      adjLbl + "…");

  // Live 1-day krono rate up front (folds krono asks in the recent-asks read).
  let rate = await fetchKronoRate(server) || 0;

  // Recent postings for EVERY id in one /sales/bulk pass (was: one /api/sales call
  // per ≥1000p item, 60/min-capped). Feeds the review/saturation/demand flags AND
  // recent-market pricing for the whole list, cheap items included.
  setStatus(`Price check: recent postings for ${ids.length} item(s)…`);
  const salesById = await fetchBulkSales(server, ids);

  let priced = 0, noData = 0, krono = 0, failed = 0, batchErr = 0;
  const under = [], over = [], adopted = [];
  try {
    for (let i = 0; i < ids.length; i += BULK_PRICE_LIMIT) {
      const batch = ids.slice(i, i + BULK_PRICE_LIMIT);
      let data = null;
      try {
        data = await fetchBulk(server, batch);
      } catch (e) {
        batchErr++; failed += batch.length;
        log(`  batch ${Math.floor(i / BULK_PRICE_LIMIT) + 1}/${batches} failed (${e.message}) — skipped`);
      }
      if (data) {
        if (!rate && data.kronoRate) rate = data.kronoRate;   // fall back to the bulk rate
        for (const r of data.items || []) {
          const rows = rowsById.get(r.itemId);
          if (!rows) continue;
          if (!(r.hasData && r.medianPlatPrice > 0)) {
            noData++;
            // Mark it so the row can say "— no sales" instead of a blank box, and
            // so a no-market item with an NPC value flags as "vendor it".
            for (const it of rows) { if (!it._manual) { it._noData = true; it._autoPriced = false; } }
            continue;
          }
          const res = resolvePrice(r, rate || DEFAULT_KRONO_RATE, pct, salesById.get(r.itemId));
          const med = Math.round(r.medianPlatPrice);
          for (const it of rows) {
            // Recent-market signals apply to EVERY row (incl. hand-priced) so the
            // review flag reflects the real market vs whatever price is posted.
            it._recentMed = res.recentMed; it._recentN = res.recentN; it._topBid = res.topBid;
            it._askPerDay = res.askPerDay || 0; it._askN = res.askN || 0; it._askSpanD = res.askSpanD || 0;
            it._bidN = res.bidN || 0; it._bidPerDay = res.bidPerDay || 0;
            if (it._manual) continue;   // never clobber a hand-typed price
            it.price = res.priceStr;
            it._noData = false;
            // Slider math runs off _median, so an adopted recent base drives it too;
            // _lastMedian stays the API history median (Recent Postings hint ref).
            it._lastMedian = med; it._median = res.adoptedBase || med;
            it._autoPriced = !res.krono;                            // krono prices aren't %-adjusted live
            if (it._priceInput) it._priceInput.value = res.priceStr;
          }
          priced++;
          if (res.krono) krono++;
          if (res.adoptedBase && !rows[0]._manual) adopted.push({ name: rows[0].name, from: med, to: rows[0].price });
          const f = reviewFlag(rows[0]);
          if (f === "under") under.push({ name: rows[0].name, you: rows[0].price, recent: res.recentMed, bid: res.topBid });
          else if (f === "over") over.push({ name: rows[0].name, you: rows[0].price, recent: res.recentMed });
        }
      }
      setStatus(`Price check: ${Math.min(i + BULK_PRICE_LIMIT, ids.length)}/${ids.length}…`);
      await sleep(120);   // gentle pacing between batches
    }
    // ---- Fallback pricing for rows the id pass couldn't price, resolved BY EXACT NAME ----
    // Three real cases: (a) EQ stamped a variant/duplicate item id that TLP has no
    // sales under while the same NAME sells under another id ("Metal Pipe" 12980 in
    // your bags vs 12979, 1,100+ sales); (b) the dump row had no id at all; (c) the
    // item is newer than our items.txt snapshot. For each still-unpriced row we gather
    // candidate ids by exact name — from the local DB first (free), then the live-API
    // name search for names our snapshot doesn't know — then bulk-price and adopt.
    const unpriced = items.filter((it) => !it._manual && !it._median && classifyPrice(it.price)[0] !== "krono");
    if (unpriced.length && state.db) {
      const byNameIds = state.db.byNameIds || new Map();
      const triedIds = new Set(ids);                     // already bulk-priced above → don't re-fetch
      const nameRows = new Map();                        // name(lower) -> rows waiting on it
      for (const it of unpriced) { const k = it.name.toLowerCase(); if (!nameRows.has(k)) nameRows.set(k, []); nameRows.get(k).push(it); }
      const idRows = new Map();                          // NEW candidate id -> rows hoping it has sales
      const addId = (id, rows) => { if (triedIds.has(id)) return; if (!idRows.has(id)) idRows.set(id, []); const b = idRows.get(id); for (const it of rows) if (!b.includes(it)) b.push(it); };
      const apiQueue = [];                               // names the local DB doesn't know → ask the server
      for (const [k, rows] of nameRows) {
        const dbIds = byNameIds.get(k) || [];
        if (dbIds.length) for (const id of dbIds) addId(id, rows);
        else apiQueue.push({ name: rows[0].name, rows });
      }
      // Look up unknown names on the live server (bounded + paced for the 60/min limit).
      let looked = 0; const API_MAX = 50;
      if (apiQueue.length) log(`  ${apiQueue.length} unpriced name(s) not in the local DB — looking up current ids on ${server}…`);
      for (const { name, rows } of apiQueue) {
        if (looked >= API_MAX) { log(`  (looked up ${API_MAX} names; run PC All again to resolve the rest)`); break; }
        looked++;
        let id = null;
        try { id = await resolveIdByName(name, server); } catch { /* skip this name */ }
        if (id) addId(id, rows);
        await sleep(150);
      }
      // Bulk-price the fresh candidate ids and adopt the median for their rows.
      const candIds = [...idRows.keys()];
      let recovered = 0;
      const candSales = candIds.length ? await fetchBulkSales(server, candIds) : new Map();
      for (let i = 0; i < candIds.length; i += BULK_PRICE_LIMIT) {
        let data = null;
        try { data = await fetchBulk(server, candIds.slice(i, i + BULK_PRICE_LIMIT)); } catch { continue; }
        for (const r of (data && data.items) || []) {
          if (!(r.hasData && r.medianPlatPrice > 0)) continue;
          const bucket = (idRows.get(r.itemId) || []).filter((it) => !it._median && !it._manual);
          if (!bucket.length) continue;                  // already filled by an earlier candidate
          const med = Math.round(r.medianPlatPrice);
          const res = resolvePrice(r, rate || DEFAULT_KRONO_RATE, pct, candSales.get(r.itemId));
          for (const it of bucket) {
            const wasNoData = it._noData;
            it._recentMed = res.recentMed; it._recentN = res.recentN; it._topBid = res.topBid;
            it._askPerDay = res.askPerDay || 0; it._askN = res.askN || 0; it._askSpanD = res.askSpanD || 0;
            it._bidN = res.bidN || 0; it._bidPerDay = res.bidPerDay || 0;
            it.price = res.priceStr; it._noData = false;
            it._lastMedian = med; it._median = res.adoptedBase || med; it._autoPriced = !res.krono;
            if (it._priceInput) it._priceInput.value = res.priceStr;
            priced++; recovered++; if (wasNoData) noData = Math.max(0, noData - 1);
            if (res.krono) krono++;
          }
          if (res.adoptedBase && bucket.length) adopted.push({ name: bucket[0].name, from: med, to: bucket[0].price });
        }
        await sleep(120);
      }
      if (recovered) log(`  ✔ Recovered pricing for ${recovered} item(s) by exact name` + (looked ? ` (${looked} via live id lookup)` : "") + ".");
    }
    log(`Price check complete: ${priced} priced` + (krono ? ` (${krono} krono)` : "") +
        `, ${noData} no data` +
        (failed ? `, ${failed} failed in ${batchErr} batch(es)` : "") +
        adjLbl +
        (rate ? ` (krono rate ~${Math.round(rate)}p)` : "") + ".");
    if (adopted.length) {
      log(`⟳ ${adopted.length} item(s) priced from RECENT asks (history median lagging ≥${ADOPT_PCT}%):`);
      for (const a of adopted.slice(0, 12)) log(`  ${a.name}: median ${a.from.toLocaleString()}p → posted ${a.to}`);
      if (adopted.length > 12) log(`  …and ${adopted.length - 12} more.`);
    }
    if (under.length) {
      log(`⚠ ${under.length} item(s) likely UNDERPRICED — recent asks/bids are higher. Use "Review" to see them, then reprice up:`);
      for (const d of under) log(`  ${d.name}: you ${d.you} · recent asks ~${d.recent ? d.recent + "p" : "?"}${d.bid ? ` · buyer bid ${d.bid.toLocaleString()}p` : ""}`);
    }
    if (over.length) {
      log(`${over.length} item(s) priced above recent asks (maybe overpriced): ` +
          over.slice(0, 8).map((d) => `${d.name} (you ${d.you} / recent ~${d.recent}p)`).join(", ") + (over.length > 8 ? "…" : ""));
    }
    if (failed) {
      if (priced === 0 && noData === 0 && !(($("useProxy") || {}).checked)) {
        log("  → Every batch failed. Direct calls only work from the deployed " +
            "origin (CORS) — for local dev, serve the app and tick 'Use local proxy'.");
      } else {
        log("  → Some batches hit a transient API error. Run Price Check All again to fill the rest.");
      }
    }
  } catch (e) {
    log("Price check error: " + (e && e.message ? e.message : e));
  } finally {
    btns.forEach((b) => b && (b.disabled = false));
  }
  state.kronoRate = rate || state.kronoRate;   // remember for the Recent Postings hint
  refreshAuction();   // rebuild rows so prices/flags (and coloring) reflect the check
}

async function priceCheckAll() {
  if (!state.auction.length) return;
  track("pc_all");
  await priceItems(state.auction);
}

async function priceCheckSelected() {
  if (!state.aucSel.size) { log("Select auction row(s) to price-check, or use PC All."); return; }
  await priceItems([...state.aucSel].map((i) => state.auction[i]));
}

// Apply the current Price-adjust % to ONLY the selected sell rows, off each row's
// stored median — same math as the global slider (niceRound(median × (1+pct/100)),
// floor 5p), just scoped to the selection. Then hold those rows (mark _manual) so the
// global slider won't move them afterward: e.g. leave the slider at 0%, select
// spells/songs, and bump just them to +100%. Re-applying recomputes from the median
// (no compounding). Krono rows and rows with no median (no market data) are skipped.
function applyAdjustToSelected() {
  if (!state.aucSel.size) { log("Select sell-list row(s) first, then Apply % to selected."); return; }
  const adj = adjustPct();
  let n = 0, skipped = 0;
  [...state.aucSel].forEach((i) => {
    const it = state.auction[i];
    if (!it) return;
    if (!it._median || classifyPrice(it.price)[0] === "krono") { skipped++; return; }
    it.price = `${Math.max(niceRound(it._median * (1 + adj / 100)), 5)}p`;
    it._manual = true; it._autoPriced = false;   // hold this price; the global slider won't touch it
    n++;
  });
  refreshAuction();
  log(`Applied ${adj >= 0 ? "+" : ""}${adj}% to ${n} selected row(s)` +
      (skipped ? `, skipped ${skipped} (krono / no median — price-check them first)` : "") + ".");
}

// ===================================================================
// Recent Postings (/api/sales viewer) + modal
// ===================================================================

// ISO UTC -> "MM/DD hh:mmAM (Nm/h/d ago)". Port of format_sale_age.
function formatSaleAge(iso) {
  const dt = new Date(iso);
  if (isNaN(dt.getTime())) return iso || "?";
  const secs = Math.floor((Date.now() - dt.getTime()) / 1000);
  const ago = secs < 3600 ? `${Math.max(Math.floor(secs / 60), 0)}m ago`
    : secs < 86400 ? `${Math.floor(secs / 3600)}h ago`
      : `${Math.floor(secs / 86400)}d ago`;
  const mo = String(dt.getMonth() + 1).padStart(2, "0"), da = String(dt.getDate()).padStart(2, "0");
  let h = dt.getHours(); const ap = h < 12 ? "AM" : "PM"; h = h % 12 || 12;
  const mi = String(dt.getMinutes()).padStart(2, "0");
  return `${mo}/${da} ${String(h).padStart(2, "0")}:${mi}${ap} (${ago})`;
}
// One posting is in plat OR krono. Port of format_posting_price.
function formatPostingPrice(plat, krono) {
  if (krono && krono > 0) return `${krono}kr`;
  if (plat && plat > 0) return `${Math.trunc(plat)}p`;
  return "—";
}

// ----- generic modal (web equivalent of the desktop's Toplevel windows) -----
function openModal(title, bodyNode) {
  $("modalTitle").textContent = title;
  const body = $("modalBody"); body.innerHTML = ""; body.appendChild(bodyNode);
  $("modal").hidden = false;
}
function closeModal() { $("modal").hidden = true; $("modalBody").innerHTML = ""; }

// Show the accumulated activity log in the modal (the log element is hidden;
// this is the on-demand viewer reached via the fixed Log button).
function showLog() {
  const pre = document.createElement("pre");
  pre.className = "postings";
  pre.style.maxHeight = "65vh";
  pre.style.whiteSpace = "pre-wrap";
  pre.textContent = ($("log").textContent || "").trim() || "(nothing logged yet)";
  openModal("Activity log", pre);
  requestAnimationFrame(() => { pre.scrollTop = pre.scrollHeight; });   // newest at bottom
}

// ----- header: live krono rate (+ Sync) and Help -----
async function syncKrono() {
  const server = SERVER;
  const btn = $("syncKronoBtn"); if (btn) btn.disabled = true;
  setStatus("Syncing krono rate…");
  try {
    const rate = await fetchKronoRate(server);
    if (rate) {
      state.kronoRate = rate;
      const t = new Date(); let h = t.getHours(); const ap = h < 12 ? "am" : "pm"; h = h % 12 || 12;
      $("kronoInfo").textContent = `krono ~${rate.toLocaleString()}p · synced @ ${h}:${String(t.getMinutes()).padStart(2, "0")}${ap}`;
      log(`Krono rate: ${rate.toLocaleString()}p/kr (1-day avg).`);
    } else {
      $("kronoInfo").textContent = `krono ~${(state.kronoRate || DEFAULT_KRONO_RATE).toLocaleString()}p (sync failed)`;
      log("Krono sync failed — using fallback rate.");
    }
  } finally { if (btn) btn.disabled = false; }
}

function showHelp() {
  const d = document.createElement("div");
  d.className = "help-body";
  const chip = (color, label) => `<span style="color:${color};font-weight:600">${label}</span>`;
  d.innerHTML =
    "<h4>Quick start</h4>" +
    "<ol>" +
    "<li>In EQ, <code>/outputfile inventory</code> on each toon. <strong>Load</strong> all the files (step 1) — they merge into one list across every character.</li>" +
    "<li>Pick items on the left (the location toggles hide worn/bank/etc.), then <strong>Add Selected &rarr;</strong> — or add everything.</li>" +
    "<li><strong>PC All</strong> to price from live TLP-Auctions data. Set the <strong>Price adjust</strong> slider / <strong>CHA</strong> / <strong>Min profit</strong> first if you like.</li>" +
    "<li><strong>Generate</strong> the macro (splits into Spell / Gear / Misc clickable-link buttons), then <strong>Write to INI</strong> (Chrome/Edge) — <em>close EQ first</em>.</li>" +
    "</ol>" +

    "<h4>How pricing works</h4>" +
    "<ul>" +
    "<li>Prices are TLP-Auctions' <strong>full-history median</strong> per item (krono-normalized, rounded to a clean number).</li>" +
    "<li>The <strong>Price adjust</strong> slider marks every auto-priced row up/down off its median, live. <strong>Freeze</strong> one item (type a price, <strong>Apply % &rarr; Sel</strong>, or the <strong>Selected</strong> slider) and a " + chip("#e2e8f0", "❄") + " marks it — the global slider skips it until you click the ❄ to reset.</li>" +
    "<li>Items <strong>≥ 1,000p</strong> also get a recent-asks lookup that powers the flags below.</li>" +
    "<li>“— no sales” = no market on Frostreaver. If a dump's item id has no sales, the app auto-finds the right id <strong>by exact name</strong> (fixes duplicate/variant ids and items newer than the bundled DB).</li>" +
    "</ul>" +

    "<h4>Reading the flags <span class='hint'>(hover any price for the full read)</span></h4>" +
    "<ul class='flag-list'>" +
    "<li>" + chip("var(--under)", "sells higher") + " — recent asks/bids sit above your price → <strong>reprice up</strong> (the median lags, especially on spells).</li>" +
    "<li>" + chip("var(--demand)", "📈 in demand") + " — heavy WTB buy-spam vs asks (krono, gems, hot rares) → <strong>hold or price up</strong>; buyers will PM you.</li>" +
    "<li>" + chip("var(--danger)", "🔥 flooded & you're high") + " — tons of fresh competing asks AND you're above the pack → <strong>undercut, or re-post to stay recent</strong>. Drop to/under the market median and it clears.</li>" +
    "<li>" + chip("var(--accent)", "🟢 thin") + " — few asks over many days → scarce, <strong>hold your price</strong>.</li>" +
    "<li>" + chip("var(--muted)", "recent asks lower") + " — you may be <strong>overpriced</strong> vs recent asks.</li>" +
    "<li>" + chip("var(--krono)", "krono") + " priced in krono · " + chip("var(--warn)", "vendor it") + " worth more to an NPC (cut from the macro) · " + chip("#e2e8f0", "❄ frozen") + " you pinned the price (click to reset).</li>" +
    "</ul>" +

    "<h4>Selling tips (how the EQ market really works)</h4>" +
    "<ul>" +
    "<li>Buyers <strong>rarely type WTB</strong>. They browse the TLP-Auctions site + the in-game auction spam, then PM the <strong>cheapest or most-recent</strong> seller. So on flooded items, being cheapest <em>or</em> re-posting to stay on top both work.</li>" +
    "<li><strong>Undercut</strong> the 🔥 flooded stuff. <strong>Hold</strong> the 🟢 thin and 📈 in-demand stuff — don't leave money on the table.</li>" +
    "<li>Krono and gems are almost always in demand.</li>" +
    "</ul>" +

    "<h4>Finding &amp; moving items to sell / parcel</h4>" +
    "<ul>" +
    "<li>The <strong>WHERE</strong> column shows which toon and exact slot holds each item — across your whole roster at once (handier than EQ's Find Item window when you box).</li>" +
    "<li>In game you can also use the <strong>Find Item</strong> window (or <code>/finditem</code>) on a toon to locate something, then <strong>parcel</strong> it to the one toon you post from.</li>" +
    "<li><strong>Workflow:</strong> decide what to sell → WHERE tells you which toons to pull from → parcel them to your selling toon → Generate + Write the macro on that toon.</li>" +
    "<li>Hover an item name for quick links: " + chip("var(--info)", "↗") + " TLP-Auctions (live price) and " + chip("var(--accent)", "AG/ZAM") + " (item details + Frostreaver farming zones).</li>" +
    "</ul>" +

    "<p class='hint'>Filters: Type / Slot / Class / Race / <strong>Stat</strong> (gear — AC, HP, haste, resists…). <strong>Exclude forever</strong> hides storage bags &amp; junk. <strong>Look up any item</strong> checks the market for things you don't own. Everything stays on your machine — only item price lookups hit TLP-Auctions.</p>";
  openModal("EQ Forge — Help & Tips", d);
}

// ----- preferences: persist the toolbar inputs (the lightweight "Settings") -----
// Saved values seed the boxes next session, exactly like the desktop's defaults.
const PREFS_KEY = "eqaf-prefs";
const PREF_IDS = ["adjust", "cha", "minProfit", "prefix", "page", "suffix"];
function savePrefs() {
  const p = {};
  for (const id of PREF_IDS) { const el = $(id); if (el) p[id] = el.value; }
  p.locs = [...state.filters.locs];   // persist the chosen location toggles
  p.useRecent = !!(($("useRecent") || {}).checked);   // checkbox → .checked, not .value
  try { localStorage.setItem(PREFS_KEY, JSON.stringify(p)); } catch { /* private mode etc. */ }
}
function loadPrefs() {
  let p;
  try { p = JSON.parse(localStorage.getItem(PREFS_KEY) || "{}"); } catch { p = {}; }
  for (const id of PREF_IDS) { if (p[id] !== undefined && $(id)) $(id).value = p[id]; }
  if (typeof p.useRecent === "boolean" && $("useRecent")) $("useRecent").checked = p.useRecent;
  if (Array.isArray(p.locs)) state.filters.locs = new Set(p.locs.filter((b) => LOC_BUCKETS.includes(b)));
}

// ----- watchlist: items to be alerted on when someone WTSs them in EC tunnel ---
// Stored as canonical item names (free text allowed; autocomplete suggests DB
// names). Persisted locally, like prefs — nothing leaves the machine. The match
// engine is docs/app/watchlist.js (WL.watchlistHits), parity-locked to desktop.
const WATCHLIST_KEY = "eqaf-watchlist";

function loadWatchlist() {
  let arr;
  try { arr = JSON.parse(localStorage.getItem(WATCHLIST_KEY) || "[]"); } catch { arr = []; }
  state.watchlist = Array.isArray(arr) ? arr.filter((x) => typeof x === "string" && x.trim()) : [];
}
function saveWatchlist() {
  try { localStorage.setItem(WATCHLIST_KEY, JSON.stringify(state.watchlist)); } catch { /* private mode */ }
}

function addToWatchlist(name) {
  const n = (name || "").trim();
  if (n.length < 2) return false;
  if (state.watchlist.some((x) => x.toLowerCase() === n.toLowerCase())) {  // case-insensitive dedupe
    setStatus(`"${n}" is already on your watchlist.`);
    return false;
  }
  state.watchlist.push(n);
  state.watchlist.sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));
  saveWatchlist();
  renderWatchlist();
  setStatus(`Added "${n}" to watchlist.`);
  log(`Watchlist + ${n}`);
  return true;
}

function removeFromWatchlist(name) {
  const i = state.watchlist.findIndex((x) => x === name);
  if (i === -1) return;
  state.watchlist.splice(i, 1);
  saveWatchlist();
  renderWatchlist();
  setStatus(`Removed "${name}" from watchlist.`);
  log(`Watchlist − ${name}`);
}

function renderWatchlist() {
  const box = $("wlList");
  if (!box) return;
  box.innerHTML = "";
  if (!state.watchlist.length) {
    const empty = document.createElement("span");
    empty.className = "hint";
    empty.textContent = "No items yet — add items you want to be alerted about.";
    box.appendChild(empty);
    return;
  }
  for (const name of state.watchlist) {
    const chip = document.createElement("span");
    chip.className = "wl-chip";
    const label = document.createElement("span");
    label.textContent = name;
    const x = document.createElement("button");
    x.type = "button";
    x.className = "wl-x";
    x.title = `Remove ${name}`;
    x.textContent = "×";
    x.addEventListener("click", () => removeFromWatchlist(name));
    chip.appendChild(label);
    chip.appendChild(x);
    box.appendChild(chip);
  }
}

// Populate the autocomplete datalist with up to 20 DB names matching the current
// input (a full 133k-option datalist would be unusably slow).
function updateWatchlistAutocomplete() {
  const dl = $("wlNames");
  if (!dl || !state.db) return;
  const q = $("wlInput").value.trim().toLowerCase();
  dl.innerHTML = "";
  if (q.length < 2) return;
  let added = 0;
  for (const n of state.db.byName.keys()) {
    if (n.toLowerCase().includes(q)) {
      const opt = document.createElement("option");
      opt.value = n;
      dl.appendChild(opt);
      if (++added >= 20) break;
    }
  }
}

// ----- view toggle: Macro Builder <-> Live Monitor (shared state, one page) -----
const VIEW_KEY = "eqaf-view";
function setView(mode) {
  const monitor = mode === "monitor";
  if ($("builderView")) $("builderView").hidden = monitor;
  if ($("monitorView")) $("monitorView").hidden = !monitor;
  if ($("tabBuilder")) $("tabBuilder").classList.toggle("active", !monitor);
  if ($("tabMonitor")) $("tabMonitor").classList.toggle("active", monitor);
  try { localStorage.setItem(VIEW_KEY, mode); } catch { /* private mode */ }
  if (monitor) updateMonitorInvNote();
}
// SELL matching needs the inventory; surface its state in the monitor view so the
// dependency is obvious (and offer to load it without switching back to Builder).
function updateMonitorInvNote() {
  const el = $("wlInvNote");
  if (!el) return;
  const n = state.inventory.length;
  el.textContent = n ? `Inventory loaded: ${n} items — SELL alerts active.`
                     : "SELL alerts (a buyer for your gear) need your inventory loaded.";
  el.style.color = n ? "var(--green)" : "";
}

// ----- log tailer: watchlist alerts from your own EQ log (visible-tab feature) --
// A browser tab only runs full-rate timers while VISIBLE; hidden/minimized it's
// throttled hard (measured 46s gaps). So this is honest about its state via the
// live/paused banner rather than pretending to work in the background. Reading is
// incremental (seek to the last byte offset), so even a multi-GB log is cheap.
const LOG_POLL_MS = 3000;
const NOTIFY_COOLDOWN_MS = 60000;   // don't re-toast the same item within a minute
const lastNotify = new Map();       // item name -> last OS-notification timestamp

function notifyReady() {
  return typeof Notification !== "undefined" && Notification.permission === "granted";
}
const TELLPING_KEY = "eqaf-tell-pings";
function tellPingsOn() { const el = $("tellPing"); return el ? el.checked : true; }   // default on

// Explicit "does this work?" button — also the clean way to trigger the browser's
// permission prompt (auto-requesting on Start is easy to dismiss without noticing).
async function testAlert() {
  if (typeof Notification === "undefined") { setStatus("This browser has no notifications API."); return; }
  let perm = Notification.permission;
  if (perm === "default") perm = await Notification.requestPermission();
  if (perm === "granted") {
    new Notification("EQ Auction Forge — test alert", {
      body: "Notifications work. You'll get one of these when a watchlist item is up for sale.",
    });
    setStatus("Test notification sent.");
  } else {
    setStatus("Notifications are blocked — click the padlock in the address bar → allow notifications for this site.");
  }
}

// Tiny IndexedDB key/value store, just to persist the FileSystemFileHandle so the
// user doesn't re-pick the log every session (localStorage can't hold a handle).
function idbOpen() {
  return new Promise((res, rej) => {
    const r = indexedDB.open("eqaf", 1);
    r.onupgradeneeded = () => r.result.createObjectStore("kv");
    r.onsuccess = () => res(r.result);
    r.onerror = () => rej(r.error);
  });
}
async function idbGet(key) {
  const db = await idbOpen();
  return new Promise((res, rej) => {
    const q = db.transaction("kv", "readonly").objectStore("kv").get(key);
    q.onsuccess = () => res(q.result); q.onerror = () => rej(q.error);
  });
}
async function idbPut(key, val) {
  const db = await idbOpen();
  return new Promise((res, rej) => {
    const tx = db.transaction("kv", "readwrite");
    tx.objectStore("kv").put(val, key);
    tx.oncomplete = () => res(); tx.onerror = () => rej(tx.error);
  });
}

async function pickLogFile() {
  if (!window.showOpenFilePicker) { setStatus("Live alerts need Chrome/Edge (File System Access)."); return; }
  try {
    const [h] = await window.showOpenFilePicker({ multiple: false });
    state.logHandle = h;
    try { await idbPut("logHandle", h); } catch { /* private mode */ }
    $("wlToggle").disabled = false;
    setStatus(`Log file: ${h.name}. Click Start monitoring.`);
    updateLogName();
  } catch { /* user cancelled */ }
}

// On load, re-attach the saved handle (permission is re-checked on Start, which is
// the user gesture the browser requires to re-grant file access).
async function restoreLogHandle() {
  if (!window.showOpenFilePicker) return;
  try {
    const h = await idbGet("logHandle");
    if (h) { state.logHandle = h; $("wlToggle").disabled = false; updateLogName(); }
  } catch { /* ignore */ }
}

function updateLogName() {
  const el = $("wlLogName");
  if (el) el.textContent = state.logHandle ? state.logHandle.name : "no log file picked";
}

async function startMonitoring() {
  if (!state.logHandle) { setStatus("Pick your EQ log file first."); return; }
  const perm = await state.logHandle.requestPermission({ mode: "read" });
  if (perm !== "granted") { setStatus("Permission to read the log was denied."); return; }
  if (typeof Notification !== "undefined" && Notification.permission === "default") {
    await Notification.requestPermission();
  }
  // Build the IDF index for SELL matching once (from the loaded DB). Skipped if
  // the DB isn't ready — BUY/watchlist matching doesn't need it.
  if (state.db && !state.idf) {
    const t0 = performance.now();
    state.idf = WL.buildIdf([...state.db.byName.keys()]).idf;
    state.aliasPats = WL.compileAliases(WL.DEFAULT_ALIASES);
    log(`Built match index: ${state.idf.size} tokens in ${Math.round(performance.now() - t0)}ms.`);
  }
  const f = await state.logHandle.getFile();
  state.logSize = f.size;          // tail from END — no history replay
  state.monitoring = true;
  state.lastCheckAt = Date.now();
  state.logTimer = setInterval(logTick, LOG_POLL_MS);
  $("wlToggle").textContent = "Stop monitoring";
  $("wlFeed").hidden = false;
  updateWlBanner();
  setStatus(`Monitoring ${state.logHandle.name} for watchlist sales.`);
  log(`Watchlist monitor: tailing ${state.logHandle.name} from end (${f.size} bytes).`);
}

function stopMonitoring() {
  state.monitoring = false;
  if (state.logTimer) { clearInterval(state.logTimer); state.logTimer = null; }
  $("wlToggle").textContent = "Start monitoring";
  updateWlBanner();
  updateMonitorTitle();   // clear the paused-tab tag
  setStatus("Stopped monitoring.");
}

// catchUp=true marks a post-background backlog read: lines still go to the feed
// (so you see what you missed) but they DON'T toast — a stale 3-min-old auction
// you can't act on shouldn't ping. Returns how many matches it added.
async function logTick(catchUp = false) {
  let added = 0;
  try {
    const f = await state.logHandle.getFile();
    if (f.size < state.logSize) state.logSize = 0;       // file rotated/truncated
    if (f.size > state.logSize) {
      const buf = await f.slice(state.logSize).arrayBuffer();
      state.logSize = f.size;
      const text = new TextDecoder("latin1").decode(buf);   // EQ logs are ANSI
      const candidates = state.inventory.map((i) => i.name);
      for (const raw of text.split(/\r?\n/)) {
        const tell = WL.parseTellLine(raw);            // a direct /tell to me → feed + ping
        if (tell) { addTell(tell.speaker, tell.msg, raw, catchUp); added++; continue; }
        const parsed = WL.parseAuctionLine(raw);
        if (!parsed) continue;
        const leads = WL.matchLine(parsed.msg, {
          candidates, idf: state.idf, aliasPats: state.aliasPats, watchlist: state.watchlist,
        });
        for (const lead of leads) { addLead(lead, parsed.speaker, parsed.msg, raw, catchUp); added++; }
      }
    }
    state.lastCheckAt = Date.now();
  } catch (e) {
    log("Watchlist monitor read error: " + e);
  }
  updateWlBanner();
  return added;
}

// ----- silenced auctioneers (mute a spammer; still show them, greyed) ----------
const SILENCED_KEY = "eqaf-silenced";
function loadSilenced() {
  try { state.silenced = new Set(JSON.parse(localStorage.getItem(SILENCED_KEY) || "[]").map((s) => String(s).toLowerCase())); }
  catch { state.silenced = new Set(); }
}
function isSilenced(who) { return state.silenced.has(String(who).toLowerCase()); }
function setSilenced(who, on) {
  const k = String(who).toLowerCase();
  if (on) state.silenced.add(k); else state.silenced.delete(k);
  try { localStorage.setItem(SILENCED_KEY, JSON.stringify([...state.silenced])); } catch { /* private mode */ }
  setStatus(`${on ? "Silenced" : "Unsilenced"} ${who}.`);
}

function copyText(s) {
  if (navigator.clipboard) navigator.clipboard.writeText(s).then(
    () => setStatus(`Copied: ${s.trim()}`), () => setStatus("Copy failed (clipboard blocked)."));
  else setStatus("Clipboard not available in this browser.");
}

function closeFeedMenu() { const m = document.querySelector(".ctx-menu"); if (m) m.remove(); }

// Right-click a feed row -> desktop-style menu: copy the /tell, copy the item,
// silence/unsilence the auctioneer, and (BUY rows) remove the item from the list.
function showFeedMenu(ev, row) {
  ev.preventDefault();
  closeFeedMenu();
  const { speaker, item, kind, term } = row.dataset;
  const menu = document.createElement("div");
  menu.className = "ctx-menu";
  const add = (label, fn) => {
    const b = document.createElement("button"); b.type = "button"; b.textContent = label;
    b.addEventListener("click", () => { fn(); closeFeedMenu(); });
    menu.appendChild(b);
  };
  if (kind === "SELL") {
    add(`Copy offer  (I have ${item} for ${myAuctionPrice(item) || "pst"})`, () => copyText(sellTell(speaker, item)));
  }
  add(`Copy  /tell ${speaker}`, () => copyText(`/tell ${speaker} `));
  if (item) add(`Copy  "${item}"`, () => copyText(item));
  const silenced = isSilenced(speaker);
  add(silenced ? `Unsilence ${speaker}` : `Silence ${speaker} (mute toasts)`, () => setSilenced(speaker, !silenced));
  if (kind === "BUY" && term && state.watchlist.some((x) => x.toLowerCase() === term.toLowerCase())) {
    add(`Remove "${term}" from watchlist`, () => removeFromWatchlist(term));
  }
  document.body.appendChild(menu);
  // clamp to viewport so a row near the edge doesn't push the menu offscreen
  const r = menu.getBoundingClientRect();
  menu.style.left = Math.min(ev.clientX, window.innerWidth - r.width - 6) + "px";
  menu.style.top = Math.min(ev.clientY, window.innerHeight - r.height - 6) + "px";
  setTimeout(() => document.addEventListener("click", closeFeedMenu, { once: true }), 0);
}

// Render one lead {kind:'SELL'|'BUY', tier, item} into the feed (+ a toast for
// HIGH-confidence ones). SELL = someone wants to buy what I have (green); BUY =
// someone's selling what I want (purple). Only HIGH toasts — MAYBE is feed-only,
// mirroring the desktop's loud/quiet tiers — and toasts are throttled per item.
function showRawLine(raw) {
  const pre = document.createElement("pre");
  pre.className = "raw-line";
  pre.textContent = raw;
  openModal("Raw auction line", pre);
}

// My asking price for an item, taken from the sell (auction) list I built. null
// if it isn't in my list or I haven't priced it.
function myAuctionPrice(name) {
  const lc = (name || "").toLowerCase();
  const it = state.auction.find((a) => (a.name || "").toLowerCase() === lc);
  return it && it.price ? it.price : null;
}
// The ready-to-paste /tell for a feed lead. For a SELL lead (a buyer for my gear)
// it's an offer at MY price; for a BUY lead (their item I want) it's an open tell.
function sellTell(speaker, item) {
  const p = myAuctionPrice(item);
  return p ? `/tell ${speaker} I have ${item} for ${p}` : `/tell ${speaker} I have ${item}, pst`;
}
function leadTell(kind, speaker, item) {
  return kind === "SELL" ? sellTell(speaker, item) : `/tell ${speaker} `;
}

function addLead(lead, speaker, msg, raw, stale) {
  const { kind, tier } = lead;
  const dir = kind === "SELL" ? "SELL TO" : "BUY FROM";
  const seg = kind === "SELL" ? WL.buySegments(msg) : WL.sellSegments(msg);
  // For a BUY lead, lead.item is the watch *word*; show the actual listed item
  // ("Deepwater" -> "Deepwater Vambraces") but keep the word for the remove action.
  const term = kind === "BUY" ? lead.item : null;
  // BUY matches are exact phrase; mark uncertain only if we couldn't isolate the
  // listed item (fell back to the watch word). SELL is fuzzy IDF — a MAYBE tier is
  // the low-confidence band, so flag it.
  let item = lead.item;
  let uncertain = kind === "SELL" && tier === "MAYBE";
  if (kind === "BUY") {
    const real = WL.listedItemFor(seg, term);
    if (real) item = real; else uncertain = true;
  }
  // Asking price: WTS for a BUY lead (seller's ask), WTB for a SELL lead (buyer's
  // offer). null when none was listed ("pst"/"offer").
  const price = WL.priceFor(seg, term || item);
  const priceStr = price ? ` ${price}` : "";
  const muted = isSilenced(speaker);
  log(`★ ${kind} ${item}${priceStr} — ${speaker}: ${msg}`);
  setStatus(`${kind === "SELL" ? "Buyer" : "Seller"} for ${item}${priceStr}: ${speaker}`);
  if (tier === "HIGH" && !muted && !stale && notifyReady()) {
    const now = Date.now();
    if (now - (lastNotify.get(item) || 0) >= NOTIFY_COOLDOWN_MS) {
      lastNotify.set(item, now);
      const body = kind === "SELL"
        ? `${speaker} wants to buy it — /tell ${speaker}`
        : `${speaker}: ${msg}`.slice(0, 180);
      new Notification(`${item}${priceStr} — ${dir} ${speaker}`, { body });
    }
  }
  const feed = $("wlFeed");
  if (!feed) return;
  const row = document.createElement("div");
  row.className = "wl-hit wl-" + kind.toLowerCase() + ((tier === "MAYBE" || muted) ? " maybe" : "");
  row.dataset.speaker = speaker; row.dataset.item = item; row.dataset.kind = kind;
  row.dataset.term = term || "";   // watchlist word that matched (for remove)
  // The ready-to-send tell. SELL = a buyer for my gear → offer it at MY price
  // ("/tell Buyer I have <item> for <myprice>"); if I haven't priced it, "pst".
  // BUY = I want their item → open a tell to them.
  const tell = leadTell(kind, speaker, item);
  row.title = "Click to copy this /tell · right-click for more";
  const t = new Date().toLocaleTimeString();
  row.innerHTML = `<button class="wl-plus" type="button" title="Show the raw log line">+</button>` +
    `<span class="wl-hit-t">${t}</span> ` +
    `<span class="wl-dir">${dir}</span> ` +
    `<span class="wl-hit-item">${escapeHtml(item)}</span>` +
    (uncertain ? `<span class="wl-fuzzy" title="fuzzy match — may not be exact">*</span>` : "") + ` ` +
    (price ? `<span class="wl-price" title="their offer">${escapeHtml(price)}</span> ` : "") +
    `<span class="wl-hit-who">${escapeHtml(tell)}</span>`;
  row.querySelector(".wl-plus").addEventListener("click", (e) => { e.stopPropagation(); showRawLine(raw); });
  row.addEventListener("click", () => copyText(leadTell(row.dataset.kind, row.dataset.speaker, row.dataset.item)));
  row.addEventListener("contextmenu", (e) => showFeedMenu(e, row));
  feed.insertBefore(row, feed.firstChild);
  while (feed.children.length > 50) feed.removeChild(feed.lastChild);
  if (document.hidden && state.monitoring) { hiddenAlertCount++; updateMonitorTitle(); }
}

// A direct /tell TO me. Not an auction match — just surface who messaged me + ping,
// so I don't miss a buyer's PM while a tab is hidden. Reuses the feed + toast plumbing.
function addTell(speaker, msg, raw, stale) {
  const muted = isSilenced(speaker);
  log(`✉ TELL — ${speaker}: ${msg}`);
  setStatus(`Tell from ${speaker}: ${msg}`.slice(0, 120));
  if (!muted && !stale && tellPingsOn() && notifyReady()) {   // ping (respects the toggle + mute + per-speaker cooldown; not on catch-up)
    const now = Date.now(), key = "tell:" + speaker.toLowerCase();
    if (now - (lastNotify.get(key) || 0) >= NOTIFY_COOLDOWN_MS) {
      lastNotify.set(key, now);
      new Notification(`Tell from ${speaker}`, { body: msg.slice(0, 180) });
    }
  }
  const feed = $("wlFeed");
  if (!feed) return;
  const row = document.createElement("div");
  row.className = "wl-hit wl-tell" + (muted ? " maybe" : "");
  row.dataset.speaker = speaker; row.dataset.kind = "TELL";
  row.title = "Click to copy a /tell reply · right-click for more";
  const t = new Date().toLocaleTimeString();
  row.innerHTML = `<button class="wl-plus" type="button" title="Show the raw log line">+</button>` +
    `<span class="wl-hit-t">${t}</span> ` +
    `<span class="wl-dir">TELL</span> ` +
    `<span class="wl-hit-who">${escapeHtml(speaker)}:</span> ` +
    `<span class="wl-tell-msg">${escapeHtml(msg)}</span>`;
  row.querySelector(".wl-plus").addEventListener("click", (e) => { e.stopPropagation(); showRawLine(raw); });
  row.addEventListener("click", () => copyText(`/tell ${speaker} `));
  row.addEventListener("contextmenu", (e) => showFeedMenu(e, row));
  feed.insertBefore(row, feed.firstChild);
  while (feed.children.length > 50) feed.removeChild(feed.lastChild);
  if (document.hidden && state.monitoring) { hiddenAlertCount++; updateMonitorTitle(); }
}

// Honest live/paused indicator. Visible tab -> live; hidden/minimized -> paused
// (the browser throttles us, so we say so instead of silently missing alerts).
function updateWlBanner() {
  const el = $("wlStatus");
  if (!el) return;
  if (!state.monitoring) { el.textContent = "Not monitoring"; el.className = "wl-status"; return; }
  if (typeof document !== "undefined" && document.hidden) {
    el.textContent = "⏸ Paused — tab not visible. Bring it to the foreground (or a 2nd monitor) to resume alerts.";
    el.className = "wl-status paused";
    return;
  }
  const ago = state.lastCheckAt ? Math.round((Date.now() - state.lastCheckAt) / 1000) : 0;
  el.textContent = `● Live — last check ${ago}s ago`;
  el.className = "wl-status live";
}

// Tab-strip tag: while monitoring + backgrounded, the tab title shows it's paused
// plus a 🔔N badge of alerts that landed while away — so a glance at the tab strip
// tells you "oops, this was in the background" even though the banner is off-screen.
let baseTitle = "";
let pausedSince = 0;
let hiddenAlertCount = 0;
function updateMonitorTitle() {
  if (typeof document === "undefined") return;
  if (state.monitoring && document.hidden) {
    const badge = hiddenAlertCount ? `🔔${hiddenAlertCount} ` : "";
    document.title = `${badge}⏸ ${baseTitle}`;
  } else {
    document.title = baseTitle;
  }
}

// Insert a divider in the feed marking a stretch where the tab was backgrounded
// (and alerts were throttled/delayed), so the blind spot is visible in the timeline.
function addFeedGapMarker(secs, n = 0) {
  const feed = $("wlFeed");
  if (!feed) return;
  const dur = secs >= 60 ? `${Math.floor(secs / 60)}m ${secs % 60}s` : `${secs}s`;
  const caught = n ? `caught up — ${n} match${n === 1 ? "" : "es"} while away (below)` : "caught up — nothing missed";
  const div = document.createElement("div");
  div.className = "wl-gap";
  div.textContent = `⏸ background ${dur} · ${caught} · now live ✓`;
  feed.insertBefore(div, feed.firstChild);
}

function onVisibilityChange() {
  if (document.hidden) {
    if (state.monitoring) { pausedSince = Date.now(); hiddenAlertCount = 0; }
  } else if (state.monitoring) {
    // Resume: a backgrounded interval gets throttled or frozen by the browser and
    // doesn't always wake cleanly — so restart the timer and force an immediate
    // catch-up read instead of waiting on the (possibly wedged) old interval.
    if (state.logTimer) clearInterval(state.logTimer);
    state.logTimer = setInterval(logTick, LOG_POLL_MS);
    const secs = pausedSince ? Math.round((Date.now() - pausedSince) / 1000) : 0;
    pausedSince = 0;
    logTick(true).then((n) => { if (secs >= 8) addFeedGapMarker(secs, n); });   // catch up (no stale toasts), then mark the gap
  }
  updateMonitorTitle();
  updateWlBanner();
}

// Set an auction item's price to the recent median (no undercut — match the live
// market, don't undercut it). Port of _use_recent_median (auction-list case).
function useRecentMedian(item, price) {
  item.price = price;
  item._manual = true; item._autoPriced = false;   // a deliberate market-match; keep it, don't slider-clobber
  if (item._priceInput) item._priceInput.value = price;
  refreshAuction();
  log(`  ${item.name}: priced at recent median ${price}`);
}

// Recent postings for ONE item by name. `item` is the auction row when we own it
// (enables the divergence hint vs the check median + a Set-price button); null
// for a DB lookup of something you don't have. Port of _show_recent_postings.
async function showRecentPostings(name, item = null) {
  const server = SERVER;
  log(`Fetching recent postings: ${name}…`);
  let sales;
  try { sales = await fetchRecentSales(name, server); }
  catch (e) { log(`  recent postings failed: ${e.message}`); alert("Couldn't fetch postings: " + e.message); return; }
  if (!sales.length) { log(`  ${name}: no recent postings on ${server}`); alert(`No recent postings found for:\n${name}`); return; }

  const rate = state.kronoRate || (await fetchKronoRate(server)) || DEFAULT_KRONO_RATE;
  const wrap = document.createElement("div");
  const sub = document.createElement("div");
  sub.className = "hint"; sub.style.marginBottom = "8px";
  sub.textContent = `Last ${sales.length} postings on ${server} (newest first)`;
  wrap.appendChild(sub);

  // Recent-asks divergence hint vs the last check median (item._lastMedian).
  const mk = recentMarket(sales, rate);
  const ref = item ? item._lastMedian : undefined;
  if (mk) {
    const shown = mk.isKrono
      ? `${mk.priceStr} (≈${Math.round(mk.effMed).toLocaleString()}p @ ${Math.round(rate).toLocaleString()}/kr)`
      : mk.priceStr;
    const hint = document.createElement("p");
    hint.className = "hint-line";
    hint.style.fontFamily = '"Segoe UI Emoji", Consolas, monospace';
    if (!ref) { hint.textContent = `Recent WTS median ${shown} (over ${mk.n} asks) — price-check this item to compare vs the live median.`; hint.style.color = "var(--fg)"; }
    else {
      const pct = (mk.effMed - ref) / ref * 100;
      if (pct <= -DIVERGE_PCT) { hint.textContent = `📉 Recent WTS median ${shown} — ~${Math.abs(Math.round(pct))}% UNDER your ${ref.toLocaleString()}p check median. Median's lagging; consider repricing.`; hint.style.color = "#ff6666"; }
      else if (pct >= DIVERGE_PCT) { hint.textContent = `📈 Recent WTS median ${shown} — ~${Math.round(pct)}% ABOVE your ${ref.toLocaleString()}p check median. Asks are climbing.`; hint.style.color = "#00ff66"; }
      else { hint.textContent = `≈ Recent WTS median ${shown} — in line with your ${ref.toLocaleString()}p check median.`; hint.style.color = "var(--muted)"; }
    }
    wrap.appendChild(hint);
    if (item) {   // only owned items can be repriced from here
      const setBtn = document.createElement("button");
      setBtn.textContent = `Set price → ${mk.priceStr}  (match recent median)`;
      setBtn.style.marginBottom = "10px";
      setBtn.addEventListener("click", () => { useRecentMedian(item, mk.priceStr); closeModal(); });
      wrap.appendChild(setBtn);
    }
  }

  const pre = document.createElement("div");
  pre.className = "postings";
  pre.textContent = sales.map((s) => {
    const when = formatSaleAge(s.datetime).padEnd(22);
    const kind = s.transactionType ? "WTB" : "WTS";
    const price = formatPostingPrice(s.platPrice, s.kronoPrice).padStart(9);
    return `${when} ${kind}  ${price}  ${s.auctioneer || "?"}`;
  }).join("\n");
  wrap.appendChild(pre);

  openModal(`Recent Postings — ${name}`, wrap);
}

// The auction item matching an inventory item, by id (unique) when present, else
// by name — same dedupe key Add uses. Returns undefined if not in the list.
function auctionMatch(inv) {
  const key = inv.id ? `#${inv.id}` : inv.name.toLowerCase();
  return state.auction.find((a) => (a.id ? `#${a.id}` : a.name.toLowerCase()) === key);
}

// Recent postings for the single selected item. Prefers a single INVENTORY
// selection (so you can look up anything you own, even if it's not on the auction
// list), else a single AUCTION selection. Mirrors the desktop's _selected_single_name
// (inventory-first) — and an auction click also mirror-selects the inventory row.
function recentPostingsSelected() {
  let name, item = null;
  if (state.invSel.size === 1) {
    const inv = state.inventory[[...state.invSel][0]];
    name = inv.name;
    item = auctionMatch(inv) || null;   // pass the auction item (if any) for the price-divergence hint
  } else if (state.aucSel.size === 1) {
    item = state.auction[[...state.aucSel][0]];
    name = item.name;
  } else {
    log("Select exactly one item (inventory or auction), then Recent Postings.");
    return;
  }
  showRecentPostings(name, item);
}

// DB lookup: recent postings for ANY item by name (owned or not). Exact match
// first, else a contains-search; multiple matches open a picker.
function recentPostingsLookup() {
  const q = $("lookupInput").value.trim();
  if (!q) return;
  if (!state.db) { log("Item DB not loaded yet."); return; }
  const lc = q.toLowerCase();
  let exact = null; const partial = [];
  for (const n of state.db.byName.keys()) {
    const nl = n.toLowerCase();
    if (nl === lc) { exact = n; break; }
    if (nl.includes(lc) && partial.length < 60) partial.push(n);
  }
  if (exact) { showRecentPostings(exact); return; }
  if (!partial.length) { log(`No DB item matches "${q}".`); alert(`No item in the database matches "${q}".`); return; }
  if (partial.length === 1) { showRecentPostings(partial[0]); return; }
  partial.sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));
  const list = document.createElement("div");
  list.className = "picker";
  for (const n of partial) {
    const row = document.createElement("div");
    row.className = "picker-row";
    row.textContent = n;
    row.addEventListener("click", () => showRecentPostings(n));   // replaces modal contents
    list.appendChild(row);
  }
  openModal(`${partial.length} matches for "${q}" — pick one`, list);
}

// =====================================================================
// UI wiring
// =====================================================================

// ----- inventory filter + column-sort helpers (port of desktop filter/sort) -----
// Bags only: general-inventory slots ('General 1', 'General 2-Slot4'); excludes
// worn gear, Bank, SharedBank, KeyRing, Power Source. Port of _is_bag_location.
function isBagLocation(loc) { return (loc || "").trim().toLowerCase().startsWith("general"); }
// Natural sort so 'General 2-Slot10' follows 'Slot9'. Port of _natkey.
function natkey(s) { return (s || "").split(/(\d+)/).filter((t) => t !== "").map((t) => /^\d+$/.test(t) ? parseInt(t, 10) : t.toLowerCase()); }
function natCmp(a, b) {
  const ax = natkey(a), bx = natkey(b);
  for (let i = 0; i < Math.max(ax.length, bx.length); i++) {
    const x = ax[i], y = bx[i];
    if (x === undefined) return -1;
    if (y === undefined) return 1;
    if (typeof x === "number" && typeof y === "number") { if (x !== y) return x - y; }
    else { const xs = String(x), ys = String(y); if (xs !== ys) return xs < ys ? -1 : 1; }
  }
  return 0;
}
// Group equipped/bank slots first, General bags second; natural-sort within. Port of _location_sort_key.
function locCmp(la, lb) {
  const ba = isBagLocation(la) ? 1 : 0, bb = isBagLocation(lb) ? 1 : 0;
  return ba !== bb ? ba - bb : natCmp(la, lb);
}
// Parse a displayed price ('500p','1.5kr','<1p','') to plat for ordering; '' sinks. Port of _price_sort_key.
function priceSortKey(v) {
  const s = (v || "").trim().toLowerCase();
  if (!s) return -1;
  const kr = s.match(/(\d+(?:\.\d+)?)\s*kr/), pp = s.match(/(\d+(?:\.\d+)?)\s*p/);
  if (kr || pp) return (kr ? parseFloat(kr[1]) * DEFAULT_KRONO_RATE : 0) + (pp ? parseFloat(pp[1]) : 0);
  const n = parseFloat(s.replace(/[^0-9.]/g, ""));
  return Number.isFinite(n) ? n : 0;
}
// Comparator for a column, operating on item objects.
function cmpFor(col) {
  if (col === "qty") return (a, b) => (a.count || 1) - (b.count || 1);
  // "vs worn" sorts by the actual delta so the biggest upgrades float to the top.
  if (col === "cmp") return (a, b) => invCmpSortKey(a) - invCmpSortKey(b);
  if (INV_COL_BY_KEY[col]) {
    if (col === "slot" || col === "effects")
      return (a, b) => String(invColValue(a, col) || "").localeCompare(String(invColValue(b, col) || ""));
    return (a, b) => (invColValue(a, col) || 0) - (invColValue(b, col) || 0);
  }
  if (col === "location") return (a, b) => locCmp(a.location, b.location);
  if (col === "price") return (a, b) => priceSortKey(a.price) - priceSortKey(b.price);
  if (col === "vendor") return (a, b) => priceSortKey(vendorStr(a)) - priceSortKey(vendorStr(b));
  if (col === "where") return (a, b) => whereStr(a).toLowerCase().localeCompare(whereStr(b).toLowerCase());
  return (a, b) => (a.name || "").toLowerCase().localeCompare((b.name || "").toLowerCase());
}
function toggleSortState(st, col) { st.desc = st.col === col ? !st.desc : false; st.col = col; }
// The header text lives in its own span so rewriting it can't wipe the <th>'s element
// children — the pick-all checkbox and coltable.js's resize grip both live in there.
function thLabel(th) {
  let lbl = th.querySelector(".th-label");
  if (!lbl) {
    lbl = document.createElement("span");
    lbl.className = "th-label";
    const txt = [...th.childNodes].filter((n) => n.nodeType === 3);   // text nodes only
    lbl.textContent = txt.map((n) => n.textContent).join("").replace(/[ ▲▼]+$/, "").trim();
    txt.forEach((n) => n.remove());
    th.insertBefore(lbl, th.firstChild);
  }
  return lbl;
}
// Show a ▲/▼ on the active column header (and clear the others).
function renderSortArrows(tableId, st) {
  document.querySelectorAll(`#${tableId} thead th[data-col]`).forEach((th) => {
    const lbl = thLabel(th);
    const base = th.dataset.label || (th.dataset.label = lbl.textContent.replace(/[ ▲▼]+$/, "").trim());
    lbl.textContent = th.dataset.col === st.col ? `${base} ${st.desc ? "▼" : "▲"}` : base;
  });
}
function sortInventory(col) { toggleSortState(state.invSort, col); renderSortArrows("invTable", state.invSort); buildInventoryTable(); }
function sortAuction(col) {
  toggleSortState(state.aucSort, col);
  renderSortArrows("aucTable", state.aucSort);
  const cmp = cmpFor(col);
  state.auction.sort((a, b) => state.aucSort.desc ? -cmp(a, b) : cmp(a, b));
  refreshAuction();
}

// ----- inventory pane (left): filtered/sorted list + multi-select + add -----
// Item quantity within the currently-enabled location buckets (grand total if no
// location filter is set). So "Bags + Bank" shows only what's in those areas.
function visibleCount(item) {
  const locs = state.filters.locs;
  if (!locs || !locs.size) return item.count;
  let n = 0;
  for (const b of locs) n += (item.buckets ? item.buckets[b] || 0 : 0);
  return n;
}

// Stable key for an item (id when present, else lowercased name). Used by the
// exclude blacklist, vendor-list dedupe, and auction dedupe.
function itemKey(it) { return it.id ? `#${it.id}` : (it.name || "").toLowerCase(); }
function isExcluded(it) { return state.excluded.has(itemKey(it)); }
// TLP-Auctions market page for an item name (opens in a new tab).
function marketUrl(name) { return `https://tlp-auctions.com/?q=${encodeURIComponent(name || "")}`; }
// Allakhazam (ZAM) clickout — by NAME, not id: ZAM uses its OWN item ids (their
// item=11621 is a different item than our game id 11621), so we can't deep-link by
// our ids. A name search lands on the item (picks the variant if there are several).
function zamUrl(name) { return `https://everquest.allakhazam.com/search.html?q=${encodeURIComponent(name || "")}`; }
// authoritygames.online deep link — richer than ZAM (stats + Frostreaver farming zones)
// and lands directly on the item, but its DB is a loot subset. Its slug is the name
// lowercased, apostrophes/backticks dropped, non-alphanumerics hyphenated (verified
// against the live site for spaces, apostrophes, and commas).
function slugify(name) { return (name || "").toLowerCase().replace(/['`]/g, "").replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, ""); }
function authUrl(name) { return `https://authoritygames.online/item/${slugify(name)}`; }
// The item-info clickout anchor: deep-link to authoritygames when the item is in our
// scraped slug set, else fall back to an Allakhazam name search (so there are never
// dead 404 links). The label (AG / ZAM) reflects where it goes.
function refAnchorHtml(name) {
  if (state.authSlugs && state.authSlugs.has(slugify(name)))
    return `<a class="ref auth" href="${authUrl(name)}" target="_blank" rel="noopener" title="Item details + Frostreaver farming zones on authoritygames.online" tabindex="-1">AG</a>`;
  return `<a class="ref" href="${zamUrl(name)}" target="_blank" rel="noopener" title="Look up ${escapeHtml(name)} on Allakhazam (search)" tabindex="-1">ZAM</a>`;
}
// Load the scraped authoritygames.online slug set (which items have a page there) so
// the info clickout can deep-link them; missing/offline just leaves links on ZAM.
async function loadAuthSlugs() {
  try {
    const resp = await fetch("authoritygames-slugs.json", { cache: "force-cache" });
    if (!resp.ok) return;
    const data = await resp.json();
    if (!Array.isArray(data.slugs)) return;
    state.authSlugs = new Set(data.slugs);
    if (state.inventory && state.inventory.length) buildInventoryTable();   // upgrade ZAM→AG where available
    if (state.auction && state.auction.length) refreshAuction();
  } catch { /* offline / not served → links stay on Allakhazam */ }
}

const EXCLUDED_KEY = "eqaf-excluded";
function saveExcluded() { try { localStorage.setItem(EXCLUDED_KEY, JSON.stringify([...state.excluded])); } catch { /* private mode */ } }
function loadExcluded() { try { state.excluded = new Set(JSON.parse(localStorage.getItem(EXCLUDED_KEY) || "[]")); } catch { state.excluded = new Set(); } }

// Does an item pass the Toon / Type / Slot / Location filters (and not blacklisted)?
function passesFilters(item) {
  if (isExcluded(item)) return false;
  const f = state.filters;
  if (f.toon && !(item.toons && item.toons.has(f.toon))) return false;
  if (f.type && item.type !== f.type) return false;
  if (f.type === "gear" && f.slot && !(item.slots || []).includes(f.slot)) return false;
  // "usable by class" — spells (caster) and gear (wearer) both carry item.classes
  if (f.class && (f.type === "spell" || f.type === "gear") && !(item.classes || []).includes(f.class)) return false;
  // "usable by race" — gear only; empty item.races means no race restriction (any race)
  if (f.type === "gear" && f.race && (item.races || []).length && !item.races.includes(f.race)) return false;
  // stat filter (gear only): item's DB stat block must carry the chosen stat at or
  // above the min (blank min → any item that has the stat at all). Value = tip key.
  if (f.type === "gear" && f.stat) {
    const rec = item.id && state.db ? state.db.byId.get(item.id) : null;
    const val = rec && rec.tip ? (rec.tip[f.stat] || 0) : 0;
    if (val < (f.statMin || 1)) return false;
  }
  if (f.locs && f.locs.size) {
    let any = false;
    for (const b of f.locs) if (item.buckets && item.buckets[b] > 0) { any = true; break; }
    if (!any) return false;
  }
  return true;
}

// Visible inventory after all filters + search, in the current sort. Each entry
// keeps its ORIGINAL index so selection maps back to state.inventory.
function inventoryView() {
  const q = $("invSearch").value.trim().toLowerCase();
  let view = state.inventory
    .map((item, i) => ({ item, i }))
    .filter(({ item }) => passesFilters(item) && passesColFilters(item) &&
      (!q || item.name.toLowerCase().includes(q)));
  // "upgrades only" is deliberately applied AFTER the normal filters so the location
  // toggles still decide which piles are even in scope.
  if (state.invCompare && state.invUpgradesOnly)
    view = view.filter(({ item }) => invIsUpgrade(item));
  if (state.invSort.col) {
    const cmp = cmpFor(state.invSort.col);
    view.sort((A, B) => state.invSort.desc ? -cmp(A.item, B.item) : cmp(A.item, B.item));
  }
  return view;
}

// Reflect state.filters.locs onto the location toggle checkboxes.
function syncLocToggles() {
  document.querySelectorAll('#locToggles input[type="checkbox"]').forEach((cb) => {
    cb.checked = state.filters.locs.has(cb.dataset.loc);
  });
}

// Repopulate the Toon dropdown (from loaded toons) and the Slot dropdown (slots
// present among loaded gear), and show the Slot control only when Type = Gear.
// Called after loads and filter changes.
function populateFilters() {
  const toonSel = $("filterToon");
  if (toonSel) {
    const cur = state.filters.toon;
    const names = state.toons.map((t) => t.name);
    toonSel.innerHTML = `<option value="">All toons</option>` +
      names.map((n) => `<option value="${escapeHtml(n)}">${escapeHtml(n)}</option>`).join("");
    toonSel.value = names.includes(cur) ? cur : "";
    state.filters.toon = toonSel.value;
  }
  const slotWrap = $("filterSlotWrap");
  const slotSel = $("filterSlot");
  if (slotSel) {
    const present = new Set();
    for (const it of state.inventory) if (it.type === "gear") for (const s of (it.slots || [])) present.add(s);
    const opts = SLOT_ORDER.filter((s) => present.has(s));
    const cur = state.filters.slot;
    slotSel.innerHTML = `<option value="">Any slot</option>` +
      opts.map((s) => `<option value="${s}">${s}</option>`).join("");
    slotSel.value = opts.includes(cur) ? cur : "";
    state.filters.slot = slotSel.value;
  }
  if (slotWrap) slotWrap.style.display = state.filters.type === "gear" ? "" : "none";
  const statWrap = $("filterStatWrap");
  if (statWrap) statWrap.style.display = state.filters.type === "gear" ? "" : "none";
  // Class filter shows for Spells (caster class) and Gear (wearer class); options
  // are the classes actually present among the current type's items.
  const classWrap = $("filterClassWrap");
  const classSel = $("filterClass");
  const classType = state.filters.type;   // "spell" | "gear" | other
  if (classSel) {
    const present = new Set();
    for (const it of state.inventory) if (it.type === classType) for (const c of (it.classes || [])) present.add(c);
    const opts = CLASS_ORDER.filter((c) => present.has(c));
    const cur = state.filters.class;
    classSel.innerHTML = `<option value="">All classes</option>` +
      opts.map((c) => `<option value="${c}">${c}</option>`).join("");
    classSel.value = opts.includes(cur) ? cur : "";
    state.filters.class = classSel.value;
  }
  if (classWrap) classWrap.style.display = (classType === "spell" || classType === "gear") ? "" : "none";
  // Race filter shows for Gear only — a character-race picker (all 16 races), so
  // you can see what a given race can equip regardless of what's currently owned.
  const raceWrap = $("filterRaceWrap");
  const raceSel = $("filterRace");
  if (raceSel) {
    const cur = state.filters.race;
    raceSel.innerHTML = `<option value="">Any race</option>` +
      RACE_ORDER.map((r) => `<option value="${r}">${r}</option>`).join("");
    raceSel.value = RACE_ORDER.includes(cur) ? cur : "";
    state.filters.race = raceSel.value;
  }
  if (raceWrap) raceWrap.style.display = state.filters.type === "gear" ? "" : "none";
  populateInvCompare();   // the "vs worn on" picker follows the loaded toons
  syncLocToggles();
}

// =====================================================================
// Inventory stat columns + "vs worn" comparison
// =====================================================================
// The list was Item/Qty/Where, so reading an item's stats meant hovering it one at
// a time — useless for scanning 450 items or deciding what to put on a toon. These
// render the DB stat block straight into the table, and (optionally) score every
// row against what a chosen toon is WEARING in that slot.

// key -> {label, tip (buildTipFields key), num, cls, title}
const INV_COLS = [
  { key: "ac", label: "AC", tip: "ac" },
  { key: "hp", label: "HP", tip: "hp" },
  { key: "mana", label: "Mana", tip: "mana" },
  { key: "atk", label: "ATK", tip: "atk" },
  { key: "haste", label: "Haste", tip: "haste", suffix: "%" },
  { key: "regen", label: "Regen", tip: "regen" },
  { key: "mregen", label: "MRegen", tip: "mregen" },
  { key: "str", label: "STR", tip: "str" }, { key: "sta", label: "STA", tip: "sta" },
  { key: "agi", label: "AGI", tip: "agi" }, { key: "dex", label: "DEX", tip: "dex" },
  { key: "wis", label: "WIS", tip: "wis" }, { key: "int", label: "INT", tip: "int" },
  { key: "cha", label: "CHA", tip: "cha" },
  { key: "svm", label: "SvM", tip: "svm" }, { key: "svf", label: "SvF", tip: "svf" },
  { key: "svc", label: "SvC", tip: "svc" }, { key: "svd", label: "SvD", tip: "svd" },
  { key: "svp", label: "SvP", tip: "svp" },
  { key: "ratio", label: "Ratio", title: "Weapon damage / delay" },
  { key: "slot", label: "Slot", text: true },
  { key: "effects", label: "Effects", text: true, title: "Click / proc / focus / worn effects, by name" },
  { key: "reclvl", label: "RecLvl", tip: "reclvl" },
];
const INV_COL_BY_KEY = Object.fromEntries(INV_COLS.map((c) => [c.key, c]));
const INV_PRESETS = {
  melee:   ["ac", "hp", "str", "dex", "atk", "haste", "ratio", "effects"],
  caster:  ["ac", "hp", "mana", "int", "svm", "effects"],
  priest:  ["ac", "hp", "mana", "wis", "svm", "effects"],
  selling: ["ac", "hp", "mana", "slot", "effects"],
  minimal: [],
};

function invTip(item) {
  const rec = item && item.id && state.db ? state.db.byId.get(item.id) : null;
  return rec && rec.tip ? rec.tip : null;
}

// Raw numeric/text value for a stat column (0/"" when the item doesn't have it).
function invColValue(item, key) {
  const t = invTip(item);
  if (!t) return key === "slot" || key === "effects" ? "" : 0;
  const col = INV_COL_BY_KEY[key];
  if (col && col.tip) return t[col.tip] || 0;
  if (key === "ratio") return (t.dmg > 0 && t.delay > 0) ? +(t.dmg / t.delay).toFixed(2) : 0;
  if (key === "slot") return (item.slots || []).join(" ");
  if (key === "effects") {
    const out = [];
    for (const [lbl, id] of [["C", t.click], ["P", t.proc], ["F", t.focus], ["W", t.worn]]) {
      if (!id) continue;
      const info = spellInfo(id);
      out.push({ lbl, name: info ? info.name : "" });
    }
    return out;
  }
  return 0;
}

// ----- "vs worn on <toon>": does this item beat what they're wearing? -----
// Reuses the Upgrade Finder's brain (toonEquipped + scoreItemFor + usableBy) so the
// two features can never disagree. Returns null when not comparable.
function invCompareInfo(item) {
  const who = state.invCompare;
  if (!who || !state.db || !item.id) return null;
  const rec = state.db.byId.get(item.id);
  if (!rec || !rec.tip || !rec.slots || isAug(rec)) return null;
  const prof = state.toonProfiles[who] || {};
  // Class/race/level gate only when the toon is labelled — an unlabelled toon still
  // gets slot-vs-slot numbers rather than nothing.
  if (prof.class && !usableBy(rec, prof)) return { cant: true };
  const equipped = toonEquipped(who);
  const cls = prof.class || "";
  const score = (tp) => scoreItemFor(tp, cls);
  const mine = score(rec.tip);
  let best = null;
  for (const slot of slotsForRec(rec)) {
    const worn = equipped[slot] || [];
    if (worn.some((w) => w.id === item.id)) return { worn: true, slot };
    if (!worn.length) {
      // Empty slot: the item's own score IS the gain. Do NOT inflate it — an empty
      // Ammo slot would otherwise rank a Throwing Spear above a +92 Legs upgrade.
      if (!best || best.delta < mine) best = { slot, delta: mine, empty: true };
      continue;
    }
    // compare against the WEAKEST worn piece in the slot — that's the one you'd
    // actually replace (matters for paired Ear/Wrist/Fingers).
    let low = Infinity, lowItem = null;
    for (const w of worn) { const s = score(w.rec && w.rec.tip); if (s < low) { low = s; lowItem = w; } }
    const d = mine - low;
    if (!best || d > best.delta) best = { slot, delta: d, vs: lowItem };
  }
  return best;
}

function invCompareCellHtml(item) {
  const info = invCompareInfo(item);
  if (!info) return `<td class="cmp"></td>`;
  if (info.cant) return `<td class="cmp cmp-no" title="This toon's class/race/level can't use it">✗</td>`;
  if (info.worn) return `<td class="cmp cmp-have" title="Already wearing this in ${escapeHtml(info.slot)}">worn</td>`;
  if (info.empty) return `<td class="cmp cmp-up" title="${escapeHtml(info.slot)} is EMPTY — this is the item's own value, not a gain over anything">` +
    `+${Math.round(info.delta)}<span class="cmp-slot">${escapeHtml(info.slot)} empty</span></td>`;
  const d = Math.round(info.delta);
  const cls = d > 0 ? "cmp-up" : d < 0 ? "cmp-down" : "cmp-same";
  const vs = info.vs ? ` vs ${info.vs.name}` : "";
  return `<td class="cmp ${cls}" title="${escapeHtml(info.slot)}${escapeHtml(vs)}">` +
    `${d > 0 ? "+" : ""}${d}<span class="cmp-slot">${escapeHtml(info.slot)}</span></td>`;
}

// Rebuild <thead> for the current column set. ColTable pins widths per column index,
// so its stored widths MUST be reset when the column set changes or they misalign.
function buildInvHead() {
  const head = $("invHead");
  if (!head) return;
  const cells = [`<th data-col="name">Item</th>`, `<th data-col="qty" class="qty">Qty</th>`];
  if (state.invCompare) cells.push(`<th data-col="cmp" class="cmp" title="Score vs what this toon is wearing in that slot">vs worn</th>`);
  for (const key of state.invCols) {
    const c = INV_COL_BY_KEY[key];
    if (!c) continue;
    const txt = c.key === "slot" || c.key === "effects";
    cells.push(`<th data-col="${c.key}" class="statcol${txt ? " txtcol" : ""}"${c.title ? ` title="${escapeHtml(c.title)}"` : ""}>${escapeHtml(c.label)}</th>`);
  }
  cells.push(`<th data-col="location" class="loc">Where</th>`);

  // Rebuild ONLY when the column set actually changes. buildInventoryTable runs on
  // every keystroke in a filter box, and blowing away the <thead> each time would
  // destroy the input the user is typing in (focus + caret lost after one character).
  const sig = state.invCols.join(",") + "|" + (state.invCompare ? "cmp" : "");
  if (sig !== state._invHeadSig) {
    state._invHeadSig = sig;
    head.innerHTML = cells.join("");
    if (window.ColTable) { try { ColTable.reset($("invTable")); } catch { /* widths are cosmetic */ } }
    // The one-shot wiring at startup can't reach headers created later — re-bind here.
    head.querySelectorAll("th[data-col]").forEach((th) =>
      th.addEventListener("click", () => sortInventory(th.dataset.col)));
    buildInvFilterRow();
  }
  renderSortArrows("invTable", state.invSort);
}

// A filter cell under each stat column: a min for numbers, a "contains" for text.
// Filters live where the data is, so "highest WIS" = type a WIS min, then click the
// WIS header to sort — no separate query builder to learn.
function buildInvFilterRow() {
  const head = $("invHead");
  const thead = head && head.parentNode;
  if (!thead) return;
  let row = thead.querySelector("tr.invfilters");
  if (row) row.remove();
  row = document.createElement("tr");
  row.className = "invfilters";
  const blank = `<th></th>`;
  const cellsHtml = [blank, blank];                       // Item (uses the search box), Qty
  if (state.invCompare) cellsHtml.push(blank);            // vs worn (use "upgrades only")
  for (const key of state.invCols) {
    const c = INV_COL_BY_KEY[key];
    if (!c) continue;
    const txt = key === "slot" || key === "effects";
    const val = state.invColFilters[key] != null ? state.invColFilters[key] : "";
    cellsHtml.push(`<th class="statcol${txt ? " txtcol" : ""}">` +
      `<input class="invf" data-col="${key}" type="${txt ? "search" : "number"}" ` +
      `value="${escapeHtml(String(val))}" placeholder="${txt ? "has…" : "min"}" ` +
      `title="${txt ? "Show only items whose " + escapeHtml(c.label) + " contains this text"
                   : "Show only items with " + escapeHtml(c.label) + " at or above this"}"></th>`);
  }
  cellsHtml.push(blank);                                  // Where
  row.innerHTML = cellsHtml.join("");
  thead.appendChild(row);
  row.querySelectorAll("input.invf").forEach((inp) => {
    // Typing must not re-sort or re-order the header, only re-filter the body.
    inp.addEventListener("click", (e) => e.stopPropagation());
    inp.addEventListener("input", () => {
      const v = inp.value.trim();
      if (!v) delete state.invColFilters[inp.dataset.col];
      else state.invColFilters[inp.dataset.col] = inp.type === "number" ? parseFloat(v) : v;
      saveInvCols();
      buildInventoryTable();
    });
  });
}

// Does the item clear every per-column filter? Numeric = at least this much;
// text = case-insensitive contains (effect NAMES are searchable this way).
function passesColFilters(item) {
  for (const [key, want] of Object.entries(state.invColFilters)) {
    if (!state.invCols.includes(key)) continue;    // filter on a hidden column is ignored
    const v = invColValue(item, key);
    if (key === "effects") {
      const hay = (v || []).map((e) => e.name).join(" ").toLowerCase();
      if (!hay.includes(String(want).toLowerCase())) return false;
    } else if (key === "slot") {
      if (!String(v || "").toLowerCase().includes(String(want).toLowerCase())) return false;
    } else if (!(Number(v) >= Number(want))) return false;
  }
  return true;
}

function invColCount() { return 3 + state.invCols.length + (state.invCompare ? 1 : 0); }

// Show the escape hatch only when a column filter is actually narrowing the list —
// a stale min hidden in a header cell is otherwise very easy to lose track of.
function syncClearFiltersBtn() {
  const btn = $("invClearFiltersBtn");
  if (!btn) return;
  const n = Object.keys(state.invColFilters).filter((k) => state.invCols.includes(k)).length;
  btn.hidden = !n;
  btn.textContent = `✕ clear ${n} column filter${n === 1 ? "" : "s"}`;
}

// Sort key for the "vs worn" column: real upgrades first (biggest delta), then
// same/worse, then things this toon can't use or already wears.
function invCmpSortKey(item) {
  const info = invCompareInfo(item);
  if (!info || info.cant) return -1e9;
  if (info.worn) return -1e8;
  return info.delta;
}

// Does this row beat what the compared toon wears? Drives "upgrades only".
// An EMPTY slot uses the same EMPTY_FLOOR junk guard as the Upgrade Finder —
// otherwise a stack of throwing axes "upgrades" a bare Ammo slot.
function invIsUpgrade(item) {
  const info = invCompareInfo(item);
  if (!info || info.cant || info.worn) return false;
  return info.empty ? info.delta >= EMPTY_FLOOR : info.delta > 0;
}

function saveInvCols() {
  try {
    localStorage.setItem("eqaf-inv-cols", JSON.stringify(state.invCols));
    localStorage.setItem("eqaf-inv-compare", state.invCompare || "");
    localStorage.setItem("eqaf-inv-colfilters", JSON.stringify(state.invColFilters));
  } catch { /* non-critical */ }
}
function loadInvCols() {
  try {
    const c = JSON.parse(localStorage.getItem("eqaf-inv-cols") || "null");
    if (Array.isArray(c)) state.invCols = c.filter((k) => INV_COL_BY_KEY[k]);
    state.invCompare = localStorage.getItem("eqaf-inv-compare") || "";
    state.invExpanded = !!localStorage.getItem("eqaf-inv-expanded");
    const f = JSON.parse(localStorage.getItem("eqaf-inv-colfilters") || "null");
    if (f && typeof f === "object") state.invColFilters = f;
  } catch { /* defaults stand */ }
}

// ----- inventory column picker + "vs worn on <toon>" -----
function renderInvColsMenu() {
  const list = $("invColsList");
  if (!list) return;
  list.innerHTML = INV_COLS.map((c) =>
    `<label class="toggle"><input type="checkbox" data-col="${c.key}"` +
    `${state.invCols.includes(c.key) ? " checked" : ""}> ${escapeHtml(c.label)}</label>`).join("");
  list.querySelectorAll("input[type=checkbox]").forEach((cb) => cb.addEventListener("change", () => {
    const key = cb.dataset.col;
    // Keep INV_COLS order rather than click order, so the table layout stays stable.
    state.invCols = cb.checked
      ? INV_COLS.map((c) => c.key).filter((k) => k === key || state.invCols.includes(k))
      : state.invCols.filter((k) => k !== key);
    saveInvCols();
    buildInventoryTable();
  }));
}

function populateInvCompare() {
  const sel = $("invCompare");
  if (!sel) return;
  const cur = state.invCompare;
  sel.innerHTML = `<option value="">— nobody —</option>` +
    state.toons.map((t) => {
      const p = state.toonProfiles[t.name] || {};
      const lbl = t.name + (p.class ? ` — ${p.class}${p.level ? " " + p.level : ""}` : "");
      return `<option value="${escapeHtml(t.name)}">${escapeHtml(lbl)}</option>`;
    }).join("");
  sel.value = state.toons.some((t) => t.name === cur) ? cur : "";
  state.invCompare = sel.value;
}

function buildInventoryTable() {
  loadRoster();          // warm the name→account map so the first expand is instant
  const body = $("invBody");
  body.innerHTML = "";
  state.invSel.clear();
  state.invAnchor = null;
  buildInvHead();
  const span = invColCount();
  const view = state.inventory.length ? inventoryView() : [];
  if (!state.inventory.length) {
    body.innerHTML = `<tr><td colspan="${span}" class="empty">Load inventory dumps above.</td></tr>`;
  } else if (!view.length) {
    body.innerHTML = `<tr><td colspan="${span}" class="empty">No items match the filters.</td></tr>`;
  } else {
    for (const { item, i } of view) {
      const cnt = visibleCount(item);
      // Per-toon breakdown tooltip so "N toons" is inspectable at a glance.
      const srcTip = (item.sources || [])
        .map((s) => `${s.toon}${s.count > 1 ? " x" + s.count : ""} @ ${s.location}`).join("\n");
      const statCells = state.invCols.map((key) => {
        const v = invColValue(item, key);
        if (key === "effects") {
          if (!v.length) return `<td class="statcol"></td>`;
          return `<td class="statcol fx">` + v.map((e) =>
            `<span class="fx-${e.lbl}" title="${e.lbl === "C" ? "Clicky" : e.lbl === "P" ? "Proc" : e.lbl === "F" ? "Focus" : "Worn"}">` +
            `${escapeHtml(e.name || e.lbl)}</span>`).join(" ") + `</td>`;
        }
        // text columns read left-aligned; only numbers get the right-aligned treatment
        if (key === "slot") return `<td class="statcol txtcol">${escapeHtml(v || "")}</td>`;
        const c = INV_COL_BY_KEY[key];
        return `<td class="statcol${v ? "" : " zero"}">${v ? escapeHtml(String(v)) + (c.suffix || "") : ""}</td>`;
      }).join("");
      const tr = document.createElement("tr");
      tr.dataset.i = i;
      tr.innerHTML =
        `<td>${escapeHtml(item.name)}</td>` +
        `<td class="qty">${cnt > 1 ? "x" + cnt : ""}</td>` +
        (state.invCompare ? invCompareCellHtml(item) : "") +
        statCells +
        `<td class="loc" title="${escapeHtml(srcTip)}">` +
          `<span class="loc-toggle" title="Who holds it — toon, slot and account">` +
            (state.invSrcOpen.has(i) ? "▾" : "▸") + `</span>` +
          escapeHtml(item.location) + `</td>`;
      tr.addEventListener("click", (e) => {
        if (e.target.closest("a") || e.target.closest(".loc-toggle")) return;
        selectRow(e, i, tr, state.invSel, "invBody", "invAnchor");
      });
      // Expand in place rather than re-rendering: buildInventoryTable() clears the
      // row selection, and losing a half-built selection to a curiosity click is
      // exactly the kind of thing that makes a tool annoying to use.
      const tog = tr.querySelector(".loc-toggle");
      tog.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (state.invSrcOpen.has(i)) {
          state.invSrcOpen.delete(i);
          const nxt = tr.nextElementSibling;
          if (nxt && nxt.classList.contains("inv-src")) nxt.remove();
          tog.textContent = "▸";
          return;
        }
        state.invSrcOpen.add(i);
        tog.textContent = "▾";
        tr.after(invSourceRow(item, invColCount(), await loadRoster()));
      });
      tr.addEventListener("dblclick", () => {
        if (addToAuction(state.inventory[i], cnt)) { log(`Added ${item.name}.`); refreshAuction(); }
      });
      body.appendChild(tr);
      // A row left open survives a sort/filter rebuild — appended AFTER its own row,
      // and with the already-resolved roster so the rebuild stays synchronous (a
      // still-pending fetch just costs account labels for one render).
      if (state.invSrcOpen.has(i)) body.appendChild(invSourceRow(item, span, _rosterCache));
    }
  }
  syncClearFiltersBtn();
  const total = state.inventory.length;
  $("invCount").textContent = view.length === total ? `${total} items` : `${view.length} of ${total}`;
  $("selAllBtn").disabled = !view.length;
  $("addSelBtn").disabled = !view.length;
}

// Explorer-style row selection mirroring the desktop trees' selectmode='extended':
// plain click selects only this row, Ctrl/Cmd-click toggles, Shift-click extends a
// range from the anchor (in visible row order). The web previously toggled on every
// plain click, so clicking around accumulated a multi-selection — which silently
// broke "exactly one" actions like Recent Postings.
function selectRow(e, i, tr, sel, bodyId, anchorKey) {
  const body = $(bodyId);
  if (e.shiftKey && state[anchorKey] != null) {
    const rows = [...body.querySelectorAll("tr[data-i]")];
    const idxs = rows.map((r) => Number(r.dataset.i));
    const a = idxs.indexOf(state[anchorKey]), b = idxs.indexOf(i);
    if (a !== -1 && b !== -1) {
      const [lo, hi] = a < b ? [a, b] : [b, a];
      sel.clear();
      rows.forEach((r) => r.classList.remove("sel"));
      for (let k = lo; k <= hi; k++) { sel.add(idxs[k]); rows[k].classList.add("sel"); }
      return;   // leave the anchor put so further shift-clicks extend from it
    }
  }
  if (e.ctrlKey || e.metaKey) {
    if (sel.has(i)) { sel.delete(i); tr.classList.remove("sel"); }
    else { sel.add(i); tr.classList.add("sel"); }
  } else {
    // Plain click on the ONLY selected row → deselect it (click it again to clear,
    // the intuitive "click away" people reach for). Otherwise single-select it.
    if (sel.size === 1 && sel.has(i)) { sel.delete(i); tr.classList.remove("sel"); state[anchorKey] = null; return; }
    sel.clear();
    body.querySelectorAll("tr.sel").forEach((r) => r.classList.remove("sel"));
    sel.add(i); tr.classList.add("sel");
  }
  state[anchorKey] = i;
}

// Clear the sell-list selection and refresh the readout (Escape, click-again, or
// clicking the total). Safe to call when nothing is selected.
function clearAuctionSelection() {
  if (!state.aucSel.size) return;
  state.aucSel.clear();
  state.aucAnchor = null;
  const body = $("aucBody");
  if (body) body.querySelectorAll("tr.sel").forEach((r) => r.classList.remove("sel"));
  syncAucChecks();
  updateSellTally();
  syncAucPickAll();
}

// Mirror an auction-row selection onto the inventory list (port of the desktop's
// _select_inventory_by_name): single-select the matching visible inventory row and
// scroll it into view, or clear the inventory selection if it's filtered out. Lets
// the inventory-first Recent Postings / left-pane actions target the clicked item
// without a second click.
function selectInventoryByName(name) {
  const body = $("invBody");
  state.invSel.clear();
  body.querySelectorAll("tr.sel").forEach((r) => r.classList.remove("sel"));
  state.invAnchor = null;
  const target = (name || "").toLowerCase();
  for (const tr of body.querySelectorAll("tr[data-i]")) {
    const it = state.inventory[Number(tr.dataset.i)];
    if (it && it.name.toLowerCase() === target) {
      const idx = Number(tr.dataset.i);
      state.invSel.add(idx); tr.classList.add("sel"); state.invAnchor = idx;
      tr.scrollIntoView({ block: "nearest" });
      return;
    }
  }
}

function selectAllInv() {
  const rows = $("invBody").querySelectorAll("tr[data-i]");
  const allSelected = rows.length > 0 && state.invSel.size >= rows.length;
  state.invSel.clear();
  rows.forEach((tr) => {
    if (allSelected) { tr.classList.remove("sel"); }
    else { state.invSel.add(Number(tr.dataset.i)); tr.classList.add("sel"); }
  });
}

// Add one inventory item to the auction list as a fresh copy. Dedupe by id
// (unique) when present, else by name. Skips blacklisted items. Returns true if added.
function addToAuction(inv, count) {
  if (isExcluded(inv)) return false;
  const key = itemKey(inv);
  if (state.auction.some((a) => itemKey(a) === key)) return false;
  // Carry the per-toon sources so the sell list can show WHERE it's held (for live
  // selling: search your macro'd list → see your price + which toon/slot has it).
  state.auction.push({ name: inv.name, location: inv.location, count: count != null ? count : inv.count, id: inv.id, price: "", sources: inv.sources ? inv.sources.slice() : [] });
  return true;
}

// ---- roster accounts (borrowed from My Characters) --------------------------
// Dumps carry a toon NAME and nothing else — but "which account is that toon on"
// is the question that decides what pulling an item costs: same account = a
// shared-bank hand-off during a login swap, different account = a parcel. My
// Characters' DB already knows it, so the Where panel borrows it instead of
// asking you to hold 46 name→account pairs in your head.
// Non-fatal by design: on GitHub Pages (no roster server) the panel simply omits
// the account column rather than failing to open.
const ACCT_COLORS = ["#38bdf8", "#a78bfa", "#22c55e", "#f59e0b", "#f472b6",
                     "#2dd4bf", "#fb923c", "#94a3b8"];   // mirrors mychars.js
let _rosterPromise = null;
let _rosterCache = null;    // resolved value, for the synchronous table rebuild

function loadRoster() {
  if (_rosterPromise) return _rosterPromise;
  _rosterPromise = (async () => {
    const out = { byName: new Map(), ok: false };
    _rosterCache = out;
    if (!isLocalhost()) return out;
    try {
      const resp = await fetch("/roster/bootstrap", { cache: "no-store" });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const d = await resp.json();
      // Colour by the account's position in launch order — same index, same colour
      // as the My Characters page, so the two pages read as one system.
      const order = (d.accounts || []).map((a) => a.id);
      for (const c of d.characters || []) {
        const i = order.indexOf(c.account_id);
        out.byName.set((c.name || "").toLowerCase(), {
          account: c.account_alias || "",
          // Golden-angle hues past the hand-picked palette so a 9th+ account never
          // reuses another account's colour (mirrors mychars.js paletteColor).
          color: i < 0 ? "#475569"
               : i < ACCT_COLORS.length ? ACCT_COLORS[i]
               : "hsl(" + Math.round((i * 137.508) % 360) + " 68% 62%)",
          cls: c.class_name || "", level: c.level || 0,
          order: i >= 0 ? i : 99,
        });
      }
      out.ok = true;
    } catch (e) {
      log("Account lookup unavailable (" + (e && e.message ? e.message : e) +
          ") — the Where panel will show toons without accounts.");
    }
    return out;
  })();
  return _rosterPromise;
}

// The expandable "who actually holds this" panel behind the Where cell. Groups the
// item's sources by ACCOUNT first (that's the transfer-cost boundary), then by toon,
// listing every location — so "6 toons" becomes six names, six slots, and the account
// each one sits on, without leaving the item browser.
function invSourceRow(item, span, roster) {
  const byToon = new Map();
  for (const s of item.sources || []) {
    const e = byToon.get(s.toon) || { toon: s.toon, count: 0, locs: [] };
    e.count += s.count || 0;
    e.locs.push(s.location);
    byToon.set(s.toon, e);
  }
  const groups = new Map();
  for (const e of byToon.values()) {
    const info = (roster && roster.byName.get(e.toon.toLowerCase())) || null;
    const key = info && info.account ? info.account : "";
    const g = groups.get(key) || { account: key, color: info ? info.color : "#475569",
                                   order: info ? info.order : 99, toons: [] };
    g.toons.push({ ...e, info });
    groups.set(key, g);
  }
  const list = [...groups.values()].sort((a, b) => a.order - b.order ||
                                                   a.account.localeCompare(b.account));
  const html = list.map((g) => {
    const toons = g.toons.sort((a, b) => a.toon.localeCompare(b.toon)).map((t) =>
      `<span class="src-toon">` +
        `<b>${escapeHtml(t.toon)}</b>` +
        (t.count > 1 ? `<span class="src-qty">×${t.count}</span>` : "") +
        (t.info && t.info.cls ? `<span class="src-cls">${escapeHtml(t.info.cls)}` +
          (t.info.level ? " " + t.info.level : "") + `</span>` : "") +
        `<span class="src-loc">${escapeHtml(t.locs.join(", "))}</span>` +
      `</span>`).join("");
    return `<div class="src-group">` +
      `<span class="src-acct" style="--acct:${g.color}">` +
        escapeHtml(g.account || "no account on roster") + `</span>` +
      `<div class="src-toons">${toons}</div></div>`;
  }).join("");
  const tr = document.createElement("tr");
  tr.className = "inv-src";
  tr.innerHTML = `<td colspan="${span}"><div class="src-wrap">${html}</div></td>`;
  return tr;
}

// Compact multi-toon label from a sources list: real character names (EQ names are
// short) instead of "N toons", so you don't have to hover to see who holds it. Merges
// by toon (an item can appear in >1 location on one toon), shows all when ≤3 toons,
// else the first 2 + "+N". withQty appends each toon's count (Rakthor ×2) — used by the
// vendor "Who" cell, where the question is "which toon do I pull from, and how many".
function whoLabel(sources, withQty) {
  const byToon = new Map();
  for (const s of sources || []) byToon.set(s.toon, (byToon.get(s.toon) || 0) + (s.count || 0));
  const toons = [...byToon.entries()];
  if (!toons.length) return "";
  const fmt = ([t, c]) => (withQty && c > 1 ? `${t} ×${c}` : t);
  if (toons.length <= 3) return toons.map(fmt).join(", ");
  return toons.slice(0, 2).map(fmt).join(", ") + ` +${toons.length - 2}`;
}

// "Where" summary for a sell row: which toon(s)/slot hold it. Single source shows
// "Rakthor · Bank2"; multiple shows the toon names (full breakdown in the cell tooltip).
function whereStr(item) {
  const s = item.sources || [];
  if (!s.length) return item.location || "";
  if (s.length === 1) return `${s[0].toon} · ${s[0].location}`;
  return whoLabel(s, false);
}
function whereTip(item) {
  return (item.sources || []).map((s) => `${s.toon}${s.count > 1 ? " x" + s.count : ""} @ ${s.location}`).join("\n");
}

// "Who" summary for a vendor row: which toon to pull from + how many (Rakthor ×2),
// capped. Slot doesn't matter for vendoring, so this drops it (kept in the tooltip).
function vendorWho(item) {
  const s = item.sources || [];
  if (!s.length) return item.location || "";
  return whoLabel(s, true);
}

function addSelectedToAuction() {
  if (!state.invSel.size) { log("Select inventory rows first (click them), then Add Selected."); return; }
  const wanted = state.invSel.size;
  let added = 0;
  [...state.invSel].sort((a, b) => a - b).forEach((i) => {
    const inv = state.inventory[i];
    if (addToAuction(inv, visibleCount(inv))) added++;
  });
  log(`Added ${added} item(s) to the auction list` +
      (added < wanted ? ` (${wanted - added} already there)` : "") + ".");
  state.invSel.clear();
  $("invBody").querySelectorAll("tr.sel").forEach((tr) => tr.classList.remove("sel"));
  refreshAuction();
}

// A table cell for the item name with a small ↗ link to its TLP-Auctions market page.
function nameCellHtml(name, frozen) {
  return `<td><div class="itemcell">` +
    (frozen ? `<span class="freeze" title="Price frozen — the global slider skips this row. Click to unfreeze (hand it back to the slider).">❄</span>` : "") +
    `<span class="iname">${escapeHtml(name)}</span>` +
    `<a class="mkt" href="${marketUrl(name)}" target="_blank" rel="noopener" title="View ${escapeHtml(name)} on TLP-Auctions" tabindex="-1">↗</a>` +
    refAnchorHtml(name) + `</div></td>`;
}

// A row is "frozen" when you've pinned a custom price (typed it, Apply % → Sel, or the
// Selected slider) on an item that otherwise HAS an auto price — so the global slider
// skips it. Unfreezing hands it back to the slider (recomputes off its median).
function isFrozen(item) {
  return !!item._manual && (item._median > 0) && classifyPrice(item.price)[0] !== "krono";
}
function unfreezeRow(i) {
  const it = state.auction[i]; if (!it || !it._median) return;
  it._manual = false; it._autoPriced = true;
  it.price = `${Math.max(niceRound(it._median * (1 + adjustPct() / 100)), 5)}p`;   // snap to the current global %
  refreshAuction();
  log(`Unfroze "${it.name}" — back on the global slider at ${it.price}.`);
}
// Add/remove the ❄ in a row's name cell live (so freezing via typing or the Selected
// slider shows the icon immediately, without waiting for a full re-render).
function syncFreezeIcon(tr, item) {
  const cell = tr && tr.querySelector(".itemcell"); if (!cell) return;
  const has = cell.querySelector(".freeze");
  if (isFrozen(item)) {
    if (!has) {
      const s = document.createElement("span");
      s.className = "freeze";
      s.title = "Price frozen — the global slider skips this row. Click to unfreeze (hand it back to the slider).";
      s.textContent = "❄";
      cell.insertBefore(s, cell.firstChild);
    }
  } else if (has) has.remove();
}

// ----- auction pane (right): the curated "to post" list -----
// Is a PLAT-priced item under/over the recent market? Computed live vs the item's
// CURRENT posted price, so re-pricing (typing or the slider) clears/updates it.
//   "under" = recent asks OR a live buyer bid sit meaningfully ABOVE your post →
//             you're leaving money on the table; reprice up. (Sunstrike/Khurenz.)
//   "over"  = recent asks run well BELOW your post → maybe overpriced.
function reviewFlag(item) {
  const [kind, plat] = classifyPrice(item.price);
  if (kind !== "plat" || plat <= 0) return null;
  let rm = item._recentMed || 0;
  const n = item._recentN || 0, bid = item._topBid || 0;
  // Rounding tolerance: if posting the recent median would niceRound to the price
  // already posted, there's nothing actionable — suppress the median-based flags.
  // Matters now that cheap items get the recent read too: a 125p recent median vs
  // a 100p post is just the nearest-100 snap, not real divergence. The live-WTB
  // bid path below is untouched.
  if (rm && niceRound(rm) === plat) rm = 0;
  if ((rm && n >= 2 && rm >= plat * (1 + UNDER_PCT / 100)) || (bid && bid >= plat * BID_MULT)) return "under";
  if (rm && n >= 3 && rm <= plat * (1 - DIVERGE_PCT / 100)) return "over";
  return null;
}

// Supply pressure from listing velocity (asks/day), made PRICE-AWARE. On a busy TLP
// most items flood, so "flooded" alone isn't actionable — what matters is whether
// YOU'RE priced above the pack. So:
//   "saturated" = flooded AND your price is above the recent ask median → undercut
//                 (or re-post) to move. Drop your price to/under the median and it
//                 clears (you're now competitive) — that's why the slider affects it.
//   "thin"      = few asks spread over many days → scarce, hold your price.
// NB: EQ buyers rarely post WTB (they just PM the cheapest/most-recent seller), so we
// deliberately DON'T treat "no live buyer" as a demand signal — it's meaningless here.
function saturationTag(item) {
  const pd = item._askPerDay || 0, n = item._askN || 0;
  if (!pd || n < 2) return null;
  if (pd <= SAT_LO && n >= 3) return "thin";
  if (pd >= SAT_HI && n >= SAT_MIN_N) {
    const [kind, plat] = classifyPrice(item.price);
    const med = item._recentMed || 0;
    if (kind === "plat" && med && plat <= med) return null;   // priced with the pack → competitive, no nag
    return "saturated";
  }
  return null;
}

// WTB demand: frequent buy bids relative to the WTS asks (buyers rarely WTB in EQ, so
// this means a real seller's market — krono, gems, WTB-spammed rares). null otherwise.
function demandTag(item) {
  const bn = item._bidN || 0, an = item._askN || 0;
  if (bn < DEMAND_MIN_BIDS) return null;
  if (bn / Math.max(an, 1) < DEMAND_RATIO) return null;
  return "demand";
}

// Hover text for a priced row's market read: demand (WTB) first when present, else
// supply pressure vs the recent ask median. Absence of WTB is NOT shown (meaningless
// in EQ); frequent WTB IS surfaced as demand.
function marketTip(item) {
  const pd = item._askPerDay || 0, n = item._askN || 0, bid = item._topBid || 0;
  const bn = item._bidN || 0, bpd = item._bidPerDay || 0;
  if (!pd && !bn) return "";
  const rnd = (x) => x >= 10 ? Math.round(x) : Math.round(x * 10) / 10;
  const span = item._askSpanD || 0;
  const spanStr = span >= 1 ? `${Math.round(span)}d` : span >= 1 / 24 ? `${Math.round(span * 24)}h` : "min";
  const med = item._recentMed || 0;
  const dem = demandTag(item);
  const st = saturationTag(item);
  const flooded = pd >= SAT_HI && n >= SAT_MIN_N;
  const head = dem ? "📈 IN DEMAND — buyers are actively WTB-ing this; hold your price or nudge it up (they'll PM you)"
             : st === "saturated" ? "🔥 FLOODED & you're above the pack — undercut (or re-post to stay recent) to move"
             : st === "thin" ? "🟢 THIN — scarce, hold your price"
             : flooded ? "Flooded, but you're priced with the pack — competitive; re-post to stay recent"
             : "Normal supply";
  const lines = [head];
  if (n) lines.push(`${n} recent asks over ${spanStr} (~${rnd(pd)}/day)`);
  if (bn) lines.push(`${bn} recent WTB bid${bn > 1 ? "s" : ""} (~${rnd(bpd)}/day)${bid ? `, top ~${bid.toLocaleString()}p` : ""}`);
  if (med) lines.push(`recent ask median ~${med.toLocaleString()}p`);
  return lines.join("\n");
}

// Single source of row color. Money-positive flags rank high so they're unmissable:
// krono > under (sells higher) > demand (buyers waiting) > saturated (undercut) >
// vendor > over (asks lower) > thin (hold — lowest, purely informational).
function rowTag(item) {
  if (classifyPrice(item.price)[0] === "krono") return "krono";
  const rf = reviewFlag(item);
  if (rf === "under") return "under";
  if (demandTag(item)) return "demand";
  const st = saturationTag(item);
  if (st === "saturated") return "saturated";
  if (isVendorTrash(item)) return "vendor";
  // No sales on the server but a real NPC value → just vendor it (Fire Emerald Ring case).
  if (item._noData && classifyPrice(item.price)[0] === "none" && (vendorPp(item) || 0) >= 1) return "vendor";
  if (rf === "over") return "diverge";
  if (st === "thin") return "thin";
  return null;
}

function refreshAuction(preserve) {
  const body = $("aucBody");
  body.innerHTML = "";
  if (preserve) {   // a live-search/filter re-render — keep the buyer's ticked selection
    for (const i of [...state.aucSel]) if (i >= state.auction.length) state.aucSel.delete(i);
  } else {          // a real list rebuild (add/remove/generate/reprice) — reset selection
    state.aucSel.clear();
    state.aucAnchor = null;
  }
  const q = (($("aucSearch") || {}).value || "").trim().toLowerCase();
  let shown = 0;
  if (!state.auction.length) {
    body.innerHTML = `<tr><td colspan="6" class="empty">Add items from the left.</td></tr>`;
  } else {
    state.auction.forEach((item, i) => {
      if (q && !item.name.toLowerCase().includes(q)) return;   // search filter (display only)
      if (state.reviewOnly && !reviewFlag(item)) return;       // "Review" filter
      shown++;
      const tr = document.createElement("tr");
      tr.dataset.i = i;
      const stacked = (item.count || 1) > 1;
      const sq = item.sellQty != null ? item.sellQty : (item.count || 1);   // how many to sell (default = whole stack)
      tr.innerHTML =
        `<td class="pick"><input type="checkbox" class="auc-pick"${state.aucSel.has(i) ? " checked" : ""}></td>` +
        nameCellHtml(item.name, isFrozen(item)) +
        `<td class="qty">${stacked ? `<input type="number" class="input input-qty auc-qty" min="1" max="${item.count}" value="${sq}" title="How many to sell — you have ${item.count}"><span class="qty-of" title="you own ${item.count}">/${item.count}</span>` : `<span class="qty-of">x1</span>`}</td>` +
        `<td class="price"></td>` +
        `<td class="qty">${escapeHtml(vendorStr(item))}</td>` +
        `<td class="loc" title="${escapeHtml(whereTip(item))}">${escapeHtml(whereStr(item))}</td>`;
      if (state.aucSel.has(i)) tr.classList.add("sel");
      const priceCell = tr.querySelector("td.price");
      const mtip = marketTip(item);          // supply-pressure read (asks/day) — hover the price
      if (mtip) priceCell.title = mtip;
      const input = document.createElement("input");
      input.type = "text";
      input.placeholder = item._noData ? "— no sales" : "e.g. 500p";
      if (item._noData && !item.price) input.classList.add("nodata");
      input.value = item.price || "";
      input.addEventListener("input", () => {
        item.price = input.value.trim();
        item._manual = true; item._autoPriced = false; item._noData = false;   // stop live-adjust for this row
        input.classList.remove("nodata");
        tr.classList.remove("krono", "under", "vendor", "diverge", "saturated", "thin", "demand");
        const t = rowTag(item);
        if (t) tr.classList.add(t);          // live recolor (under/krono/saturated/…) as you type
        priceCell.title = marketTip(item);   // market read tracks the new price
        syncFreezeIcon(tr, item);            // typing a price freezes the row → show the ❄
        updateSellTally();                   // price change moves the selected/all total
      });
      // editing the price shouldn't toggle the row's selection
      input.addEventListener("click", (e) => e.stopPropagation());
      item._priceInput = input;
      priceCell.appendChild(input);
      const tag = rowTag(item);
      if (tag) tr.classList.add(tag);
      // Checkbox: frictionless multi-select that persists across searches (no ctrl+click).
      const pick = tr.querySelector(".auc-pick");
      pick.addEventListener("click", (e) => e.stopPropagation());
      pick.addEventListener("change", () => {
        if (pick.checked) { state.aucSel.add(i); tr.classList.add("sel"); }
        else { state.aucSel.delete(i); tr.classList.remove("sel"); }
        updateSellTally(); syncAucPickAll();
      });
      // Per-row sell quantity (stacked items): the tally uses this, not the whole stack.
      const qi = tr.querySelector(".auc-qty");
      if (qi) {
        qi.addEventListener("click", (e) => e.stopPropagation());
        qi.addEventListener("input", () => {
          let v = parseInt(qi.value, 10);
          if (!Number.isFinite(v) || v < 1) v = 1;
          if (v > item.count) { v = item.count; qi.value = String(v); }
          item.sellQty = v;
          updateSellTally();
        });
      }
      tr.addEventListener("click", (e) => {
        if (e.target.closest(".freeze")) { unfreezeRow(i); return; }   // ❄ = unfreeze, not select
        if (e.target.closest("a")) return;   // clicking a clickout link (TLP / ZAM) isn't a row select
        selectRow(e, i, tr, state.aucSel, "aucBody", "aucAnchor");
        syncAucChecks();   // a plain click clears other rows' selection — resync every checkbox to aucSel
        if (state.aucSel.size === 1) selectInventoryByName(item.name);   // mirror onto the inventory list
        updateSellTally(); syncAucPickAll();
      });
      body.appendChild(tr);
    });
    if (!shown) {
      const why = state.reviewOnly ? "No items flagged for review — you're priced in line with the market."
        : q ? `No sell items match "${escapeHtml(q)}".` : "";
      if (why) body.innerHTML = `<tr><td colspan="6" class="empty">${why}</td></tr>`;
    }
  }
  const filtering = (q || state.reviewOnly) && state.auction.length;
  $("aucCount").textContent = filtering ? `${shown}/${state.auction.length}` : `${state.auction.length}`;
  // Review flag count (underpriced = the ones that matter most).
  const underN = state.auction.filter((a) => reviewFlag(a) === "under").length;
  const rb = $("reviewBtn");
  if (rb) {
    rb.textContent = `⚠ Review${underN ? ` (${underN})` : ""}`;
    rb.disabled = !underN && !state.reviewOnly;
    rb.classList.toggle("active", state.reviewOnly);
  }
  const has = state.auction.length > 0;
  $("pcBtn").disabled = !has;
  $("pcSelBtn").disabled = !has;
  if ($("applySelBtn")) $("applySelBtn").disabled = !has;
  $("rpBtn").disabled = !has;
  $("removeBtn").disabled = !has;
  $("clearBtn").disabled = !has;
  if ($("excludeBtn")) $("excludeBtn").disabled = !has;
  if ($("toVendorBtn")) $("toVendorBtn").disabled = !has;
  if ($("toKronoBtn")) $("toKronoBtn").disabled = !has;
  if ($("soldBtn")) $("soldBtn").disabled = !has;
  if ($("aucSelAllBtn")) $("aucSelAllBtn").disabled = !has;
  $("genBtn").disabled = !has;
  updateSellTally();
  syncAucPickAll();
}
// Resync every visible sell-row checkbox to the selection set (after click/ctrl/shift edits).
function syncAucChecks() {
  $("aucBody").querySelectorAll("tr[data-i]").forEach((tr) => {
    const cb = tr.querySelector(".auc-pick");
    if (cb) cb.checked = state.aucSel.has(Number(tr.dataset.i));
  });
}
// Reflect the header "select all shown" checkbox against the current selection.
function syncAucPickAll() {
  const all = $("aucPickAll"); if (!all) return;
  const rows = $("aucBody").querySelectorAll("tr[data-i]");
  let sel = 0;
  rows.forEach((tr) => { if (state.aucSel.has(Number(tr.dataset.i))) sel++; });
  all.checked = rows.length > 0 && sel === rows.length;
  all.indeterminate = sel > 0 && sel < rows.length;
}

// Select every currently-shown sell row (respects the search / Review filter) so a
// searched group ("Spell:") can be macro'd or tallied in one click.
function selectAllShownAuction() {
  const rows = $("aucBody").querySelectorAll("tr[data-i]");
  if (!rows.length) return;
  rows.forEach((tr) => {
    state.aucSel.add(Number(tr.dataset.i)); tr.classList.add("sel");
    const cb = tr.querySelector(".auc-pick"); if (cb) cb.checked = true;
  });
  state.aucAnchor = null;
  updateSellTally();
  syncAucPickAll();
}

// Sum the selected sell rows. Plat and krono are kept separate (different
// currencies) rather than folded, so the quote to a buyer is honest. count is
// the stocked qty of each row.
function sellTallyParts() {
  let plat = 0, kr = 0, n = 0;
  for (const i of state.aucSel) {
    const it = state.auction[i]; if (!it) continue;
    n++;
    const qty = it.sellQty != null ? it.sellQty : (it.count || 1);   // per-row "sell this many"
    if (classifyPrice(it.price)[0] === "krono") kr += (parseFloat((it.price || "").replace(/[^0-9.]/g, "")) || 0) * qty;
    else plat += parsePlatValue(it.price) * qty;
  }
  return { n, plat: Math.round(plat), kr };
}
function tallyMoney(plat, kr) {
  return [plat ? plat.toLocaleString() + "p" : "", kr ? kr + "kr" : ""].filter(Boolean).join(" + ") || "0p";
}
// Same total folded into "how many krono could I buy with this" at the live rate
// (plat converted + any krono already counted). "" when it rounds to nothing.
function tallyKrono(plat, kr) {
  const rate = state.kronoRate || DEFAULT_KRONO_RATE;
  const k = rate > 0 ? plat / rate + kr : kr;
  return k >= 0.05 ? `≈${(Math.round(k * 10) / 10).toLocaleString()} kr` : "";
}

// Grand "liquidate everything" total over ALL sell rows (not just the selection):
// priced rows count at their posted price (so it tracks the slider); rows with no
// sale price fall back to their NPC vendor buyback (the "…or just vendor it" value).
function sellAllParts() {
  let plat = 0, kr = 0, n = 0;
  for (const it of state.auction) {
    n++;
    const qty = it.count || 1;
    const [kind] = classifyPrice(it.price);
    if (kind === "krono") kr += (parseFloat((it.price || "").replace(/[^0-9.]/g, "")) || 0) * qty;
    else if (kind === "plat") plat += parsePlatValue(it.price) * qty;
    else { const v = vendorPp(it); if (v) plat += v * qty; }   // no sell price → vendor buyback
  }
  return { n, plat: Math.round(plat), kr };
}

// Money readout under the sell list. With rows selected → that selection's total;
// with nothing selected → the whole-list "sell/vendor everything" total. Both show
// the krono-equivalent. Updated on every selection change AND live as the slider
// moves. Copy total stays tied to the selection only.
function updateSellTally() {
  const el = $("sellTally"), btn = $("copyTallyBtn");
  const selN = state.aucSel.size;
  const { n, plat, kr } = selN ? sellTallyParts() : sellAllParts();
  if (el) {
    if (!n) { el.textContent = ""; el.removeAttribute("title"); }
    else {
      const kStr = tallyKrono(plat, kr);
      el.textContent = `${selN ? `${n} selected` : `all ${n}`} · ${tallyMoney(plat, kr)}${kStr ? ` · ${kStr}` : ""}`;
      const rate = state.kronoRate || DEFAULT_KRONO_RATE;
      el.title = (selN ? "Selected rows total — click here (or press Esc) to deselect and see the whole-list total."
                       : "Whole sell list — sell/vendor everything at current prices.") +
        ` Krono figure = total plat ÷ ${rate.toLocaleString()}/kr (live rate). Updates live with the slider.`;
      el.style.cursor = selN ? "pointer" : "";
    }
  }
  if (btn) btn.disabled = !selN;
  syncSelAdjustUI();
}

// ----- per-selection price slider (separate from the global Price-adjust) -----
// A small slider that appears only while sell rows are selected and moves ONLY
// those rows, live, off each item's median — the global slider keeps driving
// everything else. Adjusted rows are held (_manual) so the global slider leaves
// them alone afterward, exactly like "Apply % → Sel" but live.
function selAdjustPct() {
  const n = parseInt(($("selAdjustRange") || {}).value, 10);
  return Number.isFinite(n) ? n : 0;
}
// Paint the slider's current % into the read-only readout next to it.
function renderSelAdjustVal() {
  const el = $("selAdjustVal"); if (!el) return;
  const p = selAdjustPct();
  el.textContent = `${p > 0 ? "+" : ""}${p}%`;
}
function liveAdjustSelection() {
  const pct = selAdjustPct();
  const body = $("aucBody");
  for (const i of state.aucSel) {
    const it = state.auction[i]; if (!it) continue;
    if (!it._median || classifyPrice(it.price)[0] === "krono") continue;   // no median / krono → can't %-adjust
    it.price = `${Math.max(niceRound(it._median * (1 + pct / 100)), 5)}p`;
    it._manual = true; it._autoPriced = false;                             // hold it; global slider won't touch
    if (it._priceInput) it._priceInput.value = it.price;
    const tr = body && body.querySelector(`tr[data-i="${i}"]`);
    if (tr) { tr.classList.remove("krono", "under", "vendor", "diverge", "saturated", "thin", "demand"); const t = rowTag(it); if (t) tr.classList.add(t); syncFreezeIcon(tr, it); }
  }
  updateSellTally();   // reflect the new selection total (key unchanged → no reseed)
}
// Show/hide the selected-slider with the selection, and reseed it ONLY when the
// selection itself changes (a single pick seeds to that item's current % off its
// median, so you grab it right where it sits; multi resets to 0).
function syncSelAdjustUI() {
  const wrap = $("selAdjustWrap"); if (!wrap) return;
  const sel = state.aucSel, has = sel.size > 0;
  wrap.hidden = !has;
  const key = has ? [...sel].sort((a, b) => a - b).join(",") : "";
  if (key === state._selAdjustKey) return;   // same selection → leave the slider where the user put it
  state._selAdjustKey = key;
  if (!has) return;
  let pct = 0;
  if (sel.size === 1) {
    const it = state.auction[[...sel][0]];
    if (it && it._median && classifyPrice(it.price)[0] === "plat") pct = Math.round((parsePlatValue(it.price) / it._median - 1) * 100);
  }
  const r = $("selAdjustRange");
  if (r) r.value = Math.max(-50, Math.min(pct, 200));
  renderSelAdjustVal();
}

// Copy an itemized total of the selected rows for a buyer buying several items.
function copySellTally() {
  if (!state.aucSel.size) { log("Select the sell rows the buyer wants first."); return; }
  const lines = [];
  for (const i of [...state.aucSel].sort((a, b) => a - b)) {
    const it = state.auction[i]; if (!it) continue;
    const qty = (it.count || 1) > 1 ? ` x${it.count}` : "";
    lines.push(`${it.name}${qty} - ${it.price || "pst"}`);
  }
  const { n, plat, kr } = sellTallyParts();
  lines.push("---", `Total: ${tallyMoney(plat, kr)} for ${n} item${n > 1 ? "s" : ""}`);
  copyText(lines.join("\n"));
  log(`Copied itemized total: ${tallyMoney(plat, kr)} for ${n} item(s).`);
}

function removeSelectedFromAuction() {
  if (!state.aucSel.size) { log("Select auction rows to remove (click them), or use Clear."); return; }
  const n = state.aucSel.size;
  [...state.aucSel].sort((a, b) => b - a).forEach((i) => state.auction.splice(i, 1));   // high->low
  refreshAuction();
  log(`Removed ${n} item(s) from the auction list.`);
}

function clearAuction() {
  state.auction = [];
  refreshAuction();
  log("Auction list cleared.");
}

// ----- exclude-forever (blacklist) -----
// Remove selected auction rows AND blacklist them so they never show or get added
// again (persisted). This is the "I never sell this" version of Remove — for bags
// you use as storage, vendor junk, etc.
function excludeSelected() {
  if (!state.aucSel.size) { log("Select auction rows to exclude (click them)."); return; }
  const names = [];
  [...state.aucSel].sort((a, b) => b - a).forEach((i) => {
    const it = state.auction[i];
    state.excluded.add(itemKey(it));
    names.push(it.name);
    state.auction.splice(i, 1);
  });
  saveExcluded();
  refreshAuction();
  buildInventoryTable();
  log(`Excluded ${names.length} item(s) — hidden from now on: ${names.slice(0, 5).join(", ")}${names.length > 5 ? "…" : ""}. (Manage via "Excluded".)`);
}

function showExcluded() {
  const keys = [...state.excluded];
  const d = document.createElement("div");
  if (!keys.length) { d.innerHTML = "<p class='hint'>Nothing excluded yet. Select auction rows and hit <strong>Exclude</strong> to blacklist bags/junk you never sell.</p>"; openModal("Excluded items", d); return; }
  const intro = document.createElement("p");
  intro.className = "hint";
  intro.textContent = `${keys.length} item(s) are hidden from inventory and can't be added. Remove one to un-exclude it.`;
  d.appendChild(intro);
  const list = document.createElement("div"); list.className = "picker";
  for (const key of keys) {
    // Prefer a readable name: look up the id in the DB, else show the stored key.
    let label = key;
    if (key.startsWith("#") && state.db) { const rec = state.db.byId.get(parseInt(key.slice(1), 10)); if (rec) label = rec.name; }
    const row = document.createElement("div"); row.className = "picker-row excl-row";
    row.innerHTML = `<span>${escapeHtml(label)}</span>`;
    const btn = document.createElement("button"); btn.className = "btn btn-ghost btn-sm"; btn.textContent = "Un-exclude";
    btn.addEventListener("click", () => {
      state.excluded.delete(key); saveExcluded(); buildInventoryTable();
      row.remove(); log(`Un-excluded ${label}.`);
    });
    row.appendChild(btn); list.appendChild(row);
  }
  d.appendChild(list);
  openModal("Excluded items", d);
}

// ----- vendor list (sell to an NPC) -----
function addToVendor(item, count) {
  const key = itemKey(item);
  if (state.vendor.some((v) => itemKey(v) === key)) return false;
  // Carry the per-toon sources so the vendor list shows WHICH toon to vendor from
  // (inventory rows and sell rows both carry `sources`; whereStr/whereTip render it).
  state.vendor.push({ name: item.name, id: item.id, count: count != null ? count : item.count,
    sources: item.sources ? item.sources.slice() : [] });
  return true;
}
function vendorSelectedFromInv() {
  if (!state.invSel.size) { log("Select inventory rows, then → Vendor."); return; }
  let n = 0;
  [...state.invSel].forEach((i) => { const inv = state.inventory[i]; if (addToVendor(inv, visibleCount(inv))) n++; });
  state.invSel.clear();
  $("invBody").querySelectorAll("tr.sel").forEach((tr) => tr.classList.remove("sel"));
  renderVendor();
  log(`Added ${n} item(s) to the vendor list.`);
}
function moveSellToVendor() {
  if (!state.aucSel.size) { log("Select auction rows, then → Vendor."); return; }
  let n = 0;
  [...state.aucSel].sort((a, b) => b - a).forEach((i) => { const it = state.auction[i]; if (addToVendor(it, it.count)) n++; state.auction.splice(i, 1); });
  refreshAuction(); renderVendor();
  log(`Moved ${n} item(s) to the vendor list.`);
}
function removeSelectedVendor() {
  if (!state.vendorSel.size) { log("Select vendor rows to remove."); return; }
  const n = state.vendorSel.size;
  [...state.vendorSel].sort((a, b) => b - a).forEach((i) => state.vendor.splice(i, 1));
  renderVendor();
  log(`Removed ${n} item(s) from the vendor list.`);
}
function clearVendor() { state.vendor = []; renderVendor(); log("Vendor list cleared."); }

// ----- sales tracking (the "Sold" log / ledger) -----
const SALES_KEY = "eqaf-sales";
function saveSales() { try { localStorage.setItem(SALES_KEY, JSON.stringify(state.sales)); } catch { /* private mode */ } }
function loadSales() { try { state.sales = JSON.parse(localStorage.getItem(SALES_KEY) || "[]"); } catch { state.sales = []; } }

// ----- saved sell lists: name -> [{name,id,price,count}] (localStorage) ---------
// Reload a set of priced items without re-price-checking or re-loading the dump —
// the DB gives the clickable link by id, so a loaded list can macro + tally.
const LISTS_KEY = "eqaf-lists";
function loadSavedLists() {
  try { const o = JSON.parse(localStorage.getItem(LISTS_KEY) || "{}"); state.savedLists = (o && typeof o === "object") ? o : {}; }
  catch { state.savedLists = {}; }
}
function saveSavedLists() { try { localStorage.setItem(LISTS_KEY, JSON.stringify(state.savedLists)); } catch { /* private mode */ } }

function populateListSelect() {
  const sel = $("listSelect"); if (!sel) return;
  const names = Object.keys(state.savedLists).sort((a, b) => a.localeCompare(b));
  const cur = sel.value;
  sel.innerHTML = `<option value="">${names.length ? "— pick a list —" : "— no saved lists —"}</option>` +
    names.map((n) => `<option value="${escapeHtml(n)}">${escapeHtml(n)} (${state.savedLists[n].length})</option>`).join("");
  sel.value = names.includes(cur) ? cur : "";
  const has = !!sel.value;
  if ($("listLoadBtn")) $("listLoadBtn").disabled = !has;
  if ($("listDelBtn")) $("listDelBtn").disabled = !has;
}

// Save the current sell list — or just the SELECTED rows if any are highlighted
// (same selection that drives Generate) — under a name for later reload.
function saveCurrentList() {
  const rows = state.aucSel.size ? [...state.aucSel].map((i) => state.auction[i]).filter(Boolean) : state.auction;
  if (!rows.length) { log("Nothing to save — add items to the sell list first."); return; }
  const name = (prompt(`Save ${state.aucSel.size ? rows.length + " selected row(s)" : "the whole sell list"} as:`, "") || "").trim();
  if (!name) return;
  if (state.savedLists[name] && !confirm(`A list named "${name}" already exists. Overwrite it?`)) return;
  state.savedLists[name] = rows.map((it) => ({ name: it.name, id: it.id, price: it.price || "", count: it.count || 1 }));
  saveSavedLists();
  populateListSelect();
  if ($("listSelect")) { $("listSelect").value = name; populateListSelect(); }
  log(`Saved list "${name}" (${state.savedLists[name].length} item(s)).`);
}

// Load a saved list into the sell list (replaces the current one).
function loadSelectedList() {
  const name = ($("listSelect") || {}).value;
  if (!name || !state.savedLists[name]) { log("Pick a saved list to load."); return; }
  if (state.auction.length && !confirm(`Replace the current sell list (${state.auction.length} item(s)) with "${name}"?`)) return;
  state.auction = state.savedLists[name].map((s) => ({
    name: s.name, id: s.id, price: s.price || "", count: s.count || 1,
    location: "", sources: [], _manual: !!s.price, _autoPriced: false,
  }));
  refreshAuction();
  log(`Loaded list "${name}" (${state.auction.length} item(s)) into the sell list.`);
}

function deleteSelectedList() {
  const name = ($("listSelect") || {}).value;
  if (!name || !state.savedLists[name]) return;
  if (!confirm(`Delete saved list "${name}"? This can't be undone.`)) return;
  delete state.savedLists[name];
  saveSavedLists();
  populateListSelect();
  log(`Deleted saved list "${name}".`);
}

// ===================================================================
// Gear Upgrade Finder — score owned gear (Bags) or the TLP market (within a
// price threshold) against a toon's currently-equipped items, per class.
// Heuristic SHORTLIST: a class-weighted stat sum. Ignores click/proc/focus
// effects, augs, resist-set bonuses — eyeball the tooltip for the final call.
// ===================================================================
const TOONPROF_KEY = "eqaf-toon-profiles";
let _bundledProfiles = {};
async function loadToonProfiles() {
  try { const r = await fetch("toon-profiles.json", { cache: "no-store" }); if (r.ok) _bundledProfiles = await r.json(); } catch { /* not served */ }
  let over = {}; try { over = JSON.parse(localStorage.getItem(TOONPROF_KEY) || "{}"); } catch { over = {}; }
  state.toonProfiles = {};
  for (const [n, v] of Object.entries(_bundledProfiles)) state.toonProfiles[n] = { ...v };
  for (const [n, v] of Object.entries(over)) state.toonProfiles[n] = { ...(state.toonProfiles[n] || {}), ...v };
}
function saveToonProfiles() { try { localStorage.setItem(TOONPROF_KEY, JSON.stringify(state.toonProfiles)); } catch { /* private mode */ } }
// Who-Has-It gear sets: targetToon -> [{id,name}]. Purely local (no bundled file).
const GEARSETS_KEY = "eqaf-gear-sets";
function loadGearSets() {
  try { const o = JSON.parse(localStorage.getItem(GEARSETS_KEY) || "{}"); state.gearSets = (o && typeof o === "object") ? o : {}; }
  catch { state.gearSets = {}; }
}
function saveGearSets() { try { localStorage.setItem(GEARSETS_KEY, JSON.stringify(state.gearSets)); } catch { /* private mode */ } }
// Each set remembers its own "apply to" target, so several sets can be exported at once
// as "<Set> -> <Target>" plans without re-picking the toon each time.
const GEARTARGETS_KEY = "eqaf-gear-targets";
function loadGearTargets() {
  try { const o = JSON.parse(localStorage.getItem(GEARTARGETS_KEY) || "{}"); state.gearSetTargets = (o && typeof o === "object") ? o : {}; }
  catch { state.gearSetTargets = {}; }
}
function saveGearTargets() { try { localStorage.setItem(GEARTARGETS_KEY, JSON.stringify(state.gearSetTargets)); } catch { /* private mode */ } }
// Per-set: does this set respect other sets' claims? Default yes; a set flagged "free"
// may take pieces already reserved elsewhere (some sets are allowed to poach).
const GEARFREE_KEY = "eqaf-gear-free";
function loadGearFree() {
  try { const o = JSON.parse(localStorage.getItem(GEARFREE_KEY) || "{}"); state.gearSetFree = (o && typeof o === "object") ? o : {}; }
  catch { state.gearSetFree = {}; }
}
function saveGearFree() { try { localStorage.setItem(GEARFREE_KEY, JSON.stringify(state.gearSetFree)); } catch { /* private mode */ } }
// Per-set: retired sets keep their contents for reference but CLAIM NOTHING, so their
// pieces free up for every other set at once (better than un-ticking "respect" on each).
const GEAROFF_KEY = "eqaf-gear-inactive";
function loadGearInactive() {
  try { const o = JSON.parse(localStorage.getItem(GEAROFF_KEY) || "{}"); state.gearSetInactive = (o && typeof o === "object") ? o : {}; }
  catch { state.gearSetInactive = {}; }
}
function saveGearInactive() { try { localStorage.setItem(GEAROFF_KEY, JSON.stringify(state.gearSetInactive)); } catch { /* private mode */ } }
async function loadUpgradeSources() {
  try { const r = await fetch("upgrade-sources.json", { cache: "no-store" }); if (r.ok) state.upgradeSources = await r.json(); }
  catch { state.upgradeSources = {}; }
}
// tlpadvisor bis-sets.json (Frostreaver-built, era-clean, COMPLETE): class -> slot ->
// [{rank,id,name,tier,...}]. This is the RANK authority for the upgrade gate — unlike
// raidloot's curated list it keeps common all/all items (e.g. Spirit Wracked Cord #2)
// so the gate can protect a good worn piece. Lower rank = better.
async function loadBisSets() {
  try { const r = await fetch("bis-sets.json", { cache: "no-store" }); if (r.ok) state.bisSets = await r.json(); }
  catch { state.bisSets = {}; }
}
// raidloot-bis.json: kept ONLY for its richer Quest/Raid source strings (by item id).
async function loadRaidlootBis() {
  try { const r = await fetch("raidloot-bis.json", { cache: "no-store" }); if (r.ok) state.raidlootBis = await r.json(); }
  catch { state.raidlootBis = {}; }
}
// quest-armor.json: Velious class-armor planner. class -> slots -> {finishedId, moldId, gems}.
// Your Unadorned pieces are 0-stat MOLDS; this maps each to the finished piece it builds.
async function loadQuestArmor() {
  try { const r = await fetch("quest-armor.json", { cache: "no-store" }); if (r.ok) state.questArmor = await r.json(); }
  catch { state.questArmor = {}; }
}
// Lazy per-class id index over the tlpadvisor tier list: itemId -> {rank,tier,slot}.
// A multi-slot item keeps its BEST (lowest-rank) entry.
function bisIndex(className) {
  const bis = state.bisSets; if (!bis || !bis[className]) return null;
  state._bisIdx = state._bisIdx || {};
  if (state._bisIdx[className]) return state._bisIdx[className];
  const m = new Map();
  for (const [slot, rows] of Object.entries(bis[className])) {
    if (!Array.isArray(rows)) continue;
    for (const r of rows) {
      const cur = m.get(r.id);
      if (!cur || (r.rank || 999) < (cur.rank || 999)) m.set(r.id, { rank: r.rank, tier: r.tier, slot });
    }
  }
  state._bisIdx[className] = m;
  return m;
}
const bisEntry = (className, id) => { const m = bisIndex(className); return m ? (m.get(id) || null) : null; };
// raidloot source string (Quest/Raid + zone) for an item id — display enrichment only.
function bisSourceIdx(className) {
  const bis = state.raidlootBis; if (!bis || !bis[className]) return null;
  state._srcIdx = state._srcIdx || {};
  if (state._srcIdx[className]) return state._srcIdx[className];
  const m = new Map();
  for (const rows of Object.values(bis[className])) {
    if (!Array.isArray(rows)) continue;
    for (const r of rows) if (r.source && !m.has(r.id)) m.set(r.id, r.source);
  }
  state._srcIdx[className] = m;
  return m;
}
const bisSource = (className, id) => { const m = bisSourceIdx(className); return m ? (m.get(id) || null) : null; };

// class -> combat archetype -> per-point stat weights (tunable). Monk=melee,
// Shaman/Druid/Cleric=priest, etc. `ratio` weights weapon DMG/Delay.
const CLASS_ARCH = {
  Warrior: "melee", Monk: "melee", Rogue: "melee", Berserker: "melee",
  Paladin: "hybrid", Shadowknight: "hybrid", Ranger: "hybrid", Bard: "hybrid", Beastlord: "hybrid",
  Cleric: "priest", Druid: "priest", Shaman: "priest",
  Wizard: "caster", Magician: "caster", Necromancer: "caster", Enchanter: "caster",
};
// Priorities: AC, HP, MANA, Resists first; INT/WIS for casters, STR/DEX for
// melee. Resists weighted meaningfully (per-point, summed over all 5). Tunable.
const ARCH_W = {
  melee:  { ac: 1.5, hp: 0.7, str: 0.7, dex: 0.6, sta: 0.4, agi: 0.3, atk: 0.4, haste: 2.0, regen: 0.8, resist: 0.5, heroic: 1.2, ratio: 35 },
  hybrid: { ac: 1.2, hp: 0.65, str: 0.5, dex: 0.4, sta: 0.4, agi: 0.25, atk: 0.35, haste: 2.0, mana: 0.3, wis: 0.4, int: 0.4, resist: 0.5, heroic: 1.0, ratio: 25 },
  priest: { ac: 0.9, hp: 0.6, sta: 0.35, mana: 0.5, wis: 0.8, resist: 0.6, heal: 0.3, heroic: 1.0, ratio: 5 },
  caster: { ac: 0.8, hp: 0.6, sta: 0.3, mana: 0.5, int: 0.8, sdmg: 0.4, resist: 0.5, heroic: 1.0, ratio: 3 },
};
const RESIST_CAP = 45;   // SoV-era resist gear tops out here; values above are bugs/outliers (see capr)
function scoreItemFor(tip, className) {
  if (!tip) return 0;
  const w = ARCH_W[CLASS_ARCH[className]] || ARCH_W.hybrid;
  let s = (tip.ac || 0) * (w.ac || 0) + (tip.hp || 0) * (w.hp || 0) + (tip.mana || 0) * (w.mana || 0)
    + (tip.str || 0) * (w.str || 0) + (tip.sta || 0) * (w.sta || 0) + (tip.agi || 0) * (w.agi || 0) + (tip.dex || 0) * (w.dex || 0)
    + (tip.wis || 0) * (w.wis || 0) + (tip.int || 0) * (w.int || 0)
    + (tip.atk || 0) * (w.atk || 0) + (tip.haste || 0) * (w.haste || 0) + (tip.regen || 0) * (w.regen || 0)
    + (tip.heal || 0) * (w.heal || 0) + (tip.sdmg || 0) * (w.sdmg || 0);
  // Per-resist cap: one huge or DB-bugged resist stat (e.g. Chokidai Hide Pauldrons'
  // 246 Disease) must not dominate the score and fake an upgrade over a better item.
  // Magnitude-capped both ways so genuine −resist still counts.
  const capr = (v) => Math.max(-RESIST_CAP, Math.min(RESIST_CAP, v || 0));
  s += (capr(tip.svf) + capr(tip.svc) + capr(tip.svm) + capr(tip.svd) + capr(tip.svp)) * (w.resist || 0);
  s += ((tip.hstr || 0) + (tip.hsta || 0) + (tip.hagi || 0) + (tip.hdex || 0) + (tip.hwis || 0) + (tip.hint || 0) + (tip.hcha || 0)) * (w.heroic || 0);
  if (tip.dmg > 0 && tip.delay > 0) s += (tip.dmg / tip.delay) * (w.ratio || 0);
  // Effects the stat-sum can't value (epic clickies, procs, haste/focus worn effects):
  // credit their PRESENCE so an effect item outranks an identical one without.
  if (tip.click > 0) s += 15;
  if (tip.proc > 0) s += 12;
  if (tip.focus > 0) s += 10;
  if (tip.worn > 0) s += 8;
  return s;
}
const hasEffect = (tip) => !!(tip && (tip.click > 0 || tip.proc > 0 || tip.worn > 0 || tip.focus > 0));
// Augmentations (itemtype 54, e.g. Wulfenite gems, proc/stat augs) carry the equip-slot
// bitmask of the item they SOCKET INTO (Primary/Secondary/…), so the slot scan mistakes
// them for wearable upgrades. They're never a gear swap — exclude from candidates.
const isAug = (rec) => !!(rec && rec.tip && rec.tip.wtype === 54);

const slotsForRec = (rec) => (rec && rec.slots) ? SLOT_BITS.filter(([, b]) => rec.slots & b).map(([n]) => n) : [];

// {slot -> [{name,id,rec}]} currently worn by a toon (from its dump's equip-slot
// items; slot(s) derived from each item's DB bitmask).
function toonEquipped(toonName) {
  const t = state.toons.find((x) => x.name === toonName); const map = {};
  if (!t) return map;
  for (const it of t.items) {
    if (!it.buckets || !it.buckets.equipped || !it.id) continue;
    const rec = state.db ? state.db.byId.get(it.id) : null;
    if (isAug(rec)) continue;   // a socketed aug (Wulfenite gem) is listed as equipped and
                                // carries the host slot bitmask — never the slot's worn item
    for (const slot of slotsForRec(rec)) (map[slot] = map[slot] || []).push({ name: it.name, id: it.id, rec });
  }
  return map;
}
function usableBy(rec, prof) {
  if (!rec) return false;
  const cbit = CLASS_BITS.find(([n]) => n === prof.class);
  if (rec.classes && cbit && !(rec.classes & cbit[1])) return false;
  const rbit = RACE_BITS.find(([n]) => n === prof.race);
  if (rec.races && rec.races !== 65535 && rbit && !(rec.races & rbit[1])) return false;
  if (prof.level && rec.tip) {
    if (rec.tip.reqlvl && rec.tip.reqlvl > prof.level) return false;         // can't equip
    if (rec.tip.reclvl && rec.tip.reclvl > prof.level) return false;         // recommended level too high → far-future/off-era loot
  }
  return true;
}
const UPGRADE_MARGIN = 2;   // min score gain over the worn item to bother listing
const EMPTY_FLOOR = 6;      // an EMPTY slot needs a real item (score ≥ this) — kills junk (fishing poles, etc.)

// Drop source (from bundled upgrade-sources.json). {kind:'dungeon'|'raid'|'?', label, ...}.
function sourceFor(name) {
  const e = state.upgradeSources && state.upgradeSources[(name || "").toLowerCase().trim()];
  if (!e) return { kind: "?", label: "" };
  const where = `${e.z || "?"}${e.m ? " ← " + e.m : ""}${e.l ? " (L" + e.l + ")" : ""}`;
  return e.g ? { kind: "dungeon", label: "Dungeon: " + where } : { kind: "raid", label: "Raid: " + where };
}

// Core: given candidates [{rec,id,name,price?,where?}] score each vs the toon's
// worn item in every slot it fits; keep the single best slot per candidate.
function upgradesFor(prof, candidates) {
  const equipped = toonEquipped(prof.name);
  const arch = CLASS_ARCH[prof.class];
  const meleeHands = arch === "melee" || arch === "hybrid";
  const best = new Map();   // candidate id -> best upgrade row
  for (const c of candidates) {
    const rec = c.rec;
    if (!rec || !rec.slots || !rec.tip || !usableBy(rec, prof)) continue;
    if (isAug(rec)) continue;   // augment, not a wearable slot item (see isAug)
    const cScore = scoreItemFor(rec.tip, prof.class);
    if (cScore < 3) continue;   // junk guard: must carry some class-relevant value
    const candBis = bisEntry(prof.class, c.id);   // raidloot tier-list entry, or null
    for (const slot of slotsForRec(rec)) {
      // a melee/hybrid weapon hand wants a real weapon (dmg>0), not a no-damage
      // stat-stick (e.g. a statted fishing pole) — casters may still use those.
      if (meleeHands && (slot === "Primary" || slot === "Secondary") && !(rec.tip.dmg > 0)) continue;
      const worn = equipped[slot] || [];
      if (worn.some((w) => w.id === c.id)) continue;   // already wearing this exact item
      // Tier-list gate: if you're wearing a tier-list-ranked piece here, only a candidate
      // that ranks strictly BETTER (lower rank #) is an upgrade — never a worse-ranked or
      // unlisted item. Fires only when the worn item is on the list, so an unlisted worn
      // item never over-suppresses.
      let wornRank = null;
      for (const w of worn) { const e = bisEntry(prof.class, w.id); if (e && (wornRank === null || e.rank < wornRank)) wornRank = e.rank; }
      if (wornRank !== null && !(candBis && candBis.rank < wornRank)) continue;
      let base = 0, baseItem = null;
      if (worn.length) { base = Infinity; for (const w of worn) { const s = scoreItemFor(w.rec && w.rec.tip, prof.class); if (s < base) { base = s; baseItem = w; } } }
      // Never dethrone an item whose worth is an EFFECT the scorer can't judge
      // (epics, clickies, focus/haste) with an effectless candidate.
      if (baseItem && hasEffect(baseItem.rec && baseItem.rec.tip) && !hasEffect(rec.tip)) continue;
      const delta = cScore - base;
      if (delta > (worn.length ? UPGRADE_MARGIN : EMPTY_FLOOR)) {
        const row = { slot, candidate: c, delta: Math.round(delta), equipped: baseItem, price: c.price, where: c.where, source: sourceFor(c.name), bis: candBis, qsrc: bisSource(prof.class, c.id) };
        if (!best.has(c.id) || row.delta > best.get(c.id).delta) best.set(c.id, row);
      }
    }
  }
  return [...best.values()].sort((a, b) => b.delta - a.delta);
}

function scanBags(prof) {
  const cands = [];
  for (const it of state.inventory) {
    if (!it.id) continue;
    const rec = state.db.byId.get(it.id); if (!rec) continue;
    const where = (it.sources && it.sources.length)
      ? [...new Set(it.sources.map((s) => s.toon))].slice(0, 3).join(", ") : "";
    cands.push({ rec, id: it.id, name: it.name, where });
  }
  cands.push(...questArmorCandidates(prof));   // Velious class armor you can BUILD from owned molds
  return upgradesFor(prof, cands);
}
// Finished class-armor pieces you can build right now: for each slot whose Unadorned mold
// you own, offer the finished piece (0-stat molds never score; what they build does). Scored
// vs worn like any candidate, so it only surfaces where it's actually an upgrade.
function questArmorCandidates(prof) {
  const qa = state.questArmor && state.questArmor[prof.class];
  if (!qa || !qa.slots || !state.db || !state.inventory) return [];
  const owned = new Set(state.inventory.map((it) => it.id));
  const out = [];
  // Each slot lists up to 3 build options (mold tiers: Kael / Skyshrine / Thurgadin).
  for (const opts of Object.values(qa.slots)) {
    for (const e of opts) {
      if (!e.moldId || !owned.has(e.moldId)) continue;   // only tiers whose mold you own
      const rec = state.db.byId.get(e.finishedId); if (!rec) continue;
      const note = `build: ${e.moldName}${e.gems ? " + " + e.gems : ""}`;
      out.push({ rec, id: e.finishedId, name: rec.name, where: note });
    }
  }
  return out;
}
async function fetchCatalog() {
  if (state._catalog) return state._catalog;
  const r = await fetch(`${apiBase()}/items/catalog?serverName=${encodeURIComponent(SERVER)}`, { headers: apiHeaders() });
  if (!r.ok) throw new Error("HTTP " + r.status);
  const d = await r.json();
  state._catalog = (d.items || []).filter((x) => x.price > 0);
  return state._catalog;
}
async function scanTLP(prof, threshold) {
  const cat = await fetchCatalog();
  const cands = [];
  for (const row of cat) {
    if (threshold && row.price > threshold) continue;
    const rec = state.db.byId.get(row.itemId); if (!rec || !rec.slots) continue;
    cands.push({ rec, id: row.itemId, name: rec.name, price: row.price });
  }
  return upgradesFor(prof, cands);
}

const upgToons = () => state.toons.map((t) => t.name).filter((n) => state.toonProfiles[n] && state.toonProfiles[n].class);

const resistSum = (t) => t ? (t.svf || 0) + (t.svc || 0) + (t.svm || 0) + (t.svd || 0) + (t.svp || 0) + (t.svcor || 0) : 0;
// A signed stat-delta cell: green gain, red loss, dim dot for no change.
const upgDeltaCell = (n) => n ? `<td class="upg-n ${n > 0 ? "upg-pos" : "upg-neg"}">${n > 0 ? "+" : ""}${n}</td>` : `<td class="upg-n upg-z">·</td>`;
// The "special effect" badges worth surfacing (haste/focus/regen headline; proc/click noted).
function upgFxBadges(t) {
  if (!t) return "";
  const b = [];
  if (t.haste > 0) b.push(`<span class="upg-fx upg-fx-haste">Haste ${t.haste}%</span>`);
  if (t.focus > 0) b.push(`<span class="upg-fx upg-fx-focus">Focus</span>`);
  if (t.regen > 0) b.push(`<span class="upg-fx upg-fx-regen">Regen +${t.regen}</span>`);
  if (t.proc > 0) b.push(`<span class="upg-fx">Proc</span>`);
  if (t.click > 0) b.push(`<span class="upg-fx">Click</span>`);
  return b.join(" ");
}

function renderUpgradeRows(container, rows, prof, mode) {
  if (!rows.length) {
    container.innerHTML = `<p class="dim">No ${mode === "tlp" ? "buyable " : ""}upgrades found for ${escapeHtml(prof.name)}${mode === "tlp" ? " under that price" : ""} with the current filters.</p>`;
    return;
  }
  // Rows come one-per-candidate, so a slot can have several. Show the BEST per slot
  // (one clean row, head-to-toe in SLOT_ORDER); the ranked alternatives are hidden
  // until you click the slot — so the table stays tidy but "what else fits here" is a click away.
  const MAX_ALT = 6;
  const groups = new Map();
  for (const r of rows) { if (!groups.has(r.slot)) groups.set(r.slot, []); groups.get(r.slot).push(r); }
  const slotRows = [];
  for (const slot of SLOT_ORDER) {
    const g = groups.get(slot); if (!g) continue;
    g.sort((a, b) => b.delta - a.delta);
    const take = g.slice(0, 1 + MAX_ALT);
    take.forEach((r, i) => slotRows.push(Object.assign({ _first: i === 0, _more: i === 0 ? take.length - 1 : 0 }, r)));
  }
  const slotCount = slotRows.filter((r) => r._first).length;

  const body = slotRows.map((r) => {
    const ct = r.candidate.rec.tip, wt = r.equipped && r.equipped.rec ? r.equipped.rec.tip : null;
    const eq = r.equipped ? `<span data-id="${r.equipped.id}">${escapeHtml(r.equipped.name)}</span>` : `<span class="dim">(empty)</span>`;
    const dAC = (ct.ac || 0) - (wt ? wt.ac || 0 : 0);
    const dHP = (ct.hp || 0) - (wt ? wt.hp || 0 : 0);
    const dRes = resistSum(ct) - resistSum(wt);
    // Source: prefer raidloot's richer string (Quest/Raid + zone/mob) when the candidate
    // is on the tier list; else fall back to the bundled drop-source data.
    let badge, srcText = "";
    if (r.qsrc) {
      const s = r.qsrc, low = s.toLowerCase();
      badge = low.startsWith("quest") ? `<span class="upg-b upg-quest-b">Quest</span>`
        : low.startsWith("raid") ? `<span class="upg-b upg-raid-b">Raid</span>`
        : `<span class="upg-b upg-dung-b">Drop</span>`;
      srcText = escapeHtml(s.replace(/^(Quest|Raid|Mob):\s*/i, ""));
    } else {
      const src = r.source || { kind: "?" };
      badge = src.kind === "dungeon" ? `<span class="upg-b upg-dung-b">Dungeon</span>`
        : src.kind === "raid" ? `<span class="upg-b upg-raid-b">Raid</span>`
        : `<span class="upg-b upg-buy-b">${mode === "tlp" ? "Buy" : "—"}</span>`;
      srcText = src.label ? escapeHtml(src.label) : "";
    }
    const bits = [];
    if (mode === "tlp") bits.push(`${r.price.toLocaleString()}p`);
    else if (r.where) bits.push(escapeHtml(r.where));   // which of your toons has it in bags
    if (srcText) bits.push(srcText);
    const bisChip = r.bis ? `<span class="upg-b upg-bis-b" title="tier-list rank (tlpadvisor)">BIS #${r.bis.rank}</span>` : "";
    const slotCell = r._first
      ? `<td class="upg-slot${r._more ? " upg-slot-x" : ""}"${r._more ? ` data-slot="${escapeHtml(r.slot)}"` : ""}>` +
        `${r._more ? `<span class="upg-caret">▸</span>` : ""}${escapeHtml(r.slot)}${r._more ? ` <span class="upg-more">+${r._more}</span>` : ""}</td>`
      : `<td></td>`;
    return `<tr class="${r._first ? "" : "upg-alt"}"${r._first ? "" : ` data-altof="${escapeHtml(r.slot)}" hidden`}>` +
      slotCell +
      `<td class="upg-eq">${r._first ? eq : ""}</td>` +
      `<td class="upg-cand"><span data-id="${r.candidate.id}">${escapeHtml(r.candidate.name)}</span> ${bisChip}</td>` +
      upgDeltaCell(dAC) + upgDeltaCell(dHP) + upgDeltaCell(dRes) +
      `<td class="upg-fxcell">${upgFxBadges(ct)}</td>` +
      `<td class="upg-src">${badge} ${bits.join(" · ")}</td></tr>`;
  }).join("");
  container.innerHTML =
    `<div class="upg-note">${slotCount} slot${slotCount > 1 ? "s" : ""} with an upgrade for <strong>${escapeHtml(prof.name)}</strong> ` +
    `(${escapeHtml(prof.race || "?")} ${escapeHtml(prof.class)}${prof.level ? " L" + prof.level : ""}). ` +
    `Δ columns = stat gain over what you have worn; hover a name for stats. ` +
    `<span class="dim">Click a slot with a <span class="upg-more">+N</span> to see the other options.</span></div>` +
    `<table class="upg-table"><thead><tr><th>Slot</th><th>Equipped</th><th>Upgrade</th>` +
    `<th class="upg-n">ΔAC</th><th class="upg-n">ΔHP</th><th class="upg-n">ΔRes</th><th>Effects</th>` +
    `<th>${mode === "tlp" ? "Price · Source" : "Where · Source"}</th></tr></thead><tbody>${body}</tbody></table>`;
}

const classOptions = (sel) => `<option value="">—</option>` + CLASS_ORDER.map((c) => `<option${c === sel ? " selected" : ""}>${c}</option>`).join("");
const raceOptions = (sel) => `<option value="">—</option>` + RACE_ORDER.map((r) => `<option${r === sel ? " selected" : ""}>${r}</option>`).join("");

function upgTipMove(e) {
  const el = e.target.closest("[data-id]");
  const rec = el && state.db ? state.db.byId.get(Number(el.dataset.id)) : null;
  if (rec && rec.tip) { showItemTip(rec); positionItemTip(e.clientX, e.clientY); } else hideItemTip();
}

// Re-render the last scan into the results area, applying the "dungeon only" filter.
function paintUpgrades() {
  const panel = $("upgradePanel"); if (!panel || !state._lastUpg) return;
  const results = panel.querySelector(".upg-results"); if (!results) return;
  const dungOnly = panel.querySelector(".upg-dung") && panel.querySelector(".upg-dung").checked;
  let { rows, prof, mode } = state._lastUpg;
  if (dungOnly) rows = rows.filter((r) => r.source && r.source.kind === "dungeon");
  renderUpgradeRows(results, rows, prof, mode);
}

// The Upgrades tab: label toons (class/race/level, persisted) → scan bags or the
// TLP market for gear that beats what's equipped, tagged by drop source.
function renderUpgradePanel() {
  const panel = $("upgradePanel"); if (!panel) return;
  if (!state.db) { panel.innerHTML = `<p class="dim upg-pad">Item DB still loading…</p>`; return; }
  if (!state.toons.length) { panel.innerHTML = `<p class="dim upg-pad">Load a character's inventory dump (left) first, then label it here.</p>`; return; }
  const toonRows = state.toons.map((t) => {
    const p = state.toonProfiles[t.name] || {};
    return `<div class="upg-toon" data-toon="${escapeHtml(t.name)}"><span class="upg-tn">${escapeHtml(t.name)}</span>` +
      `<select class="input input-sm upg-class" title="Class">${classOptions(p.class)}</select>` +
      `<select class="input input-sm upg-race" title="Race">${raceOptions(p.race)}</select>` +
      `<input type="number" class="input input-num upg-level" min="1" max="65" placeholder="lvl" value="${p.level || ""}"></div>`;
  }).join("");
  const labeled = upgToons();
  const targetOpts = labeled.length
    ? labeled.map((n) => { const p = state.toonProfiles[n]; return `<option value="${escapeHtml(n)}">${escapeHtml(n)} — ${escapeHtml(p.race || "?")} ${escapeHtml(p.class)}${p.level ? " L" + p.level : ""}</option>`; }).join("")
    : `<option value="">— label a toon above —</option>`;
  panel.innerHTML =
    `<details class="upg-toons"${labeled.length ? "" : " open"}>
       <summary>Label toons — class · race · level <span class="upg-sum">(${labeled.length}/${state.toons.length} set)</span></summary>
       <div class="upg-toon-list">${toonRows}</div>
     </details>
     <div class="upg-controls">
       <label>Toon <select class="input input-sm upg-target">${targetOpts}</select></label>
       <label class="upg-mode"><input type="radio" name="upgMode" value="bags" checked> Bags</label>
       <label class="upg-mode"><input type="radio" name="upgMode" value="tlp"> TLP market</label>
       <label class="upg-thresh" hidden>Max <input type="number" class="input input-num upg-cap" value="5000" min="0" step="500">p</label>
       <label class="upg-dungeon"><input type="checkbox" class="upg-dung"> Dungeon-farmable only</label>
       <button class="btn btn-accent btn-sm upg-scan" type="button"${labeled.length ? "" : " disabled"}>Find upgrades</button>
     </div>
     <details class="upg-legend">
       <summary>How upgrades are ranked — read me</summary>
       <div class="upg-legend-body">
         <p>Ranks candidate gear by a <strong>class-weighted stat score</strong> against what you have equipped in each slot.</p>
         <ul>
           <li><strong>Weighted highest:</strong> AC · HP · Mana · Resists</li>
           <li><strong>Melee</strong> (Monk…): STR · DEX · Attack · Haste &nbsp;·&nbsp; <strong>Priest/Caster:</strong> WIS/INT · Mana</li>
         </ul>
         <p><strong>Guards:</strong> won't replace gear that has a <em>click/proc/focus effect</em> (epics, clickies, haste) with an effectless item · respects your <em>recommended level</em> (no off-era loot) · melee weapon hands need a real weapon (dmg&nbsp;&gt;&nbsp;0) · effect items get a score bonus.</p>
         <p class="dim">⚠ It's a <strong>shortlist</strong>, not best-in-slot. It can't judge how <em>good</em> an effect, aug, or set bonus is — always hover the item to read its tooltip before swapping.</p>
       </div>
     </details>
     <div class="upg-results"><p class="dim upg-pad">Label a toon, choose Bags or TLP, then Find upgrades.</p></div>`;

  const refreshTargets = () => {
    const lab = upgToons(); const sel = panel.querySelector(".upg-target"); const cur = sel.value;
    sel.innerHTML = lab.length ? lab.map((n) => { const p = state.toonProfiles[n]; return `<option value="${escapeHtml(n)}">${escapeHtml(n)} — ${escapeHtml(p.race || "?")} ${escapeHtml(p.class)}${p.level ? " L" + p.level : ""}</option>`; }).join("") : `<option value="">— label a toon above —</option>`;
    if (lab.includes(cur)) sel.value = cur;
    panel.querySelector(".upg-scan").disabled = !lab.length;
    const sum = panel.querySelector(".upg-sum"); if (sum) sum.textContent = `(${lab.length}/${state.toons.length} set)`;
  };
  panel.querySelectorAll(".upg-toon").forEach((row) => {
    const name = row.dataset.toon;
    const upd = (field, val) => { state.toonProfiles[name] = { ...(state.toonProfiles[name] || {}), [field]: val }; saveToonProfiles(); refreshTargets(); };
    row.querySelector(".upg-class").addEventListener("change", (e) => upd("class", e.target.value));
    row.querySelector(".upg-race").addEventListener("change", (e) => upd("race", e.target.value));
    row.querySelector(".upg-level").addEventListener("change", (e) => upd("level", parseInt(e.target.value, 10) || 0));
  });
  const threshWrap = panel.querySelector(".upg-thresh");
  panel.querySelectorAll('input[name="upgMode"]').forEach((r) => r.addEventListener("change", () => {
    threshWrap.hidden = panel.querySelector('input[name="upgMode"]:checked').value !== "tlp";
  }));
  panel.querySelector(".upg-dung").addEventListener("change", paintUpgrades);
  // Click a slot row (one with a "+N") to reveal/hide that slot's ranked alternatives.
  // Guarded: renderUpgradePanel re-runs on every tab open, and a double-bound toggle
  // would cancel itself.
  if (!panel.dataset.upgClickBound) {
    panel.dataset.upgClickBound = "1";
    panel.addEventListener("click", (e) => {
      const cell = e.target.closest(".upg-slot-x"); if (!cell) return;
      const slot = cell.dataset.slot; if (!slot) return;
      const alts = panel.querySelectorAll(`.upg-alt[data-altof="${CSS.escape(slot)}"]`);
      if (!alts.length) return;
      const show = alts[0].hasAttribute("hidden");
      alts.forEach((tr) => tr.toggleAttribute("hidden", !show));
      const caret = cell.querySelector(".upg-caret"); if (caret) caret.textContent = show ? "▾" : "▸";
    });
  }
  panel.querySelector(".upg-scan").addEventListener("click", async () => {
    const name = panel.querySelector(".upg-target").value; if (!name) return;
    const prof = { ...state.toonProfiles[name], name };
    const mode = panel.querySelector('input[name="upgMode"]:checked').value;
    const results = panel.querySelector(".upg-results");
    if (mode === "tlp") {
      const thr = parseInt(panel.querySelector(".upg-cap").value, 10) || 0;
      results.innerHTML = `<p class="dim upg-pad">Scanning the TLP catalog…</p>`;
      try { state._lastUpg = { rows: await scanTLP(prof, thr), prof, mode }; paintUpgrades(); }
      catch (e) { results.innerHTML = `<p class="dim upg-pad">Couldn't reach the catalog (${escapeHtml(e.message || "error")}). Run the app via serve.py so /api works.</p>`; }
    } else {
      state._lastUpg = { rows: scanBags(prof), prof, mode }; paintUpgrades();
    }
  });
  panel.addEventListener("mousemove", upgTipMove);
  panel.addEventListener("mouseleave", hideItemTip);
  if (state._lastUpg) paintUpgrades();   // restore last results when re-opening the tab
}

// ===== "Who Has It" — per-toon gear sets + roster scan (gear distributor) ======
// You name a TARGET toon, build/save that toon's wanted gear
// set (custom, persisted), then the scan walks EVERY loaded dump and shows, per
// wanted item, who currently holds a copy (equipped / bags / bank) so he knows
// which toon to log into, dequip, and trade/parcel it over. Delivery is MANUAL.
//   movable  = NOT free-trade-nodrop (Frostreaver is free-trade; fvnodrop=1 blocks)
//   attunable + found WORN = may already be attuned to the holder -> flag, no promise

// Preferred source order for "who do I pull it from": a copy in BAGS needs no dequip
// and can't be attuned; bank/shared next; a WORN copy last (must dequip, and if
// attuneable it is likely stuck on that toon).
// How ANNOYING each location is to actually retrieve, best first. Reordered 2026-07-22:
// TrixBox auto-dequips now, so a WORN copy is cheap — while the HOARD has no MQ automation
// at all (not in MQ's slot-name reference; it's a manual search + Retrieve), so it ranks
// last. Bank is reachable at a banker. This is the tiebreak after holder-consolidation.
const DIST_BUCKET_RANK = { bags: 0, equipped: 1, bank: 2, shared: 3, keyring: 4, depot: 5, hoard: 6, persona: 7, other: 8 };
// Pick the holder to pull from. If a `coverage` map is given (toon -> # of needed items
// it can supply), prefer the highest-coverage toon first (fewer logins), then bags-before
// -worn. Without coverage, just bags-before-worn.
function bestSource(sources, coverage) {
  return sources.slice().sort((a, b) => {
    if (coverage) {
      const ca = coverage.get(a.toon) || 0, cb = coverage.get(b.toon) || 0;
      if (cb !== ca) return cb - ca;
    }
    return (DIST_BUCKET_RANK[a.bucket] ?? 9) - (DIST_BUCKET_RANK[b.bucket] ?? 9);
  })[0] || null;
}

// Cross every wanted item for the target toon against the aggregate inventory.
// One result row per wanted item.
function scanWhoHas(setName, targetToon) {
  const wanted = state.gearSets[setName] || [];
  const invById = new Map(), invByName = new Map();
  for (const e of state.inventory) {
    if (e.id) invById.set(e.id, e);
    invByName.set((e.name || "").toLowerCase(), e);
  }
  // Which toons are TARGETS of an active set, and which item ids that set needs. Those
  // copies are off-limits as a source for other sets (a target's own kit is not a pool),
  // unless this set is flagged free-for-all via "Respect other sets".
  const protectedIds = new Map();
  if (!state.gearSetFree[setName]) {
    for (const [nm, its] of Object.entries(state.gearSets)) {
      if (state.gearSetInactive[nm] || nm === setName) continue;
      const t = state.gearSetTargets[nm];
      if (!t || t === targetToon) continue;
      let s = protectedIds.get(t);
      if (!s) { s = new Set(); protectedIds.set(t, s); }
      for (const it of its || []) if (it.id) s.add(it.id);
    }
  }

  const rows = wanted.map((w) => {
    const inv = (w.id && invById.get(w.id)) || invByName.get((w.name || "").toLowerCase()) || null;
    const rec = w.id && state.db ? state.db.byId.get(w.id) : null;
    const movable = rec ? rec.fvnodrop === 0 : true;   // free-trade: only fvnodrop blocks
    const attunable = rec ? rec.attunable === 1 : false;
    const slot = w.slot || (w.id ? (itemSlots({ id: w.id })[0] || "") : "");
    const sources = inv ? inv.sources.map((s) => ({ ...s, bucket: locBucket(s.location) })) : [];
    const owned = sources.filter((s) => s.toon === targetToon);
    const othersAll = sources.filter((s) => s.toon !== targetToon);
    // A toon that is itself the TARGET of another active set keeps the pieces that set
    // needs — don't strip one toon to dress another when the first one's own set wants it.
    const others = othersAll.filter((s) => !(protectedIds.get(s.toon) || EMPTY_SET).has(w.id));
    const ownerBlocked = othersAll.length > 0 && others.length === 0;
    return { w, rec, inv, movable, attunable, slot, owned, others, othersAll, ownerBlocked, best: null };
  });

  // Consolidate holders: when a needed item has SEVERAL holders, prefer the toon that
  // supplies the MOST other needed items — so you log into fewer toons (don't
  // trek to the SK for one neck if a toon he's already sourcing from has it too). Coverage
  // = how many needed items each toon can supply; bags-before-worn is only the tiebreak.
  const coverage = new Map();
  for (const r of rows) {
    if (!r.owned.length && r.others.length) {
      for (const t of new Set(r.others.map((s) => s.toon))) coverage.set(t, (coverage.get(t) || 0) + 1);
    }
  }
  for (const r of rows) r.best = bestSource(r.others, coverage);
  return rows;
}

// A wanted item's status pill + whether it needs a move from another toon.
function distStatus(row) {
  if (row.owned.length) {
    const worn = row.owned.some((s) => s.bucket === "equipped");
    return { need: false, cls: "dist-ok", txt: worn ? "✓ equipped" : "✓ owned — equip it" };
  }
  if (row.best) return { need: true, cls: "dist-move", txt: `→ move from ${row.best.toon}` };
  return { need: true, cls: "dist-none", txt: "✗ nobody has it" };
}

// Owned items (aggregate, across loaded toons) valid for `slotName`, usable by the
// target's class when it's labeled (else all classes). Each: {id,name,ac,hp,held};
// AC-sorted desc so the strongest option is first in the dropdown.
function slotCandidates(slotName, targetToon) {
  const prof = state.toonProfiles[targetToon];
  const cls = prof && prof.class;
  const out = [];
  for (const e of state.inventory) {
    if (!e.id) continue;
    if (!itemSlots({ id: e.id }).includes(slotName)) continue;
    if (cls && e.classes && e.classes.length && !e.classes.includes(cls)) continue;
    const rec = state.db.byId.get(e.id);
    const tip = (rec && rec.tip) || {};
    out.push({ id: e.id, name: e.name, ac: tip.ac || 0, hp: tip.hp || 0, held: e.location });
  }
  out.sort((a, b) => (b.ac - a.ac) || a.name.localeCompare(b.name));
  return out;
}

// A target toon's currently-WORN gear, head-to-toe (from its own dump), so you
// can see the loadout he's filling. Equipped items carry the slot as their location.
function equippedLoadout(targetToon) {
  const t = state.toons.find((x) => x.name === targetToon);
  if (!t) return [];
  const base = (loc) => (loc || "").split("-")[0].trim();
  return t.items
    .filter((it) => it.name && it.name !== "Empty" && locBucket(it.location) === "equipped")
    .map((it) => ({ name: it.name, id: it.id, slot: base(it.location) }))
    .sort((a, b) => { const ia = SLOT_ORDER.indexOf(a.slot), ib = SLOT_ORDER.indexOf(b.slot); return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib); });
}

// Paired slots (two of each on a paperdoll) get two rows.
const PAIRED_SLOTS = new Set(["Ear", "Wrist", "Fingers"]);
const EMPTY_SET = new Set();

// Which slot a SET ENTRY occupies. Multi-slot items (a rapier is Primary+Secondary,
// slots bitmask 24576) must remember the slot you actually picked — deriving it from the
// bitmask always returns the FIRST slot, so a Secondary rapier got filed under Primary
// and the row wouldn't take it (2026-07-22). Older entries have no .slot: fall back.
function setEntrySlot(it) {
  if (it && it.slot) return it.slot;
  return (it && it.id ? (itemSlots({ id: it.id })[0] || "?") : "?");
}

// How many copies of each item every gear set has claimed, and which sets claim it.
// Used to stop two sets (or two slots of one set) fighting over the same physical item
// when you only own one — but to ALLOW it when you own multiples.
function buildGearClaims() {
  const count = new Map(), by = new Map();
  for (const [name, items] of Object.entries(state.gearSets)) {
    if (state.gearSetInactive[name]) continue;          // retired: reserves nothing
    for (const it of items || []) {
      if (!it.id) continue;
      count.set(it.id, (count.get(it.id) || 0) + 1);
      if (!by.has(it.id)) by.set(it.id, new Set());
      by.get(it.id).add(name);
    }
  }
  return { count, by };
}

// Human label for where an item physically sits, so you can actually FIND it mid-transfer.
// Returns {badge, detail} — badge is the coarse place, detail the exact bag/slot.
const LOC_LABEL = { equipped: "worn", bags: "bags", bank: "bank", shared: "shared", hoard: "hoard", depot: "depot", keyring: "keyring", persona: "persona", other: "" };
function prettyLoc(loc) {
  const b = locBucket(loc);
  const badge = LOC_LABEL[b] || "";
  // "worn" and "shared" are self-explanatory; for bags/bank/hoard the exact slot matters.
  const detail = (b === "equipped" || b === "shared" || b === "keyring" || b === "persona" || b === "depot") ? "" : (loc || "");
  return { badge: badge || (loc || ""), detail, bucket: b };
}

// The single "Where it is" cell: folds the old Status + Held-by + Move? columns into one
// readable cell. Warnings (No Trade / maybe-attuned) show as a small icon instead of the
// confusing "Trade/Parcel" text (that was just the normal case).
function whereCell(r, targetToon, chosen, equip) {
  if (!chosen) return equip ? `<span class="w-ok">✓ equipped</span>` : `<span class="dim">—</span>`;
  if (!r) return `<span class="dim">—</span>`;
  const st = distStatus(r);
  if (!st.need) {
    const src = r.owned[0];
    if (src && locBucket(src.location) === "equipped") return `<span class="w-ok">✓ equipped</span>`;
    const p = src ? prettyLoc(src.location) : { badge: "", detail: "" };
    return `<span class="w-ok">✓ on ${escapeHtml(targetToon)}</span>` +
      (p.badge ? ` <span class="w-badge w-b-${p.bucket}">${escapeHtml(p.badge)}</span>` : "") +
      (p.detail ? ` <span class="w-loc">${escapeHtml(p.detail)}</span>` : "");
  }
  if (!r.best) {
    // The only copies sit on toons who are themselves being geared and need them.
    if (r.ownerBlocked) {
      const who = [...new Set((r.othersAll || []).map((s) => s.toon))].join(", ");
      return `<span class="w-none">in use by ${escapeHtml(who)}</span>` +
        `<span class="w-loc"> (their own set needs it)</span>`;
    }
    return `<span class="w-none">nobody has it</span>`;
  }
  const b = r.best, p = prettyLoc(b.location);
  const extra = r.others.length > 1
    ? ` <span class="dist-more" title="${escapeHtml(r.others.map((s) => `${s.toon} · ${s.location}`).join("\n"))}">+${r.others.length - 1}</span>` : "";
  const warn = !r.movable
    ? ` <span class="dist-notrade" title="No Trade — cannot be moved to ${escapeHtml(targetToon)}">⛔</span>`
    : (r.attunable && b.bucket === "equipped")
      ? ` <span class="dist-attune" title="Attuneable and worn on ${escapeHtml(b.toon)} — may be attuned/stuck. Confirm in game.">⚠</span>` : "";
  return `<span class="w-toon">${escapeHtml(b.toon)}</span>` +
    ` <span class="w-badge w-b-${p.bucket}">${escapeHtml(p.badge)}</span>` +
    (p.detail ? ` <span class="w-loc">${escapeHtml(p.detail)}</span>` : "") + extra + warn;
}

// The loadout table: ONE row per physical gear slot (head-to-toe, paired slots twice).
// The Item column is a DROPDOWN whose default is "keep equipped" — pick a piece to bring
// in, leave it to keep what the target wears. Status/Held/Move reflect the chosen piece.
// This is the whole builder: no separate add control, no × — the dropdown IS the editor.
function renderDistResults(container, setName, targetToon) {
  const set = state.gearSets[setName] || [];
  const scan = scanWhoHas(setName, targetToon);
  const rowByKey = new Map();
  scan.forEach((r) => rowByKey.set(r.w.id ? `#${r.w.id}` : (r.w.name || "").toLowerCase(), r));

  // equipped items grouped by base slot (ordered), and chosen (set) items grouped by slot.
  const equipBySlot = new Map();
  equippedLoadout(targetToon).forEach((w) => {
    if (!equipBySlot.has(w.slot)) equipBySlot.set(w.slot, []);
    equipBySlot.get(w.slot).push(w);
  });
  const chosenBySlot = new Map();
  set.forEach((it, i) => {
    const slot = setEntrySlot(it);
    if (!chosenBySlot.has(slot)) chosenBySlot.set(slot, []);
    chosenBySlot.get(slot).push({ it, i });
  });

  const plan = new Map();
  for (const r of scan) {
    const st = distStatus(r);
    if (st.need && r.best && r.movable && !(r.attunable && r.best.bucket === "equipped"))
      plan.set(r.best.toon, (plan.get(r.best.toon) || 0) + 1);
  }
  const planLine = plan.size
    ? `<div class="dist-plan"><strong>Log into:</strong> ` +
      [...plan.entries()].sort((a, b) => b[1] - a[1])
        .map(([t, n]) => `<span class="dist-src">${escapeHtml(t)} <span class="dist-cnt">${n}</span></span>`).join(" ") +
      ` <span class="dim">— dequip if worn, then trade/parcel to ${escapeHtml(targetToon)}.</span></div>`
    : "";

  const rowSlots = [];
  for (const s of SLOT_ORDER) { rowSlots.push(s); if (PAIRED_SLOTS.has(s)) rowSlots.push(s); }
  const seen = new Map();

  // Reservation math: copies you own vs. copies already claimed by any set (including
  // this set's other slots). An item with no free copy is shown reserved + unselectable.
  const claims = buildGearClaims();
  const ownedById = new Map();
  for (const e of state.inventory) if (e.id) ownedById.set(e.id, e.count || 0);
  const respectClaims = !state.gearSetFree[setName];   // per-set toggle (default: respect)

  const body = rowSlots.map((slot) => {
    const k = seen.get(slot) || 0; seen.set(slot, k + 1);
    const chosen = (chosenBySlot.get(slot) || [])[k];   // {it,i} or undefined
    const equip = (equipBySlot.get(slot) || [])[k];     // {name,id,slot} or undefined
    const selId = chosen ? (chosen.it.id || 0) : 0;

    // dropdown options: keep-equipped default + this slot's class-usable owned candidates
    let cands = slotCandidates(slot, targetToon);
    if (chosen && chosen.it.id && !cands.some((c) => c.id === chosen.it.id)) {
      const rec = state.db.byId.get(chosen.it.id); const tip = (rec && rec.tip) || {};
      cands = [{ id: chosen.it.id, name: chosen.it.name, ac: tip.ac || 0, hp: tip.hp || 0, held: "" }, ...cands];
    }
    // Option text stays short (name + AC/HP) — the holder now lives in the Where column,
    // so the dropdown no longer truncates. Options with no free copy left (already claimed
    // by another set, or by another slot of this one) are marked reserved and disabled.
    const keepLabel = equip ? `— keep equipped: ${equip.name} —` : `— empty —`;
    const opts = `<option value="0"${selId === 0 ? " selected" : ""}>${escapeHtml(keepLabel)}</option>` +
      cands.map((c) => {
        const isMine = c.id === selId;
        const own = ownedById.get(c.id) || 0;
        const used = (claims.count.get(c.id) || 0) - (isMine ? 1 : 0);   // don't count our own pick
        const free = own - used;
        const others = [...(claims.by.get(c.id) || [])].filter((n) => n !== setName);
        const taken = !isMine && free <= 0;
        // Always LABEL the contention; only LOCK it when this set respects other sets.
        // Name the holders even when copies ARE free, so you can see what to retire to
        // free up more (e.g. "1 free of 5 · 4 held by Druid PL, Tank").
        let tail = "";
        if (taken) tail = others.length ? ` — ${respectClaims ? "reserved by" : "also in"} ${others.join(", ")}` : " — already used in this set";
        else if (own > 1) tail = ` · ${free} free of ${own}` + (others.length ? ` · ${own - free} held by ${others.join(", ")}` : "");
        return `<option value="${c.id}"${isMine ? " selected" : ""}${taken && respectClaims ? " disabled" : ""}>` +
          `${escapeHtml(c.name)} — AC ${c.ac}${c.hp ? ", HP " + c.hp : ""}${escapeHtml(tail)}</option>`;
      }).join("");
    const selCell = `<td class="dist-item"><select class="input input-sm loadout-sel" data-slot="${escapeHtml(slot)}" data-k="${k}">${opts}</select></td>`;

    const r = chosen ? rowByKey.get(chosen.it.id ? `#${chosen.it.id}` : (chosen.it.name || "").toLowerCase()) : null;
    let where = whereCell(r, targetToon, chosen, equip);
    // Over-claimed: more sets/slots want this item than you own copies of.
    if (chosen && chosen.it.id) {
      const own = ownedById.get(chosen.it.id) || 0;
      const tot = claims.count.get(chosen.it.id) || 0;
      if (tot > own) {
        const others = [...(claims.by.get(chosen.it.id) || [])].filter((n) => n !== setName);
        where += ` <span class="w-conflict" title="Claimed ${tot}× but you only own ${own}${others.length ? " — also in: " + others.join(", ") : ""}">⚑ only ${own} owned</span>`;
      }
    }
    return `<tr class="${chosen ? "" : "dist-owned"}"><td class="dist-slot">${escapeHtml(slot)}</td>${selCell}` +
      `<td class="dist-where">${where}</td></tr>`;
  }).join("");

  const need = scan.filter((r) => distStatus(r).need).length;
  const cls = state.toonProfiles[targetToon] && state.toonProfiles[targetToon].class;
  container.innerHTML =
    `<div class="upg-note">Every slot for <strong>${escapeHtml(targetToon)}</strong> — pick a piece to bring in, or leave a slot to keep what's equipped. ` +
    `<strong>${need}</strong> to source. Dropdowns show your owned items${cls ? ` usable by <strong>${escapeHtml(cls)}</strong>` : ""}, AC shown. ` +
    `<span class="dim">Where-it-is tells you the toon + exactly which bag/bank slot. ⛔ = No Trade, ⚠ = maybe attuned, ⚑ = claimed by more sets than you own copies. ` +
    `Pieces another set already reserved show as <em>reserved</em> and can't be picked unless you own a spare.</span></div>` +
    planLine +
    `<table class="upg-table dist-table loadout-table"><thead><tr><th>Slot</th><th>Item</th>` +
    `<th>Where it is</th></tr></thead><tbody>${body}</tbody></table>`;
}

// Escape a JS string for embedding in a double-quoted Lua string literal.
function luaStr(s) { return '"' + String(s == null ? "" : s).replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/\n/g, "\\n") + '"'; }

// Build the Lua text for the given set(s). `onlySet` (normal case) restricts it to the
// set you're looking at, so saved-but-dormant sets never leak into the plan TrixBox acts
// on. Emits a `plans` list (TrixBox handles 1..N) AND mirrors the first plan at the top
// level so an older single-plan TrixBox still works.
function buildPlansLua(onlySet) {
  const plans = [];
  // One physical item can only be sent ONCE. Cap what we emit per (holder, item) at the
  // number of copies that holder actually has, across ALL sets — otherwise several sets
  // each "reserve" the same piece and the plan promises gear that doesn't exist
  // (2026-07-22: 33 rows for 24 real items; one belt promised to 3 rogues).
  const emitted = new Map();
  const overcommitted = [];
  for (const [name, items] of Object.entries(state.gearSets)) {
    if (onlySet && name !== onlySet) continue;
    if (state.gearSetInactive[name]) continue;            // retired sets never ship
    if (!items || !items.length) continue;
    const tgt = state.gearSetTargets[name] || "";
    if (!tgt) continue;                                   // no "apply to" chosen yet
    const rows = [];
    for (const r of scanWhoHas(name, tgt)) {
      if (!distStatus(r).need || !r.best || !r.movable) continue;
      const key = `${r.best.toon}|${r.w.id}`;
      const cap = r.best.count || 1;
      const used = emitted.get(key) || 0;
      if (used >= cap) { overcommitted.push({ name: r.w.name, toon: r.best.toon, set: name, target: tgt, cap }); continue; }
      emitted.set(key, used + 1);
      rows.push(r);
    }
    if (!rows.length) continue;                           // nothing left to move for this set
    plans.push({ name, target: tgt, rows });
  }
  if (overcommitted.length) {
    log(`⚠ ${overcommitted.length} piece(s) dropped — only so many copies exist: ` +
        overcommitted.slice(0, 6).map((o) => `${o.name} (${o.toon} has ${o.cap}, also wanted by ${o.set}→${o.target})`).join("; ") +
        (overcommitted.length > 6 ? ` …and ${overcommitted.length - 6} more` : ""));
  }
  if (!plans.length) return null;
  const planLua = plans.map((p) => {
    const moves = p.rows.map((r) =>
      `      { id = ${r.w.id || 0}, name = ${luaStr(r.w.name)}, from = ${luaStr(r.best.toon)}, ` +
      `to = ${luaStr(p.target)}, slot = ${luaStr(r.slot || "")}, fromLoc = ${luaStr(r.best.location)}, ` +
      `attuneRisk = ${r.attunable && r.best.bucket === "equipped" ? "true" : "false"} },`).join("\n");
    return `    { name = ${luaStr(p.name)}, set = ${luaStr(p.name)}, target = ${luaStr(p.target)},\n` +
           `      moves = {\n${moves}\n      } },`;
  }).join("\n");
  const first = plans[0];
  const firstMoves = first.rows.map((r) =>
    `    { id = ${r.w.id || 0}, name = ${luaStr(r.w.name)}, from = ${luaStr(r.best.toon)}, ` +
    `to = ${luaStr(first.target)}, slot = ${luaStr(r.slot || "")}, fromLoc = ${luaStr(r.best.location)}, ` +
    `attuneRisk = ${r.attunable && r.best.bucket === "equipped" ? "true" : "false"} },`).join("\n");
  const header =
    `-- Gear plans - generated by EQ Forge Gear Planner\n` +
    plans.map((p) => `--   ${p.name} -> ${p.target} (${p.rows.length} move(s))`).join("\n") + `\n` +
    `-- In game (mailgear): /mailgear plans, /mailgear dequip (on a holder),\n` +
    `-- /mailgear getbank (at a banker), /mailgear equip (on the target).\n` +
    `-- Dry-run until /mailgear live on.  TrixBox users: /trix plans, /trix sendgear.\n`;
  const text = header +
    `return {\n  plans = {\n${planLua}\n  },\n` +
    `  -- back-compat: first plan mirrored for older TrixBox builds\n` +
    `  name = ${luaStr(first.name)}, set = ${luaStr(first.name)}, target = ${luaStr(first.target)},\n` +
    `  moves = {\n${firstMoves}\n  },\n}\n`;
  return { text, plans };
}

// Write the plan file. This is a LOCAL app, so serve.py writes it straight into MQ's
// config folder — no picker, no Downloads, nothing for you to move. The download is a
// last-resort fallback for when the page isn't being served by serve.py.
async function writePlanFile(text) {
  try {
    const r = await fetch("/gearplan", { method: "POST", headers: { "Content-Type": "text/plain" }, body: text });
    if (r.ok) {
      const j = await r.json();
      if (j && j.ok) return j.path || "mailgearplan.lua";
      log(`Server couldn't write the plan: ${j && j.error ? j.error : "unknown error"}`);
    }
  } catch { /* not served by serve.py — fall through to a download */ }
  const blob = new Blob([text], { type: "text/plain" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "mailgearplan.lua";
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  return "your Downloads folder (run the app via run.bat so it writes to MQ directly)";
}

// Build a parcel "source" for DerpleDude's parcel Lua: a named filter matching exactly
// this plan's item ids. You pick it in /lua run parcel, eyeball the list, hit Send —
// so we never drive the parcel window ourselves.
function buildParcelSourceLua(plans) {
  const blocks = plans.map((p) => {
    const ids = [...new Set(p.rows.map((r) => r.w.id).filter(Boolean))];
    const idTable = ids.map((id) => `[${id}]=true`).join(", ");
    return `    {\n` +
      `        name = ${luaStr(`Gear Plan: ${p.name} -> ${p.target} (${ids.length})`)},\n` +
      `        filter = function(item)\n` +
      `            local ids = { ${idTable} }\n` +
      `            return ids[(item.ID() or 0)] == true\n` +
      `        end,\n` +
      `    },`;
  }).join("\n");
  return `-- Auto-generated by EQ Forge Gear Planner - do NOT hand-edit (it is overwritten).\n` +
    `-- Chain-loaded by config/parcel_sources.lua so the current gear plan appears as a\n` +
    `-- pickable source in DerpleDude's parcel tool. Review the list there, then Send.\n` +
    plans.map((p) => `--   ${p.name} -> ${p.target} (${p.rows.length} item(s))`).join("\n") + `\n` +
    `return {\n${blocks}\n}\n`;
}

// Send ONLY the active set's plan to TrixBox (replacing whatever was there). Other saved
// sets stay local — they're never written to the file the game reads.
async function exportGearPlan() {
  // Send EVERY active set, so one export covers everything a holder owes across all your
  // sets (no logging in and out per set). Retired sets are excluded — that's what the
  // Active toggle is for.
  const built = buildPlansLua(null);
  if (!built) {
    log("Nothing to send — no active set has movable pieces still needing sourcing (check “Apply to”, or un-retire a set).");
    return;
  }
  const where = await writePlanFile(built.text);
  // Also publish it as a parcel source so the parcel tool can load the same list.
  let parcelOk = false;
  try {
    const r = await fetch("/parcelsource", { method: "POST", headers: { "Content-Type": "text/plain" },
                                             body: buildParcelSourceLua(built.plans) });
    parcelOk = r.ok && (await r.json()).ok === true;
  } catch { /* not served by serve.py */ }
  const total = built.plans.reduce((n, p) => n + p.rows.length, 0);
  log(`Sent ${built.plans.length} active set(s), ${total} move(s) → ${where}: ` +
      built.plans.map((p) => `${p.name} → ${p.target} (${p.rows.length})`).join(", ") +
      (parcelOk ? ". Each is a parcel source too." : "") + " Retired sets were not sent.");
}

// Across EVERY active set: which toon is holding how many pieces that still need moving,
// who they're going to, and where they physically sit. Lets you work biggest -> smallest
// instead of discovering a toon owes one item after he's already logged out of them.
function holderWorkload() {
  const holders = new Map();
  for (const [name, items] of Object.entries(state.gearSets)) {
    if (state.gearSetInactive[name] || !items || !items.length) continue;
    const tgt = state.gearSetTargets[name];
    if (!tgt) continue;
    for (const r of scanWhoHas(name, tgt)) {
      const st = distStatus(r);
      if (!st.need || !r.best || !r.movable) continue;
      let h = holders.get(r.best.toon);
      if (!h) { h = { total: 0, byTarget: new Map(), buckets: {} }; holders.set(r.best.toon, h); }
      h.total++;
      h.byTarget.set(tgt, (h.byTarget.get(tgt) || 0) + 1);
      h.buckets[r.best.bucket] = (h.buckets[r.best.bucket] || 0) + 1;
    }
  }
  return [...holders.entries()].map(([toon, h]) => ({ toon, ...h })).sort((a, b) => b.total - a.total);
}

function renderWorkload(container) {
  if (!container) return;
  const rows = holderWorkload();
  if (!rows.length) { container.innerHTML = `<p class="dim">Nothing outstanding — every active set's pieces are already on their target.</p>`; return; }
  const grand = rows.reduce((n, h) => n + h.total, 0);
  const body = rows.map((h) => {
    const to = [...h.byTarget.entries()].sort((a, b) => b[1] - a[1])
      .map(([t, n]) => `<span class="dist-src">${escapeHtml(t)} <span class="dist-cnt">${n}</span></span>`).join(" ");
    const where = ["bags", "equipped", "bank", "shared", "depot", "hoard", "persona", "keyring", "other"]
      .filter((b) => h.buckets[b])
      .map((b) => `<span class="w-badge w-b-${b}">${escapeHtml(LOC_LABEL[b] || b)} ${h.buckets[b]}</span>`).join(" ");
    // Hoard is the only storage with no automation — flag toons whose pile is mostly hoard.
    const manual = (h.buckets.hoard || 0) + (h.buckets.persona || 0);
    const hoardNote = manual ? ` <span class="wl-hoard" title="Hoard pieces need a manual search+Retrieve; persona pieces need a persona switch">${manual} manual</span>` : "";
    return `<tr><td class="wl-toon">${escapeHtml(h.toon)}</td><td class="wl-n">${h.total}</td>` +
      `<td class="wl-to">${to}</td><td class="wl-where">${where}${hoardNote}</td></tr>`;
  }).join("");
  container.innerHTML =
    `<div class="upg-note">${grand} piece(s) still to move, across ${rows.length} holder(s). Work top-down — log into the biggest first.</div>` +
    `<table class="upg-table wl-table"><thead><tr><th>Log into</th><th>Pieces</th><th>Send to</th><th>Where they are</th></tr></thead><tbody>${body}</tbody></table>`;
}

// The Gear Planner tab: named reusable gear sets, a target toon's equipped loadout,
// and a who-has-it scan for the set. Items are added from the LEFT inventory pane
// (filter + select + "→ Gear set"); this tab manages the sets and shows results.
function renderGearPlanner() {
  const panel = $("distPanel"); if (!panel) return;
  if (!state.db) { panel.innerHTML = `<p class="dim upg-pad">Item DB still loading…</p>`; return; }
  if (!state.toons.length) { panel.innerHTML = `<p class="dim upg-pad">Load your characters' inventory dumps (left) first — the scan reads every loaded toon.</p>`; return; }

  const names = state.toons.map((t) => t.name);
  const setNames = Object.keys(state.gearSets);
  if (state.gearSetName && !setNames.includes(state.gearSetName)) state.gearSetName = "";
  if (!state.gearSetName && setNames.length) state.gearSetName = setNames[0];
  const active = state.gearSetName;
  // Each set remembers its own "apply to" toon, so switching sets restores its target.
  // NEVER write a fallback back into the set: if that toon's dump just isn't loaded this
  // session, silently retargeting the set would send its gear to the wrong character.
  // Only the dropdown's change handler may change a set's target.
  const remembered = state.gearSetTargets[active];
  if (remembered && names.includes(remembered)) state.distTarget = remembered;
  if (!names.includes(state.distTarget)) state.distTarget = names[0];
  const target = state.distTarget;
  if (active && !state.gearSetTargets[active]) { state.gearSetTargets[active] = target; saveGearTargets(); }
  const targetStale = remembered && !names.includes(remembered);

  const rerender = () => renderGearPlanner();

  // No sets yet — offer to create the first one.
  if (!setNames.length) {
    panel.innerHTML =
      `<div class="dist-head"><span class="dim">No gear sets yet. Create one (e.g. "Druid BIS"), then build it from the inventory on the left.</span></div>
       <button class="btn btn-accent btn-sm dist-new" type="button">+ New gear set</button>`;
    panel.querySelector(".dist-new").addEventListener("click", () => {
      const nm = (prompt('Name this gear set (e.g. "Druid BIS", "Tank raid"):', "") || "").trim();
      if (!nm) return;
      if (state.gearSets[nm]) { log(`Set "${nm}" already exists.`); return; }
      state.gearSets[nm] = []; state.gearSetName = nm; saveGearSets(); rerender();
    });
    return;
  }

  const setOpts = setNames.map((n) => `<option value="${escapeHtml(n)}"${n === active ? " selected" : ""}>${escapeHtml(n)} (${state.gearSets[n].length})${state.gearSetInactive[n] ? " · retired" : ""}</option>`).join("");
  const targetOpts = names.map((n) => `<option value="${escapeHtml(n)}"${n === target ? " selected" : ""}>${escapeHtml(n)}</option>`).join("");

  panel.innerHTML =
    `<div class="dist-head">
       <label>Set <select class="input input-sm dist-set">${setOpts}</select></label>
       <button class="btn btn-ghost btn-sm dist-new" type="button" title="Create a new named set">+ New</button>
       <button class="btn btn-ghost btn-sm dist-rename" type="button" title="Rename this set">Rename</button>
       <button class="btn btn-ghost btn-sm dist-del" type="button" title="Delete this set">Delete</button>
       <span class="dist-sep"></span>
       <label>Apply to <select class="input input-sm dist-target">${targetOpts}</select></label>
       <label title="${escapeHtml(target)}'s class — filters every slot dropdown to gear they can actually wear. Shared with the Upgrades tab.">as <select class="input input-sm dist-class">${classOptions((state.toonProfiles[target] || {}).class)}</select></label>
       <span class="dist-sep"></span>
       <label class="toggle dist-active" title="Retire a set you're done with: it keeps its contents but stops reserving pieces, freeing them for every other set.">
         <input type="checkbox"${state.gearSetInactive[active] ? "" : " checked"}> Active
       </label>
       <label class="toggle dist-respect" title="ON: pieces another ACTIVE set claimed are locked (unless you own a spare). OFF: this set may take them anyway.">
         <input type="checkbox"${state.gearSetFree[active] ? "" : " checked"}> Respect other sets
       </label>
       <span class="dist-sep"></span>
       <button class="btn btn-accent btn-sm dist-export" type="button" title="OPTIONAL — requires MacroQuest. Writes every active set's plan into MQ's config folder as mailgearplan.lua (for the included mailgear script: dequip / getbank / equip) and parcel_gearplan.lua (for DerpleDude's parcel tool, to deliver them); retired sets excluded. No MQ? Ignore this button — the list above is the worklist. Setup: see MQ-SETUP.md">⇩ Send plans to MQ</button>
     </div>
     ${targetStale ? `<p class="dist-stale">⚠ This set targets <strong>${escapeHtml(remembered)}</strong>, whose dump isn't loaded — showing <strong>${escapeHtml(target)}</strong> instead. Load ${escapeHtml(remembered)}'s dump (or pick a toon above) before sending, or it'll plan against the wrong character.</p>` : ""}
     <details class="dist-workload" open>
       <summary>Work order — who to log into (all active sets)</summary>
       <div class="wl-body"></div>
     </details>
     <p class="dist-hint dim">"<strong>${escapeHtml(active)}</strong>" for ${escapeHtml(target)}: every slot is a row. Use each slot's <strong>dropdown</strong> to pick the piece you want; leave a slot to keep what's equipped. The Status/Held-by/Move columns update for your pick.</p>
     <p class="dist-hint dim">This is a <strong>worklist you act on yourself</strong> — log into the holder, dequip, parcel it over. "⇩ Send plans to MQ" is <strong>optional</strong> and needs <strong>MacroQuest</strong>: the included <code>mailgear</code> script does the dequip/bank/equip (<code>/mailgear</code>), DerpleDude's <code>parcel</code> delivers. See <strong>MQ-SETUP.md</strong>. Without MQ, just read the rows.</p>
     <div class="dist-results"></div>`;

  panel.querySelector(".dist-set").addEventListener("change", (e) => { state.gearSetName = e.target.value; rerender(); });
  panel.querySelector(".dist-target").addEventListener("change", (e) => {
    state.distTarget = e.target.value;
    if (active) { state.gearSetTargets[active] = e.target.value; saveGearTargets(); }
    rerender();
  });
  // Class lives on the TOON (shared with the Upgrades tab), not the set — it's a fact
  // about the character, and it's what filters every slot dropdown.
  panel.querySelector(".dist-class").addEventListener("change", (e) => {
    const cls = e.target.value;
    state.toonProfiles[target] = { ...(state.toonProfiles[target] || {}), class: cls };
    saveToonProfiles();
    log(cls ? `${target} set to ${cls} — slot dropdowns now show only ${cls}-usable gear.`
            : `${target}'s class cleared — dropdowns show every item valid for the slot.`);
    rerender();
  });
  panel.querySelector(".dist-active input").addEventListener("change", (e) => {
    if (e.target.checked) delete state.gearSetInactive[active]; else state.gearSetInactive[active] = true;
    saveGearInactive();
    log(e.target.checked ? `"${active}" is active again — it reserves its pieces.`
                         : `"${active}" retired — its pieces are now free for other sets.`);
    rerender();
  });
  panel.querySelector(".dist-respect input").addEventListener("change", (e) => {
    if (e.target.checked) delete state.gearSetFree[active]; else state.gearSetFree[active] = true;
    saveGearFree(); rerender();
  });
  panel.querySelector(".dist-export").addEventListener("click", () => exportGearPlan());

  panel.querySelector(".dist-new").addEventListener("click", () => {
    const nm = (prompt('Name the new gear set:', "") || "").trim();
    if (!nm) return;
    if (state.gearSets[nm]) { log(`Set "${nm}" already exists.`); return; }
    state.gearSets[nm] = []; state.gearSetName = nm; saveGearSets(); rerender();
  });
  panel.querySelector(".dist-rename").addEventListener("click", () => {
    const nm = (prompt(`Rename "${active}" to:`, active) || "").trim();
    if (!nm || nm === active) return;
    if (state.gearSets[nm]) { log(`Set "${nm}" already exists.`); return; }
    // Carry the set's SETTINGS across too — a rename that silently dropped the "apply to"
    // toon and the Active/Respect flags would quietly retarget the set.
    state.gearSets[nm] = state.gearSets[active]; delete state.gearSets[active];
    if (state.gearSetTargets[active] !== undefined) { state.gearSetTargets[nm] = state.gearSetTargets[active]; delete state.gearSetTargets[active]; }
    if (state.gearSetFree[active]) { state.gearSetFree[nm] = true; delete state.gearSetFree[active]; }
    if (state.gearSetInactive[active]) { state.gearSetInactive[nm] = true; delete state.gearSetInactive[active]; }
    state.gearSetName = nm;
    saveGearSets(); saveGearTargets(); saveGearFree(); saveGearInactive(); rerender();
  });
  panel.querySelector(".dist-del").addEventListener("click", () => {
    if (!confirm(`Delete gear set "${active}" (${state.gearSets[active].length} items)?`)) return;
    // Clean up its settings too, so a later set reusing the name doesn't inherit them.
    delete state.gearSets[active]; delete state.gearSetTargets[active];
    delete state.gearSetFree[active]; delete state.gearSetInactive[active];
    state.gearSetName = "";
    saveGearSets(); saveGearTargets(); saveGearFree(); saveGearInactive(); rerender();
  });

  // Bind the hover-tooltip handlers ONCE: #distPanel survives every re-render (only its
  // innerHTML is replaced), so re-adding them each time stacked a listener per render.
  if (!panel.dataset.distTipBound) {
    panel.dataset.distTipBound = "1";
    panel.addEventListener("mousemove", upgTipMove);
    panel.addEventListener("mouseleave", hideItemTip);
  }
  renderWorkload(panel.querySelector(".wl-body"));
  renderDistResults(panel.querySelector(".dist-results"), active, target);

  // Per-slot dropdowns ARE the editor. Value 0 = keep equipped (remove any chosen for
  // that slot-instance); a real id = set that slot-instance's piece. Slot-instance k maps
  // to the k-th set item whose slot matches (paired slots have two).
  panel.querySelectorAll(".loadout-sel").forEach((sel) => sel.addEventListener("change", (e) => {
    const slot = sel.dataset.slot, k = parseInt(sel.dataset.k, 10) || 0;
    const id = parseInt(e.target.value, 10) || 0;
    const set = state.gearSets[active] || (state.gearSets[active] = []);
    const chosenIdx = [];
    set.forEach((it, idx) => { if (setEntrySlot(it) === slot) chosenIdx.push(idx); });
    const existing = chosenIdx[k];
    if (id === 0) { if (existing !== undefined) set.splice(existing, 1); }
    else {
      const rec = state.db.byId.get(id);
      // Record the slot this was picked FOR, so a multi-slot item (rapier = Primary and
      // Secondary) stays in the row you chose instead of collapsing to its first slot.
      const newItem = { id, name: rec ? rec.name : `#${id}`, slot };
      if (existing !== undefined) set[existing] = newItem; else set.push(newItem);
    }
    saveGearSets(); rerender();
  }));
}

// Add the selected LEFT-pane inventory rows to the active gear set (the star of the
// new workflow: filter/sort your real inventory and click these in). Creates
// a first set on the fly if none exists yet.
function gearSetSelectedFromInv() {
  if (!state.invSel.size) { log("Select inventory rows on the left first, then → Gear set."); return; }
  if (!state.gearSetName || !state.gearSets[state.gearSetName]) {
    const nm = (prompt('No gear set yet — name one (e.g. "Druid BIS"):', "") || "").trim();
    if (!nm) return;
    if (!state.gearSets[nm]) state.gearSets[nm] = [];
    state.gearSetName = nm;
  }
  const set = state.gearSets[state.gearSetName];
  let n = 0;
  [...state.invSel].forEach((i) => {
    const inv = state.inventory[i]; if (!inv) return;
    const dup = set.some((x) => (inv.id && x.id === inv.id) || (!inv.id && x.name.toLowerCase() === inv.name.toLowerCase()));
    if (!dup) { set.push({ id: inv.id || 0, name: inv.name }); n++; }
  });
  saveGearSets();
  log(`Added ${n} item(s) to gear set "${state.gearSetName}"` + (n < state.invSel.size ? ` (${state.invSel.size - n} already in it)` : "") + ".");
  state.invSel.clear();
  $("invBody").querySelectorAll("tr.sel").forEach((tr) => tr.classList.remove("sel"));
  if (state.rightTab === "dist") renderGearPlanner();
}

// Effective plat for one sale line (krono folded at the live rate × qty).
function salePlat(priceStr, count) {
  const [kind] = classifyPrice(priceStr);
  const qty = count || 1;
  if (kind === "krono") {
    const kr = parseFloat((priceStr || "").replace(/[^0-9.]/g, "")) || 0;
    return kr * (state.kronoRate || DEFAULT_KRONO_RATE) * qty;
  }
  return parsePlatValue(priceStr) * qty;
}

// Mark the selected sell rows SOLD: log them (with the current price) and remove
// them from the sell list. Records at "now" so today's take can be tallied.
function markSoldSelected() {
  if (!state.aucSel.size) { log("Select the auction rows you just sold, then Sold."); return; }
  const now = Date.now();
  const names = [];
  let soldRows = 0;
  // High->low so splicing sold-out stacks doesn't shift the indices still to process.
  [...state.aucSel].sort((a, b) => b - a).forEach((i) => {
    const it = state.auction[i];
    if (!it) return;
    const have = it.count || 1;
    let q = have;
    if (have > 1) {   // stacked → ask how many of the stack actually sold (default = all)
      const ans = prompt(`How many "${it.name}" did you sell? (1–${have})`, String(have));
      if (ans === null) return;                                    // cancelled → leave the row alone
      q = parseInt(ans, 10);
      if (!Number.isFinite(q) || q < 1) { log(`Skipped "${it.name}" — enter a number 1–${have}.`); return; }
      q = Math.min(q, have);
    }
    state.sales.push({ name: it.name, id: it.id, price: it.price || "", count: q, at: now });
    names.push(q > 1 ? `${it.name} ×${q}` : it.name);
    soldRows++;
    if (q >= have) state.auction.splice(i, 1);                     // whole stack sold → remove
    else it.count = have - q;                                      // partial → keep the remainder listed
  });
  if (!soldRows) { log("Nothing marked sold."); return; }
  saveSales();
  refreshAuction();
  renderSold();
  log(`Marked ${soldRows} item(s) sold: ${names.slice(0, 5).join(", ")}${names.length > 5 ? "…" : ""}. Total take now ${salesTotals().all.toLocaleString()}p.`);
}
function removeSale(i) { state.sales.splice(i, 1); saveSales(); renderSold(); }
function clearSales() {
  if (!state.sales.length) return;
  if (!confirm(`Clear your entire sales history (${state.sales.length} sale(s))? This can't be undone.`)) return;
  state.sales = []; saveSales(); renderSold(); log("Sales history cleared.");
}

function salesTotals() {
  const startOfDay = new Date(); startOfDay.setHours(0, 0, 0, 0);
  const dayMs = startOfDay.getTime();
  let all = 0, today = 0;
  for (const s of state.sales) { const p = salePlat(s.price, s.count); all += p; if (s.at >= dayMs) today += p; }
  return { all: Math.round(all), today: Math.round(today) };
}
function saleWhen(at) {
  const d = new Date(at);
  const today = new Date(); today.setHours(0, 0, 0, 0);
  let h = d.getHours(); const ap = h < 12 ? "am" : "pm"; h = h % 12 || 12;
  const hm = `${h}:${String(d.getMinutes()).padStart(2, "0")}${ap}`;
  return at >= today.getTime() ? hm : `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
}

function renderSold() {
  const body = $("soldBody");
  if (body) {
    body.innerHTML = "";
    if (!state.sales.length) {
      body.innerHTML = `<tr><td colspan="5" class="empty">Nothing sold yet. Select rows in your sell list and hit "Sold" to log them here.</td></tr>`;
    } else {
      // newest first
      const order = state.sales.map((_, i) => i).sort((a, b) => state.sales[b].at - state.sales[a].at);
      for (const i of order) {
        const s = state.sales[i];
        const tr = document.createElement("tr");
        tr.innerHTML =
          nameCellHtml(s.name) +
          `<td class="qty">${s.count > 1 ? "x" + s.count : ""}</td>` +
          `<td class="price"></td>` +
          `<td class="qty">${saleWhen(s.at)}</td>` +
          `<td class="qty"></td>`;
        // editable price so he can correct to the actual sale price
        const input = document.createElement("input");
        input.type = "text"; input.value = s.price || ""; input.placeholder = "price";
        input.addEventListener("click", (e) => e.stopPropagation());
        input.addEventListener("input", () => { s.price = input.value.trim(); saveSales(); updateSoldTotals(); });
        tr.children[2].appendChild(input);
        const rm = document.createElement("button");
        rm.type = "button"; rm.className = "btn btn-ghost btn-sm"; rm.textContent = "✕";
        rm.title = "Remove this sale"; rm.addEventListener("click", () => removeSale(i));
        tr.children[4].appendChild(rm);
        body.appendChild(tr);
      }
    }
  }
  updateSoldTotals();
  if ($("soldCount")) $("soldCount").textContent = `${state.sales.length}`;
  if ($("soldClearBtn")) $("soldClearBtn").disabled = !state.sales.length;
}
function updateSoldTotals() {
  const t = salesTotals();
  if ($("soldTotal")) $("soldTotal").textContent = `${t.all.toLocaleString()}p`;
  if ($("soldToday")) $("soldToday").textContent = `${t.today.toLocaleString()}p`;
}

// Total NPC buyback (plat) for the whole vendor list, at the current CHA.
function vendorTotalPp() {
  return state.vendor.reduce((s, it) => s + (vendorPp(it) || 0) * (it.count || 1), 0);
}
function renderVendor() {
  const body = $("vendorBody");
  if (body) {
    body.innerHTML = "";
    state.vendorSel.clear(); state.vendorAnchor = null;
    if (!state.vendor.length) {
      body.innerHTML = `<tr><td colspan="5" class="empty">Add items here to tally NPC value. Use → Vendor from inventory or the sell list.</td></tr>`;
    } else {
      state.vendor.forEach((item, i) => {
        const each = vendorPp(item);
        const sub = each != null ? each * (item.count || 1) : null;
        const tr = document.createElement("tr");
        tr.dataset.i = i;
        tr.innerHTML =
          nameCellHtml(item.name) +
          `<td class="loc" title="${escapeHtml(whereTip(item))}">${escapeHtml(vendorWho(item))}</td>` +
          `<td class="qty">${item.count > 1 ? "x" + item.count : ""}</td>` +
          `<td class="qty">${each == null ? "?" : (each >= 1 ? Math.round(each) + "p" : "<1p")}</td>` +
          `<td class="qty">${sub == null ? "?" : Math.round(sub) + "p"}</td>`;
        tr.addEventListener("click", (e) => {
          if (e.target.closest(".mkt")) return;
          selectRow(e, i, tr, state.vendorSel, "vendorBody", "vendorAnchor");
        });
        body.appendChild(tr);
      });
    }
  }
  const total = Math.round(vendorTotalPp());
  if ($("vendorTotal")) $("vendorTotal").textContent = `${total.toLocaleString()}p`;
  if ($("vendorCount")) $("vendorCount").textContent = `${state.vendor.length}`;
  if ($("vendorChaEcho")) { const c = chaVal(); $("vendorChaEcho").textContent = c == null ? "?" : c; }
  const has = state.vendor.length > 0;
  if ($("vendorRemoveBtn")) $("vendorRemoveBtn").disabled = !has;
  if ($("vendorClearBtn")) $("vendorClearBtn").disabled = !has;
}

// Switch the right-pane tab: sell list / vendor list / sold log.
function setRightTab(tab) {
  state.rightTab = tab;
  const panels = { sell: "sellPanel", vendor: "vendorPanel", sold: "soldPanel", upgrade: "upgradePanel", dist: "distPanel" };
  const tabs = { sell: "sellTab", vendor: "vendorTab", sold: "soldTab", upgrade: "upgradeTab", dist: "distTab" };
  for (const [k, id] of Object.entries(panels)) if ($(id)) $(id).hidden = k !== tab;
  for (const [k, id] of Object.entries(tabs)) if ($(id)) $(id).classList.toggle("active", k === tab);
  if (tab === "vendor") renderVendor();
  if (tab === "sold") renderSold();
  if (tab === "upgrade") renderUpgradePanel();
  if (tab === "dist") renderGearPlanner();
}

// ----- force-krono: convert selected plat rows to their krono equivalent -----
// For high-value items you'd rather take round krono for (e.g. a 60k item at a
// ~30k krono rate → 2kr). Rounds to the nearest half krono.
function kronoSelected() {
  if (!state.aucSel.size) { log("Select the high-value rows you want priced in krono."); return; }
  const rate = state.kronoRate || DEFAULT_KRONO_RATE;
  let n = 0;
  for (const i of state.aucSel) {
    const it = state.auction[i];
    const [, plat] = classifyPrice(it.price);
    const base = plat > 0 ? plat : (it._median || it._lastMedian || 0);
    if (base <= 0) continue;
    const kr = Math.max(Math.round((base / rate) * 2) / 2, 0.5);   // nearest 0.5 krono
    it.price = `${kr}kr`;
    it._manual = true; it._autoPriced = false;
    if (it._priceInput) it._priceInput.value = it.price;
    n++;
  }
  refreshAuction();
  log(n ? `Priced ${n} item(s) in krono at ~${Math.round(rate).toLocaleString()}p/kr.`
        : "No selected row had a plat value to convert (price-check them first).");
}

// ----- live price-adjust: re-price auto-priced rows as the slider moves -----
// Uses each row's stored median so no re-fetch is needed. Skips rows you priced by
// hand and krono rows. No-op until a price check has run.
function applyLiveAdjust() {
  const adj = adjustPct();
  const body = $("aucBody");
  let changed = 0;
  state.auction.forEach((it, idx) => {
    if (it._autoPriced && !it._manual && it._median) {
      it.price = `${Math.max(niceRound(it._median * (1 + adj / 100)), 5)}p`;
      if (it._priceInput) it._priceInput.value = it.price;
      // recolor the row live so the under/over flag tracks the new price
      const tr = body && body.querySelector(`tr[data-i="${idx}"]`);
      if (tr) {
        tr.classList.remove("krono", "under", "vendor", "diverge", "saturated", "thin", "demand");
        const t = rowTag(it); if (t) tr.classList.add(t);
        if (tr.children[2]) tr.children[2].title = marketTip(it);   // keep the market read in step with the new price
      }
      changed++;
    }
  });
  updateSellTally();   // grand/selection total reflects the new prices as the slider moves
  return changed;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

let lastEntries = null;   // [key,val] pairs from the most recent Generate
let lastGenSig = "";      // signature of the inputs at that Generate, to detect drift before Write
// Fingerprint of everything that changes what Generate produces (settings + list +
// selection). If this differs at Write time, the preview is stale → regenerate.
function genSig() {
  return ["page", "prefix", "suffix"].map((id) => ($(id) ? $(id).value : "")).join("|") +
    "|" + (state.auction ? state.auction.length : 0) + "|" + [...(state.aucSel || [])].sort((a, b) => a - b).join(",");
}

// Classify a price string for the link/text split: ['krono'|'plat'|'none', plat].
// 'kr' anywhere -> krono (always links); digits -> plat; empty -> none (unpriced).
// Port of _classify_price.
function classifyPrice(priceStr) {
  const s = (priceStr || "").trim().toLowerCase();
  if (!s) return ["none", 0];
  if (s.includes("kr")) return ["krono", 0];
  const digits = s.replace(/[^0-9.]/g, "");
  if (digits) { const n = parseFloat(digits); return Number.isFinite(n) ? ["plat", Math.trunc(n)] : ["none", 0]; }
  return ["none", 0];
}

// Parse a price box ('600', '600p', '1kr') to a plat int. 0 = OFF. Port of
// _parse_plat_value.
function parsePlatValue(raw) {
  raw = (raw || "").trim().toLowerCase().replace(/,/g, "").replace(/\s/g, "");
  if (!raw) return 0;
  let mult = 1;
  if (raw.includes("kr")) { mult = DEFAULT_KRONO_RATE; raw = raw.replace("kr", ""); }
  raw = raw.replace(/p+$/, "");
  if (!raw) return mult > 1 ? mult : 0;   // bare "kr" -> one krono
  const n = parseFloat(raw);
  return Number.isFinite(n) ? Math.max(Math.trunc(n * mult), 0) : 0;
}
// Min profit over NPC vendor value to bother listing (plat). 0 = off.
function minProfitPlat() { return parsePlatValue(($("minProfit") || {}).value); }

// ----- NPC vendor value (CHA-based) — port of vendor_multiplier/_vendor_pp/_is_vendor_trash -----
function vendorMultiplier(cha) { return Math.max(0, Math.min(VENDOR_SLOPE * cha + VENDOR_INTERCEPT, VENDOR_CAP)); }
function vendorValuePp(priceCp, cha) { return (priceCp / 1000) * vendorMultiplier(cha); }
function chaVal() { const n = parseInt(($("cha") || {}).value, 10); return Number.isFinite(n) && n >= 0 ? n : null; }
// Base merchant value (copper) for an item, by id (the DB price column). null if unknown.
function baseCopper(item) { const rec = item.id && state.db ? state.db.byId.get(item.id) : null; return rec ? rec.price : null; }
function vendorPp(item) { const c = chaVal(), base = baseCopper(item); return (c === null || !base) ? null : vendorValuePp(base, c); }
function vendorStr(item) { const v = vendorPp(item); return v === null ? "" : (v >= 1 ? `${Math.round(v)}p` : "<1p"); }
// True if a PLAT-priced item isn't worth listing: either worth >= as much to a
// vendor as your post price, OR its profit over vendor value is under the "Min
// profit" floor (not worth the hassle). Krono/unpriced are never trash (can't
// compare). Port of _is_vendor_trash, extended with the min-profit floor.
function isVendorTrash(item) {
  const [kind, plat] = classifyPrice(item.price);
  if (kind !== "plat") return false;
  const v = vendorPp(item);
  if (v === null) return false;
  return v >= plat || (plat - v) < minProfitPlat();
}

// Log AND pop up the vendor-trash items (with bag location + margin) left out of
// the macro, so web users get the same prominent heads-up the desktop app gives
// (mirrors _report_trash) instead of having to dig through the log.
function reportTrash(trash) {
  if (!trash.length) return;
  const floor = minProfitPlat();
  const reason = floor ? `profit over vendor < ${floor}p` : "worth more to a vendor";
  log(`${trash.length} item(s) not worth listing (${reason}) — left OUT of the macro:`);
  const rows = [];
  for (const it of trash) {
    const v = vendorPp(it), [, plat] = classifyPrice(it.price);
    const margin = v !== null ? Math.round(plat - v) : null;
    rows.push({ name: it.name, price: it.price || "?", vendor: vendorStr(it), loc: it.location || "?" });
    log(`  VENDOR (${vendorStr(it)} vs ${it.price}${margin !== null ? `, +${margin}p` : ""}): ` +
        `${it.name} @ ${it.location || "?"}`);
  }
  // Popup: same content as the desktop "Go vendor these" dialog. Reuse the .postings
  // <pre> styling so it matches the rest of the modal UI.
  const shown = rows.slice(0, 15);
  const body = document.createElement("div");
  const intro = document.createElement("p");
  intro.innerHTML = `<strong>${trash.length}</strong> item(s) aren't worth listing ` +
    `(${escapeHtml(reason)}), so they were left <strong>OUT</strong> of your macros. Go vendor them:`;
  body.appendChild(intro);
  const pre = document.createElement("pre");
  pre.className = "postings";
  let txt = shown.map((r) => `• ${r.name}: player ${r.price} / vendor ${r.vendor} — ${r.loc}`).join("\n");
  if (rows.length > shown.length) txt += `\n…and ${rows.length - shown.length} more (see Log).`;
  pre.textContent = txt;
  body.appendChild(pre);
  openModal("Go vendor these", body);
}

function generate() {
  if (!state.db) { log("Item DB not loaded yet — wait for it or check the connection."); return; }
  track("generate");
  const prefix = $("prefix").value;
  const suffix = $("suffix").value.trim();
  const page = parseInt($("page").value, 10) || 2;

  // Generate from the SELECTED rows when any are highlighted, else the whole sell
  // list. The list stays whole so the sale tally sees it all.
  const source = state.aucSel.size ? [...state.aucSel].map((i) => state.auction[i]).filter(Boolean) : state.auction;
  log(`Generating from ${state.aucSel.size ? `${source.length} selected row(s)` : "the whole sell list"} → page ${page}…`);

  // Band 1 (trash): worth >= as much to an NPC vendor as your post price. Dropped
  // from the macro and reported with bag locations so you know what to go sell.
  const trash = [], nontrash = [];
  for (const it of source) (isVendorTrash(it) ? trash : nontrash).push(it);
  reportTrash(trash);
  // Auto-add the "go vendor these" items to the Vendor list (deduped; they stay in
  // the sell list too, just cut from the macro). So the tally builds itself.
  if (trash.length) {
    let added = 0;
    for (const it of trash) if (addToVendor(it, it.count)) added++;
    renderVendor();
    if (added) log(`Added ${added} vendor-it item(s) to the Vendor list.`);
  }

  // Generate from the AUCTION list (minus trash), not the whole inventory.
  const sellable = nontrash.filter((i) => linkFor(i));
  const skipped = nontrash.length - sellable.length;
  if (!sellable.length) {
    log(trash.length ? "All priced items are vendor-trash — nothing to auction. Go vendor them!"
                     : "No auction items have a DB link to generate.");
    return;
  }

  // Split the sellable items into typed groups so spells, gear, and everything
  // else each get their OWN macro buttons (never mixed in one button). Everything
  // is a clickable link — no compact-text macros. Spells+songs → Spell#, anything
  // equippable → Gear#, all the rest (potions, distillates, tradeskill, food…) →
  // Misc#. Order = Spell, Gear, Misc; groups flow across the same page block.
  const buckets = { Spell: [], Gear: [], Misc: [] };
  for (const item of sellable) {
    const t = classifyType(item);
    (t === "spell" ? buckets.Spell : t === "gear" ? buckets.Gear : buckets.Misc).push(item);
  }
  const groups = [];
  for (const name of ["Spell", "Gear", "Misc"]) {
    if (!buckets[name].length) continue;
    groups.push({ name, lines: packToLines(buckets[name].map(linkToken), prefix, suffix, ", ") });
  }

  const { entries, preview, overflow } = layoutGroups(groups, page);
  const parts = ["Spell", "Gear", "Misc"].filter((n) => buckets[n].length).map((n) => `${buckets[n].length} ${n.toLowerCase()}`);
  log(`Generated ${preview.length} link button(s) — ${parts.join(", ")}` +
      (skipped ? `, ${skipped} no-link skipped` : "") + `. Start page ${page}.`);

  lastEntries = entries;
  lastGenSig = genSig();
  // Show the INI entries (DC2 rendered as a visible marker in the textarea).
  const shown = entries.map(([k, v]) => `${k}=${v}`).join("\n").replace(new RegExp(DC2, "g"), "·");
  $("output").value = shown;
  $("writeBtn").disabled = false;
  $("copyBtn").disabled = false;
  if (overflow) log(`  WARNING: ${overflow} button(s) didn't fit past page ${MAX_PAGE} — lower the start page.`);
}

// File System Access API: pick the character INI, read it, merge, write back.
async function writeInPlace() {
  if (!lastEntries) return;
  if (!window.showOpenFilePicker) {
    log("In-place write needs Chrome/Edge (File System Access API). Use 'Copy macros' instead.");
    return;
  }
  // Guard against writing a STALE macro: if the Start page / threshold / prefix /
  // suffix / list changed since Generate, rebuild from current settings first.
  if (genSig() !== lastGenSig) {
    log("⟳ Settings changed since Generate — rebuilding from current settings before writing…");
    generate();
    if (genSig() !== lastGenSig) { log("Nothing to write (no sellable items after regenerate)."); return; }
  }
  try {
    const [handle] = await window.showOpenFilePicker({
      types: [{ description: "EQ character INI", accept: { "text/plain": [".ini"] } }],
      excludeAcceptAllOption: true,   // drop the "All files" fallback → dialog shows .ini only
    });
    const file = await handle.getFile();
    const existing = latin1Decode(new Uint8Array(await file.arrayBuffer()));
    // Safety net: stash the pre-write INI so a bad write is recoverable.
    try { localStorage.setItem("eqaf-ini-backup", JSON.stringify({ name: file.name, at: Date.now(), text: existing })); } catch { /* private mode / too big */ }
    const clearFrom = ($("wipeSell") && $("wipeSell").checked) ? WIPE_FROM_PAGE : 0;
    const btnCount = lastEntries.filter(([k]) => /Button\d+Name$/.test(k)).length;
    // Pages this batch spans (text on Start page, links on the next), + how many
    // existing macros on those pages we're about to REPLACE (the silent-clobber warning).
    const pages = [...new Set(lastEntries.map(([k]) => { const m = /^Page(\d+)Button/.exec(k); return m ? +m[1] : null; }).filter((x) => x != null))].sort((a, b) => a - b);
    const pageStr = pages.length ? (pages.length > 1 ? `pages ${pages[0]}–${pages[pages.length - 1]}` : `page ${pages[0]}`) : "";
    let replaced = 0;
    if (!clearFrom) {
      const tp = new Set(pages.map(String)); let inS = false;
      for (const raw of existing.split("\n")) {
        const st = raw.trim();
        if (st === "[Socials]") inS = true;
        else if (st.startsWith("[") && st.endsWith("]")) inS = false;
        else if (inS) { const m = /^Page(\d+)Button\d+Name=(Spell|Gear|Misc|WTS|Rare)\d+$/.exec(st); if (m && tp.has(m[1])) replaced++; }
      }
    }
    const merged = mergeIntoIni(existing, lastEntries, clearFrom);
    const writable = await handle.createWritable();
    await writable.write(latin1Bytes(merged));
    await writable.close();
    log(`✔ Wrote ${btnCount} macro button${btnCount === 1 ? "" : "s"} to ${file.name}${pageStr ? ` (${pageStr})` : ""}` +
        (clearFrom ? ` — cleared old macros on pages ${WIPE_FROM_PAGE}+`
          : replaced ? ` — ⚠ REPLACED ${replaced} existing macro button${replaced === 1 ? "" : "s"} on ${pageStr}; use a higher Start page to keep both batches`
          : "") +
        `. Backup saved. Close EQ first.`);
  } catch (e) {
    if (e && e.name === "AbortError") return;   // user cancelled the picker
    log("Write failed: " + (e && e.message ? e.message : e));
  }
}

// Copy the generated [Socials] entries to the clipboard (with the real DC2 link
// char) and show paste instructions. Fallback for browsers without in-place
// write — Edge blocks .ini downloads, so this replaces the old download path.
async function copyMacros() {
  if (!lastEntries) return;
  const text = lastEntries.map(([k, v]) => `${k}=${v}`).join("\n");
  try {
    await navigator.clipboard.writeText(text);   // needs a secure context (https/localhost) + this click
    log(`Copied ${lastEntries.length} [Socials] entries to the clipboard.`);
  } catch {
    log("Clipboard blocked (needs https/localhost) — use 'Write to INI file' instead.");
  }
  const d = document.createElement("div");
  d.innerHTML =
    "<p>The macro's <code>[Socials]</code> entries are on your clipboard.</p>" +
    "<p><strong>Easiest:</strong> use <strong>Write to INI file</strong> above (Chrome/Edge) — it edits your character INI directly, no copy-paste.</p>" +
    "<p><strong>Manual paste:</strong></p>" +
    "<ol style='margin:0 0 10px 18px;padding:0'>" +
    "<li><strong>Close EverQuest first</strong> — it rewrites the INI on exit.</li>" +
    "<li>Open your character file in the EQ folder, e.g. <code>&lt;Char&gt;_&lt;server&gt;_&lt;class&gt;.ini</code> (like <code>Alan_frostreaver_ROG.ini</code>), in a text editor (Notepad is fine).</li>" +
    "<li>Find the <code>[Socials]</code> section (add it at the end if it's missing).</li>" +
    "<li>Paste, replacing any old <code>Spell#</code>/<code>Gear#</code>/<code>Misc#</code> buttons from a previous run.</li>" +
    "<li>Save, then launch EQ.</li>" +
    "</ol>" +
    "<p class='hint'>The clickable-link lines hold a special character; a plain editor preserves it fine.</p>";
  openModal("Copy macros → paste into your INI", d);
}

// ----- loaded-toon chips (multi-toon roster under the file input) -----
function renderToonChips() {
  const wrap = $("toonChips");
  if (!wrap) return;
  wrap.innerHTML = "";
  if (!state.toons.length) return;
  for (const t of state.toons) {
    const items = t.items.length;
    const chip = document.createElement("span");
    chip.className = "toon-chip";
    chip.innerHTML = `${escapeHtml(t.name)} <span class="tc-n">${items}</span>`;
    chip.title = `${t.filename} — ${items} stacks`;
    const x = document.createElement("button");
    x.type = "button"; x.className = "tc-x"; x.textContent = "×";
    x.title = `Remove ${t.name}`;
    x.addEventListener("click", () => removeToon(t.name));
    chip.appendChild(x);
    wrap.appendChild(chip);
  }
  const clr = document.createElement("button");
  clr.type = "button"; clr.className = "tc-clear"; clr.textContent = "Clear all";
  clr.addEventListener("click", clearToons);
  wrap.appendChild(clr);
}

// Re-aggregate all toons and refresh the inventory UI. Returns sharedDupes so a
// caller can log an accurate post-aggregation summary. (Building a count message
// BEFORE calling this would read the stale pre-aggregation total — the "0 unique
// items" bug.) Pass msg to have this log it after the counts are fresh.
function refreshInventoryFromToons(msg) {
  const { sharedDupes } = rebuildInventory();
  const toonCount = state.toons.length;
  $("invStatus").textContent = toonCount
    ? `${toonCount} toon${toonCount > 1 ? "s" : ""} · ${state.inventory.length} unique items`
    : "no toons loaded";
  renderToonChips();
  populateFilters();
  buildInventoryTable();
  updateMonitorInvNote();
  if (msg) log(msg + (sharedDupes ? ` (shared-bank items counted once; skipped ${sharedDupes} duplicate row(s))` : ""));
  return sharedDupes;
}

function removeToon(name) {
  state.toons = state.toons.filter((t) => t.name !== name);
  refreshInventoryFromToons(`Removed ${name}.`);
}

function clearToons() {
  state.toons = [];
  refreshInventoryFromToons("Cleared all toons.");
}

// Load one or more /outputfile inventory dumps at once. Each file is one toon;
// re-loading a toon (same character name) replaces that toon's data. All loaded
// toons are aggregated into one inventory list.
async function loadInventoryFiles(fileList) {
  const files = [...fileList];
  if (!files.length) return;
  let loaded = 0, failed = 0;
  for (const file of files) {
    try {
      const text = await file.text();
      const items = parseInventory(text);
      const name = charNameFromFilename(file.name);
      const existing = state.toons.findIndex((t) => t.name === name);
      const rec = { name, filename: file.name, items };
      if (existing >= 0) state.toons[existing] = rec;   // replace a re-loaded toon
      else state.toons.push(rec);
      loaded++;
    } catch (err) {
      failed++;
      log(`Failed to read ${file.name}: ${err && err.message ? err.message : err}`);
    }
  }
  const dupes = refreshInventoryFromToons();   // aggregate FIRST so counts are fresh
  log(`Loaded ${loaded} toon dump${loaded !== 1 ? "s" : ""}` +
    (failed ? `, ${failed} failed` : "") +
    ` → ${state.inventory.length} unique items across ${state.toons.length} toon(s)` +
    (dupes ? ` (shared-bank items counted once; skipped ${dupes} duplicate row(s))` : "") +
    `. Select items and Add them to the auction list →`);
}

// Pull every *-Inventory.txt straight from the EQ folder via serve.py. Planning from a
// stale dump silently promises gear the toon no longer has (2026-07-22), so after "Dump
// Inv All" in game this is the one click that resyncs everything.
async function reloadDumpsFromEqFolder() {
  let list;
  try {
    const r = await fetch("/dumps");
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || "server error");
    list = j.dumps || [];
    if (!list.length) { log(`No *-Inventory.txt found in ${j.dir}. In game, run /outputfile inventory on each toon (or /eqf dump with the addon), then hit this again.`); return; }
  } catch (e) {
    log(`Can't read the EQ folder (${e && e.message ? e.message : e}) — run the app via run.bat, or use Choose dumps.`);
    return;
  }
  let loaded = 0, failed = 0, newest = 0;
  for (const d of list) {
    try {
      const text = await (await fetch(`/dump/${encodeURIComponent(d.file)}`)).text();
      const items = parseInventory(text);
      const name = charNameFromFilename(d.file);
      const rec = { name, filename: d.file, items };
      const at = state.toons.findIndex((t) => t.name === name);
      if (at >= 0) state.toons[at] = rec; else state.toons.push(rec);
      loaded++; newest = Math.max(newest, d.mtime || 0);
    } catch { failed++; }
  }
  refreshInventoryFromToons();
  populateFilters();
  const age = newest ? Math.round((Date.now() / 1000 - newest) / 60) : null;
  log(`Reloaded ${loaded} dump(s) from the EQ folder` + (failed ? `, ${failed} failed` : "") +
      ` → ${state.inventory.length} unique items across ${state.toons.length} toon(s)` +
      (age !== null ? `. Newest dump is ${age} min old.` : "."));
  if (state.rightTab === "dist") renderGearPlanner();
}

// ----- input handlers (browser only) -----
if (typeof document !== "undefined") {
$("invFile").addEventListener("change", async (e) => {
  await loadInventoryFiles(e.target.files);
  e.target.value = "";   // reset so re-picking the same file(s) fires change again
});

if ($("reloadDumps")) $("reloadDumps").addEventListener("click", reloadDumpsFromEqFolder);
$("reloadDb").addEventListener("click", async () => {
  await idbDel(DB_KEY);
  await idbDel(DB_META_KEY);
  autoLoadDb({ forceNetwork: true });
});
$("cha").addEventListener("change", refreshAuction);   // recompute Vendor column + trash coloring
// Min profit drives the "vendor it" trash test (isVendorTrash) — re-render the sell
// list live so raising/lowering it re-colors rows immediately.
if ($("minProfit")) $("minProfit").addEventListener("input", refreshAuction);
$("invSearch").addEventListener("input", buildInventoryTable);
// Inventory filters: Toon / Type / Slot dropdowns + location toggles.
if ($("filterToon")) $("filterToon").addEventListener("change", () => { state.filters.toon = $("filterToon").value; buildInventoryTable(); });
if ($("filterType")) $("filterType").addEventListener("change", () => {
  state.filters.type = $("filterType").value;
  populateFilters();          // show/hide the Slot dropdown for Gear
  buildInventoryTable();
});
if ($("filterSlot")) $("filterSlot").addEventListener("change", () => { state.filters.slot = $("filterSlot").value; buildInventoryTable(); });
if ($("filterStat")) $("filterStat").addEventListener("change", () => { state.filters.stat = $("filterStat").value; buildInventoryTable(); });
if ($("filterStatMin")) $("filterStatMin").addEventListener("input", () => { const v = parseInt($("filterStatMin").value, 10); state.filters.statMin = Number.isFinite(v) && v > 0 ? v : 0; buildInventoryTable(); });
if ($("filterClass")) $("filterClass").addEventListener("change", () => { state.filters.class = $("filterClass").value; buildInventoryTable(); });
if ($("filterRace")) $("filterRace").addEventListener("change", () => { state.filters.race = $("filterRace").value; buildInventoryTable(); });
// Item tooltip on inventory hover — delegated on the (persistent) tbody so it
// survives table rebuilds. Follows the cursor; rebuilds HTML only on row change.
if ($("invBody")) {
  const invBodyEl = $("invBody");
  let _tipRow = null;
  invBodyEl.addEventListener("mousemove", (e) => {
    const tr = e.target.closest("tr[data-i]");
    const item = tr ? state.inventory[Number(tr.dataset.i)] : null;
    const rec = item && item.id && state.db ? state.db.byId.get(item.id) : null;
    if (!rec || !rec.tip) { hideItemTip(); _tipRow = null; return; }
    if (tr !== _tipRow) { _tipRow = tr; showItemTip(rec); }
    positionItemTip(e.clientX, e.clientY);
  });
  invBodyEl.addEventListener("mouseleave", () => { hideItemTip(); _tipRow = null; });
}
// Same item stat tooltip on the SELL LIST rows. Hovering the price cell instead
// shows its own market-read title, so we skip the floating tip there to avoid
// stacking two tooltips. Resolves the DB record from the row's item id.
if ($("aucBody")) {
  const aucBodyEl = $("aucBody");
  let _aucTipRow = null;
  aucBodyEl.addEventListener("mousemove", (e) => {
    if (e.target.closest("td.price")) { hideItemTip(); _aucTipRow = null; return; }
    const tr = e.target.closest("tr[data-i]");
    const item = tr ? state.auction[Number(tr.dataset.i)] : null;
    const rec = item && item.id && state.db ? state.db.byId.get(item.id) : null;
    if (!rec || !rec.tip) { hideItemTip(); _aucTipRow = null; return; }
    if (tr !== _aucTipRow) { _aucTipRow = tr; showItemTip(rec); }
    positionItemTip(e.clientX, e.clientY);
  });
  aucBodyEl.addEventListener("mouseleave", () => { hideItemTip(); _aucTipRow = null; });
}
document.querySelectorAll('#locToggles input[type="checkbox"]').forEach((cb) => {
  cb.addEventListener("change", () => {
    const b = cb.dataset.loc;
    if (cb.checked) state.filters.locs.add(b); else state.filters.locs.delete(b);
    savePrefs();
    buildInventoryTable();
  });
});
// [data-col] only — the sell list's first <th> is the pick-all checkbox, and clicking
// it used to bubble into a sort-by-name + re-render that threw the ticks away.
document.querySelectorAll("#invTable thead th[data-col]").forEach((th) => th.addEventListener("click", () => sortInventory(th.dataset.col)));
document.querySelectorAll("#aucTable thead th[data-col]").forEach((th) => th.addEventListener("click", () => sortAuction(th.dataset.col)));
$("selAllBtn").addEventListener("click", selectAllInv);
$("addSelBtn").addEventListener("click", addSelectedToAuction);
$("removeBtn").addEventListener("click", removeSelectedFromAuction);
$("clearBtn").addEventListener("click", clearAuction);
if ($("aucSearch")) $("aucSearch").addEventListener("input", () => refreshAuction(true));   // live-search, keep the ticked selection
if ($("aucPickAll")) $("aucPickAll").addEventListener("change", () => {   // header box: tick/untick all shown rows
  const on = $("aucPickAll").checked;
  $("aucBody").querySelectorAll("tr[data-i]").forEach((tr) => {
    const i = Number(tr.dataset.i);
    if (on) state.aucSel.add(i); else state.aucSel.delete(i);
    tr.classList.toggle("sel", on);
    const cb = tr.querySelector(".auc-pick"); if (cb) cb.checked = on;
  });
  updateSellTally();
});
if ($("reviewBtn")) $("reviewBtn").addEventListener("click", () => { state.reviewOnly = !state.reviewOnly; refreshAuction(); });
// exclude-forever + vendor list + force-krono
if ($("excludeBtn")) $("excludeBtn").addEventListener("click", excludeSelected);
if ($("excludedLink")) $("excludedLink").addEventListener("click", showExcluded);
if ($("toVendorBtn")) $("toVendorBtn").addEventListener("click", moveSellToVendor);
if ($("invVendorBtn")) $("invVendorBtn").addEventListener("click", vendorSelectedFromInv);
if ($("invGearBtn")) $("invGearBtn").addEventListener("click", gearSetSelectedFromInv);
if ($("toKronoBtn")) $("toKronoBtn").addEventListener("click", kronoSelected);
if ($("sellTab")) $("sellTab").addEventListener("click", () => setRightTab("sell"));
if ($("vendorTab")) $("vendorTab").addEventListener("click", () => setRightTab("vendor"));
if ($("soldTab")) $("soldTab").addEventListener("click", () => setRightTab("sold"));
if ($("vendorRemoveBtn")) $("vendorRemoveBtn").addEventListener("click", removeSelectedVendor);
if ($("vendorClearBtn")) $("vendorClearBtn").addEventListener("click", clearVendor);
if ($("soldBtn")) $("soldBtn").addEventListener("click", markSoldSelected);
if ($("aucSelAllBtn")) $("aucSelAllBtn").addEventListener("click", selectAllShownAuction);
if ($("copyTallyBtn")) $("copyTallyBtn").addEventListener("click", copySellTally);
if ($("sellTally")) $("sellTally").addEventListener("click", () => { if (state.aucSel.size) clearAuctionSelection(); });
if ($("upgradeTab")) $("upgradeTab").addEventListener("click", () => setRightTab("upgrade"));
// Gear Planner moved to My Characters → Gear Sets (2026-07-25): account-aware
// routing needs the roster DB. Old render code kept below for reference; the
// localStorage sets remain importable there ("⇪ Import old sets").
if ($("distTab")) $("distTab").addEventListener("click", () => { location.href = "mychars.html#sets"; });
if ($("upgradeBtn")) $("upgradeBtn").addEventListener("click", () => setRightTab("upgrade"));   // inventory-footer shortcut
if ($("listSaveBtn")) $("listSaveBtn").addEventListener("click", saveCurrentList);
if ($("listLoadBtn")) $("listLoadBtn").addEventListener("click", loadSelectedList);
if ($("listDelBtn")) $("listDelBtn").addEventListener("click", deleteSelectedList);
if ($("listSelect")) $("listSelect").addEventListener("change", () => {
  const has = !!$("listSelect").value;
  if ($("listLoadBtn")) $("listLoadBtn").disabled = !has;
  if ($("listDelBtn")) $("listDelBtn").disabled = !has;
});
if ($("soldClearBtn")) $("soldClearBtn").addEventListener("click", clearSales);
if ($("cha")) $("cha").addEventListener("change", () => { if (state.rightTab === "vendor") renderVendor(); });
$("pcSelBtn").addEventListener("click", priceCheckSelected);
if ($("applySelBtn")) $("applySelBtn").addEventListener("click", applyAdjustToSelected);
$("rpBtn").addEventListener("click", recentPostingsSelected);
$("pcBtn").addEventListener("click", priceCheckAll);
$("lookupBtn").addEventListener("click", recentPostingsLookup);
$("lookupInput").addEventListener("keydown", (e) => { if (e.key === "Enter") recentPostingsLookup(); });
$("logBtn").addEventListener("click", showLog);
$("syncKronoBtn").addEventListener("click", syncKrono);
$("helpBtn").addEventListener("click", showHelp);
if ($("tipsBtn")) $("tipsBtn").addEventListener("click", showHelp);
$("modalClose").addEventListener("click", closeModal);
$("modal").addEventListener("click", (e) => { if (e.target === $("modal")) closeModal(); });   // backdrop click
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("modal").hidden) { closeModal(); return; }
  // Escape clears the sell-list selection (so the grand total shows again). If a
  // price box is focused, blur it first so one Escape does the obvious thing.
  if (e.key === "Escape" && state.aucSel.size) {
    const tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea") e.target.blur();
    clearAuctionSelection();
    return;
  }
  // Delete removes selected auction rows — but not while editing a price box.
  if (e.key === "Delete" && state.aucSel.size) {
    const tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea") return;
    e.preventDefault();
    removeSelectedFromAuction();
  }
});
$("genBtn").addEventListener("click", generate);
$("writeBtn").addEventListener("click", writeInPlace);
$("copyBtn").addEventListener("click", copyMacros);

PREF_IDS.forEach((id) => { const el = $(id); if (el) el.addEventListener("change", savePrefs); });
if ($("useRecent")) $("useRecent").addEventListener("change", savePrefs);   // checkbox: .checked, kept out of PREF_IDS (.value loop)

// Price-adjust slider <-> number box: keep them in lockstep (both feed adjustPct).
if ($("adjustRange") && $("adjust")) {
  const r = $("adjustRange"), n = $("adjust");
  const syncRangeFromNum = () => {
    let v = parseInt(n.value, 10); if (!Number.isFinite(v)) v = 0;
    r.value = Math.max(-50, Math.min(v, 100));   // slider caps at ±; number can exceed
  };
  r.addEventListener("input", () => { n.value = r.value; applyLiveAdjust(); savePrefs(); });
  n.addEventListener("input", () => { syncRangeFromNum(); applyLiveAdjust(); });
  n.addEventListener("change", syncRangeFromNum);
  syncRangeFromNum();   // seed the slider from any restored/default number value
}

// Per-selection slider (scoped to the selected rows; drives the read-only % readout).
if ($("selAdjustRange")) {
  $("selAdjustRange").addEventListener("input", () => { renderSelAdjustVal(); liveAdjustSelection(); });
}

// Watchlist controls
if ($("wlInput")) {
  $("wlInput").addEventListener("input", updateWatchlistAutocomplete);
  $("wlInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); if (addToWatchlist($("wlInput").value)) $("wlInput").value = ""; }
  });
  $("wlAddBtn").addEventListener("click", () => { if (addToWatchlist($("wlInput").value)) $("wlInput").value = ""; });
}

// View toggle (Macro Builder <-> Live Monitor)
if ($("tabBuilder")) {
  $("tabBuilder").addEventListener("click", () => setView("builder"));
  $("tabMonitor").addEventListener("click", () => setView("monitor"));
  if ($("wlLoadInv")) $("wlLoadInv").addEventListener("click", () => $("invFile").click());
  let savedView = "builder";
  try { savedView = localStorage.getItem(VIEW_KEY) || "builder"; } catch { /* private mode */ }
  setView(savedView);
}

// Watchlist monitor (FSA log tailer). Hide the controls entirely where the File
// System Access API is missing (Firefox/Safari) — show a one-line note instead.
if ($("wlPickLog")) {
  if (window.showOpenFilePicker) {
    $("wlPickLog").addEventListener("click", pickLogFile);
    $("wlToggle").addEventListener("click", () => (state.monitoring ? stopMonitoring() : startMonitoring()));
    document.addEventListener("visibilitychange", onVisibilityChange);
    if ($("wlTestAlert")) $("wlTestAlert").addEventListener("click", testAlert);
    if ($("tellPing")) {   // remember the tell-ping on/off choice across sessions
      $("tellPing").checked = localStorage.getItem(TELLPING_KEY) !== "0";
      $("tellPing").addEventListener("change", () => {
        try { localStorage.setItem(TELLPING_KEY, $("tellPing").checked ? "1" : "0"); } catch { /* private mode */ }
      });
    }
    restoreLogHandle();
  } else {
    const note = $("wlMonitorRow");
    if (note) note.innerHTML = '<span class="hint">Live alerts need a Chromium browser (Chrome/Edge) for file access. Your watchlist still saves here.</span>';
  }
}

// The dev proxy only exists on localhost — hide its toggle entirely on Pages so
// a visitor never sees (or ticks) a dead control.
if (!isLocalhost()) {
  const cb = $("useProxy");
  if (cb) { cb.checked = false; const f = cb.closest(".field"); if (f) f.style.display = "none"; }
}

{ const av = $("appVersion"); if (av) av.textContent = "v" + APP_VERSION; }  // single source of truth

loadPrefs();    // restore saved toolbar values (lightweight Settings)
// Re-seed the adjust slider from the restored number value.
if ($("adjustRange") && $("adjust")) {
  let v = parseInt($("adjust").value, 10); if (!Number.isFinite(v)) v = 0;
  $("adjustRange").value = Math.max(-50, Math.min(v, 100));
}
loadExcluded();      // restore the persisted blacklist
loadSales();         // restore the sales log
populateFilters();   // seed toon/slot dropdowns + reflect restored location toggles
renderVendor();      // seed vendor-list counts/total (empty at start)
renderSold();        // seed the sold tab + totals from restored history
loadWatchlist(); renderWatchlist();   // restore the saved watchlist
loadSavedLists(); populateListSelect();   // restore saved sell lists
loadToonProfiles();   // toon class/race for the upgrade finder (bundled + saved overrides)
loadGearSets();       // Gear Planner: saved named gear sets
loadGearTargets();    // ...and each set's remembered "apply to" toon
loadGearFree();       // ...and which sets may poach pieces other sets claimed
loadGearInactive();   // ...and which sets are retired (claim nothing)
loadUpgradeSources();   // bundled drop-source data (dungeon/raid + where-to-farm)
loadBisSets();          // tlpadvisor Frostreaver tier list — RANK authority for the upgrade gate
loadRaidlootBis();      // raidloot tier list — Quest/Raid source strings (display only)
loadQuestArmor();       // Velious class-armor planner — buildable upgrades from owned Unadorned molds
loadAuthSlugs();        // authoritygames slug set → deep-link the info clickout where possible
loadSilenced();                       // restore muted auctioneers
baseTitle = document.title;           // captured for the paused-tab title tag
log("Ready.");
track("view");  // anonymous visit ping (production origin only)
if ($("invColsBtn")) {
  $("invColsBtn").addEventListener("click", (e) => {
    e.stopPropagation();
    const m = $("invColsMenu");
    m.hidden = !m.hidden;
    if (!m.hidden) renderInvColsMenu();
  });
  // click-away close; the menu itself must not bubble or it closes on every toggle
  $("invColsMenu").addEventListener("click", (e) => e.stopPropagation());
  document.addEventListener("click", () => { const m = $("invColsMenu"); if (m && !m.hidden) m.hidden = true; });
  $("invColsMenu").querySelectorAll("button[data-preset]").forEach((b) =>
    b.addEventListener("click", () => {
      state.invCols = (INV_PRESETS[b.dataset.preset] || []).slice();
      saveInvCols(); renderInvColsMenu(); buildInventoryTable();
    }));
}
// Expand = fold the sell pane away so the item browser gets the whole window. The
// sell pane is only hidden (never rebuilt), so prices/selections survive the toggle.
function applyInvExpanded() {
  const ws = document.querySelector(".workspace");
  const btn = $("invExpandBtn");
  if (!ws || !btn) return;
  ws.classList.toggle("inv-expanded", !!state.invExpanded);
  btn.textContent = state.invExpanded ? "⤡ Show sell list" : "⤢ Expand";
  btn.title = state.invExpanded
    ? "Bring the sell list back (nothing in it was lost)"
    : "Fold the sell list away and give the item browser the full window";
  // ColTable pinned widths were measured in the half-width pane — drop them so the
  // table re-fits the new width instead of keeping a squeezed layout.
  if (window.ColTable) { try { ColTable.reset($("invTable")); } catch { /* cosmetic */ } }
}
if ($("invExpandBtn")) $("invExpandBtn").addEventListener("click", () => {
  state.invExpanded = !state.invExpanded;
  try { localStorage.setItem("eqaf-inv-expanded", state.invExpanded ? "1" : ""); } catch { /* non-critical */ }
  applyInvExpanded();
  buildInventoryTable();
});

if ($("invCompare")) $("invCompare").addEventListener("change", (e) => {
  state.invCompare = e.target.value;
  saveInvCols();
  buildInventoryTable();
});
if ($("invClearFiltersBtn")) $("invClearFiltersBtn").addEventListener("click", () => {
  state.invColFilters = {};
  saveInvCols();
  state._invHeadSig = null;   // force the header (and its inputs) to redraw empty
  buildInventoryTable();
});
if ($("invUpgradesOnly")) $("invUpgradesOnly").addEventListener("change", (e) => {
  state.invUpgradesOnly = e.target.checked;
  buildInventoryTable();
});

loadInvCols();
applyInvExpanded();   // restore the folded/expanded layout from last session
autoLoadDb();   // pull the bundled DB automatically when served (localhost/Pages)
loadSpellData();   // effect names/descriptions for tooltips + the focus filter (non-fatal)
syncKrono();    // pull the live krono rate for the header (best-effort)
if (!window.showOpenFilePicker) {
  log("Note: in-place INI write needs Chrome/Edge; the Download button works everywhere.");
}
}  // end browser-only block

// Exported for Node-based logic tests; harmless/ignored in the browser.
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    makeLink, parseItemDb, parseInventory, packToLines,
    buttonsFromLines, mergeIntoIni, latin1Bytes, latin1Decode,
    buildItemlink, itemHashString, eqStringHash, splitCsvLine,
  };
}
