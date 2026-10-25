-- A representative world: nauvis (two networks, one of them two sub-networks
-- joined by a closed power switch, accumulators, logistics, labs), a platform
-- with a global network travelling to vulcanus, two forces, research, kills.
local M = require("mock")

local function W(w) return w / 60 end -- watts -> joules per tick (electric flow units)

return function(opts)
  opts = opts or {}
  -- nauvis -------------------------------------------------------------------
  local netA = M.electric_network{subs = 2, acc = {energy = 2.5e7, capacity = 5e7},
    stats = M.flow_stats(
      {["assembling-machine-2"] = {total = 1e9, flow = {default = W(9e5)}}, lab = {total = 2e8, flow = {default = W(8e5)}}},   -- input = consumption
      {["steam-engine"] = {total = 2e9, flow = {default = W(1.8e6)}}},                                                           -- output = production
      {accumulator = {total = 2.5e7}}),
    flow = {consumption_satisfaction = 0.95, maximum_production = W(2.7e6), maximum_consumption = W(1.7e6),
            solar_output = W(1e5), solar_output_usage = 0.5, primary_output = W(1.8e6), primary_output_usage = 1,
            tertiary_output = W(2e5), tertiary_output_usage = 1, tertiary_demand = W(1e5), tertiary_demand_usage = 0.5,
            total_transfer = W(1.7e6)}}
  -- "gone-entity" is a prototype of a removed mod that the statistics still remember
  local netB = M.electric_network{subs = 1, stats = M.flow_stats({}, {["solar-panel"] = {total = 5e6, flow = {default = W(6e4)}}, ["gone-entity"] = {total = 10}})}
  local netDead = M.electric_network{subs = 1} -- isolated pole, nothing ever attached
  local polesA1 = M.pole(netA.sub_networks[1]); local polesA2 = M.pole(netA.sub_networks[2])
  local poleB = M.pole(netB.sub_networks[1]); local poleDead = M.pole(netDead.sub_networks[1])
  local nauvis = M.surface{name = "nauvis", index = 1, planet = {name = "nauvis"}, pollution = 187771.506, pollutant_type = "pollution",
    daytime = 0.25, darkness = 0.2, solar = 1,
    entities = {polesA1, polesA2, poleB, poleDead},
    counts = {["assembling-machine-2"] = 45, lab = 12, ["steam-engine"] = 20, ["solar-panel"] = 300, accumulator = 2, ["type:lab"] = 12},
    pstats = M.flow_stats({boiler = {total = 5947092.908}}, {["tile-proxy"] = {total = 12407596.103}})}
  -- platform with a global network --------------------------------------------
  local netG = M.electric_network{subs = 1, acc = {energy = 1e6, capacity = 2e6},
    stats = M.flow_stats({["asteroid-collector"] = {total = 1e7, flow = {default = W(2e5)}}}, {["solar-panel"] = {total = 3e7, flow = {default = W(4e5)}}})}
  local platform = {valid = true, object_name = "LuaSpacePlatform", name = "Hauler", index = 1, state = 3, speed = 123.4,
    space_location = nil, last_visited_space_location = {name = "nauvis"}, distance = 0.42, weight = 1.2e6,
    hub = {valid = true}, completed_trips = 7, scheduled_for_deletion = 0, paused = false,
    damaged_tiles = {{damage = 1, position = {x = 0, y = 0}}, {damage = 2, position = {x = 1, y = 0}}, {damage = 3, position = {x = 2, y = 0}}},
    schedule = {current = 2, records = {{station = "nauvis"}, {station = "vulcanus"}}},
    space_connection = {from = {name = "nauvis"}, to = {name = "vulcanus"}, length = 15000}}
  local plat = M.surface{name = "platform-1", index = 5, platform = platform, global_net = netG, pollution = 0, darkness = 0, solar = 3,
    entities = {M.pole(netG.sub_networks[1])}, counts = {["asteroid-collector"] = 4, ["solar-panel"] = 40}}
  platform.surface = plat
  -- logistics on nauvis for force player --------------------------------------
  local lnet1 = M.logistic_network{id = 1, lr = 30, lra = 25, cr = 10, cra = 10,
    contents = {{name = "iron-plate", quality = "normal", count = 3000}, {name = "iron-plate", quality = "rare", count = 200}, {name = "nutrients", quality = "normal", count = -2}}}
  local lnet2 = M.logistic_network{id = 2, lr = 20, lra = 16, cr = 10, cra = 10, contents = {{name = "copper-plate", quality = "normal", count = 1800}}}
  -- forces --------------------------------------------------------------------
  local techs = {
    ["logistics"] = M.tech("logistics", {researched = true}),
    ["logistics-2"] = M.tech("logistics-2", {level = 1, units = 200, energy = 1800,
      ingredients = {{name = "automation-science-pack", amount = 1}, {name = "logistic-science-pack", amount = 1}}}),
    ["steel-processing"] = M.tech("steel-processing"),
    ["mining-productivity"] = M.tech("mining-productivity", {level = 4, max_level = 4294967295, formula = "2500*(L-3)"}),
    ["secret-tech"] = M.tech("secret-tech", {hidden = true}),
  }
  local p1 = M.player{name = "drkraus", index = 1, afk_ticks = 1200, online_ticks = 36000, surface = nauvis}
  local player = M.force{name = "player", players = {p1}, connected = {p1}, rockets = 3, items_launched = {["space-science-pack"] = 1000},
    progress = 0.62, current = techs["logistics-2"], previous = techs["logistics"],
    queue = {techs["logistics-2"], techs["steel-processing"], techs["mining-productivity"], techs["mining-productivity"]},
    technologies = techs, logistic_networks = {nauvis = {lnet1, lnet2}}, platforms = {[1] = platform}, lab_speed = 0.5,
    istats = {nauvis = M.flow_stats(
        {["iron-plate"] = {total = 51234, flow = {default = 600}}, ["automation-science-pack"] = {total = 8123, flow = {[2] = 61.2, [3] = 58.9}},
         science = {total = 7000, flow = {[2] = 120, [3] = 118}}},
        {["iron-plate"] = {total = 48000}, ["automation-science-pack"] = {total = 8000, flow = {[2] = 60.0, [3] = 60.1}}}),
      ["platform-1"] = M.flow_stats({["space-science-pack"] = {total = 500, flow = {[2] = 10}}}, {})},
    fstats = {nauvis = M.flow_stats({water = {total = 1.2e6}}, {water = {total = 1.2e6}})},
    kstats = {nauvis = M.flow_stats({["small-biter"] = {total = 500, flow = {[2] = 3}}, ["medium-biter"] = {total = 20, flow = {[2] = 0.5}}},
                                    {["gun-turret"] = {total = 2, flow = {[2] = 0.25}}})},
    bstats = {nauvis = M.flow_stats({["transport-belt"] = {total = 4000}}, {["transport-belt"] = {total = 300}})},
    entity_counts = {lab = 12}}
  local enemy = M.force{name = "enemy", evolution = {nauvis = 0.14, ["platform-1"] = 0.14},
    evolution_by = {nauvis = {pollution = 0.1, time = 0.02, spawners = 0.02}}}
  local neutral = M.force{name = "neutral"}
  local game = M.game{tick = opts.tick or 29628000, surfaces = {nauvis = nauvis, ["platform-1"] = plat},
    forces = {player = player, enemy = enemy, neutral = neutral}, connected_players = {p1}, players = {[1] = p1}, multiplayer = opts.multiplayer}
  return game, {netA = netA, netB = netB, netG = netG, nauvis = nauvis, plat = plat, player = player, platform = platform, techs = techs}
end
