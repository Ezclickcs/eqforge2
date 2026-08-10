-- My Characters: EQ multibox roster manager (EQ Forge 2.0 section)
-- SQLite schema. Rule: only ONE character per EQ account may be live in a composition.
-- NEVER any password / auth column anywhere in this schema.

CREATE TABLE IF NOT EXISTS accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    alias           TEXT NOT NULL UNIQUE,           -- primary label (login username after "Use login names")
    nickname        TEXT DEFAULT '',                -- optional friendly label ("Cleric Account"); UI can flip between them
    account_number  TEXT DEFAULT '',                -- ordinal / external ref (NEVER credentials)
    status          TEXT NOT NULL DEFAULT 'active', -- active | inactive | banned | unknown
    autologin_group TEXT DEFAULT '',                -- MQ2AutoLogin profile group
    launch_order    INTEGER DEFAULT 0,
    perks           TEXT NOT NULL DEFAULT '[]',     -- JSON array: account-wide claims (mount, clickies...)
    membership      TEXT DEFAULT '',                -- GOLD | SILVER | FREE (from Me.MembershipLevel)
    sub_expires     INTEGER,                        -- EARLIEST epoch the sub can lapse (asof + SubscriptionDays)
    sub_expires_max INTEGER,                        -- LATEST epoch it can lapse. Me.SubscriptionDays is a
                                                    -- FLOOR'd day count, so a reading of N at time T only
                                                    -- proves expiry is in [T+N days, T+N+1 days). Narrowed by
                                                    -- intersecting every toon's reading on the account.
    notes           TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS characters (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL,
    server            TEXT NOT NULL DEFAULT 'Frostreaver',
    account_id        INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    class_name        TEXT NOT NULL DEFAULT '',
    level             INTEGER,                       -- NULL = unknown
    race              TEXT DEFAULT '',
    main_role         TEXT DEFAULT '',               -- tank | healer | dps | support | puller | box
    status            TEXT NOT NULL DEFAULT 'active',-- active | parked | planned | retired
    group_tags        TEXT DEFAULT '',               -- comma list: core, raid, pl, farm...
    gear_tier         TEXT DEFAULT '',
    epic_status       TEXT DEFAULT '',               -- none | in-progress | complete
    portbot_whitelist INTEGER NOT NULL DEFAULT 0,    -- 0/1
    automation_status TEXT NOT NULL DEFAULT 'untested', -- tested | partial | untested | manual-only
    notes             TEXT DEFAULT '',
    UNIQUE(server, name COLLATE NOCASE)
);

-- Capability overrides only; defaults come from class (domain.py CLASS_DEFAULTS).
CREATE TABLE IF NOT EXISTS capability_overrides (
    character_id INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    capability   TEXT NOT NULL,
    enabled      INTEGER NOT NULL,                   -- 0/1
    PRIMARY KEY (character_id, capability)
);

-- Flexible unlock / keys / access table (no permanent columns on characters).
CREATE TABLE IF NOT EXISTS unlocks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id  INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,                     -- e.g. "Sebilis Key"
    expansion     TEXT DEFAULT '',
    category      TEXT DEFAULT 'key',                -- key | access | clicky | flag | automation
    scope         TEXT DEFAULT 'character',          -- character | account
    priority      TEXT DEFAULT 'normal',             -- critical | high | normal | low
    status        TEXT NOT NULL DEFAULT 'Unknown',   -- Complete | In Progress | Missing | Unknown | Not Needed
    verified_date TEXT DEFAULT '',
    notes         TEXT DEFAULT ''
);

-- Editable raid list for lockout identification (KT, Yelinak, Tunare, Sleeper's...).
CREATE TABLE IF NOT EXISTS raids (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS raid_lockouts (
    character_id INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    raid_id      INTEGER NOT NULL REFERENCES raids(id) ON DELETE CASCADE,
    notes        TEXT DEFAULT '',
    expires_at   INTEGER,                            -- epoch seconds; NULL = manual, no expiry
    imported_at  INTEGER,                            -- when the MQ export set this; NULL = manual check
    PRIMARY KEY (character_id, raid_id)
);

-- Gear sets: a named worn loadout (usually snapshotted from a toon's inventory dump),
-- assignable to any toon. The planner routes each piece by REAL transfer cost:
-- already-there < shared-bank grab < same-account swap < trade < parcel.
CREATE TABLE IF NOT EXISTS gear_sets (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL UNIQUE,
    class_name       TEXT DEFAULT '',
    source_char_id   INTEGER REFERENCES characters(id) ON DELETE SET NULL,  -- snapshotted from
    assigned_char_id INTEGER REFERENCES characters(id) ON DELETE SET NULL,  -- who wears it next
    active           INTEGER NOT NULL DEFAULT 1,   -- retired sets release their item claims
    notes            TEXT DEFAULT '',
    created_at       INTEGER,
    updated_at       INTEGER
);

CREATE TABLE IF NOT EXISTS gear_set_items (
    set_id     INTEGER NOT NULL REFERENCES gear_sets(id) ON DELETE CASCADE,
    slot       TEXT NOT NULL DEFAULT '',            -- worn slot label from the dump (Ear, Chest...)
    slot_index INTEGER NOT NULL DEFAULT 0,          -- 0/1 for paired slots (Ear/Wrist/Fingers)
    item_id    INTEGER NOT NULL,
    item_name  TEXT NOT NULL,
    PRIMARY KEY (set_id, slot, slot_index)
);

CREATE TABLE IF NOT EXISTS compositions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL UNIQUE,
    notes            TEXT DEFAULT '',
    required_unlocks TEXT DEFAULT ''                 -- comma list of unlock names every member needs
);

CREATE TABLE IF NOT EXISTS composition_slots (
    composition_id INTEGER NOT NULL REFERENCES compositions(id) ON DELETE CASCADE,
    slot_index     INTEGER NOT NULL,                 -- 0..5
    character_id   INTEGER REFERENCES characters(id) ON DELETE SET NULL,
    -- WHICH gear set this comp fields for that toon. Most toons own several (Zyrak
    -- has Rogue Main, Sleeper Rogue 3 and Zyrak (Rogue)), so without this the
    -- "apply this comp's gear" action has to guess, and the old guess -- newest
    -- updated active set -- silently picked a set from a different comp.
    gear_set_id    INTEGER REFERENCES gear_sets(id) ON DELETE SET NULL,
    PRIMARY KEY (composition_id, slot_index)
);
