-- Mock Factorio 2.1 runtime for exercising control.lua under plain Lua 5.2.
-- Builds a fake `game` with surfaces, electric (sub)networks, logistic
-- networks, forces, platforms; records files written via helpers.write_file.
-- Every member control.lua reads must exist here with the documented shape;
-- when the mod learns a new API member, teach it to the mock too.
local M = {}

local function flow_stats(input, output, storage)
  -- input/output/storage: name -> {total=, quality=, flow={[precision]=value, default=value}}
  local st = {valid = true, object_name = "LuaFlowStatistics"}
  st.input_counts, st.output_counts, st.storage_counts = {}, {}, {}
  st.input_quality_counts, st.output_quality_counts = {}, {}
  local function fill(tbl, src, qtbl)
    for name, e in pairs(src or {}) do
      tbl[name] = e.total or 0
      local q = e.quality or "normal"
      qtbl[q] = qtbl[q] or {}
      qtbl[q][name] = e.total or 0
    end
  end
  fill(st.input_counts, input, st.input_quality_counts)
  fill(st.output_counts, output, st.output_quality_counts)
  for name, e in pairs(storage or {}) do st.storage_counts[name] = e.total or 0 end
  local src = {input = input or {}, output = output or {}, storage = storage or {}}
  function st.get_flow_count(args)
    assert(type(args) == "table", "get_flow_count takes a table")
    assert(args.category == "input" or args.category == "output" or args.category == "storage", "bad category")
    assert(args.precision_index ~= nil, "precision_index required")
    local e = src[args.category][args.name]
    if not e then return 0 end
    local f = e.flow or {}
    local v = f[args.precision_index]
    if v == nil then v = f.default end
    return v or 0
  end
  return st
end
M.flow_stats = flow_stats

-- Electric model ------------------------------------------------------------
local next_sub_id = 1
function M.electric_network(spec)
  -- spec: {subs = n (default 1), stats = flow_stats(...), acc = {energy=, capacity=}, flow = {overrides}}
  local net = {valid = true, object_name = "LuaElectricNetwork", sub_networks = {}}
  net.statistics = spec.stats or flow_stats()
  net.flow_last_tick = {
    accumulator_capacity = (spec.acc and spec.acc.capacity or 0),
    accumulator_energy = (spec.acc and spec.acc.energy or 0),
    consumption_satisfaction = 1, production_satisfaction = 0.5, lamp_satisfaction = 1,
    maximum_consumption = 0, maximum_production = 0, solar_output = 0, solar_output_usage = 1,
    primary_demand = 0, primary_demand_usage = 1, primary_output = 0, primary_output_usage = 1,
    secondary_demand = 0, secondary_demand_usage = 1, secondary_output = 0, secondary_output_usage = 1,
    tertiary_demand = 0, tertiary_demand_usage = 1, tertiary_output = 0, tertiary_output_usage = 1,
    total_transfer = 0,
  }
  for k, v in pairs(spec.flow or {}) do net.flow_last_tick[k] = v end
  function net.get_accumulators_energy()
    local a = spec.acc or {energy = 0, capacity = 0}
    return {energy = a.energy, capacity = a.capacity}
  end
  for _ = 1, (spec.subs or 1) do
    local sub = {valid = true, object_name = "LuaElectricSubNetwork", id = next_sub_id, parent_network = net, neighbours = {}}
    next_sub_id = next_sub_id + 1
    net.sub_networks[#net.sub_networks + 1] = sub
  end
  return net
end

function M.pole(sub)
  return {
    valid = true, type = "electric-pole", name = "small-electric-pole",
    electric_network = sub, electric_network_id = sub.id,
    electric_network_statistics = sub.parent_network.statistics,
  }
end

-- Surface -------------------------------------------------------------------
function M.surface(spec)
  -- spec: {name=, index=, platform=, entities={...}, global_net=net, pollution=, pstats=,
  --        counts={name->n, ["type:lab"]->n}, planet=, pollutant_type=, daytime=, darkness=, solar=}
  local s = {valid = true, object_name = "LuaSurface", name = spec.name, index = spec.index or 1}
  s.platform = spec.platform or nil
  s.planet = spec.planet
  s.entities = spec.entities or {}
  s.has_global_electric_network = spec.global_net ~= nil
  s.global_electric_network = spec.global_net and spec.global_net.sub_networks[1] or nil
  s.global_electric_network_statistics = spec.global_net and spec.global_net.statistics or nil
  s.pollution_statistics = spec.pstats or flow_stats()
  s.pollutant_type = spec.pollutant_type and {name = spec.pollutant_type} or nil
  s.daytime = spec.daytime or 0.5
  s.darkness = spec.darkness or 0
  s.solar_power_multiplier = spec.solar or 1
  s.ticks_per_day = 25000
  function s.get_total_pollution() return spec.pollution or 0 end
  function s.find_entities_filtered(f)
    local out = {}
    for _, e in ipairs(s.entities) do
      if (not f.type or e.type == f.type) and (not f.name or e.name == f.name) then out[#out + 1] = e end
    end
    return out
  end
  function s.count_entities_filtered(f)
    if f.name and M.removed_entities[f.name] then error("Unknown entity name: " .. f.name) end
    if spec.counts then
      if f.name and spec.counts[f.name] then return spec.counts[f.name] end
      if f.type and spec.counts["type:" .. f.type] then return spec.counts["type:" .. f.type] end
      if f.name or f.type then return 0 end
    end
    return #s.find_entities_filtered(f)
  end
  return s
end

-- Logistics -----------------------------------------------------------------
function M.logistic_network(spec)
  local n = {valid = true, object_name = "LuaLogisticNetwork", network_id = spec.id or 1}
  n.all_logistic_robots = spec.lr or 0; n.available_logistic_robots = spec.lra or 0
  n.all_construction_robots = spec.cr or 0; n.available_construction_robots = spec.cra or 0
  function n.get_contents() return spec.contents or {} end
  return n
end

-- Force ---------------------------------------------------------------------
function M.force(spec)
  local f = {valid = true, object_name = "LuaForce", name = spec.name, players = spec.players or {}}
  f.rockets_launched = spec.rockets or 0
  f.items_launched = spec.items_launched or {}
  f.research_progress = spec.progress or 0
  f.current_research = spec.current
  f.previous_research = spec.previous
  f.research_queue = spec.queue or {}
  f.technologies = spec.technologies or {}
  f.platforms = spec.platforms or {}
  f.connected_players = spec.connected or {}
  f.research_enabled = spec.research_enabled ~= false
  f.logistic_networks = spec.logistic_networks or {}
  f.laboratory_speed_modifier = spec.lab_speed or 0
  local function by_surface(tbl)
    return function(surface)
      local key = type(surface) == "table" and surface.name or surface
      return (tbl or {})[key] or flow_stats()
    end
  end
  f.get_item_production_statistics = by_surface(spec.istats)
  f.get_fluid_production_statistics = by_surface(spec.fstats)
  f.get_kill_count_statistics = by_surface(spec.kstats)
  f.get_entity_build_count_statistics = by_surface(spec.bstats)
  local function evo(part)
    return function(surface)
      local key = type(surface) == "table" and surface.name or surface
      if part then return ((spec.evolution_by or {})[key] or {})[part] or 0 end
      return (spec.evolution or {})[key] or 0
    end
  end
  f.get_evolution_factor = evo(nil)
  f.get_evolution_factor_by_pollution = evo("pollution")
  f.get_evolution_factor_by_time = evo("time")
  f.get_evolution_factor_by_killing_spawners = evo("spawners")
  function f.get_entity_count(name) return (spec.entity_counts or {})[name] or 0 end
  return f
end

function M.tech(name, opts)
  opts = opts or {}
  return {
    valid = true, name = name, level = opts.level or 1, researched = opts.researched or false,
    enabled = opts.enabled ~= false, research_unit_count = opts.units or 100,
    research_unit_energy = opts.energy or 1800,
    research_unit_ingredients = opts.ingredients or {{name = "automation-science-pack", amount = 1}},
    research_unit_count_formula = opts.formula,
    prototype = {max_level = opts.max_level or 1, hidden = opts.hidden or false},
  }
end

function M.player(spec)
  return {
    valid = true, name = spec.name, index = spec.index or 1, connected = true,
    afk_time = spec.afk_ticks or 0, online_time = spec.online_ticks or 0,
    physical_surface = spec.surface,
    print = function(s) M.print_out[#M.print_out + 1] = "player.print: " .. tostring(s) end,
  }
end

-- Globals -------------------------------------------------------------------
function M.install(opts)
  opts = opts or {}
  defines = {
    flow_precision_index = {five_seconds = 1, one_minute = 2, ten_minutes = 3, one_hour = 4, ten_hours = 5, fifty_hours = 6, two_hundred_fifty_hours = 7, one_thousand_hours = 8},
    events = {on_tick = 0, on_runtime_mod_setting_changed = 100, on_research_finished = 101, on_surface_created = 102, on_surface_deleted = 103, on_surface_renamed = 104, on_space_platform_changed_state = 105, on_player_joined_game = 106, on_player_left_game = 107},
    space_platform_state = {waiting_for_starter_pack = 0, starter_pack_requested = 1, starter_pack_on_the_way = 2, on_the_path = 3, waiting_at_station = 4, paused = 5, no_schedule = 6, no_path = 7, waiting_for_departure = 8},
  }
  storage = {}
  local setting_values = opts.settings or {}
  settings = {global = setmetatable({}, {__index = function(_, k)
    if setting_values[k] == nil then error("unknown setting " .. tostring(k)) end
    return {value = setting_values[k]}
  end})}
  M.settings = setting_values
  M.handlers = {nth = {}, events = {}}
  script = {
    active_mods = {base = "2.1.23", wiretap = opts.mod_version or "1.1.0"},
    mod_name = "wiretap",
    feature_flags = {expansion = true, quality = true, space_travel = true, spoiling = true, freezing = true, segmented_units = true, rail_bridges = true, expansion_shaders = true},
    on_init = function(h) M.handlers.on_init = h end,
    on_load = function(h) M.handlers.on_load = h end,
    on_configuration_changed = function(h) M.handlers.on_configuration_changed = h end,
    on_event = function(ev, h)
      if type(ev) == "table" then for _, e in ipairs(ev) do M.handlers.events[e] = h end else M.handlers.events[ev] = h end
    end,
    on_nth_tick = function(tick, h)
      if tick == nil and h == nil then M.handlers.nth = {} return end
      if h == nil then M.handlers.nth[tick] = nil else M.handlers.nth[tick] = h end
    end,
    generate_event_name = function() return 999 end,
  }
  M.files = {}
  helpers = {
    game_version = "2.1.23",
    write_file = function(filename, data, append, for_player)
      M.files[#M.files + 1] = {filename = filename, data = data, append = append, for_player = for_player}
    end,
  }
  M.remote_interfaces = {}
  remote = {add_interface = function(name, t) M.remote_interfaces[name] = t end,
            interfaces = {}, call = function(name, fn, ...) return M.remote_interfaces[name][fn](...) end}
  M.commands = {}
  commands = {add_command = function(name, help, fn) M.commands[name] = fn end}
  M.rcon_out = {}
  rcon = {print = function(s) M.rcon_out[#M.rcon_out + 1] = s end}
  M.log_out = {}
  log = function(s) M.log_out[#M.log_out + 1] = s end
  M.print_out = {}
  print = function(s) M.print_out[#M.print_out + 1] = tostring(s) end
  -- prototypes: every entity name exists unless listed in removed_entities;
  -- items carry used_by_labs for the science packs of the world.
  M.removed_entities = opts.removed_entities or {}
  prototypes = {
    entity = setmetatable({}, {__index = function(_, name)
      if M.removed_entities[name] then return nil end
      return {name = name}
    end}),
    item = opts.items or {
      ["automation-science-pack"] = {name = "automation-science-pack", used_by_labs = true},
      ["space-science-pack"] = {name = "space-science-pack", used_by_labs = true},
      ["matter-tech-card"] = {name = "matter-tech-card", used_by_labs = true},
      ["iron-plate"] = {name = "iron-plate", used_by_labs = false},
      ["science"] = {name = "science", used_by_labs = false},
    },
  }
  game = opts.game
end

function M.game(spec)
  local g = {tick = spec.tick or 0, ticks_played = spec.ticks_played or spec.tick or 0,
             speed = spec.speed or 1, tick_paused = spec.tick_paused or false}
  g.surfaces = spec.surfaces or {}
  g.forces = spec.forces or {}
  g.connected_players = spec.connected_players or {}
  g.players = spec.players or {}
  function g.is_multiplayer() return spec.multiplayer or false end
  function g.get_player(i) return g.players[i] end
  function g.get_surface(x) return g.surfaces[x] end
  function g.print(s) M.print_out[#M.print_out + 1] = "game.print: " .. tostring(s) end
  return g
end

return M
