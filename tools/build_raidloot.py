#!/usr/bin/env python3
"""Build app/raidloot-bis.json from raidloot.com's per-class/slot ranked item lists.

raidloot rows carry the REAL game item id, a score bar, and a Source string that tags
Quest vs Raid + zone/mob/armor-set. We store ONLY {rank, id, name, score, source} per
class/slot -- every stat and class/race restriction comes from items.txt.gz (sodeq) by
id in the app, so this scraper is class-generic (no per-class stat columns to parse).

Usage:
  python build_raidloot.py Monk                # full: all slots -> writes/merges json
  python build_raidloot.py Shaman Chest Wrist  # smoke: only these slots -> PRINT, no write

Pure stdlib. Re-run when the era cap changes. urllib + browser UA (WebFetch 403s the site).
"""
import urllib.request, urllib.parse, re, json, os, sys, time, html as ihtml

# raidloot uses class NAMES; these three are the gear_review toons. Add more freely --
# nothing below is Monk-specific.
CLASS_PARAM = {"Monk": "Monk", "Shaman": "Shaman", "Druid": "Druid",
               "Cleric": "Cleric", "Warrior": "Warrior", "Rogue": "Rogue",
               "Necromancer": "Necro", "Wizard": "Wizard", "Magician": "Magician",
               "Enchanter": "Enchanter", "Paladin": "Paladin", "Shadowknight": "SK",
               "Ranger": "Ranger", "Bard": "Bard", "Beastlord": "Beastlord", "Berserker": "Berserker"}
SLOTS = ["Charm", "Ear", "Head", "Face", "Neck", "Shoulders", "Arms", "Back", "Wrist",
         "Range", "Hands", "Primary", "Secondary", "Fingers", "Chest", "Legs", "Feet", "Waist", "Ammo"]
SOURCE = "SoV and older"     # Frostreaver era cap
ORDER = "Score"              # raidloot's own BIS ranking

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
BASE = "https://www.raidloot.com/items"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "app", "raidloot-bis.json")


def fetch(cls_param, slot):
    q = urllib.parse.urlencode({"name": "", "class": cls_param, "slot": slot, "type": "",
                                "augslot": "", "level": "", "source": SOURCE,
                                "prestige": "Include", "order": ORDER, "view": "Table"})
    req = urllib.request.Request(BASE + "?" + q, headers={"User-Agent": UA, "Accept": "text/html"})
    return urllib.request.urlopen(req, timeout=45).read().decode("utf-8", "replace")


def parse_rows(page):
    # Each result is a main row <tr id="row-item<ID>"> ... </tr>; order = Score desc = rank.
    rows = []
    for m in re.finditer(r'<tr id="row-item(\d+)"[^>]*>(.*?)</tr>', page, re.S):
        iid, tr = int(m.group(1)), m.group(2)
        nm = re.search(r'/items/\d+">([^<]+)</a>', tr)
        sc = re.search(r'(\d+)% of top item score', tr)
        # Only the Type and Source cells carry class="l..."; Source is the last one.
        ltd = re.findall(r'<td[^>]*class="l[^"]*"[^>]*>([^<]*)</td>', tr)
        rows.append({
            "rank": len(rows) + 1,
            "id": iid,
            "name": ihtml.unescape(nm.group(1)).strip() if nm else "",
            "score": int(sc.group(1)) if sc else None,
            "source": ihtml.unescape(ltd[-1]).strip() if ltd else "",
        })
    return rows


def scrape(cls, slots):
    param = CLASS_PARAM.get(cls, cls)
    out = {}
    for slot in slots:
        try:
            rows = parse_rows(fetch(param, slot))
        except Exception as e:
            print(f"  {cls}/{slot}: FAILED ({type(e).__name__}: {e})", file=sys.stderr)
            continue
        if rows:
            out[slot] = rows
        q = sum(1 for r in rows if r["source"].lower().startswith("quest"))
        print(f"  {cls:8s} {slot:10s} items={len(rows):3d}  quest={q}")
        time.sleep(0.4)
    return out


def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    cls = sys.argv[1]
    slots = sys.argv[2:] or SLOTS
    smoke = bool(sys.argv[2:])
    data = scrape(cls, slots)
    if smoke:
        print(f"\n[smoke] {cls}: parsed {sum(len(v) for v in data.values())} rows across {len(data)} slots (not written)")
        return
    allj = {}
    if os.path.exists(OUT):
        try: allj = json.load(open(OUT, encoding="utf-8"))
        except Exception: allj = {}
    allj.setdefault("_meta", {})
    allj["_meta"] = {"source": "raidloot.com/items", "era": SOURCE, "order": ORDER,
                     "join": "row.id is a real EQ item id -> items.txt.gz / inventory / TLP catalog",
                     "note": "stats & class/race restriction come from sodeq by id; source tags Quest/Raid+where"}
    allj[cls] = data
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(allj, f, ensure_ascii=False, indent=0)
    n = sum(len(v) for v in data.values())
    print(f"\nwrote {os.path.normpath(OUT)}  ({cls}: {n} items, {len(data)} slots)")


if __name__ == "__main__":
    main()
