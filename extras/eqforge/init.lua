--[[ EQ Forge addon for MacroQuest  -  lua/eqforge/init.lua

One folder, one install, everything EQ Forge needs out of the game:

    roster    name / server / class / level / race / membership   (My Characters)
    lockouts  DynamicZone expedition timers                       (Keys & Access)
    dump      /outputfile inventory, verified complete            (everything else)
    beacon    tells the EQ Forge server where MQ and EQ live      (Setup, automatic)

Install:
    <MacroQuest>\lua\eqforge\init.lua        <- this file, keep the folder name

Run resident (recommended - this is what makes the auto-exports happen):
    /lua run eqforge

Run one thing and exit (no resident script needed):
    /lua run eqforge roster
    /lua run eqforge lockouts
    /lua run eqforge dump
    /lua run eqforge all

Once resident, everything is driven by /eqf (see /eqf help).

------------------------------------------------------------------------------
WHY IT WORKS THE WAY IT DOES  (these are scars, not preferences)

* Bind and event handlers NEVER do the work - they set a flag and the main loop
  does it. A handler runs inside MQ's callback, and mq.delay() cannot yield
  across that boundary; delaying there kills the script silently.
* mq.delay() is never called inside pcall() for the same reason. pcall is used
  only around TLO reads, which never delay.
* A pcall'd TLO read that fails LOGS (once per member). A silently swallowed
  throw turns "feature never fires" into an unfindable bug.
* The script never stops itself. /eqf stop queues an EXTERNAL
  `/timed 5 /lua stop eqforge`, which is identical to you typing it - the one
  form that has never crashed MQ during teardown.
* Every export writes ONE FILE PER CHARACTER. Six clients told to export at once
  used to race each other in a shared file and silently drop rows. The EQ Forge
  server globs and merges them.
* A dump is VERIFIED, not assumed: a complete /outputfile always lists 24
  top-level Bank slots and 8 SharedBank slots (Empty rows included). Fewer means
  the dump fired before the server finished sending inventory - a file that looks
  perfectly plausible and is wrong.

NOTE: MacroQuest is third-party automation and against EverQuest's Terms of
Service. Running it risks your account. Your call - this addon assumes you have
already made it.
------------------------------------------------------------------------------
]]

local mq = require('mq')

local VERSION = '1.4.0'   -- BUMP THIS on every functional change. MQ loads a Lua
                          -- script ONCE at start, so a client that was already
                          -- running keeps the old code after you copy a new file
                          -- in. Without a version you can read, that is invisible:
                          -- /eqf bank returned "unknown command" on a client whose
                          -- addon predated it, with no clue why.
local SCRIPT  = 'eqforge'               -- folder name, used for the external stop

-- Verified across 36 real dumps: a complete dump always has these, "Empty" included.
local BANK_SLOTS_EXPECTED   = 24
local SHARED_SLOTS_EXPECTED = 8

local DUMP_TRIES         = 3      -- re-dump attempts when the file comes back short
local DUMP_SETTLE_MS     = 1200   -- let /outputfile finish writing before reading it
local DUMP_RETRY_MS      = 4000   -- inventory still streaming; give it a beat
local CAMP_BUDGET_S      = 20     -- camp gives ~30s; never spend all of it
local LOGIN_DELAY_S      = 8      -- let the world settle before the login export
local TRIGGER_COOLDOWN_S = 15     -- one trigger can't fire twice in a row
local LOOP_MS            = 250

local SETTINGS_FILE = 'eqforge_addon.lua'    -- in <MQ config>
local BEACON_FILE   = 'eqforge_paths.json'   -- in %LOCALAPPDATA% (and <MQ config>)

-- ---------------------------------------------------------------------------
-- output
-- ---------------------------------------------------------------------------
local settings                               -- forward decl (quiet gates say())

local function say(msg, ...)
    if settings and settings.quiet then return end
    printf('\ag[eqforge]\ax ' .. msg, ...)
end

local function warn(msg, ...)
    printf('\ar[eqforge]\ax ' .. msg, ...)
end

local function info(msg, ...)                -- never suppressed: /eqf status etc.
    printf('\at[eqforge]\ax ' .. msg, ...)
end

-- ---------------------------------------------------------------------------
-- TLO reads
--
-- A misspelled TLO member THROWS on index, and an unarmoured throw takes the
-- whole script down. pcall is safe here precisely because nothing inside delays.
-- The warn-once table exists because a broken read at 4Hz would otherwise either
-- spam the log or (if silenced) hide the failure completely.
-- ---------------------------------------------------------------------------
local warned = {}

local function tloStr(v, label)
    local ok, s = pcall(function() return tostring(v()) end)
    if not ok or s == nil or s == 'NULL' then
        if label and not warned[label] then
            warned[label] = true
            warn('could not read %s - that feature will not work.', label)
        end
        return ''
    end
    return s
end

--- Me.Name is empty at character select and populated in the world. That single
--- read is the whole gamestate test - no second TLO to spell wrong.
local function inWorld() return tloStr(mq.TLO.Me.Name, 'Me.Name') ~= '' end

-- ---------------------------------------------------------------------------
-- settings  (<MQ config>/eqforge_addon.lua - a plain Lua table we load and rewrite)
--
-- Kept as Lua rather than ini/json so EQ Forge's Setup page can write the same
-- file with no parser on either side, and so a hand-edit is obvious.
-- ---------------------------------------------------------------------------
local DEFAULTS = {
    camp      = true,   -- export everything when you /camp   <- the headline feature
    login     = true,   -- export roster + lockouts after entering the world
    loginDump = false,  -- ALSO dump inventory on login (off: login is the noisy moment)
    zone      = false,  -- export roster + lockouts on every zone
    every     = 0,      -- minutes between automatic full exports (0 = off)
    quiet     = false,  -- suppress the routine chatter
}

local FLAG_KEYS = { 'camp', 'login', 'loginDump', 'zone', 'quiet' }
local FLAG_ALIAS = { camp = 'camp', login = 'login', logindump = 'loginDump',
                     zone = 'zone', quiet = 'quiet' }

local function settingsPath() return mq.configDir .. '/' .. SETTINGS_FILE end

local function loadSettings()
    local s = {}
    for k, v in pairs(DEFAULTS) do s[k] = v end
    local ok, chunk = pcall(loadfile, settingsPath())
    if ok and chunk then
        local ok2, t = pcall(chunk)
        if ok2 and type(t) == 'table' then
            for k, v in pairs(t) do
                if DEFAULTS[k] ~= nil and type(v) == type(DEFAULTS[k]) then s[k] = v end
            end
        else
            warn('%s is not readable - using defaults.', SETTINGS_FILE)
        end
    end
    return s
end

local function saveSettings()
    local f = io.open(settingsPath(), 'w')
    if not f then
        warn('cannot write %s', settingsPath())
        return false
    end
    f:write('-- EQ Forge addon settings. Written by /eqf and by EQ Forge -> Setup.\n')
    f:write('-- Edit by hand if you like; unknown keys are ignored.\n')
    f:write('return {\n')
    for _, k in ipairs(FLAG_KEYS) do
        f:write(string.format('    %-10s = %s,\n', k, tostring(settings[k])))
    end
    f:write(string.format('    %-10s = %d,\n', 'every', math.floor(settings.every or 0)))
    f:write('}\n')
    f:close()
    return true
end

-- ---------------------------------------------------------------------------
-- beacon  -  how EQ Forge finds MacroQuest without being told
--
-- Every MQ install lives somewhere different (redfetch, VanillaMQ, a hand build),
-- so the server used to need EQFORGE_MQ_CONFIG set by hand and silently found
-- nothing when it wasn't. The addon knows both paths for certain, so it writes
-- them where a plain Python process can always look: %LOCALAPPDATA%.
--
-- The file goes in the ROOT of LOCALAPPDATA on purpose - Lua has no portable
-- mkdir, and that directory is guaranteed to exist for the user running both
-- EverQuest and the server. A second copy lands in <MQ config> so Setup can
-- confirm it is looking at the right MQ once it knows where that is.
-- ---------------------------------------------------------------------------
local function jsonEscape(s)
    s = tostring(s or '')
    s = s:gsub('\\', '\\\\'):gsub('"', '\\"')
    s = s:gsub('\r', ''):gsub('\n', ' ')
    return s
end

--- Does the beacon on disk already point at these same two folders?
local function beaconMatches(path, mqDir, eqPath)
    local f = io.open(path, 'r')
    if not f then return false end
    local body = f:read('*a') or ''
    f:close()
    return body:find(jsonEscape(mqDir), 1, true) ~= nil
       and body:find(jsonEscape(eqPath), 1, true) ~= nil
end

local function writeBeacon()
    local mqDir = mq.configDir
    local eqPath = tloStr(mq.TLO.EverQuest.Path, 'EverQuest.Path')
    local body = string.format(
        '{\n' ..
        '  "addon_version": "%s",\n' ..
        '  "mq_config": "%s",\n' ..
        '  "eq_path": "%s",\n' ..
        '  "server": "%s",\n' ..
        '  "character": "%s",\n' ..
        '  "written": %d\n' ..
        '}\n',
        jsonEscape(VERSION),
        jsonEscape(mqDir),
        jsonEscape(eqPath),
        jsonEscape(tloStr(mq.TLO.EverQuest.Server)),
        jsonEscape(tloStr(mq.TLO.Me.Name)),
        os.time())

    local targets = {}
    local localapp = os.getenv('LOCALAPPDATA') or os.getenv('APPDATA') or os.getenv('USERPROFILE')
    if localapp and localapp ~= '' then
        targets[#targets + 1] = localapp .. '\\' .. BEACON_FILE
    end
    targets[#targets + 1] = mq.configDir .. '/' .. BEACON_FILE

    -- The beacon is the ONE file every client shares (all the exports are per
    -- character). On a 6-box that is invisible; on an 18-box crew running
    -- `/eqf crew all`, eighteen clients truncate-and-rewrite the same path at the
    -- same instant, and a torn write is unparseable JSON. Nothing here changes
    -- between clients except the character name and timestamp, which nobody reads,
    -- so skip the write entirely when the paths on disk already match. That turns
    -- a stampede into one write on the first client and none after.
    local wrote, skipped = 0, 0
    for _, path in ipairs(targets) do
        if beaconMatches(path, mqDir, eqPath) then
            skipped = skipped + 1
        else
            local f = io.open(path, 'w')
            if f then
                f:write(body)
                f:close()
                wrote = wrote + 1
            end
        end
    end
    if wrote == 0 and skipped == 0 then
        warn('could not write the paths beacon anywhere.')
    end
    return wrote + skipped, targets[1]
end

--- Broadcast an /eqf subcommand to every client, whichever box tool is installed.
---
--- People shouldn't have to remember that DanNet is `/dgae /eqf all` while EQBC is
--- `/bcaa //eqf all` (note the second slash) - getting it wrong just silently does
--- nothing on the other boxes. Both forms below INCLUDE the sending client, so this
--- must NOT also run the command locally: doing both is what made an old crew button
--- fire twice on the toon you pressed it on.
local function pluginLoaded(name)
    local ok, loaded = pcall(function() return mq.TLO.Plugin(name).IsLoaded() end)
    return ok and loaded == true
end

local function crewSend(rest)
    if rest == '' then rest = 'all' end
    if rest:lower():match('^crew') then
        warn('/eqf crew crew is not a thing.')
        return
    end
    local full = '/eqf ' .. rest
    if pluginLoaded('mq2dannet') then
        mq.cmdf('/dgae %s', full)                  -- DanNet: includes this client
        info('sent "%s" to the whole crew (DanNet).', full)
    elseif pluginLoaded('mq2eqbc') then
        mq.cmdf('/bcaa /%s', full)                 -- EQBC needs the extra slash
        info('sent "%s" to the whole crew (EQBC).', full)
    else
        warn('No MQ2DanNet or MQ2EQBC loaded - nothing to broadcast with.')
        info('Run "%s" on each client, or load one of those plugins.', full)
    end
end

--- Is `/lua run eqforge` wired into ingame.cfg? Checked by NAME, not by exact line,
--- so any spelling of the command (with or without /timed, extra flags) counts.
local function autostartInstalled()
    local f = io.open(mq.configDir .. '/ingame.cfg', 'r')
    if not f then return false end
    local body = f:read('*a') or ''
    f:close()
    for line in body:gmatch('[^\r\n]+') do
        local t = line:match('^%s*(.-)%s*$')
        -- ';' comments out a line in an MQ cfg file - a commented example is not install
        if t:sub(1, 1) ~= ';' and t:lower():find(SCRIPT, 1, true) then return true end
    end
    return false
end

-- ---------------------------------------------------------------------------
-- exports
-- ---------------------------------------------------------------------------

--- Roster row for My Characters -> Import.
--- SANITIZED BY DESIGN: name/server/class/level/race/membership only. Never an
--- account name, never a password, never anything out of AutoLogin.
local function exportRoster()
    local name = tloStr(mq.TLO.Me.Name)
    if name == '' then return false, 'not in game' end

    local path = mq.configDir .. ('/mychars_export_%s.csv'):format(name)
    local f = io.open(path, 'w')
    if not f then return false, 'cannot write ' .. path end

    local class = tloStr(mq.TLO.Me.Class.Name)
    local level = tloStr(mq.TLO.Me.Level)
    f:write('name,server,class,level,race,membership,subdays,asof\n')
    f:write(string.format('%s,%s,%s,%s,%s,%s,%s,%d\n',
        name,
        tloStr(mq.TLO.EverQuest.Server),
        class,
        level,
        tloStr(mq.TLO.Me.Race.Name),
        tloStr(mq.TLO.Me.MembershipLevel),
        tloStr(mq.TLO.Me.SubscriptionDays),
        os.time()))
    f:close()
    return true, string.format('%s (%s %s)', name, class, level)
end

--- DynamicZone / expedition lockouts for the Keys & Access board.
--- DynamicZone.Timer[i].Timer is a COUNTDOWN (time remaining), so the absolute
--- expiry has to be stamped here: now + remaining. Replay timers carry EventID -1
--- and get an empty event column.
local function exportLockouts()
    local name = tloStr(mq.TLO.Me.Name)
    if name == '' then return false, 'not in game' end
    local server = tloStr(mq.TLO.EverQuest.Server)

    local now, rows = os.time(), {}
    local maxTimers = tonumber(tloStr(mq.TLO.DynamicZone.MaxTimers, 'DynamicZone.MaxTimers')) or 0
    for i = 1, maxTimers do
        local t = mq.TLO.DynamicZone.Timer(i)
        local expedition = tloStr(t.ExpeditionName)
        if expedition ~= '' then
            local eventId = tonumber(tloStr(t.EventID)) or -1
            local event = (eventId >= 0) and tloStr(t.EventName) or ''
            local remain = tonumber(tloStr(t.Timer.TotalSeconds)) or 0
            if remain > 0 then
                rows[#rows + 1] = string.format('%s|%s|%s|%s|%d',
                    name, server, expedition, event, now + remain)
            end
        end
    end

    local path = mq.configDir .. ('/mychars_lockouts_%s.txt'):format(name)
    local f = io.open(path, 'w')
    if not f then return false, 'cannot write ' .. path end
    f:write('name|server|expedition|event|expires_epoch\n')
    for _, line in ipairs(rows) do f:write(line, '\n') end
    f:close()

    -- An empty file is a real, useful answer ("this toon holds no lockouts") and
    -- must still be written, or a cleared lockout would linger in the app forever.
    return true, string.format('%d active lockout(s)', #rows)
end

--- Rows only capturable with a window OPEN: the Dragon's Hoard and the Personal
--- Tradeskill Depot. Bank and SharedBank arrive in the login packet and need
--- nothing; these two do not.
--- Mirrors mychars/gear.py loc_bucket: hoard = Hoard*/Dragon*, depot = Depot*/Personal*.
--- Real locations carry a space ("Hoard 1", "Hoard 1-Slot1"), so match on the prefix.
local function isWindowOnlyLoc(loc)
    return loc:sub(1, 5) == 'Hoard' or loc:sub(1, 6) == 'Dragon'
        or loc:sub(1, 5) == 'Depot' or loc:sub(1, 8) == 'Personal'
end

--- Counts only rows holding something. An empty hoard slot still prints a row, and
--- counting those made a wiped hoard look populated.
local function countWindowOnly(text)
    local n = 0
    for line in (text or ''):gmatch('[^\r\n]+') do
        local loc, name = line:match('^([^\t]+)\t([^\t]*)')
        if loc and isWindowOnlyLoc(loc) and name ~= 'Empty' and name ~= '' then
            n = n + 1
        end
    end
    return n
end

--- Every window-only line of `text`, verbatim.
local function windowOnlyLines(text)
    local out = {}
    for line in (text or ''):gmatch('[^\r\n]+') do
        local loc = line:match('^([^\t]+)')
        if loc and isWindowOnlyLoc(loc) then table.insert(out, line) end
    end
    return out
end

--- Put `saved` window-only rows back into the dump at `path`, REPLACING whatever
--- window-only rows the fresh dump has (never appending - the app sums counts, so a
--- duplicated row inflates what you appear to own).
local function spliceWindowOnly(path, saved)
    local f = io.open(path, 'r')
    if not f then return false, 'dump file vanished' end
    local kept = {}
    for line in f:lines() do
        local loc = line:match('^([^\t]+)')
        if not (loc and isWindowOnlyLoc(loc)) then table.insert(kept, line) end
    end
    f:close()
    for _, l in ipairs(saved) do table.insert(kept, l) end
    local out = io.open(path, 'w')
    if not out then return false, 'could not reopen the dump for writing' end
    out:write(table.concat(kept, '\n'), '\n')
    out:close()
    return true
end

--- When hoard rows were last captured FOR REAL (window open). Restored rows keep the
--- old stamp, so the app can age them honestly instead of trusting the merged file's
--- fresh mtime.
local function writeHoardStamp(path)
    local f = io.open((path:gsub('%-Inventory%.txt$', '-Inventory.hoardasof')), 'w')
    if f then f:write(tostring(os.time()), '\n'); f:close() end
end

local function readFile(path)
    local f = io.open(path, 'r')
    if not f then return nil end
    local body = f:read('*a')
    f:close()
    return body
end

--- Counts what actually landed in the dump FILE.
local function dumpStatus(path)
    local f = io.open(path, 'r')
    if not f then return false, 'no dump file at ' .. path end
    local bank, shared = 0, 0
    for line in f:lines() do
        local loc = line:match('^([^\t]+)')
        if loc then
            if loc:match('^Bank%d+$') then bank = bank + 1
            elseif loc:match('^SharedBank%d+$') then shared = shared + 1 end
        end
    end
    f:close()
    if bank < BANK_SLOTS_EXPECTED or shared < SHARED_SLOTS_EXPECTED then
        return false, string.format('short dump (%d/%d bank, %d/%d shared)',
            bank, BANK_SLOTS_EXPECTED, shared, SHARED_SLOTS_EXPECTED)
    end
    return true, string.format('%d bank / %d shared slots', bank, shared)
end

--- /outputfile inventory, then PROVE the file is complete.
--- Bare mq.delay - call this from the main loop or a one-shot run, never from a
--- bind or event handler.
local function exportDump(budgetS, force)
    local name = tloStr(mq.TLO.Me.Name)
    if name == '' then return false, 'not in game' end

    local eqdir = tloStr(mq.TLO.EverQuest.Path, 'EverQuest.Path')
    if eqdir == '' then return false, 'cannot resolve the EverQuest folder' end
    local path = string.format('%s/%s_%s-Inventory.txt', eqdir, name,
        tloStr(mq.TLO.EverQuest.Server):lower())

    -- /outputfile REWRITES the whole file. The Dragon's Hoard and the Tradeskill
    -- Depot only appear when their window is open, so dumping from anywhere else
    -- silently replaces a dump that HAD them with one that doesn't - destroying
    -- data in the name of refreshing it. Seen for real: a hoard owner recorded
    -- with 125 items now reads 0. Keep the old text so we can put it back.
    local prevText = readFile(path)
    local prevWindowOnly = countWindowOnly(prevText)

    local deadline = os.time() + (budgetS or 60)
    local ok, detail
    for attempt = 1, DUMP_TRIES do
        mq.cmd('/outputfile inventory')
        mq.delay(DUMP_SETTLE_MS)
        ok, detail = dumpStatus(path)
        if ok then break end
        if os.time() >= deadline then
            detail = (detail or 'incomplete') .. ' (out of time)'
            break
        end
        warn('attempt %d: %s - inventory still loading.', attempt, detail)
        mq.delay(DUMP_RETRY_MS)
    end
    if not ok then return false, detail or 'dump never completed' end

    -- Did this dump throw away hoard/depot rows the previous one had?
    local nowWindowOnly = countWindowOnly(readFile(path))
    if nowWindowOnly > 0 then
        writeHoardStamp(path)                        -- captured live, window was open
    end
    if prevWindowOnly > 0 and nowWindowOnly == 0 and not force then
        -- SPLICE, don't restore the whole file. The old behaviour rewrote `prevText`
        -- wholesale, which protected the hoard but silently threw away the refresh you
        -- had just asked for - worn/bags/bank all reverted to the older snapshot. Only
        -- the window-only rows actually need rescuing (reported 2026-08-09).
        local ok2, err2 = spliceWindowOnly(path, windowOnlyLines(prevText))
        if ok2 then
            warn('kept %d Hoard/Depot row(s) this dump would have wiped.', prevWindowOnly)
            info('Those only export with the window OPEN. Everything else in the dump is '
                 .. 'freshly refreshed; the hoard rows are as of the last bank visit.')
            info('To drop them anyway (e.g. you emptied it): /eqf dump force')
            return true, string.format('%s - protected %d Hoard/Depot row(s)',
                                       detail or 'refreshed', prevWindowOnly)
        end
        warn('could not splice the hoard rows back into %s (%s) - restoring the whole '
             .. 'previous dump instead so nothing is lost.', path, tostring(err2))
        local f = io.open(path, 'w')
        if f then f:write(prevText); f:close()
        else warn('could not restore the previous dump at %s', path) end
    elseif prevWindowOnly > 0 and nowWindowOnly < prevWindowOnly / 2 then
        -- Contents STREAM in after the window opens; dumping too early captures a
        -- partial hoard that still looks like a real one. Loud, but not blocking.
        warn('Hoard/Depot rows dropped %d -> %d. If the window was open, it may not '
             .. 'have finished loading - re-run /eqf dump.', prevWindowOnly, nowWindowOnly)
    end
    return ok, detail or 'dump never completed'
end

--- Walk to a nearby banker, open the bank + Dragon's Hoard, then dump.
---
--- EXPLICIT COMMAND ONLY - never wired to the /camp or login triggers. Two reasons:
--- moving CANCELS a camp in progress, and nothing automatic should ever take the
--- controls off a character you might be driving. You ask for it, it happens.
---
--- Sequence and constants are lifted from harvest_step.lua, which has run this
--- against a live crew. Only visits a banker that is ALREADY CLOSE - it will never
--- send you on a cross-zone trek you didn't ask for.
local BANK_OPEN_RANGE = 20      -- right-click range for a banker
local BANK_MAX_WALK   = 300     -- further than this: refuse, don't trek
local HOARD_POPULATE  = 1500    -- hoard contents STREAM in after the window opens

local function bankRun()
    if tloStr(mq.TLO.Me.CombatState) == 'COMBAT' then
        return false, 'in combat - not moving you anywhere'
    end
    local banker = mq.TLO.NearestSpawn('banker')
    local id = (banker() and banker.ID()) or 0
    if id == 0 then
        return false, 'no banker in ' .. tloStr(mq.TLO.Zone.ShortName)
    end
    local dist = banker.Distance3D() or 9999
    if dist > BANK_MAX_WALK then
        return false, string.format('nearest banker is %.0f away (limit %d) - walk closer '
                                    .. 'yourself, then re-run', dist, BANK_MAX_WALK)
    end

    if dist > BANK_OPEN_RANGE then
        if not mq.TLO.Navigation.MeshLoaded() then
            return false, string.format('banker is %.0f away and this zone has no navmesh '
                                        .. '- walk into range yourself, then re-run', dist)
        end
        say('banker %s at %.0f - heading over.', tloStr(banker.CleanName), dist)
        mq.cmd('/squelch /rgl pause')            -- no-op if rgmercs isn't loaded
        mq.cmdf('/nav id %d distance=%d', id, BANK_OPEN_RANGE - 5)
        local until_ = os.time() + 60
        while os.time() < until_ and mq.TLO.Navigation.Active()
              and (banker.Distance3D() or 9999) > BANK_OPEN_RANGE do
            mq.delay(250)
        end
        mq.cmd('/nav stop')
    end

    if (banker.Distance3D() or 9999) > BANK_OPEN_RANGE + 10 then
        return false, string.format('could not get to the banker (%.0f away)',
                                    banker.Distance3D() or 9999)
    end

    mq.cmdf('/target id %d', id)
    mq.delay(300)
    mq.cmd('/click right target')
    local until_ = os.time() + 10
    while os.time() < until_ and not mq.TLO.Window('BigBankWnd').Open() do
        mq.delay(250)
    end
    if not mq.TLO.Window('BigBankWnd').Open() then
        return false, 'the bank window never opened'
    end

    -- Both control names confirmed from uifiles/default/EQUI_BigBankWnd.xml, and both
    -- window names from their own EQUI_*.xml (DragonHoardWnd, TradeskillDepotWnd).
    -- Harmless on a character that owns neither: the click just does nothing, and we
    -- check the WINDOW rather than assuming the click worked.
    local EXTRA = {
        { button = 'BNK_DragonHoard',     window = 'DragonHoardWnd',    label = "Dragon's Hoard" },
        { button = 'BNK_TradeskillDepot', window = 'TradeskillDepotWnd', label = 'Tradeskill Depot' },
    }
    local opened = {}
    for _, e in ipairs(EXTRA) do
        if not mq.TLO.Window(e.window).Open() then
            mq.cmdf('/notify BigBankWnd %s leftmouseup', e.button)
            mq.delay(800)
        end
        if mq.TLO.Window(e.window).Open() then
            opened[#opened + 1] = e.label
        end
    end
    if #opened > 0 then
        -- Contents STREAM in after the window opens; dumping immediately captures a
        -- partial container that still looks complete.
        mq.delay(HOARD_POPULATE)
        say('%s open.', table.concat(opened, ' + '))
    end

    -- Deliberately NOT forced: if the hoard failed to open, the dump guard still
    -- protects whatever the previous dump had captured.
    local ok, detail = exportDump(120, false)

    -- An open bank window BLOCKS /camp and makes nav flaky, so never leave it open.
    local w = mq.TLO.Window('BigBankWnd')
    if w.Open() then w.DoClose() mq.delay(400) end

    if not ok then return false, detail end
    return true, detail .. (#opened > 0
        and (' (' .. table.concat(opened, ' + ') .. ')')
        or ' (no hoard/depot on this character)')
end

-- ---------------------------------------------------------------------------
-- jobs  -  the only things the main loop ever runs
-- ---------------------------------------------------------------------------
local lastRun = { roster = 0, lockouts = 0, dump = 0, achievements = 0 }

--- /outputfile achievements -> <Name>_<server>-Achievements.txt in the EQ folder.
---
--- Moved here from TrixBox 2026-08-09. Achievements are the authoritative record of
--- keys and flags (Sleeper's Key and friends) and NOTHING in the public addon exported
--- them - only TrixBox, which no one outside the user's box runs, and harvest_step, which
--- only runs during a harvest. So every tester's key/flag data was silently empty.
---
--- Unlike the inventory dump this needs no settle delay or completeness gate: verified
--- 2026-08-07 across three toons, the file is fully written at export time (22.8-22.9k
--- lines each, statuses differentiating correctly). There is also nothing window-only
--- in it, so no hoard-style guard is required.
local function exportAchievements()
    local name = tloStr(mq.TLO.Me.Name)
    if name == '' then return false, 'not in game' end
    mq.cmd('/outputfile achievements')
    return true, string.format('%s_%s-Achievements.txt', name,
                               tloStr(mq.TLO.EverQuest.Server):lower())
end

local function runAchievements()
    local ok, detail = exportAchievements()
    if ok then
        lastRun.achievements = os.time()
        say('achievements: %s', detail)
    else
        warn('achievements export failed: %s', detail)
    end
    return ok
end

local function runRoster()
    local ok, detail = exportRoster()
    if ok then
        lastRun.roster = os.time()
        say('roster: %s', detail)
    else
        warn('roster export failed: %s', detail)
    end
    return ok
end

local function runLockouts()
    local ok, detail = exportLockouts()
    if ok then
        lastRun.lockouts = os.time()
        say('lockouts: %s', detail)
    else
        warn('lockout export failed: %s', detail)
    end
    return ok
end

local function runDump(budgetS, force)
    local ok, detail = exportDump(budgetS, force)
    if ok then
        lastRun.dump = os.time()
        say('inventory: %s', detail)
    else
        warn('inventory dump failed: %s', detail)
    end
    return ok
end

local function runBank()
    local ok, detail = bankRun()
    if ok then
        lastRun.dump = os.time()
        say('bank run: %s', detail)
    else
        warn('bank run: %s', detail)
    end
    return ok
end

local function runBeacon(loud)
    local n, where = writeBeacon()
    if n > 0 and loud then info('paths beacon -> %s', where) end
    return n > 0
end

local function runAll(budgetS)
    runRoster()
    runLockouts()
    runAchievements()
    runDump(budgetS)
    runBeacon(false)
end

-- ---------------------------------------------------------------------------
-- resident state
--
-- JOBS are queued by name and drained by the main loop:
--   roster | lockouts | dump | light (roster+lockouts) | all | beacon
--   campnow  = all, then issue /camp        (you typed /eqf camp)
--   campauto = all, do NOT issue /camp      (you typed /camp yourself)
-- ---------------------------------------------------------------------------
local running   = true
local pending   = {}
local lastFired = {}
local lastZone  = ''
local nextEvery = 0

-- loginDue:  -1 = armed, waiting to enter the world
--             0 = nothing scheduled
--            >0 = run the login export at this epoch
local loginDue = 0

local function queue(job) pending[job] = true end

local function fire(trigger, job)
    local now = os.time()
    if (now - (lastFired[trigger] or 0)) < TRIGGER_COOLDOWN_S then return end
    lastFired[trigger] = now
    queue(job)
end

-- ---------------------------------------------------------------------------
-- /eqf
-- ---------------------------------------------------------------------------
local function showHelp()
    info('EQ Forge addon v%s', VERSION)
    printf('  /eqf status              what is on, and when each export last ran')
    printf('  /eqf roster              export this toon for My Characters')
    printf('  /eqf lockouts            export this toon\'s expedition lockouts')
    printf('  /eqf dump [force]        /outputfile inventory, verified')
    printf('      Refuses to replace a dump holding Hoard/Depot rows with one that')
    printf('      has none (those need the window OPEN). "force" overrides.')
    printf('  /eqf bank                walk to a NEARBY banker, open the bank +')
    printf("                           Dragon's Hoard, then dump (never automatic)")
    printf('  /eqf all                 all three + refresh the paths beacon')
    printf('  /eqf camp                all three, then /camp')
    printf('  /eqf on <feature>        camp | login | logindump | zone | quiet')
    printf('  /eqf off <feature>')
    printf('  /eqf every <minutes>     automatic full export on a timer (0 = off)')
    printf('  /eqf beacon              rewrite the paths file EQ Forge reads')
    printf('  /eqf stop                stop the addon')
    printf('  \agEVERY BOX AT ONCE:\ax /eqf crew <cmd>   e.g. \ag/eqf crew all\ax')
    printf('      uses MQ2DanNet or MQ2EQBC, whichever you have. Make it a hotkey:')
    printf('      \ag/eqf crew all\ax  in a social or Button Master button.')
end

local function ago(t)
    if not t or t == 0 then return 'never' end
    local d = os.time() - t
    if d < 60 then return string.format('%ds ago', d) end
    if d < 3600 then return string.format('%dm ago', math.floor(d / 60)) end
    return string.format('%.1fh ago', d / 3600)
end

local function showStatus()
    local me = tloStr(mq.TLO.Me.Name)
    info('EQ Forge addon v%s on %s', VERSION, me ~= '' and me or 'no character')
    printf('  auto on camp   : %s', settings.camp and '\agON\ax' or '\ayoff\ax')
    printf('  auto on login  : %s%s', settings.login and '\agON\ax' or '\ayoff\ax',
        settings.loginDump and ' (+ inventory)' or '')
    printf('  auto on zone   : %s', settings.zone and '\agON\ax' or '\ayoff\ax')
    printf('  auto every     : %s', (settings.every or 0) > 0
        and string.format('\ag%d min\ax', settings.every) or '\ayoff\ax')
    printf('  last roster    : %s', ago(lastRun.roster))
    printf('  last lockouts  : %s', ago(lastRun.lockouts))
    printf('  last achieve.  : %s', ago(lastRun.achievements))
    -- "never" here is NORMAL on a toon that has not camped yet: loginDump defaults OFF
    -- because login is the noisy moment, so the login export is roster + lockouts only.
    printf('  last inventory : %s%s', ago(lastRun.dump),
           lastRun.dump == 0 and ' (camp to get one - login does not dump by default)' or '')
    printf('  autostart      : %s', autostartInstalled()
        and '\agingame.cfg\ax' or '\aroff - stops when you camp\ax')
    printf('  MQ config      : %s', mq.configDir)
    printf('  EverQuest      : %s', tloStr(mq.TLO.EverQuest.Path))
    printf('  settings       : %s', settingsPath())
end

local function setFlag(key, value)
    local field = FLAG_ALIAS[(key or ''):lower()]
    if not field then
        warn('unknown feature "%s" - try camp, login, logindump, zone, quiet.', tostring(key))
        return
    end
    settings[field] = value
    saveSettings()
    info('%s is now %s', field, value and 'ON' or 'off')
end

-- Handlers ONLY set flags. Anything that delays goes through `pending`, because a
-- bind callback cannot yield (see the header).
local function onCommand(...)
    local argv = { ... }
    local cmd = (argv[1] or 'help'):lower()
    local a = argv[2]
    if cmd == 'crew' or cmd == 'all-boxes' then
        return crewSend(table.concat(argv, ' ', 2))
    end
    if cmd == 'help' or cmd == '?' then showHelp()
    elseif cmd == 'status' then showStatus()
    elseif cmd == 'roster' or cmd == 'export' then queue('roster')
    elseif cmd == 'lockouts' or cmd == 'lockout' then queue('lockouts')
    elseif cmd == 'dump' or cmd == 'inv' or cmd == 'inventory' then
        queue((a or ''):lower() == 'force' and 'dumpforce' or 'dump')
    elseif cmd == 'all' then queue('all')
    elseif cmd == 'camp' then queue('campnow')
    elseif cmd == 'bank' or cmd == 'bankrun' then queue('bank')
    elseif cmd == 'beacon' then queue('beacon')
    elseif cmd == 'on' then setFlag(a, true)
    elseif cmd == 'off' then setFlag(a, false)
    elseif cmd == 'every' then
        local n = tonumber(a)
        if not n or n < 0 then
            warn('usage: /eqf every <minutes>  (0 = off)')
            return
        end
        settings.every = math.floor(n)
        nextEvery = settings.every > 0 and (os.time() + settings.every * 60) or 0
        saveSettings()
        info('automatic full export %s', settings.every > 0
            and string.format('every %d min', settings.every) or 'disabled')
    elseif cmd == 'stop' then
        -- NEVER /lua stop ourselves from in here. Queue it externally and keep
        -- running until MQ's own command queue delivers the stop.
        info('stopping in 5s...')
        mq.cmdf('/timed 5 /lua stop %s', SCRIPT)
    else
        warn('unknown command "%s"', cmd)
        showHelp()
    end
end

-- ---------------------------------------------------------------------------
-- events
--
-- The camp message wording varies with posture and client build ("about 30
-- seconds", "about 20 seconds"), so the pattern is deliberately loose around the
-- one phrase that never changes.
-- ---------------------------------------------------------------------------
local function onCampStart()
    if settings.camp then fire('camp', 'campauto') end
end

-- ---------------------------------------------------------------------------
-- main
-- ---------------------------------------------------------------------------
local function drain()
    -- Take the whole queue and clear it first: a job can take seconds, and an
    -- event may queue more work while it runs.
    local jobs = pending
    pending = {}

    local campAuto = jobs.campauto
    local campNow  = jobs.campnow
    local wantAll  = jobs.all or campAuto or campNow
    local budget   = (campAuto or campNow) and CAMP_BUDGET_S or nil

    if campAuto then say('camp detected - exporting before you go.') end

    if wantAll then
        runAll(budget)                        -- runAll refreshes the beacon itself
    else
        if jobs.roster or jobs.light then runRoster() end
        if jobs.lockouts or jobs.light then runLockouts() end
        if jobs.dump then runDump() end
        if jobs.dumpforce then runDump(nil, true) end
        if jobs.bank then runBank() end
        if jobs.beacon then runBeacon(true) end
    end

    -- Only the explicit /eqf camp issues the camp. campauto was TRIGGERED by one
    -- already; re-issuing would re-camp after a cancelled one.
    if campNow then mq.cmd('/camp') end
end

local function oneShot(cmd, arg2)
    -- No binds, no events, no loop: run the thing and let the script exit.
    if cmd == 'roster' or cmd == 'export' then runRoster()
    elseif cmd == 'lockouts' or cmd == 'lockout' then runLockouts()
    elseif cmd == 'dump' or cmd == 'inv' or cmd == 'inventory' then
        runDump(nil, (arg2 or ''):lower() == 'force')
    elseif cmd == 'bank' or cmd == 'bankrun' then runBank()
    elseif cmd == 'beacon' then runBeacon(true)
    elseif cmd == 'all' then runAll()
    else
        warn('"%s" is not a one-shot command.', tostring(cmd))
        info('one-shot: roster | lockouts | dump | bank | all | beacon')
        info('everything else needs the resident addon: /lua run %s', SCRIPT)
    end
end

local function main(...)
    settings = loadSettings()

    local args = { ... }
    if args[1] then
        oneShot(tostring(args[1]):lower(), args[2] and tostring(args[2]) or nil)
        return
    end

    mq.bind('/eqf', onCommand)
    -- CAMP START ONLY. The first pattern here was '#*#prepare your camp#*#', which also
    -- matched every countdown tick ("It will take about 25 MORE seconds to prepare your
    -- camp."). The 15s trigger cooldown hid the early ticks, then the ~20s tick landed
    -- just past it and fired a SECOND full export - measured in a live log: dumps at
    -- 16:10:36 and again at 16:10:55 off one camp.
    -- "you" is the discriminator: the start line says "It will take YOU about N
    -- seconds", every tick says "It will take about N MORE seconds". The restart line
    -- ("about 30 more seconds") is a genuinely new camp after an interrupted one, so it
    -- gets its own event rather than being lumped in with the ticks.
    mq.event('eqf_camp', 'It will take you about #1# seconds to prepare your camp.',
             onCampStart)
    mq.event('eqf_camp_restart', 'It will take about 30 more seconds to prepare your camp.',
             onCampStart)

    info('EQ Forge addon v%s loaded. /eqf for commands.', VERSION)
    if settings.camp then
        say('auto-export on /camp is ON - roster, lockouts, achievements and inventory.')
    else
        say('auto-export on /camp is off (/eqf on camp to enable).')
    end

    -- MEASURED 2026-08-07: this script does NOT survive camping to character select.
    -- The camp export still fires (it runs ~30s before you actually leave), but the
    -- script is gone afterwards, so a hand-started addon gives exactly ONE camp export
    -- and then silently stops. ingame.cfg is the fix and it is not optional; say so
    -- when it is missing rather than letting people discover it weeks later.
    if not autostartInstalled() then
        warn('This script stops when you camp to character select.')
        warn('Add this line to %s/ingame.cfg so it restarts on every login:', mq.configDir)
        printf('    \ag/timed 50 /lua run %s\ax', SCRIPT)
    end

    runBeacon(false)
    lastZone = tloStr(mq.TLO.Zone.ShortName)
    if settings.login then loginDue = inWorld() and (os.time() + LOGIN_DELAY_S) or -1 end
    if (settings.every or 0) > 0 then nextEvery = os.time() + settings.every * 60 end

    while running do
        mq.doevents()

        local here = inWorld()
        local now = os.time()

        if not here then
            -- Camped, zoning, or sitting at character select: re-arm the login
            -- export so the next character to enter the world gets one.
            if settings.login then loginDue = -1 end
            lastZone = ''
        else
            if loginDue == -1 then
                loginDue = now + LOGIN_DELAY_S
            elseif loginDue > 0 and now >= loginDue then
                loginDue = 0
                fire('login', settings.loginDump and 'all' or 'light')
            end

            if settings.zone then
                local zone = tloStr(mq.TLO.Zone.ShortName)
                if zone ~= '' and zone ~= lastZone then
                    if lastZone ~= '' then fire('zone', 'light') end
                    lastZone = zone
                end
            end

            if (settings.every or 0) > 0 and nextEvery > 0 and now >= nextEvery then
                nextEvery = now + settings.every * 60
                fire('every', 'all')
            end
        end

        if next(pending) then drain() end
        mq.delay(LOOP_MS)
    end
end

main(...)
