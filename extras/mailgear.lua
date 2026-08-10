--[[ ------------------------------------------------------------------------
  MailGear  -  standalone MacroQuest Lua
  Version 1.1.0

  Acts on the gear plan exported by EQ Forge 2.0's Gear Planner
  (<MQ>/config/mailgearplan.lua). Dequips planned pieces into bags, pulls
  them out of the bank, equips them on the receiving toon, and lists the ones
  that CANNOT be automated (Hoard / persona).

  Install:  <MacroQuest>/lua/mailgear/init.lua
  Run:      /lua run mailgear
  Stop:     /lua stop mailgear        (never stopped from inside - see NOTES)

  SAFETY MODEL
    * DRY-RUN by default. Nothing moves until /mailgear live on.
    * /mailgear stop is an emergency stop: it clears every queue instantly.
    * Queues are ticked ONE item per pass by the main loop, never by the command
      handler, so the stop always wins.
    * Every pickup verifies the cursor holds EXACTLY the intended item id before
      it does anything with it.

  The item-moving primitives here are ported from TrixBox, where they were
  debugged against a live 6-box crew. The comments marked "proven <date>" record
  real failures - do not "simplify" those paths without re-testing in game.

  NOTES / hard rules this script obeys (learned the hard way in MQ2Lua):
    * mq.delay() yields the coroutine and CANNOT cross a pcall boundary. pcall
      is therefore only ever wrapped around TLO reads, never around anything
      that delays. A pcall'd TLO read that fails must LOG, never silently pass.
    * A script must never participate in stopping itself. There is deliberately
      no /mailgear quit. Use /lua stop mailgear.
    * TLO member names must be EXACT - a wrong one throws on index.
------------------------------------------------------------------------ ]]

local mq = require('mq')

local VERSION = '1.2.0'

-- EQ Forge writes the plan under BOTH names: 'mailgearplan.lua' (ours) and the older
-- 'trixbox_gearplan.lua' (which TrixBox hardcodes). Prefer ours, fall back to the old
-- one so a stale export - or a plan written by an older build - still loads.
local PLAN_FILES = { 'mailgearplan.lua', 'trixbox_gearplan.lua' }

local Me = tostring(mq.TLO.Me.CleanName() or 'unknown')

local State = {
    plans      = nil,     -- loaded plan list
    planIdx    = 1,       -- active plan number
    live       = false,   -- false = dry-run
    estopped   = false,
    queue      = nil,     -- { kind='dequip'|'bank'|'equip', items={}, i=1, done=0, used={} }
    hoardList  = nil,
    hoardWin   = false,
    lastAction = '',
}

-- ==========================================================================
-- logging
-- ==========================================================================
local function log(msg, ...)
    local ok, line = pcall(string.format, msg, ...)
    if not ok then line = tostring(msg) end
    State.lastAction = line
    if printf then printf('\at[MailGear]\ax %s', line) else print('[MailGear] ' .. line) end
end

-- ==========================================================================
-- plan loading
-- ==========================================================================
-- The exported file returns { plans = { {name,set,target,moves={...}}, ... },
-- plus a back-compat mirror of the first plan at the top level. A move is
-- { id, name, from, to, slot, fromLoc, attuneRisk }.
local function loadPlans()
    local path, chunk, lerr
    for _, fn in ipairs(PLAN_FILES) do
        local p = string.format('%s/%s', mq.configDir, fn)
        local c, e = loadfile(p)
        if c then path, chunk = p, c break end
        if not lerr then lerr = e end            -- report the PREFERRED name's error
    end
    if not chunk then
        log('no gear plan in %s (looked for %s)', mq.configDir, table.concat(PLAN_FILES, ', '))
        log('  %s', tostring(lerr))
        log('  In EQ Forge: Gear Planner -> "Send plans to MQ", then /mailgear plans')
        return nil
    end
    local ok, data = pcall(chunk)
    if not ok or type(data) ~= 'table' then
        log('gear plan at %s is malformed - re-export it.', path)
        return nil
    end
    local plans = {}
    if type(data.plans) == 'table' then
        for _, p in ipairs(data.plans) do
            if type(p) == 'table' and type(p.moves) == 'table' then table.insert(plans, p) end
        end
    end
    if #plans == 0 and type(data.moves) == 'table' then
        table.insert(plans, { name = data.name or data.set or 'plan', target = data.target, moves = data.moves })
    end
    if #plans == 0 then log('gear plan at %s has no plans - re-export it.', path); return nil end
    return plans
end

local function activePlan()
    if not State.plans then State.plans = loadPlans() end
    if not State.plans then return nil end
    local p = State.plans[State.planIdx or 1]
    if not p then
        log('plan #%d does not exist (%d loaded). /mailgear useplan <n>', State.planIdx or 1, #State.plans)
        return nil
    end
    return p
end

-- Every routed piece across EVERY loaded plan, so one toon can settle all its
-- debts in one pass instead of switching plans by hand.
--
-- Prefer p.rows over p.moves. `moves` is swap/trade/parcel only - the pieces some
-- OTHER toon has to hand over. `rows` (EQ Forge 2026-08-09 and later) is every
-- piece the set calls for, including the ones already sitting on the target:
--   have  - in the target's own bags/bank. Nobody hands it over, but it still has
--           to be equipped, and before `rows` existed it never reached this script
--           at all: "even when the item is already on the toon it needs to be,
--           sitting in their bag" it just got skipped.
--   grab  - in the target's own shared bank. A bank pull, not a transfer.
--   worn  - already on the body. No work, but it RESERVES its slot so the equip
--           step cannot overwrite a piece the set already has right (see
--           doEquipItem).
-- An older export has no `rows`; falling back to `moves` keeps it working exactly
-- as before.
local function allMoves()
    if not State.plans then State.plans = loadPlans() end
    if not State.plans then return {} end
    local out = {}
    for _, p in ipairs(State.plans) do
        for _, mv in ipairs(p.rows or p.moves or {}) do
            local c = {}
            for k, v in pairs(mv) do c[k] = v end
            c._plan, c._target = p.name or 'plan', mv.to or p.target
            table.insert(out, c)
        end
    end
    return out
end

-- fromLoc is the RAW dump Location string ("Chest", "Ear-Slot1", "General1-Slot3",
-- "Bank3-Slot8", "Hoard12", "Equipment"), NOT a category. This mirrors locBucket()
-- in app/forge.js exactly - keep the two in sync or filtering silently misses
-- pieces. (A worn item reports its SLOT NAME, which is why testing for the string
-- "equipped" here finds nothing.)
local EQUIP_SLOTS = {
    Charm = true, Ear = true, Head = true, Face = true, Neck = true, Shoulders = true,
    Arms = true, Back = true, Wrist = true, Range = true, Hands = true, Primary = true,
    Secondary = true, Fingers = true, Chest = true, Legs = true, Feet = true,
    Waist = true, Ammo = true, ['Power Source'] = true, Held = true,
}

local function locBucket(loc)
    local l   = tostring(loc or ''):gsub('^%s+', ''):gsub('%s+$', '')
    local low = string.lower(l)
    if low:find('^sharedbank') then return 'shared'  end
    if low:find('^bank')       then return 'bank'    end
    if low:find('^general')    then return 'bags'    end
    if low:find('^hoard')      then return 'hoard'   end
    -- "Equipment" = a PERSONA's closet: gear worn by an inactive persona. Reachable
    -- only by switching to that persona and unequipping.
    if low == 'equipment'      then return 'persona' end
    if low == 'keyring'        then return 'keyring' end
    local head = l:match('^([^-]*)') or l
    head = head:gsub('^%s+', ''):gsub('%s+$', '')
    return EQUIP_SLOTS[head] and 'equipped' or 'other'
end

-- The exporter (mychars/gear.py loc_bucket) is the authority on what a location
-- means and knows buckets this file's string matching does not - 'depot' being the
-- live one: a Personal Tradeskill Depot location falls through locBucket() to
-- 'other', which would have queued an unreachable item for equipping. Prefer the
-- exported bucket, normalising its one naming difference.
local BUCKET_ALIAS = { worn = 'equipped' }
local function bucketOf(mv)
    local b = string.lower(tostring(mv.bucket or ''))
    if b ~= '' then return BUCKET_ALIAS[b] or b end
    return locBucket(mv.fromLoc)            -- older plan: no bucket field
end

-- Row status as exported by EQ Forge. An OLDER plan file carries no status field
-- at all, and everything in one of those was a swap/trade/parcel - so a missing
-- status has to read as transfer work, or upgrading the app would silently empty
-- every queue built from a plan the user had already exported.
local function statusOf(mv)
    local s = string.lower(tostring(mv.status or ''))
    if s == '' then return 'move' end
    return s
end

-- Somebody else hands it over: it lands in this toon's bags, then gets equipped.
local TRANSFER_STATUS = { swap = true, trade = true, parcel = true, move = true }
-- Already on the target. No transfer, but not necessarily nothing to do.
local SELF_STATUS     = { have = true, grab = true }

-- ==========================================================================
-- cursor + inventory primitives  (ported from TrixBox - proven in the field)
-- ==========================================================================
local function cursorId()
    local ok, id = pcall(function() return tonumber(tostring(mq.TLO.Cursor.ID() or 0)) or 0 end)
    if not ok then log('  (cursor read failed)'); return 0 end
    return id or 0
end

-- Place the CURSOR item into a specific empty bag slot, trying each empty slot
-- until one accepts it. '/autoinventory' is all-or-nothing and silent about why
-- it failed; EQ also refuses a slot in a bag too SMALL for the item, so "free
-- slots" alone is not enough. Returns placed, tried, packN, slotN.
local function stowToBags()
    if cursorId() == 0 then return true, 0 end
    local tried = 0
    for packIdx = 23, 34 do
        local pack = mq.TLO.Me.Inventory(packIdx)
        local okC, cap = pcall(function() return tonumber(tostring(pack.Container() or 0)) or 0 end)
        if not okC then log('  (container read failed on pack %d)', packIdx - 22) end
        if pack() and okC and cap > 0 then
            for j = 1, cap do
                local occupied = true
                local okI = pcall(function() occupied = pack.Item(j)() ~= nil end)
                if not okI then log('  (slot read failed on pack %d slot %d)', packIdx - 22, j) end
                if okI and not occupied then
                    tried = tried + 1
                    mq.cmdf('/nomodkey /itemnotify in pack%d %d leftmouseup', packIdx - 22, j)
                    mq.delay(600, function() return cursorId() == 0 end)
                    if cursorId() == 0 then return true, tried, packIdx - 22, j end
                end
            end
        end
    end
    return false, tried
end

local function stowCursor()
    if cursorId() ~= 0 then stowToBags() end
    if cursorId() ~= 0 then
        mq.cmd('/autoinventory')
        mq.delay(400, function() return cursorId() == 0 end)
    end
end

-- Put a specific item id on the cursor. Returns true only if the cursor ends up
-- holding EXACTLY that id.
local function grabItemById(id)
    if cursorId() == id then return true end
    -- A leftover from a previous failed attempt would otherwise block every
    -- pickup ("could not pick up" on the very first item, proven 2026-07-22).
    if cursorId() ~= 0 then stowCursor() end
    if cursorId() ~= 0 then log('  grab %d: cursor is stuck holding something else', id); return false end
    local it = mq.TLO.FindItem(id)
    if not it() then return false end
    local okS,  slot  = pcall(function() return tonumber(tostring(it.ItemSlot()  or -1)) or -1 end)
    local okS2, slot2 = pcall(function() return tonumber(tostring(it.ItemSlot2() or -2)) or -2 end)
    if not okS or slot < 0 then log('  grab %d: bad ItemSlot', id); return false end
    if not okS2 then log('  grab %d: ItemSlot2 read failed', id); slot2 = -2 end
    if slot >= 0 and slot <= 22 then
        -- WORN slot: plain itemnotify. '/shift' is the stack-SPLIT modifier and
        -- does NOT reliably lift equipped gear - that was the pickup failure.
        mq.cmdf('/nomodkey /itemnotify %d leftmouseup', slot)
    elseif slot2 >= 0 then
        mq.cmdf('/nomodkey /shift /itemnotify in pack%d %d leftmouseup', slot - 22, slot2 + 1)
    else
        mq.cmdf('/nomodkey /itemnotify %d leftmouseup', slot)
    end
    mq.delay(800, function() return cursorId() ~= 0 end)
    return cursorId() == id
end

-- ==========================================================================
-- DEQUIP  (worn -> bags)
-- ==========================================================================
-- Returns ok, fatal. FATAL means "no point trying any further item" - the ONLY
-- such case is zero empty bag slots. Every other failure is THIS item's problem
-- and the caller must skip it and keep going (a blanket bail once stopped a
-- 17-piece batch on piece 1; the next run moved 16 of them).
local function doDequipItem(it)
    local fi = mq.TLO.FindItem(it.id)
    if not fi() then log('  dequip %s: not on this toon', it.name); return false, false end
    local okS, slot = pcall(function() return tonumber(tostring(fi.ItemSlot() or -1)) or -1 end)
    if not okS then log('  dequip %s: ItemSlot read failed', it.name); return false, false end
    if slot < 0 or slot > 22 then return true, false end          -- already in bags
    if not grabItemById(it.id) then log('  dequip %s: could not pick up', it.name); return false, false end
    if cursorId() ~= it.id then
        log('  dequip %s: wrong item on cursor - abort', it.name); stowCursor(); return false, false
    end
    local placed, tried, packN, slotN = stowToBags()
    if placed then
        log('  dequipped %s -> bag %s slot %s', it.name, tostring(packN or '?'), tostring(slotN or '?'))
        return true, false
    end
    log('  dequip BLOCKED: %s - tried %d empty bag slot(s), none accepted it (too big?).', it.name, tried or 0)
    local noSpaceAtAll = (tried or 0) == 0
    if noSpaceAtAll then log('    -> you have NO empty bag slots at all.') end
    stowCursor()
    if cursorId() ~= 0 then log('    -> WARNING: %s is still ON YOUR CURSOR - put it away manually.', it.name) end
    return false, noSpaceAtAll
end

-- ==========================================================================
-- BANK PULL  (bank / shared bank -> bags)
-- ==========================================================================
-- Bank slots ARE addressable (bank1..bank24, sharedbank1..6; items inside a bank
-- bag use "in bankN <pos>"), unlike the HOARD which has no MQ slot names at all.
-- NOTE FindItem does not search the bank - FindItemBank does. Bank window must
-- be open.
local function clickBankSlot(base, n, slot2)
    if slot2 >= 0 then mq.cmdf('/nomodkey /itemnotify in %s%d %d leftmouseup', base, n, slot2 + 1)
    else mq.cmdf('/nomodkey /itemnotify %s%d leftmouseup', base, n) end
end

-- Returns true only when OUR item is on the cursor; if the click lifted
-- something else, it puts that straight back before giving up.
local function tryBankPickup(it, base, n, slot2)
    clickBankSlot(base, n, slot2)
    mq.delay(800, function() return cursorId() ~= 0 end)
    local got = cursorId()
    if got == it.id then return true end
    if got ~= 0 then
        clickBankSlot(base, n, slot2)                       -- wrong item: put it back
        mq.delay(600, function() return cursorId() == 0 end)
    end
    return false
end

local function doPullFromBank(it)
    if not mq.TLO.Window('BigBankWnd').Open() then log('  bank: bank window is not open'); return false end
    local fi = mq.TLO.FindItemBank(it.id)
    if not fi() then return true end                        -- not in the bank; nothing to do
    local okS,  slot  = pcall(function() return tonumber(tostring(fi.ItemSlot()  or -1)) or -1 end)
    local okS2, slot2 = pcall(function() return tonumber(tostring(fi.ItemSlot2() or -2)) or -2 end)
    if not okS or slot < 0 then log('  bank %s: bad ItemSlot', it.name); return false end
    if not okS2 then log('  bank %s: ItemSlot2 read failed', it.name); slot2 = -2 end
    -- FindItemBank reports a ZERO-BASED bank index - NOT the 2000-based
    -- /itemnotify id the slot-name docs list. Proven 2026-07-22: it returned 2
    -- for an item the dump places in "Bank3-Slot8", and 'bank0' made MQ answer
    -- "Could not find slot to send notification to". So /itemnotify wants slot+1.
    local n = slot + 1
    if slot >= 2500 then n = slot - 2499 elseif slot >= 2000 then n = slot - 1999 end
    -- Bank vs SHARED bank comes from the plan's recorded location. NO blind
    -- fallback: "try the other one at the same index" once lifted UNRELATED
    -- items out of the shared bank and could not put them back (2026-07-22).
    local base = (bucketOf(it) == 'shared') and 'sharedbank' or 'bank'
    -- Loose in the slot, or inside a bag sitting in that slot? ASK the slot
    -- instead of inferring from ItemSlot2 (a loose item reports 0, not -1).
    local holder = (base == 'sharedbank') and mq.TLO.Me.SharedBank(n) or mq.TLO.Me.Bank(n)
    local isBag = false
    local okB = pcall(function() isBag = (tonumber(tostring(holder.Container() or 0)) or 0) > 0 end)
    if not okB then log('  bank %s: container read failed on %s%d', it.name, base, n) end
    local pos = isBag and (slot2 >= 0 and slot2 or 0) or -1
    if cursorId() ~= 0 then stowCursor() end
    if not tryBankPickup(it, base, n, pos) then
        log('  bank %s: could not lift it from %s%d%s', it.name, base, n,
            isBag and string.format(' (bag pos %d)', pos + 1) or ' (loose slot)')
        return false
    end
    local placed, tried, packN, slotN = stowToBags()
    if placed then
        log('  pulled %s from %s%d -> bag %s slot %s', it.name, base, n, tostring(packN or '?'), tostring(slotN or '?'))
        return true
    end
    -- Bags can't take it: put it back in the bank rather than leave it on the cursor.
    log('  bank %s: no bag slot accepted it (tried %d) - returning it to %s%d', it.name, tried or 0, base, n)
    clickBankSlot(base, n, pos)
    mq.delay(600, function() return cursorId() == 0 end)
    if cursorId() ~= 0 then log('    -> WARNING: %s is on your CURSOR - put it away manually.', it.name) end
    return false
end

-- ==========================================================================
-- EQUIP  (bags -> worn)
-- ==========================================================================
local EQUIP_SLOT_IDX = {
    Charm = {0}, Ear = {1, 4}, Head = {2}, Face = {3}, Neck = {5}, Shoulders = {6},
    Arms = {7}, Back = {8}, Wrist = {9, 10}, Range = {11}, Hands = {12},
    Primary = {13}, Secondary = {14}, Fingers = {15, 16}, Chest = {17}, Legs = {18},
    Feet = {19}, Waist = {20}, ['Power Source'] = {21}, Ammo = {22},
}

local function isWornSlot(n) return n and n >= 0 and n <= 22 end

-- Equip by putting the piece on the cursor and CLICKING the worn slot - the
-- exact inverse of the dequip that already works. '/autoinventory' does NOT
-- equip (it just stows to a bag), and MQ has no /exchange, so the slot click is
-- the way. Returns 'equipped' | 'worn' | 'missing' | 'failed'.
local function doEquipItem(it, used)
    local fi = mq.TLO.FindItem(it.id)
    if not fi() then log('  equip %s: not in inventory yet (not delivered?)', it.name); return 'missing' end
    local okA, cur = pcall(function() return tonumber(tostring(fi.ItemSlot() or -1)) or -1 end)
    if not okA then log('  equip %s: ItemSlot read failed', it.name); return 'failed' end
    if isWornSlot(cur) then log('  already worn: %s', it.name); return 'worn' end

    local cands = EQUIP_SLOT_IDX[tostring(it.slot or '')]
    if not cands then
        -- No slot recorded (older plan): stow it and say so rather than guess.
        if not grabItemById(it.id) then log('  equip %s: could not pick up', it.name); return 'failed' end
        mq.cmd('/autoinventory'); mq.delay(500, function() return cursorId() == 0 end); stowCursor()
        log('  left in bags (no slot recorded): %s', it.name)
        return 'failed'
    end
    -- SET-SWAP FIX: two-slot types (Ear / Wrist / Fingers) used to fall back to
    -- cands[1] whenever BOTH slots were occupied - exactly the case mid-swap,
    -- when both still hold the OLD set. The 2nd earring then overwrote the 1st
    -- one just equipped. Also avoid any slot THIS batch already filled.
    --
    -- `used` is now PRE-SEEDED by reservedWornSlots() with every slot that already
    -- holds a piece this plan wants (see armQueue). That closes the other half of
    -- the same bug: when only ONE of the two slots needed changing, the good piece
    -- was in cands[1] and the fallback below overwrote it. reported 2026-08-09: "just
    -- swaps out the wrist/ring that was recently equipped."
    local target = nil
    for _, idx in ipairs(cands) do              -- 1st choice: empty and untouched
        local okE, empty = pcall(function() return mq.TLO.Me.Inventory(idx)() == nil end)
        if not okE then log('  (worn slot %d read failed)', idx) end
        if okE and empty and not (used and used[idx]) then target = idx break end
    end
    if not target then                         -- 2nd: occupied by the OLD set, not one we just filled
        for _, idx in ipairs(cands) do
            if not (used and used[idx]) then target = idx break end
        end
    end
    if not target then
        -- Every slot this piece could take is already holding a piece the set
        -- wants. Overwriting one would undo gear that is already correct, so stop
        -- and say so - the old `target = target or cands[1]` fallback is exactly
        -- what made the second ring eat the first.
        log('  %s: every %s slot already holds a piece this set wants - left in bags',
            tostring(it.name), tostring(it.slot))
        return 'reserved'
    end
    if used then used[target] = true end
    if not grabItemById(it.id) then log('  equip %s: could not pick up', it.name); return 'failed' end
    if cursorId() ~= it.id then log('  equip %s: wrong item on cursor - abort', it.name); stowCursor(); return 'failed' end
    mq.cmdf('/nomodkey /itemnotify %d leftmouseup', target)     -- cursor -> worn slot
    mq.delay(800, function() return cursorId() ~= it.id end)
    -- A swap hands the OLD piece back on the cursor; put it away rather than drop it.
    if cursorId() ~= 0 then
        local swapped = cursorId()
        stowToBags()
        if cursorId() ~= 0 then stowCursor() end
        if cursorId() == 0 and swapped ~= it.id then log('    (swapped out the old piece into bags)') end
    end
    local okN, now = pcall(function() return tonumber(tostring(mq.TLO.FindItem(it.id).ItemSlot() or -1)) or -1 end)
    if not okN then log('  equip %s: verify read failed', it.name); return 'failed' end
    if isWornSlot(now) then log('  equipped %s', it.name); return 'equipped' end
    log('  would not go on: %s (left in bags)', it.name)
    return 'failed'
end

-- ==========================================================================
-- queue building
-- ==========================================================================
local function mineFrom()   -- transfers this toon is HOLDING for somebody else
    local out = {}
    for _, mv in ipairs(allMoves()) do
        -- `from` is only ever set on a transfer; self-sourced rows leave it empty
        -- precisely so they never land in a holder's dequip queue.
        if tostring(mv.from or '') ~= '' and tostring(mv.from or '') == Me then
            table.insert(out, mv)
        end
    end
    return out
end

local function mineRows()   -- every row destined FOR this toon, whatever its status
    local out = {}
    for _, mv in ipairs(allMoves()) do
        if tostring(mv.to or '') == Me then table.insert(out, mv) end
    end
    return out
end

-- Pieces this toon can put ON right now: anything handed over by somebody else
-- (it arrives in the bags), plus anything the set wants that is ALREADY in this
-- toon's own bags. The second half is the fix for "it's already in their bag and
-- nothing equips it" - those rows are status 'have', and they used to be filtered
-- out of the export entirely.
--
-- Deliberately NOT queued here:
--   worn            nothing to do; it only reserves its slot in doEquipItem
--   have/grab in a bank, shared bank or depot   -> /mailgear getbank first
--   have in hoard/persona                       -> /mailgear hoard (manual)
local function mineTo()
    local out = {}
    for _, mv in ipairs(mineRows()) do
        local st = statusOf(mv)
        if TRANSFER_STATUS[st] then
            table.insert(out, mv)
        elseif st == 'have' then
            local b = bucketOf(mv)
            -- 'other' is kept as a best-effort: doEquipItem re-checks FindItem and
            -- reports honestly if it cannot reach the piece. bank/shared/depot/
            -- hoard/persona/keyring are all deliberately excluded - each has its
            -- own verb, and queuing one here just produces a confusing failure.
            if b == 'bags' or b == 'other' then table.insert(out, mv) end
        end
    end
    return out
end

-- This toon's OWN bank / shared bank rows. Same bank-window work as a holder's
-- pull, but sourced from the target itself, so mineFrom() never sees them.
local function mineOwnBank()
    local out = {}
    for _, mv in ipairs(mineRows()) do
        local st = statusOf(mv)
        if SELF_STATUS[st] then
            local b = bucketOf(mv)
            if b == 'bank' or b == 'shared' or b == 'depot' then table.insert(out, mv) end
        end
    end
    return out
end

-- The Hoard has no MQ slot names, so it cannot be automated. Persona gear needs
-- a persona switch. Both are surfaced as a manual checklist instead.
local function manualPulls()
    local out = {}
    local seen = {}
    local function add(mv)
        local b = bucketOf(mv)
        local key = tostring(mv.id) .. '|' .. tostring(mv.fromLoc)
        if seen[key] then return end
        if b == 'hoard' then
            seen[key] = true
            table.insert(out, { name = mv.name, loc = mv.fromLoc, why = 'hoard', to = mv._target })
        elseif b == 'persona' then
            seen[key] = true
            table.insert(out, { name = mv.name, loc = 'persona closet', why = 'persona', to = mv._target })
        end
    end
    for _, mv in ipairs(mineFrom()) do add(mv) end
    -- The target's OWN hoard/persona counts too: the set wants the piece, it is on
    -- this very toon, and it is still un-automatable. Before rows/status existed
    -- these were routed 'have' and dropped from the export, so a piece sitting in
    -- your own Dragon's Hoard showed up nowhere at all.
    for _, mv in ipairs(mineRows()) do
        if SELF_STATUS[statusOf(mv)] then add(mv) end
    end
    return out
end

-- Worn slots that must NOT be touched by this equip batch, because they already
-- hold a piece the plan wants on this toon. Two-slot types are the whole reason
-- this exists: with one wrist already correct, the incoming wrist has exactly one
-- legal destination, and without a reservation the fallback picked the wrong one.
--
-- Pure TLO reads, so pcall is safe here (no mq.delay may ever cross a pcall).
local function reservedWornSlots()
    local rows = mineRows()
    local wanted = {}                       -- every item id this plan wants on me
    for _, mv in ipairs(rows) do
        local id = tonumber(mv.id or 0) or 0
        if id > 0 then wanted[id] = true end
    end
    local used, n = {}, 0
    for _, mv in ipairs(rows) do
        local cands = EQUIP_SLOT_IDX[tostring(mv.slot or '')]
        if cands then
            for _, idx in ipairs(cands) do
                if not used[idx] then
                    local okI, id = pcall(function()
                        local inv = mq.TLO.Me.Inventory(idx)
                        if inv() == nil then return 0 end
                        return tonumber(tostring(inv.ID() or 0)) or 0
                    end)
                    if not okI then
                        log('  (worn slot %d read failed while reserving)', idx)
                    elseif id > 0 and wanted[id] then
                        used[idx] = true
                        n = n + 1
                    end
                end
            end
        end
    end
    return used, n
end

local function armQueue(kind, items)
    if State.estopped then
        log('%s: NOT armed - e-stopped. /mailgear resume first.', kind)
        return
    end
    if #items == 0 then log('%s: nothing to do on %s.', kind, Me); return end
    local used, reserved = {}, 0
    if kind == 'equip' then used, reserved = reservedWornSlots() end
    State.queue = { kind = kind, items = items, i = 1, done = 0, skipped = 0, used = used }
    log('%s LIVE: %d item(s) armed%s. (/mailgear stop aborts.)', kind, #items,
        reserved > 0 and string.format(', %d worn slot(s) already correct and protected', reserved) or '')
end

-- ==========================================================================
-- commands
-- ==========================================================================
local HELP = {
    'MailGear v' .. VERSION .. '  -  /mailgear <verb>',
    '  plans            list the plans in config/' .. PLAN_FILES[1],
    '  useplan <n>      make plan #n active',
    '  status           what is loaded, live/dry-run, queue progress',
    '  dequip           move THIS toon\'s planned pieces from worn -> bags',
    '  getbank          at a BANKER (bank window open): pull planned pieces -> bags',
    '  equip            on the RECEIVING toon: equip the pieces meant for it',
    '  hoard            list pieces needing a MANUAL pull (Hoard / persona)',
    '  live [on|off]    arm or disarm real movement (default OFF = dry-run)',
    '  stop             EMERGENCY STOP - clears any running queue',
    '  resume           clear the stop so queues can run again',
    '  reload           re-read the plan file from disk',
    'Stop the script with: /lua stop mailgear',
}

local function cmdPlans()
    State.plans = loadPlans()
    if not State.plans then return end
    log('gear plans loaded: %d (active #%d)', #State.plans, State.planIdx or 1)
    for i, p in ipairs(State.plans) do
        local mine, to, here = 0, 0, 0
        for _, mv in ipairs(p.rows or p.moves or {}) do
            local st = statusOf(mv)
            if tostring(mv.from or '') ~= '' and tostring(mv.from or '') == Me then mine = mine + 1 end
            if tostring(mv.to or '') == Me then
                -- Separate "somebody owes you this" from "it is already on you and
                -- just needs putting on" - they are different jobs on your side.
                if TRANSFER_STATUS[st] then to = to + 1
                elseif SELF_STATUS[st] then here = here + 1 end
            end
        end
        log('  %s#%d %s -> %s  (%d move(s)%s%s%s)', (i == (State.planIdx or 1)) and '*' or ' ',
            i, tostring(p.name or 'plan'), tostring(p.target or '?'), #(p.moves or {}),
            mine > 0 and string.format(', %d from you', mine) or '',
            to   > 0 and string.format(', %d TO you',  to)   or '',
            here > 0 and string.format(', %d already on you', here) or '')
    end
end

local function cmdStatus()
    log('v%s on %s | %s | plans: %s | %s', VERSION, Me,
        State.live and 'LIVE - gear will move' or 'DRY-RUN',
        State.plans and tostring(#State.plans) or 'none loaded',
        State.estopped and 'E-STOPPED' or 'ready')
    if State.queue then
        log('  queue: %s %d/%d (%d done, %d skipped)', State.queue.kind,
            State.queue.i, #State.queue.items, State.queue.done, State.queue.skipped)
    end
end

local function cmdHoard()
    local h = manualPulls()
    if #h == 0 then log('hoard: none of %s\'s plan pieces are in the Hoard or a persona closet.', Me); State.hoardList = nil; return end
    State.hoardList = h
    State.hoardWin  = true
    log('MANUAL PULLS - retrieve these %d by hand (see the Manual Pulls window):', #h)
    for _, x in ipairs(h) do
        log('  %s  [%s]%s', tostring(x.name), tostring(x.loc or x.why),
            x.to and string.format(' -> %s', tostring(x.to)) or '')
    end
    log('  Hoard: open it, search the name, Retrieve. Persona: switch persona, then dequip.')
end

-- Dry-run preview shared by the three movement verbs.
local function preview(kind, items, verb)
    log('%s DRY-RUN: %d item(s) on %s. /mailgear live on to arm.', kind, #items, Me)
    for _, it in ipairs(items) do
        log('  WOULD %s %s%s', verb, tostring(it.name),
            it.slot and it.slot ~= '' and string.format(' (%s)', tostring(it.slot)) or '')
    end
end

local function cmdDequip()
    local items = {}
    for _, mv in ipairs(mineFrom()) do
        -- Only pieces actually WORN need dequipping. doDequipItem re-checks the live
        -- ItemSlot anyway and no-ops if it is already in bags, so a stale dump costs
        -- nothing here.
        if bucketOf(mv) == 'equipped' then table.insert(items, mv) end
    end
    if #items == 0 then log('dequip: %s has no WORN pieces in the plan.', Me); return end
    if not State.live then return preview('dequip', items, 'dequip') end
    armQueue('dequip', items)
end

local function cmdGetBank()
    local items = {}
    for _, mv in ipairs(mineFrom()) do
        local b = bucketOf(mv)
        if b == 'bank' or b == 'shared' then table.insert(items, mv) end
    end
    -- ...plus this toon's own banked pieces (status have/grab). Same pull, but the
    -- toon is both holder and target, so mineFrom() cannot see them.
    for _, mv in ipairs(mineOwnBank()) do table.insert(items, mv) end
    if #items == 0 then log('getbank: %s has no plan pieces in the bank.', Me); return end
    if not State.live then return preview('getbank', items, 'pull from bank') end
    if not mq.TLO.Window('BigBankWnd').Open() then
        log('getbank: open the bank window first (talk to a banker), then re-run.')
        return
    end
    armQueue('bank', items)
end

local function cmdEquip()
    local items = mineTo()
    if #items == 0 then
        -- Distinguish "no rows for you" from "your rows are all somewhere this verb
        -- cannot reach", which is what sends people looking for a bug that isn't one.
        local banked, manual = #mineOwnBank(), #manualPulls()
        if banked > 0 or manual > 0 then
            log('equip: nothing to put on yet - %d piece(s) are still in the bank (/mailgear getbank at a banker)%s.',
                banked, manual > 0 and string.format(' and %d need a manual pull (/mailgear hoard)', manual) or '')
        else
            log('equip: nothing in the plan is destined for %s.', Me)
        end
        return
    end
    if not State.live then return preview('equip', items, 'equip') end
    armQueue('equip', items)
end

local function cmdLive(a)
    if a == 'on' then State.live = true
    elseif a == 'off' then State.live = false
    else State.live = not State.live end
    log('LIVE mode: %s', State.live and 'ON - real gear will move (/mailgear stop aborts)' or 'OFF (dry-run)')
end

local function cmdStop()
    State.estopped = true
    if State.queue then
        log('STOP: %s queue aborted (%d item(s) not processed).',
            State.queue.kind, math.max(0, #State.queue.items - State.queue.i + 1))
        State.queue = nil
    else
        log('STOP: nothing was running. Queues are now blocked until /mailgear resume.')
    end
    if cursorId() ~= 0 then log('  NOTE: something is on your cursor - put it away manually.') end
end

local function cmdBind(...)
    local a = { ... }
    local sub = string.lower(tostring(a[1] or ''))
    if sub == '' or sub == 'help' then for _, l in ipairs(HELP) do log('%s', l) end; return end
    if sub == 'plans'   then return cmdPlans()  end
    if sub == 'status'  then return cmdStatus() end
    if sub == 'hoard'   then return cmdHoard()  end
    if sub == 'dequip'  then return cmdDequip() end
    if sub == 'getbank' then return cmdGetBank() end
    if sub == 'equip'   then return cmdEquip()  end
    if sub == 'live'    then return cmdLive(string.lower(tostring(a[2] or ''))) end
    if sub == 'stop'    then return cmdStop()   end
    if sub == 'resume'  then State.estopped = false; log('resumed - queues may run again.'); return end
    if sub == 'reload'  then State.plans = nil; return cmdPlans() end
    if sub == 'useplan' then
        local n = tonumber(a[2] or '')
        if not State.plans then State.plans = loadPlans() end
        if not State.plans then return end
        if not n or not State.plans[n] then
            log('useplan: pick 1..%d', #State.plans); return
        end
        State.planIdx = n
        local p = State.plans[n]
        log('active plan #%d: %s -> %s (%d move(s))', n, tostring(p.name or 'plan'),
            tostring(p.target or '?'), #(p.moves or {}))
        return
    end
    log('unknown verb "%s" - /mailgear help', tostring(a[1]))
end

-- ==========================================================================
-- Manual Pulls window (the Hoard checklist)
-- ==========================================================================
-- The Hoard cannot be automated, so the whole value here is a list you can read
-- while typing names into the Hoard search box - which is exactly what scrolls
-- away in chat.
local function drawHoardWindow()
    if not State.hoardWin then return end
    local open, show = ImGui.Begin('MailGear Manual Pulls##' .. Me, true)
    State.hoardWin = open
    if show then
        ImGui.TextWrapped('These pieces CANNOT be moved by MacroQuest. The Hoard has no ' ..
                          'addressable slots, and persona gear needs a persona switch.')
        ImGui.Separator()
        if State.hoardList and #State.hoardList > 0 then
            for _, x in ipairs(State.hoardList) do
                ImGui.Bullet()
                ImGui.TextWrapped(string.format('%s   [%s]%s', tostring(x.name),
                    tostring(x.loc or x.why), x.to and (' -> ' .. tostring(x.to)) or ''))
            end
            ImGui.Separator()
            ImGui.TextWrapped('Hoard: open it, search the name, Retrieve. Persona: switch ' ..
                              'persona, then /mailgear dequip.')
        else
            ImGui.Text('Nothing pending. Run /mailgear hoard.')
        end
    end
    ImGui.End()
end

-- ==========================================================================
-- wiring
-- ==========================================================================
mq.bind('/mailgear', cmdBind)
mq.imgui.init('MailGearPulls', drawHoardWindow)

log('MailGear v%s loaded on %s. /mailgear for help. DRY-RUN until /mailgear live on.', VERSION, Me)
if mq.TLO.Me.CleanName() then cmdPlans() end

-- ==========================================================================
-- main loop
-- ==========================================================================
-- The queue is ticked ONE item per pass HERE, never from the bind handler.
-- That is what makes /mailgear stop authoritative: it clears State.queue between
-- passes. The helpers do the mq.delay waits, which is legal here (main loop)
-- and illegal inside pcall or a bind callback.
while true do
    mq.doevents()

    if State.queue and State.estopped then
        State.queue = nil                                 -- belt and braces
    elseif State.queue then
        local q = State.queue
        if q.i > #q.items then
            log('%s done: %d moved, %d skipped, of %d.', q.kind, q.done, q.skipped, #q.items)
            if q.kind == 'dequip' then
                local h = manualPulls()
                if #h > 0 then
                    State.hoardList, State.hoardWin = h, true
                    log('  NOTE: %d piece(s) still need a MANUAL pull (Hoard / persona) - see the window.', #h)
                end
                log('  Next: parcel or trade them over, then /mailgear equip on the receiving toon.')
            end
            State.queue = nil
        else
            local it = q.items[q.i]
            local advance = true
            if q.kind == 'dequip' then
                local ok, fatal = doDequipItem(it)
                if ok then q.done = q.done + 1 else q.skipped = q.skipped + 1 end
                if fatal then
                    log('dequip STOPPED: no empty bag slots at all - free some and re-run.')
                    State.queue = nil
                    advance = false
                end
            elseif q.kind == 'bank' then
                if doPullFromBank(it) then q.done = q.done + 1 else q.skipped = q.skipped + 1 end
            elseif q.kind == 'equip' then
                local r = doEquipItem(it, q.used)
                -- 'reserved' = the slot already holds a piece this set wants. That is
                -- a correct outcome, not a failure, so it must not read as skipped.
                if r == 'equipped' or r == 'worn' or r == 'reserved' then q.done = q.done + 1
                else q.skipped = q.skipped + 1 end
            end
            if advance and State.queue then q.i = q.i + 1 end
        end
    end

    mq.delay(100)
end
