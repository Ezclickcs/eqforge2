--[[ mychars_lockouts.lua - dump this toon's DZ/expedition lockouts for My Characters.

Run on each toon (after raid night):   /lua run mychars_lockouts
Writes/updates pipe-delimited rows in  <MQ config>/mychars_lockouts.txt:
    name|server|expedition|event|expires_epoch
then EQ Forge -> My Characters -> Keys & Access -> "Load lockouts from MQ".

Source: DynamicZone.MaxTimers / DynamicZone.Timer[i] (the DZ window's Timers tab,
readable anywhere, not just inside a DZ). dztimer.Timer is a COUNTDOWN (ms
remaining, docs.macroquest.org datatype-timestamp), so expiry = now + remaining.
Replay timers have EventID -1 (empty event column). Fire-and-forget: no delays,
no loops, exits immediately - safe per TrixBox rules.
]]

local mq = require('mq')

-- ONE FILE PER TOON (mychars_lockouts_<Name>.txt) - same race fix as mychars_export:
-- broadcast runs clobbered each other in a shared file. Server merges all of them.
local HEADER = 'name|server|expedition|event|expires_epoch'

local function tloStr(v)
    local ok, s = pcall(function() return tostring(v()) end)
    if not ok or s == nil or s == 'NULL' then return '' end
    return s
end

local name   = tloStr(mq.TLO.Me.Name)
local server = tloStr(mq.TLO.EverQuest.Server)
if name == '' then
    printf('\ar[mychars] Not in game - nothing exported.\ax')
    return
end

local now = os.time()
local mine = {}
local maxTimers = tonumber(tloStr(mq.TLO.DynamicZone.MaxTimers)) or 0
for i = 1, maxTimers do
    local t = mq.TLO.DynamicZone.Timer(i)
    local expedition = tloStr(t.ExpeditionName)
    if expedition ~= '' then
        local eventId = tonumber(tloStr(t.EventID)) or -1
        local event = (eventId >= 0) and tloStr(t.EventName) or ''
        local remain = tonumber(tloStr(t.Timer.TotalSeconds)) or 0
        if remain > 0 then
            mine[#mine + 1] = string.format('%s|%s|%s|%s|%d',
                name, server, expedition, event, now + remain)
        end
    end
end

local OUT = mq.configDir .. ('/mychars_lockouts_%s.txt'):format(name)
local f = io.open(OUT, 'w')
if not f then
    printf('\ar[mychars] Cannot write %s\ax', OUT)
    return
end
f:write(HEADER, '\n')
for _, line in ipairs(mine) do f:write(line, '\n') end
f:close()

printf('\ag[mychars] %s: %d active lockout(s) exported -> %s\ax', name, #mine, OUT)
if #mine == 0 then
    printf('\ay[mychars] (no active DZ lockouts on this character)\ax')
end
