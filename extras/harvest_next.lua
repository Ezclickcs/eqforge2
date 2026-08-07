--[[ harvest_next.lua - character-select half of the unattended inventory harvest.

Hooked from charselect.cfg, which fires EVERY time any client reaches character
select - including when you are just logging in normally. So the very first thing
this does is look for an armed queue and exit silently if there isn't one.

  ARMING:   <MQ config>/harvest_<LoginName>.queue   (written by EQ Forge -> Harvest tab)
  DISARM:   delete that file. That is the hard stop; nothing else is needed.

Flow:  read queue -> take the top toon -> /switchchar <name>.
The in-game half (harvest_step.lua, hooked from ingame.cfg) does the dump and pops
the line, so when this runs again after the camp the next toon is on top.

Fire-and-forget by design: no loops, no mq.delay, runs once and exits. It never
has to survive a gamestate change, which is the whole reason this is two scripts
hanging off cfg hooks instead of one long-lived script (TrixBox rule, 2026-07-16).
]]

local mq = require('mq')

local CONFIG = mq.configDir
local PREFIX = '/harvest_'

local function log(msg, ...) printf('\at[harvest] ' .. msg .. '\ax', ...) end
local function warn(msg, ...) printf('\ar[harvest] ' .. msg .. '\ax', ...) end

local function tloStr(v)
    local ok, s = pcall(function() return tostring(v()) end)
    if not ok or s == nil or s == 'NULL' then return '' end
    return s
end

-- Which station is this client? EverQuest.LoginName is the account name and is
-- readable at character select, where Me.Name is not. Verified member spelling on
-- docs.macroquest.org (a wrong TLO member THROWS on index - 2026-07-17).
local account = tloStr(mq.TLO.EverQuest.LoginName)
if account == '' then return end        -- too early / unknown; next pulse will retry

local qpath = CONFIG .. PREFIX .. account .. '.queue'
local lpath = CONFIG .. PREFIX .. account .. '.log'

local qf = io.open(qpath, 'r')
if not qf then return end               -- NOT ARMED: normal login, stay silent
local lines = {}
for line in qf:lines() do lines[#lines + 1] = line end
qf:close()

local function appendLog(line)
    local f = io.open(lpath, 'a')
    if f then f:write(line, '\n') f:close() end
end

-- first real entry = the toon we owe a dump
local nextName
for _, line in ipairs(lines) do
    local t = line:match('^%s*(.-)%s*$')
    if t ~= '' and t:sub(1, 1) ~= '#' and not t:lower():match('^account=') then
        nextName = t:match('^([^|]+)')
        if nextName then nextName = nextName:match('^%s*(.-)%s*$') end
        break
    end
end

if not nextName or nextName == '' then
    -- Queue drained. Remove it so the next normal login is not treated as armed,
    -- and stamp the log so the report shows the run as finished rather than stalled.
    appendLog(string.format('finish|%d', os.time()))
    os.remove(qpath)
    log('Harvest complete for %s - queue emptied and disarmed.', account)
    return
end

if tloStr(mq.TLO.EverQuest.GameState) ~= 'CHARSELECT' then
    warn('Not at character select (state=%s) - skipping switch.',
         tloStr(mq.TLO.EverQuest.GameState))
    return
end

appendLog(string.format('current|%s|%d', nextName, os.time()))
log('Next up: %s (%d left). Switching...', nextName, #lines)
mq.cmdf('/switchchar %s', nextName)
