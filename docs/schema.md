# Snapshot schema

Reference for the JSON the mod writes (`script-output/wiretap/stats.json`, one object
per line in the NDJSON journal, and the `/wiretap` RCON reply). Version 1.1.1.

Conventions:

- Empty tables are omitted, never written as `{}` or `[]`. Consumers must default.
- Numbers: integers are written as integers, fractions with the configured precision
  (setting "Number precision", default 3 decimals, trailing zeros trimmed); fractions
  between -1 and 1 always keep at least three decimals. Non-finite values become
  `null`. Object keys are sorted, numerically where both keys are numbers.
- Fields are only ever added. Renames or unit changes get a new field name.
- Watts are 5-second averages unless the field says `last tick`. Joules are
  instantaneous. Rates in `science_packs`, `kills` and `research` are per minute.

## `meta`

| Field | Meaning |
| --- | --- |
| `mod_version` | wiretap version (from the mod list, always matches info.json) |
| `game_version` | Factorio version (`helpers.game_version`) |
| `tick` | map tick the export started on |
| `export_ticks` | ticks the export spanned (0 when written in one tick; surfaces and forces are collected one per tick with the spread setting) |
| `ticks_played` | ticks since the map was created |
| `game_time_seconds` | `ticks_played / 60`, floored |
| `game_speed` | 1 is normal speed |
| `tick_paused` | true while the game is paused (single player menu, `/toggle-pause`) |
| `player_count` | connected players |
| `players_online` | array of player names; absent when nobody is online |
| `players.<name>` | `afk_seconds`, `online_seconds`, `surface` for each connected player |
| `interval_seconds` | export interval setting |
| `mods` | active mod name to version |
| `features` | feature flags (`quality`, `space_travel`, `spoiling`, …) |

## `surfaces.<surface name>`

| Field | Meaning |
| --- | --- |
| `index` | surface index (unique id) |
| `is_platform` | true for space platform surfaces |
| `planet` | planet name; absent for platforms and plain surfaces |
| `pollutant` | airborne pollutant on this surface (`pollution`, `spores`); absent where none |
| `daytime` | time of day, 0 to 1 |
| `darkness` | 0 (day) to 1 (night) |
| `solar_multiplier` | solar power multiplier of the surface |
| `pollution` | total pollution on the surface; 0 on surfaces without a pollutant (setting: pollution) |
| `pollution_produced_total` | cumulative pollution per source entity name |
| `pollution_absorbed_total` | cumulative absorption per sink (`tile-proxy`, `tree-proxy`, …) |
| `power` | see below (setting: power) |
| `logistics.<force>` | see below (setting: logistics) |

### `power`

`totals` always present when power is enabled:

| Field | Meaning |
| --- | --- |
| `production_watts`, `consumption_watts` | summed over listed networks |
| `accumulator_charge_joules`, `accumulator_capacity_joules` | summed over listed networks |
| `network_count` | number of listed networks |
| `consumption_satisfaction` | demand-weighted satisfaction across networks on the export tick; 1 when there is no demand (setting: power flow) |
| `consumption_satisfaction_min` | lowest demand-weighted satisfaction of the surface sampled once a second since the previous export, including the export tick itself (setting: power flow) |
| `maximum_production_watts`, `maximum_consumption_watts`, `solar_watts` | summed last-tick values (setting: power flow) |
| `accumulator_charge_watts`, `accumulator_discharge_watts` | power flowing into / out of accumulators last tick, summed (setting: power flow) |

`networks.<key>`: one entry per Factorio 2.1 electric network (`LuaElectricNetwork`),
which is one or more pole groups (`LuaElectricSubNetwork`) joined by closed power
switches or by a platform's global network. The key is `"global"` for the network
that contains the surface's global sub network (space platforms, as in 1.0.x),
otherwise the lowest sub network id as a string. Networks that never had anything
attached (no statistics, no capacity, no demand) are dropped.

| Field | Meaning |
| --- | --- |
| `production_watts`, `consumption_watts` | 5-second averages from the network statistics |
| `accumulator_charge_joules`, `accumulator_capacity_joules` | from the network's last-tick flow |
| `sub_network_count` | pole groups in this network |
| `production_by_entity`, `consumption_by_entity` | watts per entity prototype name, including names with 0 W that the statistics remember (setting: power breakdown) |
| `flow.consumption_satisfaction` | fraction of demand met last tick; below 1 is a brownout |
| `flow.consumption_satisfaction_min` | lowest value of this network sampled since the previous export, including the export tick |
| `flow.production_satisfaction` | fraction of available production used last tick |
| `flow.maximum_production_watts` | available production last tick, incl. accumulator discharge |
| `flow.maximum_consumption_watts` | demand last tick, incl. accumulator charging |
| `flow.solar_watts`, `flow.solar_used_watts` | solar output offered / actually used last tick |
| `flow.primary_output_watts`, `flow.secondary_output_watts` | generation used last tick by priority tier (steam, nuclear and fusion are primary) |
| `flow.accumulator_charge_watts`, `flow.accumulator_discharge_watts` | accumulator flows last tick |
| `flow.transfer_watts` | energy moved from producers to consumers last tick |

`count_by_entity`: entities per prototype name on the surface, all forces, for every
name that appears in a breakdown and still exists as a prototype (setting: power
breakdown). Zero counts are omitted.

### `logistics.<force>`

| Field | Meaning |
| --- | --- |
| `network_count` | logistic networks of the force on this surface |
| `items.<item>` | item count summed over the force's networks on this surface; can be briefly negative |
| `items_by_quality.<item>:<quality>` | same, per quality (setting: quality breakdown) |
| `robots.logistic_total`, `robots.logistic_available` | logistic robots |
| `robots.construction_total`, `robots.construction_available` | construction robots |

## `forces.<force name>`

Forces are listed when they are `player` or have at least one player. The `enemy`
force only carries `evolution` and `evolution_by`.

| Field | Meaning |
| --- | --- |
| `rockets_launched` | rockets launched by the force |
| `items_launched.<item>` | items launched in rockets, all time |
| `research` | see below (always exported) |
| `science_packs.<surface>.<pack>` | per-pack rates and totals (setting: science) |
| `production.<surface>` | all-time totals (setting: production) |
| `kills.<surface>` | kills and losses (setting: kills) |
| `builds.<surface>` | entities built and mined, all time (setting: build counts, off by default) |
| `platforms.<surface name>` | space platforms (setting: platforms) |
| `evolution.<surface>` | enemy evolution factor per planet surface (`enemy` force only; platforms are skipped) |
| `evolution_by.<surface>` | `pollution`, `time`, `spawners` parts of the evolution factor |

### `research`

| Field | Meaning |
| --- | --- |
| `enabled` | whether the force can research |
| `lab_speed_modifier` | lab speed bonus from research (0.5 = +50%) |
| `current` | technology being researched; absent when idle |
| `progress` | 0 to 1; absent when idle |
| `current_level` | level of the current technology |
| `current_units` | research units the current technology needs |
| `current_unit_energy` | lab time per unit as the API reports it (ticks of lab work at speed 1) |
| `current_ingredients.<pack>` | packs consumed per research unit |
| `progress_per_minute` | progress gained per minute between the last two timer exports of the same technology; absent on the first export or right after a switch |
| `eta_seconds` | `(1 - progress) / progress_per_minute * 60`; absent while the rate is unknown or zero |
| `previous` | last researched technology |
| `queue` | technology names, current first |
| `queue_details` | `[{name, level}]`, the level each queued entry will reach (repeated infinite technologies count up) |
| `technologies_researched`, `technologies_total` | all technologies known to the force |
| `technologies_available` | enabled, visible, finite technologies |
| `technologies_infinite` | level-based technologies (never "researched") |
| `labs.<surface>` | labs of the force per surface |
| `science_total` | research units completed, all time, summed over surfaces (the hidden `science` item; setting: science) |
| `science_per_minute`, `science_per_minute_10m` | research units completed per minute, 1- and 10-minute averages |

### `science_packs.<surface>.<pack>`

Packs are every item some lab accepts (`used_by_labs`), plus anything named
`*science-pack*` as a fallback.

| Field | Meaning |
| --- | --- |
| `produced_per_minute`, `consumed_per_minute` | 1-minute averages |
| `produced_per_minute_10m`, `consumed_per_minute_10m` | 10-minute averages |
| `produced_total`, `consumed_total` | all time; 0 when the pack was never produced or consumed here |

### `production.<surface>`

| Field | Meaning |
| --- | --- |
| `items_produced_total`, `items_consumed_total` | item name to count |
| `fluids_produced_total`, `fluids_consumed_total` | fluid name to amount |
| `items_produced_total_by_quality`, `items_consumed_total_by_quality` | quality name to (item name to count); includes `normal` (setting: production by quality) |

### `kills.<surface>`

| Field | Meaning |
| --- | --- |
| `kills_total.<entity>` | enemies of this type killed by the force, all time |
| `losses_total.<entity>` | entities of this type the force lost, all time |
| `kills_per_minute`, `losses_per_minute` | 1-minute averages summed over types |

### `builds.<surface>`

`built_total.<entity>` and `mined_total.<entity>`, all time.

### `platforms.<surface name>`

Keyed by the platform's surface name (`platform-3`) because platform display names
need not be unique; the same key appears under `surfaces`.

| Field | Meaning |
| --- | --- |
| `name` | display name |
| `index` | platform index within the force |
| `state` | `waiting_for_starter_pack`, `starter_pack_requested`, `starter_pack_on_the_way`, `on_the_path`, `waiting_at_station`, `paused`, `no_schedule`, `no_path`, `waiting_for_departure` |
| `paused` | thrust paused by the player |
| `speed` | current speed |
| `location` | space location the platform is stopped at; absent while travelling |
| `last_location` | space location last visited or passed |
| `destination` | station of the current schedule record; absent without a schedule |
| `route` | `from>to` of the space connection being travelled; absent while parked |
| `distance` | 0 to 1 along the current space connection; absent while parked |
| `weight` | total platform weight |
| `completed_trips` | trips completed |
| `damaged_tiles` | tiles currently damaged (asteroid hits) |
| `has_hub` | false before the starter pack is applied |
| `deletion_in_ticks` | present only when the platform is scheduled for deletion |

## Changes from 1.0.x

- New: `meta.{game_version,game_speed,tick_paused,players,mods,features}`,
  `surfaces.*.{planet,pollutant,daytime,darkness,solar_multiplier}`,
  `power.totals.{consumption_satisfaction,consumption_satisfaction_min,maximum_production_watts,maximum_consumption_watts,solar_watts,accumulator_charge_watts,accumulator_discharge_watts}`,
  `power.networks.*.{sub_network_count,flow}`, `research.{enabled,lab_speed_modifier,current_units,current_unit_energy,current_ingredients,progress_per_minute,eta_seconds,queue_details,technologies_available,technologies_infinite,labs,science_total,science_per_minute,science_per_minute_10m}`,
  `production.*.*_by_quality`, `forces.*.{kills,builds,platforms}`, `forces.enemy.evolution_by`.
- `game_time_seconds` now derives from `ticks_played` (identical for normal saves).
- Network keys: `"global"` for platforms as before; a pole network joined across a
  power switch is now one entry (keyed by its lowest group id) instead of one entry
  per group. Idle networks with machines stay listed; only never-used pole groups
  are dropped.
- Accumulator charge and capacity now come from the electric network; accumulators
  out of range of every pole are no longer counted.
- Enemy evolution is no longer listed for platform surfaces.
