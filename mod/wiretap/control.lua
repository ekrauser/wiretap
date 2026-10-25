-- wiretap — export game vitals to script-output as JSON so external tools
-- (Home Assistant, Prometheus, MQTT bridges, ESPHome devices) can monitor the
-- factory.
--
-- Factorio 2.1 API facts this file relies on (checked against the 2.1.23
-- runtime-api.json shipped with the game):
--  * helpers.write_file targets the script-output folder; for_player = 0
--    restricts the write to the server's output "if present" — in single
--    player there is no server and the write is silently skipped, so 0 is
--    only passed when game.is_multiplayer().
--  * Electricity: a LuaElectricNetwork is the unit that moves power. It groups
--    one or more LuaElectricSubNetworks (groups of poles joined by copper
--    wire); sub networks are joined into one network by closed power switches
--    or by a surface's global network. Statistics, last-tick flow and
--    accumulator totals live on the network; ids live on the sub networks.
--    Poles expose their sub network as LuaEntity.electric_network, a global
--    network as LuaSurface.global_electric_network. Reading statistics per
--    pole id (the 2.0 way) counts a switch-joined network once per sub
--    network, so networks are grouped by parent. A network that contains the
--    surface's global sub network is keyed "global" (as in 1.0.x); any other
--    network is keyed by its lowest sub network id.
--  * Electric network statistics mirror GUI column positions: category
--    "input" is CONSUMPTION, "output" is PRODUCTION. Flow values are per
--    tick, so watts = flow * 60. flow_last_tick values are joules moved in the
--    last update, also * 60 for watts.
--  * Item/fluid production statistics are the other way around: "input" is
--    production, "output" is consumption; flows are per minute. Kill
--    statistics: "input" is kills, "output" is losses. Build statistics:
--    "input" is built, "output" is mined.
--  * LuaLogisticNetwork.get_contents() returns an array of
--    {name=..., quality=..., count=...}; quality is "normal" when the quality
--    mod is disabled.
--  * helpers.table_to_json prints lossy, over-long decimal expansions for
--    most fractional doubles (open engine issue), so the snapshot is encoded
--    here with explicit number formatting.
--  * on_load may only read `storage`, so the export interval is mirrored
--    into storage for timer re-registration on load.
--  * Everything here runs on every client in multiplayer. The export only
--    reads game state (optionally one surface or force per tick). The only
--    `storage` write is the interval mirror, done from synchronized events.
--    The research sample behind the ETA and the brownout sampler keep local
--    Lua state only: the spread export runs in an on_tick handler that a
--    client joining mid-export does not have, so a storage write there would
--    desync.

local MOD_VERSION = script.active_mods[script.mod_name] or "unknown"
local SETTING_PREFIX = "wiretap-"
local PRECISION = defines.flow_precision_index.five_seconds
local PRECISION_1M = defines.flow_precision_index.one_minute
local PRECISION_10M = defines.flow_precision_index.ten_minutes
local SAMPLE_TICKS = 60 -- brownout sampler cadence

local function cfg(name)
  return settings.global[SETTING_PREFIX .. name].value
end

-- Empty tables are omitted from the snapshot (they would be ambiguous
-- between [] and {} to consumers).
local function nonempty(t)
  if next(t) == nil then return nil end
  return t
end

-- --------------------------------------------------------------------------
-- JSON encoder with controlled number formatting.
-- Integral numbers are printed as integers, fractional ones with the
-- configured number of decimals (trailing zeros trimmed); fractions below 1
-- (progress, evolution, satisfaction) always keep at least three decimals so
-- a low precision setting cannot flatten them to 0 or 1. Non-finite numbers
-- become null. Object keys are sorted (numerically where both keys are
-- numbers) so output is stable across exports.
-- --------------------------------------------------------------------------

local ESCAPES = {
  ['"'] = '\\"', ['\\'] = '\\\\', ['\b'] = '\\b', ['\f'] = '\\f',
  ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t',
}

local function encode_string(s)
  local escaped = s:gsub('[%c"\\]', function(c)
    return ESCAPES[c] or string.format("\\u%04x", c:byte())
  end)
  return '"' .. escaped .. '"'
end

local number_format = "%.3f"
local fraction_format = "%.3f"

local function trim_zeros(s)
  if s:find(".", 1, true) then
    s = s:gsub("0+$", "")
    s = s:gsub("%.$", "")
  end
  if s == "-0" or s == "" then return "0" end
  return s
end

local function encode_number(v)
  if v ~= v or v == math.huge or v == -math.huge then return "null" end
  if v % 1 == 0 then
    if v == 0 then return "0" end -- also normalizes -0
    return string.format("%.0f", v)
  end
  if v > -1 and v < 1 then
    return trim_zeros(string.format(fraction_format, v))
  end
  return trim_zeros(string.format(number_format, v))
end

local function is_array(t)
  local n = #t
  if n == 0 then return false end
  for k in pairs(t) do
    if type(k) ~= "number" or k % 1 ~= 0 or k < 1 or k > n then return false end
  end
  return true
end

local function key_less(a, b)
  local na, nb = tonumber(a), tonumber(b)
  if na and nb then return na < nb end
  return tostring(a) < tostring(b)
end

local function encode(v, buf)
  local tv = type(v)
  if tv == "string" then
    buf[#buf + 1] = encode_string(v)
  elseif tv == "number" then
    buf[#buf + 1] = encode_number(v)
  elseif tv == "boolean" then
    buf[#buf + 1] = v and "true" or "false"
  elseif tv == "nil" then
    buf[#buf + 1] = "null"
  elseif tv == "table" then
    if is_array(v) then
      buf[#buf + 1] = "["
      for i = 1, #v do
        if i > 1 then buf[#buf + 1] = "," end
        encode(v[i], buf)
      end
      buf[#buf + 1] = "]"
    else
      local keys = {}
      for k in pairs(v) do keys[#keys + 1] = k end
      table.sort(keys, key_less)
      buf[#buf + 1] = "{"
      for i, k in ipairs(keys) do
        if i > 1 then buf[#buf + 1] = "," end
        buf[#buf + 1] = encode_string(tostring(k))
        buf[#buf + 1] = ":"
        encode(v[k], buf)
      end
      buf[#buf + 1] = "}"
    end
  else
    error("wiretap: cannot encode value of type " .. tv)
  end
end

local function to_json(t)
  local decimals = cfg("decimals")
  if type(decimals) ~= "number" or decimals < 0 or decimals > 12 then decimals = 3 end
  decimals = math.floor(decimals)
  number_format = "%." .. decimals .. "f"
  fraction_format = "%." .. math.max(decimals, 3) .. "f"
  local buf = {}
  encode(t, buf)
  return table.concat(buf)
end

-- --------------------------------------------------------------------------
-- Science packs: every item some lab accepts (2.1 made packs plain items, so
-- the lab inputs are the authoritative list); the classic name pattern is
-- kept as a fallback for odd prototype setups.
-- --------------------------------------------------------------------------

local LAB_ITEMS = {}
pcall(function()
  for name, item in pairs(prototypes.item) do
    if item.used_by_labs then LAB_ITEMS[name] = true end
  end
end)

local function is_science_pack(name)
  return LAB_ITEMS[name] == true or name:find("science%-pack") ~= nil
end

local function entity_prototype_exists(name)
  -- Statistics remember prototype names of removed mods forever; counting
  -- such a name would raise and kill the export.
  return prototypes.entity[name] ~= nil
end

-- --------------------------------------------------------------------------
-- Brownout sampler: between exports, the networks found by the last export
-- are polled every second for their last-tick consumption satisfaction and
-- the minimum is kept, so a short brownout between two exports still shows
-- up. Local state only (never written to storage); rebuilt by the next
-- export after a load.
-- --------------------------------------------------------------------------

local sampled = {}       -- surface index -> array of {key=, net=}
local sampled_min = {}   -- surface index -> key -> minimum satisfaction seen

local function sample_satisfaction()
  for surface_index, list in pairs(sampled) do
    local mins = sampled_min[surface_index]
    if not mins then mins = {}; sampled_min[surface_index] = mins end
    for _, rec in pairs(list) do
      if rec.net.valid then
        local s = rec.net.flow_last_tick.consumption_satisfaction
        local m = mins[rec.key]
        if m == nil or s < m then mins[rec.key] = s end
      end
    end
  end
end

local function reset_samples(surface_index, list)
  sampled[surface_index] = list
  sampled_min[surface_index] = {}
end

-- --------------------------------------------------------------------------
-- Power: one entry per electric network, plus per-surface totals.
-- --------------------------------------------------------------------------

local function collect_power(surface, include_breakdown, include_flow)
  local nets = {}       -- key -> {net = LuaElectricNetwork, sub_count = n}
  local seen_sub = {}   -- sub network id -> true
  local seen_pole = {}  -- pole.electric_network_id -> true (separate id space)

  local global_sub = surface.has_global_electric_network and surface.global_electric_network or nil
  local global_id = (global_sub and global_sub.valid) and global_sub.id or nil

  local function add_sub(sub)
    if not sub or not sub.valid or seen_sub[sub.id] then return end
    seen_sub[sub.id] = true
    local parent = sub.parent_network
    if not parent or not parent.valid then return end
    -- One pass over the parent's sub networks: mark them seen (so the
    -- remaining poles of this network cost one id read each), find the
    -- lowest id for the key and count them.
    local lowest, count, is_global = nil, 0, false
    for _, s in pairs(parent.sub_networks) do
      if s.valid then
        seen_sub[s.id] = true
        count = count + 1
        if s.id == global_id then is_global = true end
        if lowest == nil or s.id < lowest then lowest = s.id end
      end
    end
    local key = is_global and "global" or (lowest and tostring(lowest) or nil)
    if key and not nets[key] then nets[key] = {net = parent, sub_count = count} end
  end

  add_sub(global_sub)
  for _, pole in pairs(surface.find_entities_filtered{type = "electric-pole"}) do
    local id = pole.electric_network_id
    if id and not seen_pole[id] then
      seen_pole[id] = true
      add_sub(pole.electric_network)
    end
  end

  local totals = {
    production_watts = 0,
    consumption_watts = 0,
    accumulator_charge_joules = 0,
    accumulator_capacity_joules = 0,
    network_count = 0,
  }
  if include_flow then
    totals.maximum_production_watts = 0
    totals.maximum_consumption_watts = 0
    totals.solar_watts = 0
    totals.accumulator_charge_watts = 0
    totals.accumulator_discharge_watts = 0
  end
  local satisfied, demanded = 0, 0
  local min_satisfaction
  local networks = {}
  local seen_names = include_breakdown and {} or nil
  local mins = sampled_min[surface.index] or {}
  local next_sampled = {}

  for key, rec in pairs(nets) do
    local net = rec.net
    local stats = net.statistics
    local production, consumption = 0, 0
    local prod_by, cons_by = {}, {}
    local has_entities = false
    for name in pairs(stats.output_counts) do
      has_entities = true
      local watts = stats.get_flow_count{
        name = name, category = "output", precision_index = PRECISION,
      } * 60
      production = production + watts
      if include_breakdown then prod_by[name] = watts; seen_names[name] = true end
    end
    for name in pairs(stats.input_counts) do
      has_entities = true
      local watts = stats.get_flow_count{
        name = name, category = "input", precision_index = PRECISION,
      } * 60
      consumption = consumption + watts
      if include_breakdown then cons_by[name] = watts; seen_names[name] = true end
    end

    -- Last-tick flow carries accumulator totals (no entity scan needed) and
    -- the satisfaction fractions the GUI's power bars are drawn from.
    local flow = net.flow_last_tick
    -- A network is dead (isolated poles) only if nothing was ever attached
    -- to it; an idle grid with machines stays listed with zeros.
    local alive = has_entities or flow.accumulator_capacity ~= 0
      or flow.maximum_production ~= 0 or flow.maximum_consumption ~= 0
    if alive then
      local entry = {
        production_watts = production,
        consumption_watts = consumption,
        accumulator_charge_joules = flow.accumulator_energy,
        accumulator_capacity_joules = flow.accumulator_capacity,
        sub_network_count = rec.sub_count,
      }
      if include_breakdown then
        entry.production_by_entity = nonempty(prod_by)
        entry.consumption_by_entity = nonempty(cons_by)
      end
      if include_flow then
        local charge_w = flow.tertiary_demand * flow.tertiary_demand_usage * 60
        local discharge_w = flow.tertiary_output * flow.tertiary_output_usage * 60
        local f = {
          consumption_satisfaction = flow.consumption_satisfaction,
          production_satisfaction = flow.production_satisfaction,
          maximum_production_watts = flow.maximum_production * 60,
          maximum_consumption_watts = flow.maximum_consumption * 60,
          solar_watts = flow.solar_output * 60,
          solar_used_watts = flow.solar_output * flow.solar_output_usage * 60,
          primary_output_watts = flow.primary_output * flow.primary_output_usage * 60,
          secondary_output_watts = flow.secondary_output * flow.secondary_output_usage * 60,
          accumulator_charge_watts = charge_w,
          accumulator_discharge_watts = discharge_w,
          transfer_watts = flow.total_transfer * 60,
        }
        local m = mins[key]
        if m ~= nil then
          if flow.consumption_satisfaction < m then m = flow.consumption_satisfaction end
          f.consumption_satisfaction_min = m
          if min_satisfaction == nil or m < min_satisfaction then min_satisfaction = m end
        end
        entry.flow = f
        totals.maximum_production_watts = totals.maximum_production_watts + flow.maximum_production * 60
        totals.maximum_consumption_watts = totals.maximum_consumption_watts + flow.maximum_consumption * 60
        totals.solar_watts = totals.solar_watts + flow.solar_output * 60
        totals.accumulator_charge_watts = totals.accumulator_charge_watts + charge_w
        totals.accumulator_discharge_watts = totals.accumulator_discharge_watts + discharge_w
        satisfied = satisfied + flow.consumption_satisfaction * flow.maximum_consumption
        demanded = demanded + flow.maximum_consumption
        next_sampled[#next_sampled + 1] = {key = key, net = net}
      end
      networks[key] = entry
      totals.production_watts = totals.production_watts + production
      totals.consumption_watts = totals.consumption_watts + consumption
      totals.accumulator_charge_joules = totals.accumulator_charge_joules + flow.accumulator_energy
      totals.accumulator_capacity_joules = totals.accumulator_capacity_joules + flow.accumulator_capacity
      totals.network_count = totals.network_count + 1
    end
  end

  if include_flow then
    -- Demand-weighted satisfaction across the surface: 1 = every consumer
    -- got what it asked for, lower = brownout somewhere.
    totals.consumption_satisfaction = demanded > 0 and (satisfied / demanded) or 1
    if min_satisfaction ~= nil then totals.consumption_satisfaction_min = min_satisfaction end
  end
  reset_samples(surface.index, next_sampled)

  local out = {totals = totals, networks = nonempty(networks)}

  -- Entity counts for the types seen in the power breakdown, so
  -- watts-per-type can be turned into utilization per machine.
  if seen_names and next(seen_names) ~= nil then
    local counts = {}
    for name in pairs(seen_names) do
      if entity_prototype_exists(name) then
        local n = surface.count_entities_filtered{name = name}
        if n > 0 then counts[name] = n end
      end
    end
    out.count_by_entity = nonempty(counts)
  end

  return out
end

-- --------------------------------------------------------------------------
-- Logistics: aggregated item and robot counts per force, per surface.
-- `networks_by_force` is force name -> (surface name -> networks), read once
-- per export because every access to LuaForce.logistic_networks copies the
-- whole dictionary.
-- --------------------------------------------------------------------------

local function collect_logistics(surface, networks_by_force, quality_breakdown)
  local out = {}
  for force_name, by_surface in pairs(networks_by_force) do
    local nets = by_surface[surface.name]
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
      out[force_name] = entry
    end
  end
  return nonempty(out)
end

-- --------------------------------------------------------------------------
-- Space platforms: one entry per platform, keyed by its surface name so it
-- can be joined with the matching `surfaces` entry (platform display names
-- need not be unique).
-- --------------------------------------------------------------------------

local STATE_NAMES = {}
for name, value in pairs(defines.space_platform_state) do STATE_NAMES[value] = name end

local function collect_platforms(force)
  local out = {}
  for _, platform in pairs(force.platforms) do
    if platform.valid then
      local surface = platform.surface
      local key = (surface and surface.valid) and surface.name or ("platform-index-" .. platform.index)
      local location = platform.space_location
      local last = platform.last_visited_space_location
      local hub = platform.hub
      local deletion = platform.scheduled_for_deletion
      local destination
      local schedule = platform.schedule
      if schedule and schedule.records and schedule.current then
        local record = schedule.records[schedule.current]
        destination = record and record.station or nil
      end
      local connection = platform.space_connection
      local route
      if connection then
        local from, to = connection.from, connection.to
        route = (from and to) and (from.name .. ">" .. to.name) or nil
      end
      out[key] = {
        name = platform.name,
        index = platform.index,
        state = STATE_NAMES[platform.state] or tostring(platform.state),
        paused = platform.paused,
        speed = platform.speed,
        location = location and location.name or nil,
        last_location = last and last.name or nil,
        destination = destination,
        route = route,
        distance = platform.distance,
        weight = platform.weight,
        completed_trips = platform.completed_trips,
        damaged_tiles = #platform.damaged_tiles,
        has_hub = (hub ~= nil and hub.valid) or false,
        deletion_in_ticks = (deletion ~= nil and deletion ~= 0) and deletion or nil,
      }
    end
  end
  return nonempty(out)
end

-- --------------------------------------------------------------------------
-- Forces: research, rockets, science, production, kills, builds, platforms;
-- evolution for enemy forces.
-- --------------------------------------------------------------------------

-- force name -> {name, level, progress, tick} from the last timer export.
-- Local state on purpose (see the header); lost on load, rebuilt by the next
-- two exports.
local research_samples = {}

local function collect_research(force, record_sample)
  local research = {
    enabled = force.research_enabled,
    lab_speed_modifier = force.laboratory_speed_modifier,
  }
  local current = force.current_research
  if current then
    local progress = force.research_progress
    research.current = current.name
    research.progress = progress
    research.current_level = current.level
    research.current_units = current.research_unit_count
    research.current_unit_energy = current.research_unit_energy
    local ingredients = {}
    for _, ingredient in pairs(current.research_unit_ingredients) do
      ingredients[ingredient.name] = ingredient.amount
    end
    research.current_ingredients = nonempty(ingredients)
    -- Rate and ETA from the previous export's sample of the same technology.
    local previous = research_samples[force.name]
    if previous and previous.name == current.name and previous.level == current.level
        and game.tick > previous.tick then
      local rate = (progress - previous.progress) * 3600 / (game.tick - previous.tick)
      if rate > 0 then
        research.progress_per_minute = rate
        research.eta_seconds = (1 - progress) / rate * 60
      end
    end
    if record_sample then
      research_samples[force.name] = {
        name = current.name, level = current.level, progress = progress, tick = game.tick,
      }
    end
  elseif record_sample then
    research_samples[force.name] = nil
  end
  local previous_tech = force.previous_research
  if previous_tech then
    research.previous = previous_tech.name
  end
  local queue, details, seen = {}, {}, {}
  for _, tech in pairs(force.research_queue) do
    local n = seen[tech.name] or 0
    seen[tech.name] = n + 1
    queue[#queue + 1] = tech.name
    details[#details + 1] = {name = tech.name, level = tech.level + n}
  end
  research.queue = nonempty(queue)
  research.queue_details = nonempty(details)
  local researched, total, available, infinite = 0, 0, 0, 0
  for _, tech in pairs(force.technologies) do
    total = total + 1
    if tech.researched then researched = researched + 1 end
    if tech.research_unit_count_formula then
      infinite = infinite + 1
    elseif tech.enabled and not tech.prototype.hidden then
      available = available + 1
    end
  end
  research.technologies_researched = researched
  research.technologies_total = total
  research.technologies_available = available
  research.technologies_infinite = infinite
  local labs = {}
  for _, surface in pairs(game.surfaces) do
    local n = surface.count_entities_filtered{type = "lab", force = force}
    if n > 0 then labs[surface.name] = n end
  end
  research.labs = nonempty(labs)
  return research
end

local function collect_force_statistics(force, opts, research)
  local science_by_surface, production_by_surface = {}, {}
  local kills_by_surface, builds_by_surface = {}, {}
  local science_total, spm, spm_10m, have_science = 0, 0, 0, false
  for _, surface in pairs(game.surfaces) do
    if opts.science or opts.production then
      local istats = force.get_item_production_statistics(surface)
      local produced, consumed = istats.input_counts, istats.output_counts
      if opts.science then
        local packs = {}
        for name, total in pairs(produced) do
          if is_science_pack(name) then packs[name] = {produced_total = total} end
        end
        for name, total in pairs(consumed) do
          if is_science_pack(name) then
            packs[name] = packs[name] or {}
            packs[name].consumed_total = total
          end
        end
        for name, pack in pairs(packs) do
          -- A pack only consumed (imported) or only produced on this surface
          -- would otherwise omit one of the totals; emit 0 so the schema is
          -- uniform for consumers.
          pack.produced_total = pack.produced_total or 0
          pack.consumed_total = pack.consumed_total or 0
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
        if next(packs) ~= nil then science_by_surface[surface.name] = packs end
        -- The hidden "science" item is the engine's own count of research
        -- units completed (lab productivity included): the true SPM.
        local sci = produced["science"]
        if sci then
          have_science = true
          science_total = science_total + sci
          spm = spm + istats.get_flow_count{
            name = "science", category = "input", precision_index = PRECISION_1M,
          }
          spm_10m = spm_10m + istats.get_flow_count{
            name = "science", category = "input", precision_index = PRECISION_10M,
          }
        end
      end
      if opts.production then
        local fstats = force.get_fluid_production_statistics(surface)
        local p = {
          items_produced_total = nonempty(produced),
          items_consumed_total = nonempty(consumed),
          fluids_produced_total = nonempty(fstats.input_counts),
          fluids_consumed_total = nonempty(fstats.output_counts),
        }
        if opts.production_quality then
          -- quality name -> item name -> count; "normal" is included so the
          -- breakdown sums to the totals above.
          p.items_produced_total_by_quality = nonempty(istats.input_quality_counts)
          p.items_consumed_total_by_quality = nonempty(istats.output_quality_counts)
        end
        if next(p) ~= nil then production_by_surface[surface.name] = p end
      end
    end
    if opts.kills then
      local kstats = force.get_kill_count_statistics(surface)
      local kills, losses = kstats.input_counts, kstats.output_counts
      if next(kills) ~= nil or next(losses) ~= nil then
        local kills_per_minute, losses_per_minute = 0, 0
        for name in pairs(kills) do
          kills_per_minute = kills_per_minute + kstats.get_flow_count{
            name = name, category = "input", precision_index = PRECISION_1M,
          }
        end
        for name in pairs(losses) do
          losses_per_minute = losses_per_minute + kstats.get_flow_count{
            name = name, category = "output", precision_index = PRECISION_1M,
          }
        end
        kills_by_surface[surface.name] = {
          kills_total = nonempty(kills),
          losses_total = nonempty(losses),
          kills_per_minute = kills_per_minute,
          losses_per_minute = losses_per_minute,
        }
      end
    end
    if opts.builds then
      local bstats = force.get_entity_build_count_statistics(surface)
      local built, mined = bstats.input_counts, bstats.output_counts
      if next(built) ~= nil or next(mined) ~= nil then
        builds_by_surface[surface.name] = {
          built_total = nonempty(built),
          mined_total = nonempty(mined),
        }
      end
    end
  end
  if have_science then
    research.science_total = science_total
    research.science_per_minute = spm
    research.science_per_minute_10m = spm_10m
  end
  return nonempty(science_by_surface), nonempty(production_by_surface),
    nonempty(kills_by_surface), nonempty(builds_by_surface)
end

local function collect_force(force, opts, record_sample)
  if force.name == "player" or #force.players > 0 then
    local entry = {
      rockets_launched = force.rockets_launched,
      items_launched = nonempty(force.items_launched),
    }
    local research = collect_research(force, record_sample)
    entry.science_packs, entry.production, entry.kills, entry.builds =
      collect_force_statistics(force, opts, research)
    entry.research = research
    if opts.platforms then
      entry.platforms = collect_platforms(force)
    end
    return entry
  elseif force.name == "enemy" then
    local evolution, by_driver = {}, {}
    for _, surface in pairs(game.surfaces) do
      if surface.platform == nil then
        evolution[surface.name] = force.get_evolution_factor(surface)
        by_driver[surface.name] = {
          pollution = force.get_evolution_factor_by_pollution(surface),
          time = force.get_evolution_factor_by_time(surface),
          spawners = force.get_evolution_factor_by_killing_spawners(surface),
        }
      end
    end
    return {evolution = nonempty(evolution), evolution_by = nonempty(by_driver)}
  end
  return nil
end

-- --------------------------------------------------------------------------
-- Snapshot assembly. The work is split into steps (one surface or one force
-- per step) so the timer export can spread it over consecutive ticks; the
-- command and remote paths run every step in one go.
-- --------------------------------------------------------------------------

local function snapshot_options()
  return {
    logistics = cfg("logistics"),
    quality_breakdown = cfg("quality-breakdown"),
    power = cfg("power"),
    power_breakdown = cfg("power-breakdown"),
    power_flow = cfg("power-flow"),
    pollution = cfg("pollution"),
    force = {
      production = cfg("production"),
      production_quality = cfg("production-quality"),
      science = cfg("science"),
      platforms = cfg("platforms"),
      kills = cfg("kills"),
      builds = cfg("build-counts"),
    },
  }
end

local function begin_snapshot(record_sample)
  local opts = snapshot_options()
  local job = {
    opts = opts,
    record_sample = record_sample,
    start_tick = game.tick,
    surfaces = {},
    forces = {},
    surface_queue = {},
    force_queue = {},
    next_surface = 1,
    next_force = 1,
  }
  for _, surface in pairs(game.surfaces) do
    job.surface_queue[#job.surface_queue + 1] = surface.name
  end
  for _, force in pairs(game.forces) do
    job.force_queue[#job.force_queue + 1] = force.name
  end
  if opts.logistics then
    -- Every access to LuaForce.logistic_networks copies the whole dictionary,
    -- so it is read once per export, not once per surface.
    job.networks_by_force = {}
    for _, force in pairs(game.forces) do
      job.networks_by_force[force.name] = force.logistic_networks
    end
  end
  local players, player_details = {}, {}
  for _, player in pairs(game.connected_players) do
    players[#players + 1] = player.name
    local surface = player.physical_surface
    player_details[player.name] = {
      afk_seconds = math.floor(player.afk_time / 60),
      online_seconds = math.floor(player.online_time / 60),
      surface = (surface and surface.valid) and surface.name or nil,
    }
  end
  job.meta = {
    mod_version = MOD_VERSION,
    game_version = helpers.game_version,
    tick = game.tick,
    ticks_played = game.ticks_played,
    game_time_seconds = math.floor(game.ticks_played / 60),
    game_speed = game.speed,
    tick_paused = game.tick_paused,
    player_count = #game.connected_players,
    players_online = nonempty(players),
    players = nonempty(player_details),
    interval_seconds = cfg("interval-seconds"),
    mods = script.active_mods,
    features = script.feature_flags,
  }
  return job
end

local function step_surface(job, surface)
  local opts = job.opts
  local planet = surface.planet
  local pollutant = surface.pollutant_type
  local s = {
    index = surface.index,
    is_platform = surface.platform ~= nil,
    planet = planet and planet.name or nil,
    pollutant = pollutant and pollutant.name or nil,
    daytime = surface.daytime,
    darkness = surface.darkness,
    solar_multiplier = surface.solar_power_multiplier,
  }
  if opts.pollution then
    if pollutant then
      s.pollution = surface.get_total_pollution()
      local pstats = surface.pollution_statistics
      s.pollution_produced_total = nonempty(pstats.input_counts)
      s.pollution_absorbed_total = nonempty(pstats.output_counts)
    else
      s.pollution = 0
    end
  end
  if opts.power then
    s.power = collect_power(surface, opts.power_breakdown, opts.power_flow)
  end
  if opts.logistics then
    s.logistics = collect_logistics(surface, job.networks_by_force, opts.quality_breakdown)
  end
  job.surfaces[surface.name] = s
end

local function step_force(job, force)
  local entry = collect_force(force, job.opts.force, job.record_sample)
  if entry then job.forces[force.name] = entry end
end

-- Runs one step; returns true when the job still has steps left.
local function step_job(job)
  if job.next_surface <= #job.surface_queue then
    local surface = game.surfaces[job.surface_queue[job.next_surface]]
    job.next_surface = job.next_surface + 1
    if surface and surface.valid then step_surface(job, surface) end
    return true
  end
  if job.next_force <= #job.force_queue then
    local force = game.forces[job.force_queue[job.next_force]]
    job.next_force = job.next_force + 1
    if force and force.valid then step_force(job, force) end
    return true
  end
  return false
end

local function finish_snapshot(job)
  job.meta.export_ticks = game.tick - job.start_tick
  return {
    meta = job.meta,
    surfaces = job.surfaces,
    forces = nonempty(job.forces),
  }
end

local function build_snapshot(record_sample)
  local job = begin_snapshot(record_sample)
  while step_job(job) do end
  return finish_snapshot(job)
end

local last_logged_error

local function refresh_interval()
  storage.interval_ticks = math.max(1, cfg("interval-seconds")) * 60
end

local register_timer -- defined below; the timer export may re-register on drift

local function report_failure(err)
  local message = "[wiretap] export failed: " .. tostring(err)
  if message ~= last_logged_error then
    log(message)
    last_logged_error = message
  end
  return false, tostring(err)
end

local function write_snapshot(snapshot)
  local json = to_json(snapshot)
  -- for_player = 0 restricts the write to the server's output "if present";
  -- in single player there is no server, so the write is silently skipped.
  -- Only honor the setting in actual multiplayer.
  local target = (cfg("server-only") and game.is_multiplayer()) and 0 or nil
  local mode = cfg("mode")
  if mode == "snapshot" or mode == "both" then
    helpers.write_file(cfg("filename"), json, false, target)
  end
  if mode == "journal" or mode == "both" then
    helpers.write_file(cfg("journal-filename"), json .. "\n", true, target)
  end
end

-- Build and write in one go (commands, remote interface, "spread" off).
local function export_now()
  local ok, err = pcall(function()
    write_snapshot(build_snapshot(true))
  end)
  if not ok then return report_failure(err) end
  last_logged_error = nil
  return true
end

-- Spread export: one surface or force per tick, then write. The job is local
-- state; a client that loads a save mid-job simply has no job and starts a
-- fresh one on the next timer tick. Nothing here touches game state, so the
-- handler being registered on some clients and not others cannot desync.
local job

local function stop_job()
  job = nil
  script.on_event(defines.events.on_tick, nil)
end

local function job_tick()
  if not job then stop_job() return end
  local ok, err = pcall(function()
    if not step_job(job) then
      local snapshot = finish_snapshot(job)
      stop_job()
      write_snapshot(snapshot)
      last_logged_error = nil
    end
  end)
  if not ok then
    stop_job()
    report_failure(err)
  end
end

local function do_export()
  -- Self-heal if another mod changed the interval without an event.
  local want = math.max(1, cfg("interval-seconds")) * 60
  if storage.interval_ticks ~= want then
    refresh_interval()
    register_timer()
  end
  if not cfg("spread") then
    return export_now()
  end
  if job then
    return false, "previous export still running"
  end
  local ok, err = pcall(function()
    job = begin_snapshot(true)
    script.on_event(defines.events.on_tick, job_tick)
  end)
  if not ok then
    stop_job()
    return report_failure(err)
  end
  return true
end

-- --------------------------------------------------------------------------
-- Timer wiring. on_load may only read `storage`, so the interval is
-- mirrored there whenever it can be read from settings (game context). The
-- brownout sampler runs every second and checks its setting at run time.
-- --------------------------------------------------------------------------

local function sampler_tick()
  if next(sampled) ~= nil and cfg("power-flow") then sample_satisfaction() end
end

register_timer = function()
  script.on_nth_tick(nil)
  script.on_nth_tick(storage.interval_ticks or 600, do_export)
  script.on_nth_tick(SAMPLE_TICKS, sampler_tick)
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
  if event.setting == SETTING_PREFIX .. "interval-seconds" then
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
  stats = function() return build_snapshot(false) end,
  stats_json = function() return to_json(build_snapshot(false)) end,
  write_now = export_now,
  version = function() return MOD_VERSION end,
})

commands.add_command(
  "wiretap",
  "Print current stats as JSON; '/wiretap write' forces a file export.",
  function(cmd)
    local function reply(text)
      if cmd.player_index then
        local player = game.get_player(cmd.player_index)
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
        local written_ok, write_err = export_now()
        if not written_ok then
          reply("[wiretap] export failed: " .. tostring(write_err))
          return
        end
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
        reply(to_json(build_snapshot(false)))
      end
    end)
    if not ok then
      reply("[wiretap] error: " .. tostring(err))
    end
  end)
