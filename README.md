# wiretap

Export as many vitals as possible from Factorio **2.1** to the outside world, so you
can watch your factory from Home Assistant, Grafana, MQTT dashboards, or an ESP32 on
your desk. (Still on Factorio 2.0? Use wiretap 1.0.3; this version needs 2.1.15 or
newer.)

Two parts:

| Part | What it does |
| --- | --- |
| [`mod/wiretap`](https://github.com/ekrauser/wiretap/tree/main/mod/wiretap) | Factorio mod. Every N seconds it writes a JSON snapshot of the game's stats to `script-output/wiretap/stats.json` (and/or an append-only NDJSON journal at `script-output/wiretap/stats.ndjson`). Also exposes a remote interface and a `/wiretap` command for live RCON polling. |
| [`companion/wiretap_bridge.py`](https://github.com/ekrauser/wiretap/blob/main/companion/wiretap_bridge.py) | Python bridge (stdlib-only, Python 3.8+). Watches the JSON file and re-serves it as an HTTP API (`/stats`), Prometheus metrics (`/metrics`), and optionally MQTT: JSON topics, **flat scalar topics for ESPHome and other microcontrollers**, and Home Assistant auto-discovery. |

The mod can't talk to the network directly (Factorio's Lua sandbox only allows writing
to `script-output`), hence the bridge. If you already run RCON, you can skip the file
entirely (see [RCON](#option-c-rcon-no-file-no-bridge)).

Mod portal: <https://mods.factorio.com/mod/wiretap>. License: MIT.

## What gets exported

Per **surface** (every planet, moon, and space platform is a surface):

- **Power, per electric network and totals**: production and consumption in watts
  (5-second average, same as the in-game GUI), accumulator charge and capacity in
  joules, and a `flow` block from the last tick: `consumption_satisfaction` (1 means
  every consumer got what it asked for, lower is a brownout) plus
  `consumption_satisfaction_min`, the lowest value seen by a once-per-second sampler
  since the previous export, so a two-second brownout between exports still shows;
  `production_satisfaction`, `maximum_production_watts` (what generators and
  accumulators could deliver), `maximum_consumption_watts` (demand), solar offered
  and used, primary/secondary generation used, accumulator charge and discharge
  watts, transferred watts. Optionally broken down by entity type (how much your
  steam engines make, how much your labs eat) with a matching `count_by_entity`.
  A network is one Factorio 2.1 *electric network*: pole groups joined by a closed
  power switch count as one, keyed by the lowest id among its groups
  (`sub_network_count` says how many it spans); a space platform's grid keeps the
  key `"global"`. Never-used networks (isolated poles) are dropped. Surface totals
  add a demand-weighted `consumption_satisfaction`, its interval minimum, and the
  summed maximum production/consumption, solar and accumulator charge/discharge.
- **Logistics, per force**: item counts aggregated across all logistic networks
  (optionally also as `items_by_quality` keyed `name:quality`), robot counts
  (logistic/construction, total/available), network count.
- **Pollution**: total on the surface, cumulative produced/absorbed by source, and the
  surface's `pollutant` name (`pollution`, or `spores` on Gleba). Surfaces without a
  pollutant report 0 and skip the chunk walk.
- `planet`, `is_platform`, `daytime`, `darkness` (0 day to 1 night, explains solar
  dips) and `solar_multiplier`.

Per **force**: research (current technology, progress, level, research units, unit
energy, required packs, **progress per minute and ETA in seconds** computed from
consecutive exports, previous research, queue with levels, researched/total/
available/infinite technology counts, labs per surface, lab speed bonus), the
**effective science rate** (research units completed per minute, lab productivity
included, from the game's hidden `science` item), **science pack rates** (packs per
minute produced/consumed, 1- and 10-minute averages, all-time totals, per surface),
rockets launched, items launched, all-time production/consumption totals for items
and fluids per surface (optionally per quality), **kills and losses** per surface
(all time and per minute: your "base under attack" signal), optional entity
build/mine counts, and every **space platform**: state (`on_the_path`,
`waiting_at_station`, …), paused flag, speed, current location, destination and
route, last location, distance along the route, weight, completed trips, damaged
tiles, whether it has a hub, pending deletion.

Plus: game tick, play time, game speed and pause state, game and mod versions,
active mods and feature flags, online players with AFK time, online time and current
surface, enemy evolution per planet and its split by pollution, time and spawner
kills.

<details>
<summary>Snapshot shape (abridged; full field reference in <a href="docs/schema.md">docs/schema.md</a>)</summary>

```json
{
  "meta": {"tick": 12345, "game_time_seconds": 205, "game_speed": 1, "tick_paused": false,
            "player_count": 1, "players_online": ["kraus"],
            "players": {"kraus": {"afk_seconds": 3, "online_seconds": 205, "surface": "nauvis"}},
            "interval_seconds": 10, "mod_version": "1.1.0", "game_version": "2.1.23",
            "mods": {"base": "2.1.23", "space-age": "2.1.23", "wiretap": "1.1.0"},
            "features": {"quality": true, "space_travel": true}},
  "surfaces": {
    "nauvis": {
      "index": 1, "is_platform": false, "planet": "nauvis", "pollutant": "pollution",
      "daytime": 0.3, "darkness": 0, "solar_multiplier": 1,
      "pollution": 5231.4,
      "power": {
        "totals": {"production_watts": 1.8e6, "consumption_watts": 1.7e6,
                    "accumulator_charge_joules": 2.5e7, "accumulator_capacity_joules": 5e7,
                    "network_count": 1, "consumption_satisfaction": 1,
                    "consumption_satisfaction_min": 0.97,
                    "maximum_production_watts": 2.7e6, "maximum_consumption_watts": 1.7e6,
                    "solar_watts": 1e5, "accumulator_charge_watts": 0,
                    "accumulator_discharge_watts": 0},
        "networks": {
          "1": {"production_watts": 1.8e6, "consumption_watts": 1.7e6,
                 "accumulator_charge_joules": 2.5e7, "accumulator_capacity_joules": 5e7,
                 "sub_network_count": 2,
                 "flow": {"consumption_satisfaction": 1, "consumption_satisfaction_min": 0.97,
                           "production_satisfaction": 0.63,
                           "maximum_production_watts": 2.7e6, "maximum_consumption_watts": 1.7e6,
                           "solar_watts": 1e5, "solar_used_watts": 1e5,
                           "primary_output_watts": 1.6e6, "secondary_output_watts": 0,
                           "accumulator_charge_watts": 0, "accumulator_discharge_watts": 0,
                           "transfer_watts": 1.7e6},
                 "production_by_entity": {"steam-engine": 1.8e6},
                 "consumption_by_entity": {"assembling-machine-2": 9e5, "lab": 8e5}}
        },
        "count_by_entity": {"steam-engine": 20, "assembling-machine-2": 45, "lab": 12}
      },
      "logistics": {
        "player": {"network_count": 2, "items": {"iron-plate": 3200},
                    "robots": {"logistic_total": 50, "logistic_available": 41,
                                "construction_total": 20, "construction_available": 20}}
      }
    }
  },
  "forces": {
    "player": {
      "rockets_launched": 0,
      "research": {"current": "logistics-2", "progress": 0.62, "current_level": 1,
                    "current_units": 200, "current_unit_energy": 1800,
                    "current_ingredients": {"automation-science-pack": 1, "logistic-science-pack": 1},
                    "progress_per_minute": 0.06, "eta_seconds": 380,
                    "previous": "logistics", "queue": ["logistics-2", "steel-processing"],
                    "queue_details": [{"name": "logistics-2", "level": 1}, {"name": "steel-processing", "level": 1}],
                    "technologies_researched": 24, "technologies_total": 214,
                    "technologies_available": 190, "technologies_infinite": 12,
                    "labs": {"nauvis": 12}, "lab_speed_modifier": 0.5, "enabled": true,
                    "science_per_minute": 61.5, "science_per_minute_10m": 59.2, "science_total": 8200},
      "science_packs": {"nauvis": {"automation-science-pack": {
          "produced_per_minute": 61.2, "consumed_per_minute": 60.0,
          "produced_per_minute_10m": 58.9, "consumed_per_minute_10m": 60.1,
          "produced_total": 8123, "consumed_total": 8000}}},
      "kills": {"nauvis": {"kills_per_minute": 3.5, "losses_per_minute": 0,
                            "kills_total": {"small-biter": 500}, "losses_total": {"gun-turret": 2}}},
      "production": {"nauvis": {"items_produced_total": {"iron-plate": 51234},
                                  "items_consumed_total": {"iron-plate": 48000},
                                  "fluids_produced_total": {"water": 1.2e6},
                                  "fluids_consumed_total": {"water": 1.2e6}}},
      "platforms": {
        "platform-1": {"name": "Hauler", "index": 1, "state": "on_the_path", "paused": false,
                        "speed": 123.4, "last_location": "nauvis", "destination": "vulcanus",
                        "route": "nauvis>vulcanus", "distance": 0.42, "weight": 1.2e6,
                        "completed_trips": 7, "damaged_tiles": 0, "has_hub": true}
      }
    },
    "enemy": {"evolution": {"nauvis": 0.14},
              "evolution_by": {"nauvis": {"pollution": 0.1, "time": 0.02, "spawners": 0.02}}}
  }
}
```

Empty tables are omitted rather than serialized, so always use defaults when a key
might be missing. Numbers are written by the mod itself: integers as integers,
fractions with the configured precision (3 decimals by default; fractions below 1
always keep three), object keys sorted, so the file stays small and diffs cleanly.
</details>

## 1. Install the mod

Option A — mod portal: <https://mods.factorio.com/mod/wiretap> (in-game mod browser:
search "Wiretap").

Option B — folder (easiest for development):

```bash
# Linux / WSL (adjust the target to your Factorio user-data dir)
cp -r mod/wiretap ~/.factorio/mods/wiretap
```

Option C — zip:

```bash
./scripts/package.sh          # builds dist/wiretap_<version>.zip
```

then drop the zip into your mods folder:

- Windows: `%APPDATA%\Factorio\mods\`
- Linux: `~/.factorio/mods/`
- Playing on Windows but running the bridge in WSL? The mods folder is at
  `/mnt/c/Users/<you>/AppData/Roaming/Factorio/mods/`.

Enable **Wiretap – Stats Exporter** in the in-game Mods menu and load your save.
Within one interval (default 10 s) the snapshot appears at:

- Windows: `%APPDATA%\Factorio\script-output\wiretap\stats.json`
- Linux: `~/.factorio/script-output/wiretap/stats.json`

All knobs live in **Settings → Mod settings → Map**: interval, number precision
(exported values are rounded to 3 decimal places by default; fractions keep at least
3), output mode (snapshot / journal / both), filenames, and per-category toggles:
logistics, logistics by quality (off), power, per-entity power breakdown with entity
counts, power flow and satisfaction, production totals, production by quality (off),
science pack rates, pollution, space platforms, kills and losses, entity build counts
(off), and "spread the export over several ticks" (on): the timer collects one
surface or force per game tick and writes when done, so even a large save never gets
one long tick; `meta.export_ticks` says how many ticks a snapshot spans. Changes apply
immediately, no restart needed. Research state is always exported.

Two quirks in the exported data are the game's own, passed through faithfully:

- Production totals contain an item literally named `science`: the hidden item
  Factorio uses to count research units completed, lab productivity included. It
  never matches the `science_packs` section; its rate is exported as
  `research.science_per_minute`, your true science-per-minute. Since Factorio 2.1.20
  it is credited to the surface whose labs did the work.
- Logistic item counts can go briefly **negative** (e.g. `nutrients: -2`):
  robots over-reserve items they are flying to pick up. Officially "not a bug";
  counts self-correct when the robot arrives. Clamp on the consumer side if it
  bothers your dashboard.

> **Multiplayer note:** "Write on server only" is on by default, so in multiplayer
> only the host / dedicated server writes the file. The setting is ignored in
> single player (the file is always written there).

> **Performance note:** each export scans the electric poles on every surface to find
> its networks and, with the per-entity breakdown on, counts entities once per entity
> type seen in the power statistics (one count query per type per surface).
> Accumulator charge comes from the network itself at no extra cost; the brownout
> sampler polls one value per known network once a second. With the default "spread"
> setting the work is split one surface or force per tick, so no single tick carries
> the whole export. On a megabase, raise the interval or turn off the per-entity
> breakdown if you still notice it.

## 2. Run the bridge

```bash
python3 companion/wiretap_bridge.py \
    --file ~/.factorio/script-output/wiretap/stats.json
```

- `http://<host>:8787/stats` — full JSON snapshot (plus `X-Stats-Age-Seconds`: seconds
  since the mod last wrote the file, which survives bridge restarts)
- `http://<host>:8787/metrics` — Prometheus format with HELP/TYPE lines (power,
  satisfaction, items, robots, pollution, evolution, research, kills, platforms…)
- `http://<host>:8787/health` — 200 while the snapshot is fresh, 503 before the first
  snapshot or once it is older than `--stale-after` (default 3× the mod's interval,
  at least 30 s); the body says why

Useful flags: `--token` (or `WIRETAP_TOKEN`) requires `Authorization: Bearer` on
`/stats` and `/metrics`; `--cors-origin none` drops the CORS header; every item and
fluid gets `factorio_items_*_total` / `factorio_fluids_*_total` counters by default,
and `--production-items science,iron-plate` limits them if the series count bothers
your Prometheus; `--file` may point at the `.ndjson` journal, whose last complete line
is served. The bridge binds to all interfaces by
default; use `--bind` to restrict it.

With MQTT + Home Assistant discovery:

```bash
pip install paho-mqtt
export WIRETAP_MQTT_PASSWORD=secret        # or --mqtt-password-file /etc/wiretap/mqtt
python3 companion/wiretap_bridge.py \
    --file ~/.factorio/script-output/wiretap/stats.json \
    --mqtt-host homeassistant.local --mqtt-user mqtt \
    --ha-discovery --watch-items iron-plate,copper-plate,rocket-fuel
```

Sensors then appear in HA automatically under one "Factorio (wiretap)" device
(model = game version, sw version = mod version). Per surface: power production and
consumption (W), power satisfaction and its interval minimum (%), a **brownout**
binary sensor, headroom (W), solar output, accumulator charge (%), accumulator
charging and discharging (W), pollution, darkness, evolution (%), logistic and
construction robots available, kills and losses per minute plus an **under attack**
binary sensor, science consumed per minute (1 and 10 minute windows). Per force:
current research (with level, queue and required packs as attributes), research
progress (%), research ETA (s), science per minute (1 and 10 minute), technologies
researched, rockets launched. Per platform: state, location, destination, speed,
completed trips, damaged tiles, paused. Plus players online (names as an attribute),
play time, game tick, game speed, a paused binary sensor, and one sensor per
`--watch-items` item (logistic count summed across all surfaces).

Topics are published under `wiretap/…` (retained), with availability on
`wiretap/availability`. `--mqtt-prefix` namespaces topics, entity ids and the HA
device, so two bridges (two servers) can share one broker. `--mqtt-tls` connects over
TLS, `--ha-prefix` changes the discovery prefix, `--mqtt-client-id` overrides the
client id.

### Flat topics for ESPHome and other microcontrollers

Besides the JSON topics, the bridge publishes every headline value as a plain scalar
(no JSON) under `wiretap/flat/…`, retained, so an ESP32 can subscribe without parsing
anything (`--no-flat` turns this off):

| Topic | Payload |
| --- | --- |
| `wiretap/flat/meta/{tick,player_count,players_online,game_time_seconds,game_speed,paused,mod_version,game_version}` | number, comma-separated names, `ON`/`OFF` |
| `wiretap/flat/<surface>/power/{production_watts,consumption_watts,satisfaction_percent,satisfaction_min_percent,brownout,charge_percent,accumulator_charge_joules,accumulator_capacity_joules,maximum_production_watts,maximum_consumption_watts,headroom_watts,solar_watts,accumulator_charge_watts,accumulator_discharge_watts,network_count}` | number, `brownout` is `ON`/`OFF` |
| `wiretap/flat/<surface>/{pollution,darkness,evolution}` | number |
| `wiretap/flat/<surface>/robots/{logistic_available,logistic_total,construction_available,construction_total}` | number |
| `wiretap/flat/<surface>/kills/{kills_per_minute,losses_per_minute,under_attack}` | number, `ON`/`OFF` |
| `wiretap/flat/<surface>/science/{consumed_per_minute,consumed_per_minute_10m}` | number |
| `wiretap/flat/<force>/research/{current,progress_percent,eta_seconds,science_per_minute,science_per_minute_10m,technologies_researched}` | text / number |
| `wiretap/flat/<force>/rockets_launched` | number |
| `wiretap/flat/platforms/<force>_<platform surface>/{state,location,destination,speed,completed_trips,damaged_tiles,paused,distance}` | text / number / `ON`/`OFF` |
| `wiretap/flat/items/<item>` | number, for each `--watch-items` item |

Surface, force and item names are slugged (`platform-1` → `platform_1`,
`iron-plate` → `iron_plate`). An ESPHome device showing the factory's power and a
brownout LED needs only:

```yaml
mqtt:
  broker: 192.168.1.10

sensor:
  - platform: mqtt_subscribe
    name: "Factory power"
    topic: wiretap/flat/nauvis/power/production_watts
    unit_of_measurement: W
  - platform: mqtt_subscribe
    name: "Power satisfaction"
    topic: wiretap/flat/nauvis/power/satisfaction_min_percent
    unit_of_measurement: "%"
  - platform: mqtt_subscribe
    name: "Research ETA"
    topic: wiretap/flat/player/research/eta_seconds
    unit_of_measurement: s

text_sensor:
  - platform: mqtt_subscribe
    name: "Research"
    topic: wiretap/flat/player/research/current
  - platform: mqtt_subscribe
    name: "Brownout"
    topic: wiretap/flat/nauvis/power/brownout
    on_value:
      then:
        - if:
            condition:
              lambda: 'return x == "ON";'
            then:
              - light.turn_on: status_led
            else:
              - light.turn_off: status_led
```

<details>
<summary>systemd unit example</summary>

```ini
[Unit]
Description=wiretap bridge
After=network.target

[Service]
Environment=WIRETAP_MQTT_PASSWORD=secret
ExecStart=/usr/bin/python3 /opt/wiretap/companion/wiretap_bridge.py \
    --file /home/factorio/.factorio/script-output/wiretap/stats.json \
    --mqtt-host 192.168.1.10 --mqtt-user wiretap --ha-discovery
Restart=always

[Install]
WantedBy=multi-user.target
```

The bridge marks itself offline on the broker and exits cleanly on SIGTERM.
</details>

## 3. Hook up Home Assistant

### Option A: MQTT (recommended)

Run the bridge with `--mqtt-host … --ha-discovery` as above. Done: entities
auto-appear, and entities of a deleted platform or renamed surface are removed again.
Works great for driving physical hardware too: any ESPHome/ESP32 device can subscribe
to the same `wiretap/…` topics (see the flat topics above).

### Option B: REST (no MQTT broker needed)

```yaml
# configuration.yaml
rest:
  - resource: http://192.168.1.50:8787/stats
    scan_interval: 15
    sensor:
      - name: "Factorio power production"
        unit_of_measurement: "W"
        device_class: power
        value_template: >-
          {{ value_json.surfaces.nauvis.power.totals.production_watts | round(0) }}
      - name: "Factorio power consumption"
        unit_of_measurement: "W"
        device_class: power
        value_template: >-
          {{ value_json.surfaces.nauvis.power.totals.consumption_watts | round(0) }}
      - name: "Factorio power satisfaction"
        unit_of_measurement: "%"
        value_template: >-
          {{ (100 * value_json.surfaces.nauvis.power.totals.consumption_satisfaction) | round(1) }}
      - name: "Factorio accumulator charge"
        unit_of_measurement: "%"
        value_template: >-
          {% set p = value_json.surfaces.nauvis.power.totals %}
          {{ (100 * p.accumulator_charge_joules / p.accumulator_capacity_joules)
             | round(1) if p.accumulator_capacity_joules else 0 }}
      - name: "Factorio research ETA"
        unit_of_measurement: "s"
        device_class: duration
        value_template: >-
          {{ value_json.forces.player.research.get('eta_seconds', 0) | round(0) }}
      - name: "Factorio iron plates in logistics"
        value_template: >-
          {{ value_json.surfaces.nauvis.get('logistics', {}).get('player', {})
             .get('items', {}).get('iron-plate', 0) }}
      - name: "Factorio players online"
        value_template: "{{ value_json.meta.player_count }}"
```

Note the `.get(..., {})` chaining on the iron-plate sensor: the mod omits empty
sections entirely, so intermediate keys like `logistics` are absent until you build
your first roboport. A plain dotted path would throw a template error until then. The
same applies to `power.networks`, `power.count_by_entity`, `production`,
`science_packs`, `kills`, `platforms` and `items_by_quality`; `power.totals` is
always present while the power toggle is on.

Swap `nauvis` for any surface name (e.g. `vulcanus`, or a platform) to monitor other
worlds. Platform names contain hyphens, so use bracket syntax for them:
`value_json.surfaces['platform-1'].power.totals.production_watts`.

### Option C: RCON (no file, no bridge)

If your server has RCON enabled, poll the mod directly. The command prints the same
JSON snapshot to the RCON client:

```bash
# any rcon client, e.g. `pip install factorio-rcon-py`
python3 -c "
import factorio_rcon
client = factorio_rcon.RCONClient('127.0.0.1', 27015, 'rconpassword')
print(client.send_command('/wiretap'))
"
```

Other mods can call `remote.call("wiretap", "stats")` (table),
`remote.call("wiretap", "stats_json")` (string), `remote.call("wiretap", "write_now")`
(forces a file export, returns `true` or `false, error`) and
`remote.call("wiretap", "version")`. `/wiretap write` forces an immediate file export
and reports failures.

Heads-up for dedicated-server admins: typed into the server's *stdin console*, the
full JSON dump of `/wiretap` is delivered to RCON callers only (short messages
like the `write` confirmation and errors do echo to stdout). Use RCON or an in-game
player console to see the dump.

## Prometheus / Grafana

Point a scrape job at the bridge:

```yaml
scrape_configs:
  - job_name: factorio
    static_configs: [{targets: ["192.168.1.50:8787"]}]
```

Useful series: `factorio_power_production_watts{surface=…}`,
`factorio_power_consumption_satisfaction_min{surface=…}` (alert below 1: brownout
since the last export), `factorio_power_maximum_production_watts` (headroom when
compared with consumption), `factorio_power_accumulator_discharge_watts`,
`factorio_network_consumption_by_entity_watts{entity=…}` divided by
`factorio_entity_count{entity=…}` for per-machine utilization,
`factorio_logistic_item_count{item=…}`, `factorio_science_per_minute{force=…}` (your
live SPM), `factorio_research_info{technology=…}`, `factorio_research_eta_seconds`,
`factorio_losses_per_minute{surface=…}` (base under attack),
`factorio_platform_info{platform=…,state=…,destination=…}`,
`factorio_evolution_factor_part{part=…}`, `factorio_items_produced_total{item="science"}`
(a true counter, so `rate()` works), `factorio_stats_age_seconds` (alert if it grows:
the game is paused, crashed, or the mod is disabled; measured from the file's mtime
so a bridge restart does not reset it). Every family carries `HELP` and `TYPE` lines.

## Development

- `./scripts/test.sh` runs the mod under a mocked Factorio 2.1 runtime with Lua 5.2
  (`tests/lua/`) and the bridge's unit tests (`tests/python/`). No game needed.
- `./scripts/package.sh` builds `dist/wiretap_<version>.zip` for the mod portal.
- [docs/releasing.md](docs/releasing.md) covers versioning, the portal upload and
  local deployment; [docs/schema.md](docs/schema.md) is the field reference.
