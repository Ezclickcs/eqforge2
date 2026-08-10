--[[ mychars_bankrun.lua - walk to the nearest banker, open bank + Dragon's Hoard,
then /outputfile inventory so the snapshot includes bags + bank + hoard.

Crew:  /dgae /lua run mychars_bankrun     (then run it on your driver too)
Abort: /dgae /lua stop mychars_bankrun    (and TrixBox E-STOP's /nav stop halts movement)

Why: /outputfile inventory only includes bank reliably at a banker, and Dragon's
Hoard ONLY while the DragonHoardWnd is open. This gets every toon to that state.

Safety per TrixBox rules: single main loop, bare mq.delay (never inside pcall),
hard 90s timeout, no combat, exits when done. Same-zone only - if there is no
banker in the zone it reports and exits (park the crew in town first).
]]

local mq = require('mq')

local TIMEOUT = 90          -- seconds, whole run
local OPEN_RANGE = 20       -- banker right-click range

local function log(msg, ...) printf('\at[bankrun] ' .. msg .. '\ax', ...) end
local function fail(msg, ...)
    printf('\ar[bankrun] ' .. msg .. '\ax', ...)
end

local function tloStr(v)
    local ok, s = pcall(function() return tostring(v()) end)
    if not ok or s == nil or s == 'NULL' then return '' end
    return s
end

if tloStr(mq.TLO.Me.Name) == '' then fail('Not in game.') return end
local deadline = os.time() + TIMEOUT

-- 1. nearest banker in zone
local banker = mq.TLO.NearestSpawn('banker')
if not banker() or not banker.ID() or banker.ID() == 0 then
    fail('No banker in this zone - park the crew in town and rerun.')
    return
end
log('Banker: %s (%.0f away)', tloStr(banker.CleanName), banker.Distance3D() or 999)

-- 2. get in range (nav if we have a mesh and are far)
if (banker.Distance3D() or 999) > OPEN_RANGE then
    if mq.TLO.Navigation.MeshLoaded() then
        mq.cmdf('/nav id %d distance=%d', banker.ID(), OPEN_RANGE - 5)
        while os.time() < deadline and mq.TLO.Navigation.Active()
              and (banker.Distance3D() or 999) > OPEN_RANGE do
            mq.delay(250)
        end
        mq.cmd('/nav stop')
    else
        fail('No navmesh for this zone - move me near the banker and rerun.')
        return
    end
end
if (banker.Distance3D() or 999) > OPEN_RANGE + 10 then
    fail('Could not reach the banker (%.0f away).', banker.Distance3D() or 999)
    return
end

-- 3. open the bank
mq.cmdf('/target id %d', banker.ID())
mq.delay(300)
mq.cmd('/click right target')
while os.time() < deadline and not mq.TLO.Window('BigBankWnd').Open() do
    mq.delay(250)
end
if not mq.TLO.Window('BigBankWnd').Open() then
    fail('Bank window never opened.')
    return
end
log('Bank open.')

-- 4. open Dragon's Hoard (button name varies by client build - try known names,
--    confirm via the DragonHoardWnd actually opening)
-- confirmed from uifiles/default/EQUI_BigBankWnd.xml: ScreenID is BNK_DragonHoard
local DH_BUTTONS = { 'BNK_DragonHoard' }
if not mq.TLO.Window('DragonHoardWnd').Open() then
    for _, btn in ipairs(DH_BUTTONS) do
        mq.cmdf('/notify BigBankWnd %s leftmouseup', btn)
        mq.delay(600)
        if mq.TLO.Window('DragonHoardWnd').Open() then break end
    end
end
if mq.TLO.Window('DragonHoardWnd').Open() then
    log("Dragon's Hoard open.")
    mq.delay(500)     -- let contents populate before dumping
else
    fail("Couldn't click the Dragon's Hoard button - open it by hand within 10s and the dump will still include it.")
    mq.delay(10000)
end

-- 5. snapshot (includes bank + hoard because the windows are open)
--
-- GUARDED. /outputfile rewrites the whole file and Hoard rows only export while the
-- window is OPEN, so if the hoard failed to open above (the 10s hand-open expired, or
-- this toon has none) a bare dump would replace a good hoard-bearing dump with an
-- empty one - the exact loss this script exists to prevent. Read the old rows into
-- memory FIRST; splice them back if the fresh dump came up empty.
-- Mirrors the guards in extras/eqforge/init.lua and trixbox's dumpInvGuarded().
local function isWindowOnlyLoc(loc)
    return loc:sub(1, 5) == 'Hoard' or loc:sub(1, 6) == 'Dragon'
        or loc:sub(1, 5) == 'Depot' or loc:sub(1, 8) == 'Personal'
end

local dumpPath
do
    local eqdir, srv, nm = '', '', ''
    pcall(function() eqdir = tostring(mq.TLO.EverQuest.Path() or '') end)
    pcall(function() srv   = tostring(mq.TLO.EverQuest.Server() or '') end)
    pcall(function() nm    = tostring(mq.TLO.Me.Name() or '') end)
    if eqdir ~= '' and nm ~= '' then
        dumpPath = string.format('%s/%s_%s-Inventory.txt', eqdir, nm, srv:lower())
    end
end

local function readWindowOnly(path)
    local lines, n = {}, 0
    local f = path and io.open(path, 'r')
    if not f then return lines, 0 end
    for line in f:lines() do
        local loc, name = line:match('^([^\t]+)\t([^\t]*)')
        if loc and isWindowOnlyLoc(loc) then
            table.insert(lines, line)
            if name ~= 'Empty' and name ~= '' then n = n + 1 end
        end
    end
    f:close()
    return lines, n
end

local prevRows, prevN = readWindowOnly(dumpPath)

mq.cmd('/outputfile inventory')
mq.delay(500)

local _, nowN = readWindowOnly(dumpPath)
if prevN > 0 and nowN == 0 and dumpPath then
    local f = io.open(dumpPath, 'r')
    local kept = {}
    if f then
        for line in f:lines() do
            local loc = line:match('^([^\t]+)')
            if not (loc and isWindowOnlyLoc(loc)) then table.insert(kept, line) end
        end
        f:close()
        for _, l in ipairs(prevRows) do table.insert(kept, l) end
        local out = io.open(dumpPath, 'w')
        if out then
            out:write(table.concat(kept, '\n'), '\n')
            out:close()
            nowN = prevN
            fail(("Hoard didn't open - KEPT the %d Hoard row(s) from the previous dump. "
                  .. 'Bank/bags are fresh; hoard rows are from the last visit.'):format(prevN))
        end
    end
elseif nowN > 0 and dumpPath then
    local st = io.open((dumpPath:gsub('%-Inventory%.txt$', '-Inventory.hoardasof')), 'w')
    if st then st:write(tostring(os.time()), '\n'); st:close() end
end

log('Inventory dumped WITH bank%s. Refresh the Gear tab.',
    nowN > 0 and " + Dragon's Hoard" or '')
