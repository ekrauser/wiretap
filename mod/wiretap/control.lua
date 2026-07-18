-- wiretap — export game vitals to script-output as JSON so external tools
-- (Home Assistant, Prometheus, MQTT bridges, microcontrollers) can monitor
-- the factory.
--
-- Factorio 2.0 API facts this file relies on:
--  * helpers.write_file targets the script-output folder; for_player = 0
--    restricts the write to the server.
--  * Electric network statistics mirror GUI column positions: category
--    "input" is CONSUMPTION, "output" is PRODUCTION, "storage" is
--    accumulator charge. Flow values are normalized per tick, so
--    watts = flow * 60.
--  * Item/fluid production statistics are the other way around: "input" is
--    production, "output" is consumption.
--  * LuaLogisticNetwork.get_contents() returns an array of
--    {name=..., quality=..., count=...}.
--  * electric_network_statistics is only readable on electric poles, so
--    networks are enumerated by scanning poles and deduping on
--    electric_network_id.
--  * on_load may only read `storage`, so the export interval is mirrored
--    into storage for timer re-registration on load.

local MOD_VERSION = "1.0.0"
local SETTING_PREFIX = "wiretap-"
local PRECISION = defines.flow_precision_index.five_seconds

local function cfg(name)
  return settings.global[SETTING_PREFIX .. name].value
end

-- helpers.table_to_json serializes {} ambiguously; omit empty tables instead.
local function nonempty(t)
  if next(t) == nil then return nil end
  return t
end

-- --------------------------------------------------------------------------
-- Power: one entry per electric network, plus per-surface totals.
-- --------------------------------------------------------------------------

local function collect_power(surface, include_breakdown, include_accumulators)
  local reps = {} -- network id -> representative pole
  for _, pole in pairs(surface.find_entities_filtered{type = "electric-pole"}) do
    local id = pole.electric_network_id
    if id and not reps[id] then reps[id] = pole end
  end

  local totals = {
    production_watts = 0,
    consumption_watts = 0,
    accumulator_charge_joules = 0,
    accumulator_capacity_joules = 0,
    network_count = 0,
  }
  local networks = {}

  for id, pole in pairs(reps) do
    local stats = pole.electric_network_statistics
    local production, consumption = 0, 0
    local prod_by, cons_by = {}, {}
    for name in pairs(stats.output_counts) do
      local watts = stats.get_flow_count{
        name = name, category = "output", precision_index = PRECISION,
      } * 60
      production = production + watts
      if include_breakdown then prod_by[name] = watts end
    end
    for name in pairs(stats.input_counts) do
      local watts = stats.get_flow_count{
        name = name, category = "input", precision_index = PRECISION,
      } * 60
      consumption = consumption + watts
      if include_breakdown then cons_by[name] = watts end
    end

    local net = {
      production_watts = production,
      consumption_watts = consumption,
      accumulator_charge_joules = 0,
      accumulator_capacity_joules = 0,
    }
    if include_breakdown then
      net.production_by_entity = nonempty(prod_by)
      net.consumption_by_entity = nonempty(cons_by)
    end
    networks[tostring(id)] = net

    totals.production_watts = totals.production_watts + production
    totals.consumption_watts = totals.consumption_watts + consumption
    totals.network_count = totals.network_count + 1
  end

  if include_accumulators then
    for _, acc in pairs(surface.find_entities_filtered{type = "accumulator"}) do
      local charge = acc.energy
      local capacity = acc.electric_buffer_size or 0
      totals.accumulator_charge_joules = totals.accumulator_charge_joules + charge
      totals.accumulator_capacity_joules = totals.accumulator_capacity_joules + capacity
      local id = acc.electric_network_id
      local net = id and networks[tostring(id)]
      if net then
        net.accumulator_charge_joules = net.accumulator_charge_joules + charge
        net.accumulator_capacity_joules = net.accumulator_capacity_joules + capacity
      end
    end
  end

  return {totals = totals, networks = nonempty(networks)}
end

-- --------------------------------------------------------------------------
-- Logistics: aggregated item and robot counts per force, per surface.
-- --------------------------------------------------------------------------

local function collect_logistics(surface, quality_breakdown)
  local out = {}
  for _, force in pairs(game.forces) do
    local nets = force.logistic_networks[surface.name]
    if nets and #nets > 0 then
      local items, by_quality = {}, {}
      local robots = {
        logistic_total = 0,
        logistic_available = 0,
        construction_total = 0,
        construction_available = 0,
      }
      for _, net in pairs(nets) do
        for _, entry in pairs(net.get_contents()) do
          items[entry.name] = (items[entry.name] or 0) + entry.count
          if quality_breakdown then
            local key = entry.name .. ":" .. (entry.quality or "normal")
            by_quality[key] = (by_quality[key] or 0) + entry.count
          end
        end
        robots.logistic_total = robots.logistic_total + net.all_logistic_robots
        robots.logistic_available = robots.logistic_available + net.available_logistic_robots
        robots.construction_total = robots.construction_total + net.all_construction_robots
        robots.construction_available = robots.construction_available + net.available_construction_robots
      end
      local entry = {
        network_count = #nets,
        robots = robots,
        items = nonempty(items),
      }
      if quality_breakdown then
        entry.items_by_quality = nonempty(by_quality)
      end
      out[force.name] = entry
    end
  end
  return nonempty(out)
end

-- --------------------------------------------------------------------------
-- Science: per-pack production/consumption rates and totals, per surface.
-- Science packs are recognized by the "*science-pack*" naming convention
-- (all vanilla and Space Age packs follow it, as do most mods).
-- --------------------------------------------------------------------------

local PRECISION_1M = defines.flow_precision_index.one_minute
local PRECISION_10M = defines.flow_precision_index.ten_minutes

local function collect_science_packs(force)
  local by_surface = {}
  for _, surface in pairs(game.surfaces) do
    local istats = force.get_item_production_statistics(surface)
    local packs = {}
    -- Item stats: "input" is production, "output" is consumption; flows are
    -- normalized per minute, so get_flow_count is already items/minute.
    for name, total in pairs(istats.input_counts) do
      if name:find("science%-pack") then
        packs[name] = {produced_total = total}
      end
    end
    for name, total in pairs(istats.output_counts) do
      if name:find("science%-pack") then
        packs[name] = packs[name] or {}
        packs[name].consumed_total = total
      end
    end
    for name, pack in pairs(packs) do
      pack.produced_per_minute = istats.get_flow_count{
        name = name, category = "input", precision_index = PRECISION_1M,
      }
      pack.consumed_per_minute = istats.get_flow_count{
        name = name, category = "output", precision_index = PRECISION_1M,
      }
      pack.produced_per_minute_10m = istats.get_flow_count{
        name = name, category = "input", precision_index = PRECISION_10M,
      }
      pack.consumed_per_minute_10m = istats.get_flow_count{
        name = name, category = "output", precision_index = PRECISION_10M,
      }
    end
    if next(packs) ~= nil then
      by_surface[surface.name] = packs
    end
  end
  return nonempty(by_surface)
end

-- --------------------------------------------------------------------------
-- Forces: research, rockets, production totals; evolution for the enemy.
-- --------------------------------------------------------------------------

local function collect_forces(include_production, include_science)
  local out = {}
  for _, force in pairs(game.forces) do
    if force.name == "player" or #force.players > 0 then
      local entry = {
        rockets_launched = force.rockets_launched,
        items_launched = nonempty(force.items_launched),
      }
      local research = {}
      local current = force.current_research
      if current then
        research.current = current.name
        research.progress = force.research_progress
        research.current_level = current.level
      end
      local previous = force.previous_research
      if previous then
        research.previous = previous.name
      end
      local queue = {}
      for _, tech in pairs(force.research_queue) do
        queue[#queue + 1] = tech.name
      end
      research.queue = nonempty(queue)
      local researched, total = 0, 0
      for _, tech in pairs(force.technologies) do
        total = total + 1
        if tech.researched then researched = researched + 1 end
      end
      research.technologies_researched = researched
      research.technologies_total = total
      entry.research = research
      if include_science then
        entry.science_packs = collect_science_packs(force)
      end
      if include_production then
        local production = {}
        for _, surface in pairs(game.surfaces) do
          local istats = force.get_item_production_statistics(surface)
          local fstats = force.get_fluid_production_statistics(surface)
          local p = {
            items_produced_total = nonempty(istats.input_counts),
            items_consumed_total = nonempty(istats.output_counts),
            fluids_produced_total = nonempty(fstats.input_counts),
            fluids_consumed_total = nonempty(fstats.output_counts),
          }
          if next(p) ~= nil then production[surface.name] = p end
        end
        entry.production = nonempty(production)
      end
      out[force.name] = entry
    elseif force.name == "enemy" then
      local evolution = {}
      for _, surface in pairs(game.surfaces) do
        evolution[surface.name] = force.get_evolution_factor(surface)
      end
      out[force.name] = {evolution = evolution}
    end
  end
  return nonempty(out)
end

-- --------------------------------------------------------------------------
-- Snapshot assembly and export.
-- --------------------------------------------------------------------------

local function build_snapshot()
  local include_logistics = cfg("logistics")
  local quality_breakdown = cfg("quality-breakdown")
  local include_power = cfg("power")
  local power_breakdown = cfg("power-breakdown")
  local include_accumulators = cfg("accumulators")
  local include_production = cfg("production")
  local include_science = cfg("science")
  local include_pollution = cfg("pollution")

  local players = {}
  for _, player in pairs(game.connected_players) do
    players[#players + 1] = player.name
  end

  local surfaces = {}
  for _, surface in pairs(game.surfaces) do
    local s = {
      index = surface.index,
      is_platform = surface.platform ~= nil,
    }
    if include_pollution then
      s.pollution = surface.get_total_pollution()
      local pstats = surface.pollution_statistics
      s.pollution_produced_total = nonempty(pstats.input_counts)
      s.pollution_absorbed_total = nonempty(pstats.output_counts)
    end
    if include_power then
      s.power = collect_power(surface, power_breakdown, include_accumulators)
    end
    if include_logistics then
      s.logistics = collect_logistics(surface, quality_breakdown)
    end
    surfaces[surface.name] = s
  end

  return {
    meta = {
      mod_version = MOD_VERSION,
      tick = game.tick,
      ticks_played = game.ticks_played,
      game_time_seconds = math.floor(game.tick / 60),
      player_count = #game.connected_players,
      players_online = nonempty(players),
      interval_seconds = cfg("interval-seconds"),
    },
    surfaces = surfaces,
    forces = collect_forces(include_production, include_science),
  }
end

local function do_export()
  local ok, err = pcall(function()
    local json = helpers.table_to_json(build_snapshot())
    local target = cfg("server-only") and 0 or nil
    local mode = cfg("mode")
    if mode == "snapshot" or mode == "both" then
      helpers.write_file(cfg("filename"), json, false, target)
    end
    if mode == "journal" or mode == "both" then
      helpers.write_file(cfg("journal-filename"), json .. "\n", true, target)
    end
  end)
  if not ok then
    log("[wiretap] export failed: " .. tostring(err))
  end
end

-- --------------------------------------------------------------------------
-- Timer wiring. on_load may only read `storage`, so the interval is
-- mirrored there whenever it can be read from settings (game context).
-- --------------------------------------------------------------------------

local function refresh_interval()
  storage.interval_ticks = math.max(1, cfg("interval-seconds")) * 60
end

local function register_timer()
  script.on_nth_tick(nil)
  script.on_nth_tick(storage.interval_ticks or 600, do_export)
end

script.on_init(function()
  refresh_interval()
  register_timer()
end)

script.on_load(register_timer)

script.on_configuration_changed(function()
  refresh_interval()
  register_timer()
end)

script.on_event(defines.events.on_runtime_mod_setting_changed, function(event)
  if event.setting and event.setting:sub(1, #SETTING_PREFIX) == SETTING_PREFIX then
    refresh_interval()
    register_timer()
  end
end)

-- --------------------------------------------------------------------------
-- Live access: remote interface (for other mods / RCON) and a command.
--   RCON:  /wiretap        -> prints the JSON snapshot to the caller
--          /wiretap write  -> forces a file export now
-- --------------------------------------------------------------------------

remote.add_interface("wiretap", {
  stats = build_snapshot,
  stats_json = function() return helpers.table_to_json(build_snapshot()) end,
  write_now = do_export,
  version = function() return MOD_VERSION end,
})

commands.add_command(
  "wiretap",
  "Print current stats as JSON; '/wiretap write' forces a file export.",
  function(cmd)
    local function reply(text)
      if cmd.player_index then
        local player = game.players[cmd.player_index]
        if player then player.print(text) end
      else
        -- player_index is nil for both RCON and the server's stdin console,
        -- and rcon.print only reaches an RCON caller. Echo short messages to
        -- stdout too so console invocations aren't silent; the full JSON dump
        -- stays RCON-only to avoid spamming the server log on every poll.
        rcon.print(text)
        if #text <= 512 then print(text) end
      end
    end
    local ok, err = pcall(function()
      if cmd.parameter == "write" then
        do_export()
        local mode = cfg("mode")
        local written = {}
        if mode == "snapshot" or mode == "both" then
          written[#written + 1] = "script-output/" .. cfg("filename")
        end
        if mode == "journal" or mode == "both" then
          written[#written + 1] = "script-output/" .. cfg("journal-filename")
        end
        reply("[wiretap] wrote " .. table.concat(written, " and "))
      else
        reply(helpers.table_to_json(build_snapshot()))
      end
    end)
    if not ok then
      reply("[wiretap] error: " .. tostring(err))
    end
  end)
