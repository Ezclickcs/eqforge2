#!/usr/bin/env python3
"""Build app/spell-effects.json.gz + app/focus-families.json from the EQ client files.

WHY THIS EXISTS
---------------
items.txt.gz stores every item effect as a bare SPELL ID (clickeffect / proceffect /
worneffect / focuseffect / scrolleffect / bardeffect). Its own *name* columns
(clickname, procname, ...) are EMPTY in the sodeq export -- verified 2026-08-05, all
blank -- so the app could only ever render "Clicky" / "Combat Proc" with no idea what
the effect actually does. The names and descriptions live in the EQ client instead:

  spells_us.txt   ^-delimited, 166 fields. [0]=id [1]=name [11]=duration formula
                  [12]=duration cap in ticks [165]=effect slots.
  dbstr_us.txt    ^-delimited "id^type^text"; type 6 == spell description, and its
                  id IS the spell id (verified: 508 -> "Creates a rift of bitter
                  cold, causing between #1 and @1 damage to your target.").

EFFECT SLOTS (field 165) are "$"-separated, each "slot|spa|base|base2|calc|max":
  Frost Strike (508) -> "1|0|-96|0|103|156"   base -96 == its real 96 cold damage
  Superior Healing (9) -> "1|0|200|0|10|600"
Descriptions reference those numbers positionally: #N = slot N base, @N = slot N max,
$N = slot N base2, %z = duration. We substitute them so a proc reads
"causing between 96 and 156 damage" instead of "between #1 and @1 damage".

FOCUS TAXONOMY comes free from the same pass: focus effect names encode family + rank
as a trailing Roman numeral ("Improved Damage III"), so the whole filter taxonomy is
derivable instead of hardcoded. araduneauctions.net's focus filter hardcodes 12
families capped at rank III; the real data has 86 parseable families reaching rank XV.

Pure stdlib (matches serve.py). Re-run only if the EQ client patches spell data.

Usage:  python tools/build_spells.py [--eq-dir PATH]
"""
import argparse, gzip, io, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "app")
ITEMS_GZ = os.path.join(HERE, "..", "items.txt.gz")

DEFAULT_EQ_DIR = os.environ.get(
    "EQFORGE_EQ_DIR",
    r"C:\Users\Public\Daybreak Game Company\Installed Games\EverQuest")

# Item columns that hold a spell id, and whether a prose description is worth the
# bytes. Focus/worn/bard names are self-describing ("Improved Damage III",
# "Ferocity XI") -- their descriptions are generic boilerplate, so names only.
EFFECT_COLS = {
    "clickeffect":  True,    # clickies -- "what does it do" is the whole question
    "proceffect":   True,    # weapon procs -- explicitly requested
    "scrolleffect": True,    # scrolls teach a spell; the spell matters
    "worneffect":   False,
    "focuseffect":  False,
    "bardeffect":   False,
}

ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8,
         "IX": 9, "X": 10, "XI": 11, "XII": 12, "XIII": 13, "XIV": 14, "XV": 15,
         "XVI": 16, "XVII": 17, "XVIII": 18, "XIX": 19, "XX": 20}
# Longest-first so "III" wins over "II" over "I".
RANK_RE = re.compile(r"^(.*?)\s+(" + "|".join(sorted(ROMAN, key=len, reverse=True)) + r")$")
# Post-Velious focus naming ("Detrimental Haste 23 L85") -- a different scheme with an
# absolute value and a level cap rather than a rank. Kept separate so it can't pollute
# the rank ladder with fake families.
MODERN_FOCUS_RE = re.compile(r"^(.*?)\s+(\d+)\s+L(\d+)$")


def read_item_effect_ids(path):
    """Scan items.txt.gz once.

    Returns (refs, counts):
      refs   {spell_id: set(column names)} -- which effect kinds reference this spell
      counts {spell_id: n items}           -- popularity, used to order the focus UI
    """
    refs, counts = {}, {}
    with gzip.open(path, "rt", encoding="latin-1") as f:
        header = f.readline().rstrip("\n").split("|")
        idx = {n: i for i, n in enumerate(header)}
        missing = [c for c in EFFECT_COLS if c not in idx]
        if missing:
            sys.exit("items.txt.gz missing effect columns: %s" % ", ".join(missing))
        width = len(header)
        for line in f:
            row = line.rstrip("\n").split("|")
            if len(row) < width:
                continue
            for col in EFFECT_COLS:
                v = row[idx[col]]
                # -1 and 0 both mean "no effect" in this export.
                if v.lstrip("-").isdigit() and int(v) > 0:
                    sid = int(v)
                    refs.setdefault(sid, set()).add(col)
                    counts[sid] = counts.get(sid, 0) + 1
    return refs, counts


def parse_effect_slots(raw):
    """'1|0|-96|0|103|156$2|...' -> {slot: (base, base2, max)}."""
    out = {}
    for chunk in (raw or "").split("$"):
        parts = chunk.split("|")
        if len(parts) < 6:
            continue
        try:
            slot = int(parts[0])
            out[slot] = (int(parts[2]), int(parts[3]), int(parts[5]))
        except ValueError:
            continue
    return out


def duration_text(ticks):
    """Buff duration cap -> human text. EQ ticks are 6 seconds."""
    if ticks <= 0:
        return ""
    secs = ticks * 6
    if secs < 60:
        return "%ds" % secs
    mins = secs // 60
    if mins < 60:
        return "%d min" % mins
    hours, rem = divmod(mins, 60)
    return "%dh" % hours if not rem else "%dh %dmin" % (hours, rem)


TOKEN_RE = re.compile(r"([#@$])(\d+)")


def resolve(desc, slots, ticks):
    """Substitute #N / @N / $N / %z in a spell description.

    Values are rendered as magnitudes: the sign only encodes helpful-vs-harmful and
    the surrounding prose ("causing ... damage" / "healing ...") already says which.
    """
    if not desc:
        return ""

    def sub(m):
        kind, n = m.group(1), int(m.group(2))
        s = slots.get(n)
        if not s:
            return m.group(0)          # leave unknown slots visible rather than lie
        base, base2, mx = s
        v = {"#": base, "$": base2, "@": (mx or base)}[kind]
        return str(abs(v))

    text = TOKEN_RE.sub(sub, desc)
    text = text.replace("%z", duration_text(ticks) or "its duration")
    # Remaining %-tokens are caster/target/level interpolations we cannot resolve
    # without a live cast context (%N %T %R %L %Z %i ...). Drop them and tidy up
    # rather than showing raw control codes.
    text = re.sub(r"%[a-zA-Z]", "", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text


def read_descriptions(path, wanted):
    out = {}
    with io.open(path, "r", encoding="latin-1") as f:
        for line in f:
            p = line.rstrip("\n").split("^")
            if len(p) >= 3 and p[1] == "6" and p[0].isdigit():
                sid = int(p[0])
                if sid in wanted:
                    out[sid] = p[2]
    return out


def read_spells(path, wanted):
    """{id: (name, slots, duration_ticks)} for the ids we care about."""
    out = {}
    with io.open(path, "r", encoding="latin-1") as f:
        for line in f:
            p = line.rstrip("\n").split("^")
            if len(p) < 166 or not p[0].isdigit():
                continue
            sid = int(p[0])
            if sid not in wanted:
                continue
            ticks = int(p[12]) if p[12].lstrip("-").isdigit() else 0
            out[sid] = (p[1], parse_effect_slots(p[165]), ticks)
    return out


def build_focus_taxonomy(refs, spells, item_counts):
    """Focus taxonomy for the filter UI, derived from spell NAMES.

    Shape:
      families: [{name, ranked, count, items, ranks:[...]}]  -- count = spell ids,
                items = how many ITEMS carry any rank of it (what the UI sorts on)
      spells:   {spell_id: [familyIndex, rank]}              -- the runtime lookup

    `ranked` separates the real rank ladders ("Improved Damage" I..XV) from one-off
    named foci ("Marr's Gift") that have no ladder. Both are filterable, but the UI
    should lead with the ranked ones -- they are what a rank>=N control is for.
    Post-Velious "Detrimental Haste 23 L85" foci use a different naming scheme and are
    kept out of the ladder entirely so they cannot invent fake families.
    """
    order, fams = {}, []

    def fam_index(name, ranked):
        if name not in order:
            order[name] = len(fams)
            fams.append({"name": name, "ranked": ranked, "count": 0,
                         "items": 0, "ranks": []})
        return order[name]

    by_spell, modern = {}, {}
    for sid, cols in refs.items():
        if "focuseffect" not in cols or sid not in spells:
            continue
        name = spells[sid][0]
        m = MODERN_FOCUS_RE.match(name)
        if m:
            e = modern.setdefault(m.group(1), {"count": 0, "maxLevel": 0})
            e["count"] += 1
            e["maxLevel"] = max(e["maxLevel"], int(m.group(3)))
            continue
        m = RANK_RE.match(name)
        family, rank, ranked = ((m.group(1), ROMAN[m.group(2)], True) if m
                                else (name, 0, False))
        fi = fam_index(family, ranked)
        e = fams[fi]
        e["count"] += 1
        e["items"] += item_counts.get(sid, 0)
        if ranked and rank not in e["ranks"]:
            e["ranks"].append(rank)
        by_spell[str(sid)] = [fi, rank]

    # A name only earns "ranked" once a real ladder exists; a lone "Foo III" with no
    # siblings is a one-off, not a family worth a rank selector.
    for e in fams:
        e["ranks"].sort()
        if e["ranked"] and len(e["ranks"]) < 2:
            e["ranked"] = False
    return fams, by_spell, modern


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eq-dir", default=DEFAULT_EQ_DIR)
    args = ap.parse_args()

    spells_path = os.path.join(args.eq_dir, "spells_us.txt")
    dbstr_path = os.path.join(args.eq_dir, "dbstr_us.txt")
    for p in (spells_path, dbstr_path, ITEMS_GZ):
        if not os.path.exists(p):
            sys.exit("missing input: %s" % p)

    print("scanning items.txt.gz for effect spell ids...")
    refs, item_counts = read_item_effect_ids(ITEMS_GZ)
    print("  %d distinct spell ids referenced by items" % len(refs))

    print("reading spells_us.txt...")
    spells = read_spells(spells_path, set(refs))
    print("  %d/%d resolved to names (%.1f%%)"
          % (len(spells), len(refs), 100.0 * len(spells) / max(1, len(refs))))

    # Descriptions only for the effect kinds where prose earns its bytes.
    desc_wanted = {sid for sid, cols in refs.items()
                   if any(EFFECT_COLS[c] for c in cols)} & set(spells)
    print("reading dbstr_us.txt (descriptions for %d ids)..." % len(desc_wanted))
    raw_desc = read_descriptions(dbstr_path, desc_wanted)
    print("  %d descriptions found" % len(raw_desc))

    # Payload: id -> [name] or [name, description]. Array form keeps the file small.
    payload = {}
    resolved = 0
    for sid, (name, slots, ticks) in spells.items():
        d = resolve(raw_desc.get(sid, ""), slots, ticks) if sid in desc_wanted else ""
        if d and d != name:
            payload[str(sid)] = [name, d]
            resolved += 1
        else:
            payload[str(sid)] = [name]

    out_gz = os.path.join(APP, "spell-effects.json.gz")
    blob = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    # mtime=0 so rebuilds are byte-identical when the data hasn't changed (clean diffs).
    with gzip.GzipFile(out_gz, "wb", compresslevel=9, mtime=0) as f:
        f.write(blob)
    print("wrote %s  (%d spells, %d with descriptions, %.0f KB raw / %.0f KB gz)"
          % (out_gz, len(payload), resolved, len(blob) / 1024.0,
             os.path.getsize(out_gz) / 1024.0))

    fams, by_spell, modern = build_focus_taxonomy(refs, spells, item_counts)
    out_focus = os.path.join(APP, "focus-families.json")
    with io.open(out_focus, "w", encoding="utf-8") as f:
        json.dump({"families": fams, "spells": by_spell, "modern": modern}, f,
                  separators=(",", ":"), ensure_ascii=False)
    ranked = [e for e in fams if e["ranked"]]
    print("wrote %s  (%d families: %d ranked ladders + %d one-offs, "
          "%d modern-scheme, %.0f KB)"
          % (out_focus, len(fams), len(ranked), len(fams) - len(ranked),
             len(modern), os.path.getsize(out_focus) / 1024.0))
    for e in sorted(ranked, key=lambda x: -x["items"])[:12]:
        print("    %-30s %5d items  rank I..%s"
              % (e["name"], e["items"], _roman(e["ranks"][-1])))


_ROMAN_OUT = {v: k for k, v in ROMAN.items()}


def _roman(n):
    return _ROMAN_OUT.get(n, str(n))


if __name__ == "__main__":
    main()
