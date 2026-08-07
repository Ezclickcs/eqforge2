--[[ mychars_export.lua - dump SANITIZED character info for the My Characters importer.

Run on each toon:   /lua run mychars_export
Appends/updates one CSV row per character in  <MQ config>/mychars_export.csv
(merge key = name+server, so re-running just refreshes the row). Then paste that
file into EQ Forge -> My Characters -> Import.

Exports ONLY: name, server, class, level, race. Never accounts, never passwords,
never anything from AutoLogin. Fire-and-forget: runs once, writes, exits
(no delays, no loops - safe per TrixBox rules).
]]

local mq = require('mq')

-- ONE FILE PER TOON (mychars_export_<Name>.csv): six clients broadcast-running this
-- simultaneously clobbered each other's rows in a shared file (read-merge-write race,
-- caught live 2026-07-25). The EQ Forge server merges all mychars_export_*.csv files.
-- membership/subdays/asof: account-level sub info (Me.MembershipLevel GOLD/SILVER/FREE,
-- Me.SubscriptionDays = days remaining). asof = export time so the server can store an
-- absolute expiry that stays correct as days pass.
local HEADER = 'name,server,class,level,race,membership,subdays,asof'

local function tloStr(v)
    local ok, s = pcall(function() return tostring(v()) end)
    if not ok or s == nil or s == 'NULL' then
        printf('\ar[mychars] TLO read failed (%s)\ax', tostring(s))
        return ''
    end
    return s
end

local name   = tloStr(mq.TLO.Me.Name)
local server = tloStr(mq.TLO.EverQuest.Server)
local class  = tloStr(mq.TLO.Me.Class.Name)
local level  = tloStr(mq.TLO.Me.Level)
local race   = tloStr(mq.TLO.Me.Race.Name)
local member = tloStr(mq.TLO.Me.MembershipLevel)
local subd   = tloStr(mq.TLO.Me.SubscriptionDays)

if name == '' then
    printf('\ar[mychars] Not in game - nothing exported.\ax')
    return
end

local OUT = mq.configDir .. ('/mychars_export_%s.csv'):format(name)
local f = io.open(OUT, 'w')
if not f then
    printf('\ar[mychars] Cannot write %s\ax', OUT)
    return
end
f:write(HEADER, '\n')
f:write(string.format('%s,%s,%s,%s,%s,%s,%s,%d\n',
    name, server, class, level, race, member, subd, os.time()))
f:close()

printf('\ag[mychars] Exported %s (%s %s %s, %s %sd) -> %s\ax',
    name, class, level, server, member, subd, OUT)
printf('\ag[mychars] Safe to broadcast: every toon writes its OWN file now.\ax')
