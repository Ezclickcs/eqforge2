#!/usr/bin/env python3
"""Build app/bis-sets.json from tlpadvisor.com's Frostreaver best-available lists.

Source: tlpadvisor.com/everquest/tlp/frostreaver/best-available-gear-and-items?class=<bit>
It is server- and era-aware (Frostreaver's current unlock state) and its rows carry
REAL EQ item ids (verified 253/253 join to items.txt.gz), so downstream we join by id
straight to inventory dumps and the TLP catalog -- no name matching.

Re-run when Frostreaver unlocks a new era. Pure stdlib (matches serve.py).

Data shape per row in the page (Next.js RSC stream, backslash-escaped JSON):
  "sections":[{"slotKey":"HEAD","slotLabel":"Head","equipSlotMask":4,"rows":[
     {"itemId":31223,"name":"Brother Xave's Headband",
      "itemMiniData":{...,"statsLine":"...","flagsLine":"Lore ... ","effectLine":null},
      "tier":"class-pick"}, ... ]}, ...]
Rows are already in rank order (best first). tier in {class-pick, eligible}.
"""
import json, re, sys, urllib.request, datetime, os

# tlpadvisor's ?class= is the standard EQ class bitmask.
CLASSES = {"Monk": 64, "Shaman": 512, "Druid": 32, "Warrior": 1, "Cleric": 2}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
URL = ("https://tlpadvisor.com/everquest/tlp/frostreaver/"
       "best-available-gear-and-items?class={bit}")

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "app", "bis-sets.json")


def fetch(bit):
    req = urllib.request.Request(URL.format(bit=bit),
                                 headers={"User-Agent": UA, "Accept": "text/html"})
    return urllib.request.urlopen(req, timeout=45).read().decode("utf-8", "replace")


def unescape(html):
    # RSC stream escapes the embedded JSON: \" -> " and \\ -> \ . Order matters.
    return html.replace('\\"', '"').replace('\\\\', '\\')


def extract_sections(text):
    """Bracket-match the array that follows the first '"sections":'."""
    key = '"sections":'
    i = text.find(key)
    if i < 0:
        raise ValueError('no "sections" key in page')
    i = text.index('[', i)
    depth, j, instr, esc = 0, i, False, False
    while j < len(text):
        c = text[j]
        if instr:
            if esc:
                esc = False
            elif c == '\\':
                esc = True
            elif c == '"':
                instr = False
        else:
            if c == '"':
                instr = True
            elif c == '[':
                depth += 1
            elif c == ']':
                depth -= 1
                if depth == 0:
                    return json.loads(text[i:j + 1])
        j += 1
    raise ValueError("unterminated sections array")


def parse_class(bit):
    text = unescape(fetch(bit))
    sections = extract_sections(text)
    out = {}
    for sec in sections:
        label = sec.get("slotLabel") or sec.get("slotKey")
        rows = []
        for rank, r in enumerate(sec.get("rows", []), 1):
            mini = r.get("itemMiniData") or {}
            rows.append({
                "rank": rank,
                "id": r.get("itemId") or mini.get("id"),
                "name": r.get("name") or mini.get("name"),
                "tier": r.get("tier"),                       # class-pick | eligible
                "slotMask": sec.get("equipSlotMask"),
                "stats": mini.get("statsLine"),
                "flags": mini.get("flagsLine"),
                "effect": mini.get("effectLine"),
            })
        if rows:
            out[label] = rows
    return out


def main():
    result = {"_meta": {
        "source": "tlpadvisor.com/everquest/tlp/frostreaver/best-available-gear-and-items",
        "server": "frostreaver",
        "fetched": datetime.date.today().isoformat(),
        "join": "row.id is a real EQ item id -> items.txt.gz / inventory / TLP catalog",
        "tiers": ["class-pick", "eligible"],
    }}
    for cls, bit in CLASSES.items():
        try:
            slots = parse_class(bit)
        except Exception as e:
            print(f"  {cls}: FAILED ({type(e).__name__}: {e})", file=sys.stderr)
            continue
        result[cls] = slots
        n = sum(len(v) for v in slots.values())
        picks = sum(1 for v in slots.values() for r in v if r["tier"] == "class-pick")
        print(f"  {cls:8s} class={bit:<4} slots={len(slots):2d} items={n:4d} class-pick={picks}")

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=0)
    print("wrote", os.path.normpath(OUT))


if __name__ == "__main__":
    main()
