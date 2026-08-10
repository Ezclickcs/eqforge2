--[[ harvest_step.lua - in-game half of the unattended inventory harvest.

Hooked from ingame.cfg, which fires for EVERY character entering the world - so it
exits instantly and silently unless this account has an armed queue AND this toon is
the one on top of it. That double check is what stops it hijacking a normal login.

  ARMING:   <MQ config>/harvest_<LoginName>.queue   (EQ Forge -> Harvest tab)
  DISARM:   delete that file. Checked between every step, so it aborts mid-run too.

One toon's leg:
  wait for the world -> [banker + Dragon's Hoard, if this toon is flagged] ->
  /outputfile inventory -> VERIFY the dump is complete -> pop the queue -> /camp

Then charselect.cfg runs harvest_next.lua, which switches to the next name.

TrixBox rules honoured: bare mq.delay in the main flow (NEVER inside pcall, 2026-07-16),
no /timed for anything that must respect a stop, the script never stops itself, and a
hard overall timeout so one stuck toon can't hang the whole rotation.
]]

local mq = require('mq')

local SETTLE       = 5      -- seconds to let inventory populate after entering world
local WORLD_WAIT   = 60     -- max seconds to wait for the world to finish loading
local BUDGET       = 180    -- max seconds for this toon, then give up and camp
local OPEN_RANGE   = 20     -- banker right-click range (from mychars_bankrun.lua)
local MAX_WALK     = 300    -- only visit a banker that is already close; never a trek
local HOARD_POPULATE = 1500 -- ms to let Dragon's Hoard contents stream in before dumping
local DUMP_TRIES   = 3      -- re-dump attempts if the file comes back short

-- Where /outputfile writes. EverQuest.Path is read defensively because a wrong TLO
-- member throws; the fallback is a common default install path.
local EQ_DIR_FALLBACK = [[C:\Users\Public\Daybreak Game Company\Installed Games\EverQuest]]

-- A complete dump always lists 24 top-level Bank slots and 8 SharedBank slots, "Empty"
-- rows included (verified across 36 real dumps, 2026-08-04). Fewer than that means the
-- dump fired before the server finished sending inventory - the one failure mode that
-- silently produces a plausible-looking but wrong file.
local BANK_SLOTS_EXPECTED   = 24
local SHARED_SLOTS_EXPECTED = 8

local function log(msg, ...) printf('\at[harvest] ' .. msg .. '\ax', ...) end
local function warn(msg, ...) printf('\ar[harvest] ' .. msg .. '\ax', ...) end

local function tloStr(v, label)
    local ok, s = pcall(function() return tostring(v()) end)
    if not ok or s == nil or s == 'NULL' then
        if label then warn('TLO read failed: %s', label) end
        return ''
    end
    return s
end

local CONFIG = mq.configDir
local account = tloStr(mq.TLO.EverQuest.LoginName)
if account == '' then return end

local qpath = CONFIG .. '/harvest_' .. account .. '.queue'
local lpath = CONFIG .. '/harvest_' .. account .. '.log'

local function readQueue()
    local f = io.open(qpath, 'r')
    if not f then return nil end
    local lines = {}
    for line in f:lines() do lines[#lines + 1] = line end
    f:close()
    return lines
end

local function armed() return io.open(qpath, 'r') ~= nil end

local function appendLog(line)
    local f = io.open(lpath, 'a')
    if f then f:write(line, '\n') f:close() end
end

local function isEntry(t)
    return t ~= '' and t:sub(1, 1) ~= '#' and not t:lower():match('^account=')
end

-- NOT ARMED -> normal login, do nothing at all.
local lines = readQueue()
if not lines then return end

-- Which toon does the queue want, and does it need the bank?
local wantName, wantHoard, entryIdx
for i, line in ipairs(lines) do
    local t = line:match('^%s*(.-)%s*$')
    if isEntry(t) then
        wantName = (t:match('^([^|]+)') or ''):match('^%s*(.-)%s*$')
        wantHoard = (t:lower():match('|%s*hoard') ~= nil)
        entryIdx = i
        break
    end
end
if not wantName or wantName == '' then return end

local me = tloStr(mq.TLO.Me.Name)
if me == '' then
    -- world not ready yet; wait briefly for a name before deciding it isn't us
    local until_ = os.time() + WORLD_WAIT
    while os.time() < until_ and me == '' do
        mq.delay(500)
        me = tloStr(mq.TLO.Me.Name)
    end
end
if me:lower() ~= wantName:lower() then
    -- Someone logged in by hand during an armed run. Leave them completely alone.
    log('%s is not the queued toon (%s) - harvest stays out of the way.', me, wantName)
    return
end

local deadline = os.time() + BUDGET
local function expired() return os.time() >= deadline end

--- pop this toon off the queue, whatever the outcome (never retry-loop forever)
local function popQueue()
    local out = {}
    for i, line in ipairs(lines) do
        if i ~= entryIdx then out[#out + 1] = line end
    end
    local f = io.open(qpath, 'w')
    if not f then return false end
    f:write(table.concat(out, '\n'))
    if #out > 0 then f:write('\n') end
    f:close()
    return true
end

local function finish(result, note)
    popQueue()
    appendLog(string.format('%s|%s|%d|%s|%s',
        result == 'error' and 'error' or 'done', me, os.time(), result, note or ''))
    -- close anything that would block /camp, then leave (TrixBox closeBankIfOpen)
    local w = mq.TLO.Window('BigBankWnd')
    if w.Open() then w.DoClose() mq.delay(400) end
    mq.cmd('/squelch /rgl pause')
    mq.cmd('/squelch /nav stop')
    log('%s: %s. Camping.', me, note or result)
    mq.cmd('/camp')
end

-- 1. let the world settle. rgmercs gets paused first so it can't start a fight or
--    wander off mid-harvest (its own cfg may have just launched it).
mq.cmd('/squelch /rgl pause')
local settleUntil = os.time() + SETTLE
while os.time() < settleUntil do
    if not armed() then log('Disarmed mid-run - stopping.') return end
    mq.delay(500)
end

if tloStr(mq.TLO.Me.CombatState) == 'COMBAT' then
    finish('error', 'in combat on login - skipped, re-queue this toon')
    return
end

-- 1b. Roster + DZ lockouts, harvested here rather than with the inventory dump on
--     purpose: they cost nothing (each script writes one file and exits), they do not
--     depend on the bank leg, and putting them BEFORE the dump means a toon whose dump
--     later fails - short inventory, missing hoard window, combat - still contributes
--     its level/class/membership and its raid lockouts. Both write ONE FILE PER TOON
--     (mychars_export_<Name>.csv / mychars_lockouts_<Name>.txt) so the whole rotation
--     can run them without the 2026-07-25 shared-file write race. EQ Forge merges them.
mq.cmd('/lua run mychars_export')
mq.cmd('/lua run mychars_lockouts')
-- Achievements go here too, NOT with the inventory dump below: they are the authoritative
-- record of keys/flags (Sleeper's Key, tomb/zone access) and do not depend on the bank leg,
-- so a toon whose inventory dump later fails still contributes them. Verified 2026-08-07
-- across three toons that the file is fully populated at export time (22.8-22.9k lines,
-- statuses differentiating correctly), so it needs no settle delay like the Hoard does.
mq.cmd('/outputfile achievements')
log('roster + lockout + achievement export queued for %s', me)

-- 2. optional bank leg. Bank and SharedBank arrive in the login packet and need NO
--    banker; only the Dragon's Hoard (and the Tradeskill Depot) need their window
--    open. So this runs ONLY for toons flagged `hoard`, and only if a banker is
--    already in this zone - never a cross-zone trip (Velious era has no PoK).
local hoardOk = false
local banker = mq.TLO.NearestSpawn('banker')
local bdist = nil
if banker() and banker.ID() and banker.ID() ~= 0 then
    bdist = banker.Distance3D() or 9999
end

if bdist == nil then
    log('No banker in %s - dumping where I stand (bank + shared still included).',
        tloStr(mq.TLO.Zone.ShortName))
elseif bdist > MAX_WALK then
    -- "if a bank is close by" - never send a toon on a cross-zone trek it did not
    -- ask for. Parked-near-a-bank is the intended case; anything else dumps in place.
    log('Nearest banker is %.0f away (limit %d) - dumping where I stand.', bdist, MAX_WALK)
else
    log('Banker %s at %.0f - walking over.', tloStr(banker.CleanName), bdist)
    if bdist > OPEN_RANGE then
        if mq.TLO.Navigation.MeshLoaded() then
            mq.cmdf('/nav id %d distance=%d', banker.ID(), OPEN_RANGE - 5)
            while not expired() and armed() and mq.TLO.Navigation.Active()
                  and (banker.Distance3D() or 9999) > OPEN_RANGE do
                mq.delay(250)
            end
            mq.cmd('/nav stop')
        else
            warn('No navmesh in this zone - cannot reach the banker.')
        end
    end
    if (banker.Distance3D() or 9999) <= OPEN_RANGE + 10 then
        mq.cmdf('/target id %d', banker.ID())
        mq.delay(300)
        mq.cmd('/click right target')
        while not expired() and not mq.TLO.Window('BigBankWnd').Open() do
            mq.delay(250)
        end
        if mq.TLO.Window('BigBankWnd').Open() then
            -- BNK_DragonHoard confirmed from uifiles/default/EQUI_BigBankWnd.xml.
            -- Harmless on a toon with no hoard: the button click just does nothing.
            if not mq.TLO.Window('DragonHoardWnd').Open() then
                mq.cmd('/notify BigBankWnd BNK_DragonHoard leftmouseup')
                mq.delay(800)
            end
            if mq.TLO.Window('DragonHoardWnd').Open() then
                hoardOk = true
                -- Hoard contents STREAM in from the server after the window opens.
                -- Dumping too early captures a partial hoard that looks complete, so
                -- this wait is deliberately generous.
                mq.delay(HOARD_POPULATE)
                log("Dragon's Hoard open.")
            elseif wantHoard then
                warn("Couldn't open the Dragon's Hoard.")
            end
        end
    else
        warn('Could not reach the banker (%.0f away) - dumping where I stand.',
             banker.Distance3D() or 9999)
    end
end

if not armed() then log('Disarmed mid-run - stopping.') return end

-- 2b. REFUSE to dump a hoard toon whose hoard we could not open.
-- /outputfile rewrites the whole file, so dumping now would REPLACE a good dump that
-- had hoard rows with one that has none - silently destroying data to "refresh" it.
-- Skipping leaves the existing dump intact and the report says why.
if wantHoard and not hoardOk then
    finish('error', 'no Dragon\'s Hoard window - dump SKIPPED so the existing one '
                    .. 'is not overwritten; park this toon at a banker and re-queue')
    return
end

-- 3. dump, then PROVE the file is complete rather than assuming it
local eqdir = tloStr(mq.TLO.EverQuest.Path)
if eqdir == '' then eqdir = EQ_DIR_FALLBACK end
local dumpPath = string.format('%s/%s_%s-Inventory.txt', eqdir, me,
                               tloStr(mq.TLO.EverQuest.Server):lower())

-- Counts what actually landed in the FILE. The Dragon's Hoard window opens on any
-- toon, empty or not, so "the window opened" is NOT evidence the dump caught anything -
-- only the file is. Hoard items are counted here so the log reports the real number.
local function dumpIsComplete()
    local f = io.open(dumpPath, 'r')
    if not f then return false, 'dump file not found at ' .. dumpPath, 0 end
    local bank, shared, hoard = 0, 0, 0
    for line in f:lines() do
        local loc, name = line:match('^([^\t]+)\t([^\t]*)')
        if loc then
            if loc:match('^Bank%d+$') then bank = bank + 1
            elseif loc:match('^SharedBank%d+$') then shared = shared + 1
            elseif loc:match('Hoard') and name ~= 'Empty' and name ~= '' then
                hoard = hoard + 1
            end
        end
    end
    f:close()
    if bank < BANK_SLOTS_EXPECTED or shared < SHARED_SLOTS_EXPECTED then
        return false, string.format('short dump (%d/%d bank, %d/%d shared)',
                                    bank, BANK_SLOTS_EXPECTED, shared,
                                    SHARED_SLOTS_EXPECTED), hoard
    end
    return true, string.format('%d bank / %d shared slots', bank, shared), hoard
end

local ok, detail, hoardItems
for attempt = 1, DUMP_TRIES do
    mq.cmd('/outputfile inventory')
    mq.delay(1200)
    ok, detail, hoardItems = dumpIsComplete()
    if ok then break end
    warn('Attempt %d: %s - waiting for inventory to finish loading.', attempt, detail)
    if expired() or not armed() then break end
    mq.delay(4000)
end

if not ok then
    finish('error', detail or 'dump never completed')
elseif wantHoard and (hoardItems or 0) == 0 then
    -- Known hoard owner, hoard window was open, yet nothing landed: the contents
    -- almost certainly had not finished streaming. Loud, because this OVERWROTE a
    -- dump that used to have them.
    finish('error', 'hoard owner but 0 hoard items captured - re-run this toon')
else
    finish('dumped', (hoardItems or 0) > 0
        and string.format('complete, %d hoard items', hoardItems)
        or 'complete (no hoard)')
end
