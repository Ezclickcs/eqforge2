-- Custom parcel sources for DerpleDude/parcel
-- Install to:  <MacroQuest>/config/parcel_sources.lua   (the parcel script auto-loads it)
--
-- The bottom block is the part EQ Forge needs: it chain-loads the gear plan the
-- Gear Planner exports (config/parcel_gearplan.lua) so the set you just planned
-- shows up in the parcel window as its own pickable source.
--
-- This file is update-safe: it never touches the parcel script itself. If you already
-- have a parcel_sources.lua, don't overwrite it -- just copy the CHAIN-LOAD block at
-- the bottom into your existing file, above its `return sources`.
--
-- Note: the parcel tool already excludes No-Trade (No-Drop) and No-Rent items before
-- these filters run, so a filter only ever sees items that CAN be parceled.

local sources = {
    {
        -- Everything in your bags that is not No-Trade. On a free-trade TLP where most
        -- loot is sendable, this beats marking items one at a time.
        name = "All Sendable (not No-Trade)",
        filter = function(item)
            return true
        end,
    },
    {
        -- Just equippable gear in bags (armor/jewelry via AC, weapons via Damage).
        name = "All Gear in Bags",
        filter = function(item)
            return ((item.AC() or 0) > 0) or ((item.Damage() or 0) > 0)
        end,
    },
    {
        -- Spell/song scrolls to scribe: EQ names them "Spell: X" / "Song: X".
        name = "All Spells & Songs",
        filter = function(item)
            local n = item.Name() or ""
            return n:find("^Spell:") ~= nil or n:find("^Song:") ~= nil
        end,
    },
}

-- ---------------------------------------------------------------------------
-- CHAIN-LOAD BLOCK -- this is what EQ Forge's Gear Planner needs.
-- Safe if the generated file is missing: the pcall just skips it. Your own
-- hand-written sources above are never touched.
-- ---------------------------------------------------------------------------
local ok, generated = pcall(function()
    local mq = require('mq')
    local chunk = loadfile(mq.configDir .. '/parcel_gearplan.lua')
    return chunk and chunk() or nil
end)
if ok and type(generated) == 'table' then
    for _, s in ipairs(generated) do
        if type(s) == 'table' and s.name and s.filter then table.insert(sources, s) end
    end
end

return sources
