"""Domain logic: class-default capabilities, composition validation, roster recommendations.

Pure functions over plain dicts — no DB access — so tests exercise the exact code the API uses.
Era assumptions are classic->Velious TLP (Frostreaver); tweak CLASS_DEFAULTS as the server ages.
"""

# (key, label) — canonical capability order for the UI.
CAPABILITIES = [
    ("tank", "Tank"),
    ("healing", "Healing"),
    ("slow", "Slow"),
    ("haste", "Haste"),
    ("cc", "Crowd Control"),
    ("pulling", "Pulling"),
    ("feign_death", "Feign Death"),
    ("ports", "Ports"),
    ("evac", "Evac"),
    ("resurrection", "Resurrection"),
    ("tracking", "Tracking"),
    ("snare", "Snare"),
    ("mana_regen", "Mana Regen"),
    ("melee_support", "Melee Support"),
    ("caster_support", "Caster Support"),
    ("coth", "Call of the Hero"),
    ("lockpick", "Lockpick"),
]
CAP_KEYS = [k for k, _ in CAPABILITIES]
CAP_LABELS = dict(CAPABILITIES)

CLASSES = ["Warrior", "Cleric", "Paladin", "Ranger", "Shadow Knight", "Druid", "Monk",
           "Bard", "Rogue", "Shaman", "Necromancer", "Wizard", "Magician", "Enchanter",
           "Beastlord", "Berserker"]

CLASS_DEFAULTS = {
    "Warrior": {"tank"},
    "Cleric": {"healing", "resurrection"},
    "Paladin": {"tank", "healing", "resurrection"},
    "Ranger": {"tracking", "snare", "pulling"},
    "Shadow Knight": {"tank", "pulling", "feign_death", "snare"},
    "Druid": {"healing", "ports", "evac", "snare", "tracking"},
    "Monk": {"pulling", "feign_death"},
    "Bard": {"haste", "slow", "cc", "pulling", "snare", "mana_regen",
             "melee_support", "caster_support", "tracking", "lockpick"},
    "Rogue": {"lockpick"},
    "Shaman": {"healing", "slow", "haste", "melee_support"},
    "Necromancer": {"feign_death", "snare", "pulling", "mana_regen"},
    "Wizard": {"ports", "evac"},
    "Magician": {"coth", "caster_support"},
    "Enchanter": {"cc", "haste", "slow", "mana_regen", "caster_support"},
    "Beastlord": {"slow", "haste", "healing", "melee_support"},
    "Berserker": {"melee_support"},
}

UNLOCK_EXAMPLES = [
    {"name": "Sebilis Key", "expansion": "Kunark", "category": "key", "priority": "high"},
    {"name": "Howling Stones Key", "expansion": "Kunark", "category": "key", "priority": "normal"},
    {"name": "Veeshan's Peak Access", "expansion": "Kunark", "category": "access", "priority": "high"},
    {"name": "Sleeper's Tomb Key", "expansion": "Velious", "category": "key", "priority": "high"},
    {"name": "Port-bot whitelist", "expansion": "", "category": "access", "priority": "high"},
    {"name": "OT Hammer", "expansion": "Kunark", "category": "clicky", "priority": "normal"},
    {"name": "Gate clicky", "expansion": "", "category": "clicky", "priority": "normal"},
    {"name": "Evac clicky", "expansion": "", "category": "clicky", "priority": "normal"},
    {"name": "MQ profile tested", "expansion": "", "category": "automation", "priority": "critical"},
    {"name": "Emergency pause tested", "expansion": "", "category": "automation", "priority": "critical"},
]

UNLOCK_OK_STATUSES = {"Complete", "Not Needed"}

# TLP challenge-claim items. ACCOUNT-BOUND: claimable per account, passable only
# between characters on the same account+server. Because they're items, seeing one
# in ANY toon's /outputfile inventory dump PROVES the account has that claim —
# no per-toon export needed. (design notes, 2026-07-25.)
CLAIM_ITEMS = [
    {"name": "Frightening Food Flask",
     "earned": "Kill Cazic-Thule at level 40 or below (DZ requires 36+)",
     "use": "On-demand stat food, long duration. Tradeable to friends on Teek."},
    {"name": "Trinket of the Far Frozen Wastes",
     "earned": "Kill Yelinak at level 50 or below",
     "use": "Teleport just outside Temple of Veeshan. DON'T MOVE on port-in until you check Sontalak — he won't aggro while you stand at the port spot."},
    {"name": "Token of the Magus",
     "earned": "Defeat Deepest Guk: The Curse Reborn by level 55",
     "use": "Summons a Magus vendor. Once LDoN launches, hail-ports to any LDoN camp; pre-GoD you can set the Gates teleport as an in-zone evac trick."},
    {"name": "Visage of Vaniki",
     "earned": "Reach level 60",
     "use": "Rat form — acts like a shrink, handy junk buff early."},
    {"name": "White Skystrider Whistle",
     "earned": "Defeat The Tolling of Distant Bells by level 60",
     "use": "Free mount when Luclin opens."},
    {"name": "Ashen Ampule",
     "earned": "Defeat Lethar by level 65",
     "use": "Food clicky, same idea as the Cazic flask."},
]


def resolve_capabilities(class_name, overrides=None):
    """Class defaults + per-character overrides -> {cap_key: bool}."""
    base = CLASS_DEFAULTS.get(class_name or "", set())
    out = {k: (k in base) for k in CAP_KEYS}
    for k, v in (overrides or {}).items():
        if k in out and v is not None:
            out[k] = bool(v)
    return out


def group_caps(members):
    """Union of member capabilities. members: [{caps: {...}}]"""
    have = set()
    for m in members:
        have |= {k for k, v in (m.get("caps") or {}).items() if v}
    return have


# (code, needed-any-of, message)
ROLE_CHECKS = [
    ("no_tank", {"tank"}, "No tank"),
    ("no_healer", {"healing"}, "No healer"),
    ("no_slow", {"slow"}, "No slow"),
    ("no_haste", {"haste"}, "No haste"),
    ("no_cc", {"cc"}, "No crowd control"),
    ("no_rez", {"resurrection"}, "No resurrection"),
    ("no_ports", {"ports", "evac"}, "No ports or evac"),
    ("no_puller", {"pulling"}, "No puller"),
]


def account_conflicts(members):
    """Groups of members sharing an account. members need account_id/account_alias/name."""
    by_acct = {}
    for m in members:
        aid = m.get("account_id")
        if aid is not None:
            by_acct.setdefault(aid, []).append(m)
    return [v for v in by_acct.values() if len(v) > 1]


def validate_composition(members, required_unlocks=None, unlocks_by_char=None):
    """Return warnings for a live composition.

    members: [{id, name, account_id, account_alias, class_name, automation_status, caps}]
    required_unlocks: list of unlock names every member should have
    unlocks_by_char: {character_id: {unlock_name: status}}
    Warnings: [{code, level: error|warn|info, message}]
    """
    warns = []

    for grp in account_conflicts(members):
        names = ", ".join(m["name"] for m in grp)
        alias = grp[0].get("account_alias") or "same account"
        warns.append({"code": "account_conflict", "level": "error",
                      "message": "Account conflict: %s are all on %s — only one can be logged in." % (names, alias)})

    if members:
        have = group_caps(members)
        for code, need, msg in ROLE_CHECKS:
            if not (have & need):
                warns.append({"code": code, "level": "warn", "message": msg})

    untested = [m["name"] for m in members
                if (m.get("automation_status") or "untested") not in ("tested",)]
    if untested:
        warns.append({"code": "untested_automation", "level": "info",
                      "message": "Untested automation: " + ", ".join(untested)})

    for req in (required_unlocks or []):
        req = req.strip()
        if not req:
            continue
        missing = []
        for m in members:
            st = (unlocks_by_char or {}).get(m["id"], {}).get(req)
            if st not in UNLOCK_OK_STATUSES:
                missing.append("%s (%s)" % (m["name"], st or "no entry"))
        if missing:
            warns.append({"code": "missing_access", "level": "warn",
                          "message": "Missing %s: %s" % (req, ", ".join(missing))})
    return warns


# --- Auto comp builder ------------------------------------------------------

# Classes that CAN cover a capability but shouldn't be the plan: tier-1 fallback
# providers (vs tier-2 primaries). Canonical example: a bard can weave an AoE
# slow, but it competes with keeping support songs up — risky. The solver only
# uses fallbacks when no primary fits, and says so.
FALLBACK_PROVIDERS = {
    "slow": {"Bard"},
    "healing": {"Paladin", "Beastlord"},
    "resurrection": {"Paladin"},
    "cc": {"Necromancer"},
}

# Requirement tokens the auto-builder accepts (besides exact class names).
REQ_DEFS = {
    "tank": {"any_of": ["tank"], "label": "Tank"},
    "healer": {"any_of": ["healing"], "label": "Healer"},
    "slow": {"any_of": ["slow"], "label": "Slow"},
    "haste": {"any_of": ["haste"], "label": "Haste"},
    "cc": {"any_of": ["cc"], "label": "Crowd Control"},
    "rez": {"any_of": ["resurrection"], "label": "Rez"},
    "ports": {"any_of": ["ports", "evac"], "label": "Ports/Evac"},
    "puller": {"any_of": ["pulling"], "label": "Puller"},
    "mana_regen": {"any_of": ["mana_regen"], "label": "Mana Regen"},
    "coth": {"any_of": ["coth"], "label": "CoH"},
    "tracking": {"any_of": ["tracking"], "label": "Tracking"},
}


def _req_tier(char, req):
    """0 = can't cover, 1 = fallback (risky), 2 = primary provider."""
    if req in REQ_DEFS:
        best = 0
        for cap in REQ_DEFS[req]["any_of"]:
            if (char.get("caps") or {}).get(cap):
                tier = 1 if char.get("class_name") in FALLBACK_PROVIDERS.get(cap, ()) else 2
                best = max(best, tier)
        return best
    if req in CLASSES:                          # exact-class requirement
        return 2 if char.get("class_name") == req else 0
    return 0


def _n_choose_6(n):
    if n <= 6:
        return 1
    out = 1
    for i in range(6):
        out = out * (n - i) // (i + 1)
    return out


def _search_cost(by_acct, acct_ids):
    """Team evaluations an exhaustive search would perform (upper bound).

    For <=6 accounts that's the product of every account's candidate list. Above
    6 it is ALSO multiplied by C(n,6), because the search picks which six accounts
    to use — the part the original budget forgot.
    """
    sizes = sorted((len(by_acct[a]) for a in acct_ids), reverse=True)
    top = 1
    for s in sizes[:6]:
        top *= s
    return top * _n_choose_6(len(sizes))


def _team_key(team, reqs):
    score, unmet, low = _score_team(team, reqs)
    return (len(unmet), -score), unmet, low


def _exhaustive_best(by_acct, acct_ids, reqs):
    """Every legal one-per-account six. Exact, and only used when it's affordable."""
    import itertools
    best = None
    groups = ([acct_ids] if len(acct_ids) <= 6
              else itertools.combinations(acct_ids, 6))
    for chosen in groups:
        for team in itertools.product(*(by_acct[a] for a in chosen)):
            team = list(team)[:6]
            key, unmet, low = _team_key(team, reqs)
            if best is None or key < best[0]:
                best = (key, team, unmet, low)
    return best


def _greedy_best(by_acct, acct_ids, reqs):
    """Fill the six slots greediest-first, then improve by swapping.

    Not guaranteed optimal, but it is linear in roster size instead of
    combinatorial, and on every roster small enough to check it against the
    exhaustive search it returns an equally good comp. Used only when exhaustive
    would blow the budget, i.e. exactly the big-boxer case that used to hang.
    """
    team = []
    used = set()

    # 1. Cover the REQUIREMENTS first, rarest provider count first.
    #
    # Filling purely by score is myopic and measurably wrong: on an 18-account
    # roster it seated six high-scoring toons and only then discovered the sole
    # slower sat on an account already spent, which no single swap can undo (its
    # account is occupied, so the swap pass skips it). Reserving accounts for the
    # scarce roles up front removes that trap entirely.
    def satisfiers(req):
        return [(a, c) for a in acct_ids for c in by_acct[a] if _req_tier(c, req) > 0]

    for req in sorted(reqs, key=lambda r: len(satisfiers(r))):
        if len(team) >= 6:
            break
        if any(_req_tier(c, req) > 0 for c in team):
            continue                            # already covered by someone seated
        free = [(a, c) for (a, c) in satisfiers(req) if a not in used]
        if not free:
            continue                            # genuinely uncoverable; _score_team says so
        a, c = min(free, key=lambda ac: _team_key(team + [ac[1]], reqs)[0])
        used.add(a)
        team.append(c)

    # 2. Fill whatever is left with the best remaining candidates.
    while len(team) < 6:
        pick, pick_key = None, None
        for a in acct_ids:
            if a in used:
                continue
            for c in by_acct[a]:
                key, _u, _l = _team_key(team + [c], reqs)
                if pick_key is None or key < pick_key:
                    pick_key, pick = key, (a, c)
        if pick is None:
            break
        used.add(pick[0])
        team.append(pick[1])
    if not team:
        return None
    # Swap passes: each slot may be replaced by any candidate whose account is
    # free (or the account already sitting in that slot).
    for _pass in range(3):
        improved = False
        for i in range(len(team)):
            cur_key, _u, _l = _team_key(team, reqs)
            own = team[i]["account_id"]
            for a in acct_ids:
                if a in used and a != own:
                    continue
                for c in by_acct[a]:
                    if c is team[i]:
                        continue
                    trial = list(team)
                    trial[i] = c
                    key, _u2, _l2 = _team_key(trial, reqs)
                    if key < cur_key:
                        used.discard(own)
                        used.add(a)
                        team, cur_key, own, improved = trial, key, a, True
        if not improved:
            break
    key, unmet, low = _team_key(team, reqs)
    return (key, team, unmet, low)


def auto_comp(chars, requirements, cap_level=60, max_combos=300000):
    """Pick the best one-per-account six for the given requirements.

    chars: roster dicts with caps/account_id/class_name/level/automation_status.
    requirements: list of REQ_DEFS keys and/or exact class names.
    Returns {slots, team, assignments, warnings, unmet} — unmet non-empty means
    no valid comp exists; slots then hold the best attempt.
    """
    import itertools

    reqs = [r for r in (requirements or []) if r in REQ_DEFS or r in CLASSES]
    pool = [c for c in chars
            if (c.get("status") or "active") == "active"
            and c.get("account_id") is not None
            and (c.get("level") or 0) >= cap_level]
    by_acct = {}
    for c in pool:
        by_acct.setdefault(c["account_id"], []).append(c)
    # Rank each account's candidates so ties resolve the same way every run.
    # Nothing is ever dropped from these lists - see the note below.
    for aid in by_acct:
        by_acct[aid].sort(key=lambda c: (c.get("automation_status") != "tested",
                                         -(c.get("level") or 0), c["name"]))
    # NO TRIMMING. The old code shrank per-account candidate lists until the
    # exhaustive search fit a budget — and since the ranking is by
    # tested/level/name, not by what the comp NEEDS, that quietly deleted the only
    # provider of a scarce role. Measured: an 18-account roster with slowers on 6
    # separate accounts came back "slow: unmet", because every slower had been
    # trimmed away before the search ever ran. Degrading the INPUT to fit the
    # algorithm is always the wrong trade; pick an algorithm that fits instead.
    #
    # The budget also has to include C(n,6) — the choice of which six accounts to
    # use — which the original cost calculation omitted entirely. That omission is
    # why 18 accounts x 2 toons took 12.7s and doubled every two accounts.
    acct_ids = sorted(by_acct)
    if _search_cost(by_acct, acct_ids) > max_combos:
        best = _greedy_best(by_acct, acct_ids, reqs)     # full lists, linear cost
    else:
        best = _exhaustive_best(by_acct, acct_ids, reqs)  # exact, affordable
    if best is None:
        return {"slots": [], "team": [], "assignments": [], "unmet": reqs,
                "warnings": ["No level-%d characters with accounts to build from." % cap_level]}

    _, team, unmet, low = best
    assignments = []
    for r in reqs:
        holders = sorted(team, key=lambda c: -_req_tier(c, r))
        top = holders[0] if holders and _req_tier(holders[0], r) > 0 else None
        label = REQ_DEFS[r]["label"] if r in REQ_DEFS else r
        assignments.append({
            "req": r, "label": label,
            "toon": top["name"] if top else None,
            "tier": _req_tier(top, r) if top else 0,
        })
    warnings = []
    for a in assignments:
        if a["tier"] == 1:
            warnings.append("%s is only covered by %s as a FALLBACK (e.g. bard-weave slow is "
                            "risky vs keeping support songs up) — no primary provider fits."
                            % (a["label"], a["toon"]))
    return {"slots": [c["id"] for c in team], "team": [c["name"] for c in team],
            "assignments": assignments, "unmet": unmet, "warnings": warnings}


def _score_team(team, reqs):
    unmet, low = [], []
    score = 0
    for r in reqs:
        tier = max((_req_tier(c, r) for c in team), default=0)
        if tier == 0:
            unmet.append(r)
        else:
            score += tier * 100
            if tier == 1:
                low.append(r)
    score += 15 * sum(1 for c in team if c.get("automation_status") == "tested")
    score += sum(c.get("level") or 0 for c in team) // 10
    # mild bonus for capability breadth so free slots pick complementary toons
    score += len(group_caps(team))
    return score, unmet, low


# --- Roster optimizer -------------------------------------------------------

# Roles you usually want live at the same time — pairs of these stuck on one
# account are the "dangerous pairings".
KEY_ROLES = ["tank", "healing", "slow", "cc", "resurrection", "haste"]
CRITICAL_ROLES = [("tank", "No tank in the roster"),
                  ("healing", "No healer in the roster"),
                  ("slow", "No slow in the roster"),
                  ("haste", "No haste in the roster"),
                  ("cc", "No crowd control in the roster"),
                  ("resurrection", "No resurrection in the roster"),
                  ("pulling", "No puller in the roster")]

# Which class most cheaply re-covers a lost role (for the "level a second X" suggestion).
ROLE_FIX_CLASS = {"tank": "Warrior", "healing": "Cleric", "slow": "Shaman",
                  "cc": "Enchanter", "resurrection": "Cleric", "haste": "Enchanter"}


def _active(chars):
    return [c for c in chars if (c.get("status") or "active") != "retired"]


def _best_open_accounts(accounts, chars, exclude_ids=()):
    """Accounts sorted emptiest-first (fewest non-retired characters)."""
    counts = {a["id"]: 0 for a in accounts}
    for c in _active(chars):
        if c.get("account_id") in counts:
            counts[c["account_id"]] += 1
    ranked = sorted((a for a in accounts if a["id"] not in exclude_ids),
                    key=lambda a: (counts.get(a["id"], 0), a.get("launch_order") or 99))
    return [(a, counts.get(a["id"], 0)) for a in ranked]


def sub_window(acct):
    """(earliest, latest) epoch the sub can lapse. Accounts written before the
    window migration only have the earliest, so it stands in for both."""
    lo = acct.get("sub_expires")
    if not lo:
        return None, None
    return lo, acct.get("sub_expires_max") or lo


def _hours(seconds):
    """Whole hours, rounded up — 'expires in 1h' should not read 0h at 59 min."""
    return max(0, -(-int(seconds) // 3600))


def sub_expiry_message(alias, lo, hi, now):
    """Krono warning for one account, or None when it is comfortably fine.

    lo/hi bracket the true expiry (see api._expiry_window). Everything is phrased
    relative — hours near the end, days further out — so the caller never has to
    trust a precise-looking timestamp that is really a floor'd day count.
    """
    hi = hi or lo
    if now >= hi:
        return "%s's membership has LAPSED — apply a krono." % alias
    if now >= lo:
        # Inside the uncertainty window: it is live right now but can drop any tick.
        return ("%s's membership runs out within the next %dh — it can drop mid-session. "
                "Apply a krono now." % (alias, _hours(hi - now)))
    if hi - now <= 48 * 3600:
        return ("%s's membership expires in %dh–%dh — krono today."
                % (alias, _hours(lo - now), _hours(hi - now)))
    lo_d, hi_d = int((lo - now) / 86400), int((hi - now) / 86400)
    if lo_d <= 7:
        span = "%d" % lo_d if lo_d == hi_d else "%d–%d" % (lo_d, hi_d)
        return ("%s's membership expires in %s day%s — krono soon."
                % (alias, span, "" if hi_d == 1 else "s"))
    return None


def recommendations(accounts, chars, unlocks, compositions, now=None):
    """Roster-wide suggestions. chars must carry resolved caps + account_alias.
    now (epoch seconds) enables membership-expiry checks; None skips them."""
    recs = []
    active = _active(chars)

    # 0. Memberships lapsing (krono due) or already lapsed.
    if now is not None:
        for a in accounts:
            member = (a.get("membership") or "").upper()
            lo, hi = sub_window(a)
            if member == "FREE":
                recs.append({"kind": "membership",
                             "message": "%s has NO active membership (FREE) — TLP access blocked until you apply a krono." % a["alias"]})
            elif member and lo:
                msg = sub_expiry_message(a["alias"], lo, hi, now)
                if msg:
                    recs.append({"kind": "membership", "message": msg})

    # 1. Critical roles nobody covers.
    have = group_caps(active)
    for role, msg in CRITICAL_ROLES:
        if role not in have:
            fix = ROLE_FIX_CLASS.get(role)
            extra = (" Consider a %s on %s." % (fix, _best_open_accounts(accounts, chars)[0][0]["alias"])
                     if fix and accounts else "")
            recs.append({"kind": "missing_role", "message": msg + "." + extra})

    # 2. Dangerous same-account pairings: two toons on one account each holding a
    #    key role the other doesn't, where losing either coverage hurts a live six.
    holders = {r: set() for r in KEY_ROLES}
    for c in active:
        for r in KEY_ROLES:
            if (c.get("caps") or {}).get(r):
                holders[r].add(c["id"])
    by_acct = {}
    for c in active:
        if c.get("account_id") is not None:
            by_acct.setdefault(c["account_id"], []).append(c)
    for aid, group in sorted(by_acct.items()):
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                ra = {r for r in KEY_ROLES if (a.get("caps") or {}).get(r)}
                rb = {r for r in KEY_ROLES if (b.get("caps") or {}).get(r)}
                # roles each uniquely brings vs the other
                only_a, only_b = ra - rb, rb - ra
                if not (only_a and only_b):
                    continue
                # is some pair of (role from a, role from b) impossible to run together
                # without these two toons? i.e. one of them is the ONLY other holder.
                blocked = []
                for x in sorted(only_a):
                    for y in sorted(only_b):
                        if not ((holders[x] - {a["id"], b["id"]}) and (holders[y] - {a["id"], b["id"]})):
                            blocked.append((x, y))
                if not blocked:
                    continue
                x, y = blocked[0]
                alias = a.get("account_alias") or ("account %s" % aid)
                open_accts = _best_open_accounts(accounts, chars, exclude_ids={aid})
                suggest = ""
                if open_accts:
                    fix_role = x if len(holders[x]) <= len(holders[y]) else y
                    fix_cls = ROLE_FIX_CLASS.get(fix_role, "")
                    suggest = (" Consider leveling a second %s on %s." %
                               (fix_cls or fix_role, open_accts[0][0]["alias"]))
                recs.append({"kind": "account_pairing",
                             "message": "%s (%s) and %s (%s) share %s, so you cannot use %s and %s together.%s"
                             % (a["name"], a.get("class_name", "?"), b["name"], b.get("class_name", "?"),
                                alias, CAP_LABELS.get(x, x).lower(), CAP_LABELS.get(y, y).lower(), suggest)})

    # 3. Best account for the next character.
    if accounts:
        ranked = _best_open_accounts(accounts, chars)
        best, n = ranked[0]
        recs.append({"kind": "next_account",
                     "message": "Best account for your next character: %s (%d character%s on it)."
                     % (best["alias"], n, "" if n == 1 else "s")})

    # 4. Characters missing critical unlocks.
    by_char = {c["id"]: c for c in chars}
    for u in unlocks:
        if u.get("priority") in ("critical", "high") and u.get("status") in ("Missing", "Unknown"):
            c = by_char.get(u["character_id"])
            if c and (c.get("status") or "active") != "retired":
                recs.append({"kind": "missing_unlock",
                             "message": "%s: %s is %s (%s priority)."
                             % (c["name"], u["name"], u["status"].lower(), u["priority"])})

    # 5. Compositions demanding too much attention (3+ not-fully-automated members).
    for comp in compositions:
        needy = [m["name"] for m in comp.get("members", [])
                 if (m.get("automation_status") or "untested") != "tested"]
        if len(needy) >= 3:
            recs.append({"kind": "attention",
                         "message": "'%s' needs heavy attention: %d of %d members have untested/partial automation (%s)."
                         % (comp["name"], len(needy), len(comp.get("members", [])), ", ".join(needy))})
    return recs
