-- Runs mod/wiretap/control.lua under a mocked Factorio 2.1 runtime (mock.lua)
-- against a representative world (world.lua) and asserts on the snapshot.
-- Usage: lua tests/lua/test_control.lua [path/to/control.lua]
local here = arg[0]:match("^(.*)/[^/]*$") or "."
package.path = here .. "/?.lua;" .. package.path
local M = require("mock")
local world = require("world")
local control_path = arg[1] or (here .. "/../../mod/wiretap/control.lua")
local SAMPLE_TICKS = 60

local function defaults()
  return {["wiretap-interval-seconds"] = 10, ["wiretap-decimals"] = 3, ["wiretap-spread"] = true, ["wiretap-mode"] = "snapshot",
    ["wiretap-filename"] = "wiretap/stats.json", ["wiretap-journal-filename"] = "wiretap/stats.ndjson",
    ["wiretap-server-only"] = true, ["wiretap-logistics"] = true, ["wiretap-quality-breakdown"] = true,
    ["wiretap-power"] = true, ["wiretap-power-breakdown"] = true, ["wiretap-power-flow"] = true,
    ["wiretap-production"] = true, ["wiretap-production-quality"] = true, ["wiretap-science"] = true,
    ["wiretap-pollution"] = true, ["wiretap-platforms"] = true, ["wiretap-kills"] = true,
    ["wiretap-build-counts"] = false}
end

local passed = 0
local function check(cond, msg) if not cond then error("FAIL: " .. msg, 2) end passed = passed + 1 end
local function has(s, needle) return s:find(needle, 1, true) ~= nil end

local function boot(opts)
  opts = opts or {}
  M.install{settings = opts.settings or defaults(), removed_entities = {["gone-entity"] = true}}
  local g, refs = world{multiplayer = opts.multiplayer}
  game = g
  if opts.mutate then opts.mutate(g, refs) end
  dofile(control_path)
  M.handlers.on_init()
  return g, refs
end

local function export_handler()
  local found, tick
  for t, h in pairs(M.handlers.nth) do
    if t ~= SAMPLE_TICKS then
      check(found == nil, "exactly one export handler registered")
      found, tick = h, t
    end
  end
  check(found ~= nil, "export handler registered")
  check(M.handlers.nth[SAMPLE_TICKS] ~= nil, "brownout sampler registered")
  return found, tick
end

-- Drive the spread export: run on_tick steps until the job unregisters itself.
local function drive_ticks()
  local steps = 0
  while M.handlers.events[defines.events.on_tick] do
    steps = steps + 1
    check(steps < 100, "spread export finishes within 100 ticks")
    game.tick = game.tick + 1
    M.handlers.events[defines.events.on_tick]({tick = game.tick})
  end
  return steps
end

local function export()
  local h, tick = export_handler()
  local before = #M.files
  local ok, err = h({tick = game.tick, nth_tick = tick})
  check(ok == true, "export handler reports success: " .. tostring(err))
  local steps = drive_ticks()
  check(#M.log_out == 0, "export logged no error: " .. table.concat(M.log_out, " | "))
  check(#M.files == before + 1 or #M.files == before + 2, "export wrote the file")
  return M.files[#M.files], tick, steps
end

local function sample() M.handlers.nth[SAMPLE_TICKS]({tick = game.tick, nth_tick = SAMPLE_TICKS}) end

-- 1. default export ---------------------------------------------------------
do
  local g, refs = boot()
  local f, nth = export()
  check(nth == 600, "10 s interval registers on tick 600")
  check(f.filename == "wiretap/stats.json" and f.append == false, "snapshot written to configured path")
  check(f.for_player == nil, "single player never passes for_player")
  local j = f.data
  check(not j:find("%d%.%d%d%d%d%d%d%d%d"), "no over-long float expansions")
  check(has(j, '"mod_version":"1.1.0"'), "mod version comes from script.active_mods")
  check(has(j, '"game_version":"2.1.23"') and has(j, '"game_speed":1') and has(j, '"tick_paused":false'), "game meta")
  check(has(j, '"mods":{"base":"2.1.23","wiretap":"1.1.0"}'), "active mods in meta")
  check(has(j, '"features":{') and has(j, '"quality":true'), "feature flags in meta")
  check(has(j, '"players":{"drkraus":{"afk_seconds":20,"online_seconds":600,"surface":"nauvis"}}'), "player details")
  check(has(j, '"game_time_seconds":493800'), "play time from ticks_played")
  check(has(j, '"export_ticks":6') and has(j, '"tick":29628000'), "spread export spans the step ticks, meta.tick is the start tick")
  -- power: the switch-joined network (two sub networks) is reported once, keyed by its lowest sub id
  local a1, a2 = refs.netA.sub_networks[1].id, refs.netA.sub_networks[2].id
  check(has(j, '"' .. a1 .. '":{'), "network keyed by lowest sub network id")
  check(not has(j, '"' .. a2 .. '":{'), "second sub network not reported as its own network")
  check(has(j, '"sub_network_count":2'), "sub network count reported")
  check(has(j, '"production_watts":1860000'), "surface production is the sum over networks, not double counted")
  check(has(j, '"consumption_watts":1700000'), "surface consumption not double counted")
  check(has(j, '"accumulator_charge_joules":25000000'), "accumulator charge from flow_last_tick")
  check(has(j, '"network_count":2'), "never-used network (isolated pole) dropped from nauvis")
  check(has(j, '"consumption_satisfaction":0.95'), "satisfaction exported")
  check(not has(j, 'consumption_satisfaction_min'), "no interval minimum before any sample")
  check(has(j, '"maximum_production_watts":2700000') and has(j, '"solar_watts":100000'), "maximum production and solar in watts")
  check(has(j, '"solar_used_watts":50000') and has(j, '"primary_output_watts":1800000'), "output tier split")
  check(has(j, '"accumulator_discharge_watts":200000') and has(j, '"accumulator_charge_watts":50000'), "accumulator charge/discharge watts")
  check(has(j, '"transfer_watts":1700000'), "transfer watts")
  check(has(j, '"gone-entity":0'), "zero-watt name of a removed prototype still listed in the breakdown")
  check(has(j, '"count_by_entity":{"assembling-machine-2":45,"lab":12,"solar-panel":300,"steam-engine":20}'), "entity counts skip unknown prototypes, sorted keys")
  -- platform network keyed "global" as in 1.0.x
  check(has(j, '"networks":{"global":{'), "platform network keyed global")
  check(has(j, '"planet":"nauvis"') and has(j, '"pollutant":"pollution"'), "planet and pollutant names")
  check(has(j, '"daytime":0.25') and has(j, '"darkness":0.2') and has(j, '"solar_multiplier":3'), "day/night and solar multiplier")
  check(has(j, '"pollution":187771.506'), "pollution rounded to 3 decimals")
  check(has(j, '"pollution":0') and not has(j, '"platform-1":{"index":5,"is_platform":true,"pollution_produced_total"'), "pollutant-free surface keeps pollution 0 without stats")
  check(has(j, '"nutrients":-2'), "negative logistic counts pass through")
  check(has(j, '"iron-plate:rare":200'), "quality breakdown of logistics")
  -- platforms
  check(has(j, '"platforms":{"platform-1":{'), "platforms keyed by surface name")
  check(has(j, '"name":"Hauler"') and has(j, '"state":"on_the_path"') and has(j, '"completed_trips":7'), "platform fields")
  check(has(j, '"destination":"vulcanus"') and has(j, '"route":"nauvis>vulcanus"'), "platform destination and route")
  check(has(j, '"paused":false') and has(j, '"damaged_tiles":3') and has(j, '"last_location":"nauvis"'), "platform paused, damage, last location")
  check(not has(j, '"location"'), "no location while travelling")
  check(has(j, '"is_platform":true'), "platform surface flagged")
  -- research
  check(has(j, '"current":"logistics-2"') and has(j, '"progress":0.62') and has(j, '"current_units":200'), "current research")
  check(has(j, '"current_unit_energy":1800') and has(j, '"current_ingredients":{"automation-science-pack":1,"logistic-science-pack":1}'), "research unit energy and ingredients")
  check(has(j, '"enabled":true') and has(j, '"lab_speed_modifier":0.5') and has(j, '"labs":{"nauvis":12}'), "research enabled, lab speed, labs per surface")
  check(has(j, '"queue":["logistics-2","steel-processing","mining-productivity","mining-productivity"]'), "research queue as array")
  check(has(j, '"queue_details":[{"level":1,"name":"logistics-2"},{"level":1,"name":"steel-processing"},{"level":4,"name":"mining-productivity"},{"level":5,"name":"mining-productivity"}]'), "queue details with levels for repeated infinite techs")
  check(has(j, '"technologies_researched":1') and has(j, '"technologies_total":5') and has(j, '"technologies_available":3') and has(j, '"technologies_infinite":1'), "technology counts")
  check(not has(j, 'eta_seconds') and not has(j, 'progress_per_minute'), "no ETA on the first export")
  check(has(j, '"science_per_minute":120') and has(j, '"science_per_minute_10m":118') and has(j, '"science_total":7000'), "effective science rate from the hidden science item")
  check(has(j, '"science":7000'), "science pseudo-item still in production totals")
  check(has(j, '"space-science-pack":{"consumed_per_minute":0'), "pack recognised via used_by_labs")
  -- kills, production, builds
  check(has(j, '"kills":{"nauvis":{"kills_per_minute":3.5,"kills_total":{"medium-biter":20,"small-biter":500},"losses_per_minute":0.25,"losses_total":{"gun-turret":2}}}'), "kills and losses")
  check(not has(j, '"builds"'), "build counts off by default")
  check(has(j, '"items_produced_total_by_quality":{"normal":{'), "production by quality")
  check(has(j, '"evolution":{"nauvis":0.14}'), "evolution only for non-platform surfaces")
  check(has(j, '"evolution_by":{"nauvis":{"pollution":0.1,"spawners":0.02,"time":0.02}}'), "evolution by driver")
  check(not has(j, '"neutral"'), "forces without players omitted")
  check(not has(j, 'null'), "no nulls in a normal snapshot")
  -- command and remote paths
  M.commands.wiretap({name = "wiretap", tick = g.tick})
  check(#M.rcon_out == 1 and #M.rcon_out[1] > 1000, "/wiretap prints JSON to rcon")
  check(#M.print_out == 0, "long JSON is not echoed to stdout")
  M.commands.wiretap({name = "wiretap", tick = g.tick, parameter = "write"})
  check(#M.files == 2 and has(M.rcon_out[2], "wrote script-output/wiretap/stats.json"), "/wiretap write exports and confirms")
  check(M.print_out[1] == M.rcon_out[2], "short confirmation echoed to stdout")
  M.commands.wiretap({name = "wiretap", tick = g.tick, player_index = 1})
  check(has(M.print_out[#M.print_out], "player.print: {"), "player invocation prints to the player")
  local r = M.remote_interfaces.wiretap
  check(type(r.stats()) == "table" and type(r.stats_json()) == "string" and r.version() == "1.1.0", "remote interface")
  check(r.write_now() == true and #M.files == 3, "remote write_now exports and returns true")
  check(storage.research == nil, "research sample is not game state (no storage write from the spread job)")
end

-- 2. brownout minimum and research ETA across exports -------------------------
do
  local s = defaults(); s["wiretap-spread"] = false
  local g, refs = boot{settings = s}
  export()
  refs.netA.flow_last_tick.consumption_satisfaction = 0.4
  sample()
  refs.netA.flow_last_tick.consumption_satisfaction = 0.95
  sample()
  g.tick = g.tick + 600
  refs.player.research_progress = 0.63
  local j = export().data
  check(has(j, '"consumption_satisfaction_min":0.4'), "minimum satisfaction since the previous export")
  check(has(j, '"consumption_satisfaction":0.95'), "instantaneous satisfaction unchanged")
  check(has(j, '"progress_per_minute":0.06') and has(j, '"eta_seconds":370'), "research rate and ETA from two samples")
  -- a different technology resets the ETA
  g.tick = g.tick + 600
  refs.player.current_research = refs.techs["steel-processing"]
  refs.player.research_progress = 0.1
  j = export().data
  check(not has(j, 'eta_seconds'), "no ETA right after switching research")
  -- the RCON path must not consume the sample window
  g.tick = g.tick + 600
  refs.player.research_progress = 0.2
  local via_rcon = M.remote_interfaces.wiretap.stats_json()
  check(has(via_rcon, '"eta_seconds":80') and has(via_rcon, '"progress_per_minute":0.6'), "remote stats computes the ETA from the last timer sample")
  g.tick = g.tick + 600
  refs.player.research_progress = 0.3
  via_rcon = M.remote_interfaces.wiretap.stats_json()
  check(has(via_rcon, '"progress_per_minute":0.6'), "remote stats does not move the sample window (rate still measured from the timer export)")
  -- sampler ignores a setting that is off
  M.settings["wiretap-power-flow"] = false
  refs.netA.flow_last_tick.consumption_satisfaction = 0.1
  sample()
  M.settings["wiretap-power-flow"] = true
  refs.netA.flow_last_tick.consumption_satisfaction = 0.95
  j = export().data
  check(not has(j, '"consumption_satisfaction_min":0.1'), "sampler idle while power flow is disabled")
end

-- 3. interval change re-registers; other setting changes do not ------------
do
  boot()
  M.settings["wiretap-interval-seconds"] = 30
  M.handlers.events[defines.events.on_runtime_mod_setting_changed]({setting = "wiretap-interval-seconds", setting_type = "runtime-global"})
  local _, t = export_handler()
  check(t == 1800 and storage.interval_ticks == 1800, "interval change re-registers timer at 1800")
  M.handlers.events[defines.events.on_runtime_mod_setting_changed]({setting = "some-other-mod-setting", setting_type = "runtime-global"})
  check(storage.interval_ticks == 1800, "foreign setting change ignored")
  -- on_load touches only storage
  local saved_game, saved_settings = game, settings
  game = nil
  settings = setmetatable({}, {__index = function() error("settings read during on_load") end})
  M.handlers.on_load()
  _, t = export_handler()
  check(t == 1800, "on_load re-registers from storage")
  game, settings = saved_game, saved_settings
  M.handlers.on_configuration_changed({mod_changes = {}})
  check(storage.interval_ticks == 1800, "configuration change keeps interval")
  -- interval changed behind the mod's back heals on the next export
  M.settings["wiretap-interval-seconds"] = 5
  export()
  _, t = export_handler()
  check(t == 300 and storage.interval_ticks == 300, "export self-heals a drifted interval")
end

-- 4. multiplayer + server-only -> for_player 0; journal + both modes ---------
do
  boot{multiplayer = true}
  local f = export()
  check(f.for_player == 0, "multiplayer with server-only writes for player 0")
  M.settings["wiretap-server-only"] = false
  f = export()
  check(f.for_player == nil, "server-only off writes everywhere")
  M.settings["wiretap-mode"] = "journal"
  f = export()
  check(f.filename == "wiretap/stats.ndjson" and f.append == true and f.data:sub(-1) == "\n", "journal appends one line")
  M.settings["wiretap-mode"] = "both"
  local before = #M.files
  export()
  check(#M.files == before + 2, "both mode writes two files")
end

-- 5. settings toggles prune sections -----------------------------------------
do
  local s = defaults()
  s["wiretap-logistics"] = false; s["wiretap-power"] = false; s["wiretap-production"] = false
  s["wiretap-science"] = false; s["wiretap-pollution"] = false; s["wiretap-platforms"] = false; s["wiretap-kills"] = false
  boot{settings = s}
  local j = export().data
  for _, k in ipairs{'"logistics":{', '"power":{', '"production":{', '"science_packs":{', '"pollutant":"pollution","pollution":', '"platforms":{', '"kills":{', 'science_per_minute'} do
    check(not has(j, k), k .. " omitted when disabled")
  end
  check(has(j, '"research":{'), "research always exported")
  s = defaults(); s["wiretap-power-flow"] = false; s["wiretap-power-breakdown"] = false; s["wiretap-production-quality"] = false
  s["wiretap-quality-breakdown"] = false; s["wiretap-build-counts"] = true
  boot{settings = s}
  j = export().data
  check(not has(j, '"flow"') and not has(j, 'consumption_satisfaction') and not has(j, '"count_by_entity"'), "flow and breakdown omitted when disabled")
  check(has(j, '"accumulator_charge_joules":25000000'), "accumulators still reported without flow section")
  check(not has(j, 'by_quality'), "quality breakdowns omitted when disabled")
  check(has(j, '"builds":{"nauvis":{"built_total":{"transport-belt":4000},"mined_total":{"transport-belt":300}}}'), "build counts when enabled")
end

-- 6. encoder edge cases -------------------------------------------------------
do
  local weird = 'we"ird\\name\n\tx\1'
  boot{mutate = function(g, refs)
    -- an extra surface with a hostile name, absurd pollution and an infinite total
    g.surfaces[weird] = M.surface{name = weird, index = 9, pollution = 1e21, pollutant_type = "pollution",
      pstats = M.flow_stats({boiler = {total = math.huge}}, {})}
    refs.netB.statistics = M.flow_stats({}, {["solar-panel"] = {total = 5e6, flow = {default = 0 / 0}}}) -- NaN flow
    refs.player.research_progress = -0.0
    g.tick = 2 ^ 53 + 2
    g.ticks_played = g.tick
  end}
  local j = export().data
  check(has(j, '"we\\"ird\\\\name\\n\\tx\\u0001"'), "strings escaped")
  check(has(j, '"solar-panel":null'), "NaN encodes as null")
  check(has(j, '"progress":0'), "negative zero encodes as 0")
  check(has(j, '"tick":9007199254740994'), "large integers printed in full")
  check(has(j, '"pollution":1000000000000000000000'), "huge integral float printed without exponent")
  check(has(j, '"boiler":null'), "infinity encodes as null")
  M.settings["wiretap-decimals"] = 1
  j = export().data
  check(has(j, '"pollution":187771.5'), "decimals=1 rounds large values to one place")
  check(has(j, '"consumption_satisfaction":0.95') and has(j, '"nauvis":{"pollution":0.1,"spawners":0.02,"time":0.02}'), "fractions keep three decimals at low precision")
  check(has(j, '"count_by_entity":{"assembling-machine-2":45,"lab":12,"solar-panel":300,"steam-engine":20}'), "integers untouched by precision")
  M.settings["wiretap-decimals"] = 6
  j = export().data
  check(has(j, '"pollution":187771.506'), "decimals=6 trims trailing zeros")
end

-- 7. export failure is logged once, reported to callers ----------------------
do
  boot{mutate = function(g, refs) refs.player.get_item_production_statistics = function() error("boom") end end}
  local h, tick = export_handler()
  local ok = pcall(h, {tick = game.tick, nth_tick = tick})
  drive_ticks()
  check(ok and #M.log_out == 1 and has(M.log_out[1], "[wiretap] export failed") and has(M.log_out[1], "boom"), "export failure logged")
  check(#M.files == 0, "nothing written on failure")
  check(M.handlers.events[defines.events.on_tick] == nil, "failed job unregisters its tick handler")
  pcall(h, {tick = game.tick, nth_tick = tick})
  drive_ticks()
  check(#M.log_out == 1, "repeated identical failure logged once")
  M.commands.wiretap({name = "wiretap", tick = game.tick, parameter = "write"})
  check(has(M.rcon_out[#M.rcon_out], "[wiretap] export failed") and has(M.rcon_out[#M.rcon_out], "boom"), "/wiretap write reports the failure")
  local ok2, err2 = M.remote_interfaces.wiretap.write_now()
  check(ok2 == false and has(err2, "boom"), "remote write_now returns false and the error")
  M.commands.wiretap({name = "wiretap", tick = game.tick})
  check(has(M.rcon_out[#M.rcon_out], "[wiretap] error:"), "command reports errors to caller")
end

-- 8. spread export mechanics -----------------------------------------------------
do
  local g, refs = boot()
  local h, tick = export_handler()
  check(M.handlers.events[defines.events.on_tick] == nil, "no tick handler before an export")
  check(h({tick = g.tick, nth_tick = tick}) == true, "timer export starts a job")
  check(#M.files == 0 and M.handlers.events[defines.events.on_tick] ~= nil, "nothing written yet; tick handler registered")
  local again, why = h({tick = g.tick, nth_tick = tick})
  check(again == false and has(why, "still running"), "a second timer tick during a job is refused")
  -- first step: surface 1; then delete the other surface before its step
  g.tick = g.tick + 1
  M.handlers.events[defines.events.on_tick]({tick = g.tick})
  g.surfaces["platform-1"] = nil
  local steps = drive_ticks()
  check(#M.files == 1 and M.handlers.events[defines.events.on_tick] == nil, "file written when the job finishes; handler gone")
  local j = M.files[1].data
  check(has(j, '"nauvis":{') and not has(j, '"platform-1":{"index"'), "surface deleted mid-job is skipped, the rest is exported")
  check(has(j, '"export_ticks":' .. (steps + 1)), "export_ticks counts the ticks the job took")
  check(#M.log_out == 0, "no errors")
  for key in pairs(storage) do
    check(key == "interval_ticks", "spread job writes nothing to storage except the interval mirror (found " .. tostring(key) .. ")")
  end
  -- commands and remote stay synchronous
  M.commands.wiretap({name = "wiretap", tick = g.tick, parameter = "write"})
  check(#M.files == 2 and M.handlers.events[defines.events.on_tick] == nil, "/wiretap write exports immediately")
  check(M.remote_interfaces.wiretap.write_now() == true and #M.files == 3, "remote write_now exports immediately")
  -- spread off: the timer writes in the same tick
  local s = defaults(); s["wiretap-spread"] = false
  boot{settings = s}
  h, tick = export_handler()
  check(h({tick = game.tick, nth_tick = tick}) == true and #M.files == 1, "spread off writes immediately")
  check(M.handlers.events[defines.events.on_tick] == nil, "spread off registers no tick handler")
  check(has(M.files[1].data, '"export_ticks":0'), "single-tick export reports zero spread")
end

io.write("test_control.lua: ", passed, " checks passed\n")
