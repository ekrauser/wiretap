# wiretap

Export as many vitals as possible from Factorio **2.0.x** to the outside world, so you
can watch your factory from Home Assistant, Grafana, MQTT dashboards, or an ESP32 on
your desk.

Two parts:

| Part | What it does |
| --- | --- |
| [`mod/wiretap`](mod/wiretap/) | Factorio mod. Every N seconds it writes a JSON snapshot of the game's stats to `script-output/wiretap/stats.json` (and/or an append-only NDJSON journal). Also exposes a remote interface and a `/wiretap` command for live RCON polling. |
| [`companion/wiretap_bridge.py`](companion/wiretap_bridge.py) | Python bridge (stdlib-only). Watches the JSON file and re-serves it as an HTTP API (`/stats`), Prometheus metrics (`/metrics`), and optionally MQTT with Home Assistant auto-discovery. |

The mod can't talk to the network directly — Factorio's Lua sandbox only allows writing
to `script-output` — hence the bridge. If you already run RCON, you can skip the file
entirely (see [RCON](#option-c-rcon-no-file-no-bridge)).

## What gets exported

Per **surface** (every planet, moon, and space platform is a surface):

- **Power, per electric network and totals**: production and consumption in watts
  (5-second average, same as the in-game GUI), optionally broken down by entity type
  (how much your steam engines make, how much your labs eat), plus accumulator
  charge/capacity in joules.
- **Logistics, per force**: item counts aggregated across all logistic networks
  (optionally broken down by quality), robot counts (logistic/construction,
  total/available), network count.
- **Pollution**: total on the surface, plus cumulative produced/absorbed by source.

Per **force**: current research + progress + level, research queue, previous
research, researched/total technology counts, **science pack rates** (packs per
minute produced/consumed, 1- and 10-minute averages, plus all-time totals, per
surface), rockets launched, items launched, all-time production/consumption totals
for items and fluids per surface.

Plus: game tick, play time, online player names, evolution factor per surface.

<details>
<summary>Snapshot shape (abridged)</summary>

```json
{
  "meta": {"tick": 12345, "game_time_seconds": 205, "player_count": 1,
            "players_online": ["kraus"], "interval_seconds": 10, "mod_version": "1.0.0"},
  "surfaces": {
    "nauvis": {
      "index": 1,
      "is_platform": false,
      "pollution": 5231.4,
      "power": {
        "totals": {"production_watts": 1.8e6, "consumption_watts": 1.7e6,
                    "accumulator_charge_joules": 2.5e7, "accumulator_capacity_joules": 5e7,
                    "network_count": 1},
        "networks": {
          "1": {"production_watts": 1.8e6, "consumption_watts": 1.7e6,
                 "accumulator_charge_joules": 2.5e7, "accumulator_capacity_joules": 5e7,
                 "production_by_entity": {"steam-engine": 1.8e6},
                 "consumption_by_entity": {"assembling-machine-2": 9e5, "lab": 8e5}}
        }
      },
      "logistics": {
        "player": {
          "network_count": 2,
          "items": {"iron-plate": 3200, "copper-plate": 1800},
          "robots": {"logistic_total": 50, "logistic_available": 41,
                      "construction_total": 20, "construction_available": 20}
        }
      }
    }
  },
  "forces": {
    "player": {
      "rockets_launched": 0,
      "research": {"current": "logistics-2", "progress": 0.62, "current_level": 1,
                    "previous": "logistics", "queue": ["logistics-2", "steel-processing"],
                    "technologies_researched": 24, "technologies_total": 214},
      "science_packs": {
        "nauvis": {
          "automation-science-pack": {"produced_per_minute": 61.2, "consumed_per_minute": 60.0,
                                        "produced_per_minute_10m": 58.9, "consumed_per_minute_10m": 60.1,
                                        "produced_total": 8123, "consumed_total": 8000}
        }
      },
      "production": {"nauvis": {"items_produced_total": {"iron-plate": 51234},
                                  "items_consumed_total": {"iron-plate": 48000},
                                  "fluids_produced_total": {"water": 1.2e6},
                                  "fluids_consumed_total": {"water": 1.2e6}}}
    },
    "enemy": {"evolution": {"nauvis": 0.14}}
  }
}
```

Empty tables are omitted rather than serialized, so always use defaults when a key
might be missing.
</details>

## 1. Install the mod

Option A — folder (easiest for development):

```bash
# Linux / WSL (adjust the target to your Factorio user-data dir)
cp -r mod/wiretap ~/.factorio/mods/wiretap
```

Option B — zip:

```bash
./scripts/package.sh          # builds dist/wiretap_1.0.0.zip
```

then drop the zip into your mods folder:

- Windows: `%APPDATA%\Factorio\mods\`
- Linux: `~/.factorio/mods/`
- Playing on Windows but running the bridge in WSL? The mods folder is at
  `/mnt/c/Users/<you>/AppData/Roaming/Factorio/mods/`.

Enable **Wiretap** in the in-game Mods menu and load your save. Within one
interval (default 10 s) the snapshot appears at:

- Windows: `%APPDATA%\Factorio\script-output\wiretap\stats.json`
- Linux: `~/.factorio/script-output/wiretap/stats.json`

All knobs live in **Settings → Mod settings → Map**: interval, output mode
(snapshot / journal / both), filenames, and per-category toggles (logistics, quality
breakdown, power, per-entity power breakdown, accumulator scan, production totals,
pollution). Changes apply immediately, no restart needed.

> **Multiplayer note:** "Write on server only" is on by default, so only the host /
> dedicated server writes the file. If no file appears in an unusual setup, try
> turning it off.

> **Performance note:** each export scans electric poles and accumulators on every
> surface. At the default 10 s interval this is unnoticeable on normal bases; on a
> megabase, raise the interval or disable the accumulator scan / per-entity breakdown.

## 2. Run the bridge

```bash
python3 companion/wiretap_bridge.py \
    --file ~/.factorio/script-output/wiretap/stats.json
```

- `http://<host>:8787/stats` — full JSON snapshot (plus `X-Stats-Age-Seconds` header)
- `http://<host>:8787/metrics` — Prometheus format (power, items, robots, pollution,
  evolution, research, players…)
- `http://<host>:8787/health` — returns 503 until the first snapshot is parsed

With MQTT + Home Assistant discovery:

```bash
pip install paho-mqtt
python3 companion/wiretap_bridge.py \
    --file ~/.factorio/script-output/wiretap/stats.json \
    --mqtt-host homeassistant.local --mqtt-user mqtt --mqtt-password secret \
    --ha-discovery --watch-items iron-plate,copper-plate,rocket-fuel
```

Sensors then appear in HA automatically under one "Factorio (wiretap)" device:
per-surface power production/consumption (W), accumulator charge (%), pollution,
science consumed per minute, players online, game tick, and one sensor per
`--watch-items` item (logistic count summed across all surfaces). Topics are published under `wiretap/…` (retained),
with availability on `wiretap/availability`.

<details>
<summary>systemd unit example</summary>

```ini
[Unit]
Description=wiretap bridge
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/wiretap/companion/wiretap_bridge.py \
    --file /home/factorio/.factorio/script-output/wiretap/stats.json \
    --mqtt-host 192.168.1.10 --ha-discovery
Restart=always

[Install]
WantedBy=multi-user.target
```
</details>

## 3. Hook up Home Assistant

### Option A: MQTT (recommended)

Run the bridge with `--mqtt-host … --ha-discovery` as above. Done — entities
auto-appear. Works great for driving physical hardware too: any ESPHome/ESP32 device
can subscribe to the same `wiretap/…` topics.

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
      - name: "Factorio accumulator charge"
        unit_of_measurement: "%"
        value_template: >-
          {% set p = value_json.surfaces.nauvis.power.totals %}
          {{ (100 * p.accumulator_charge_joules / p.accumulator_capacity_joules)
             | round(1) if p.accumulator_capacity_joules else 0 }}
      - name: "Factorio iron plates in logistics"
        value_template: >-
          {{ value_json.surfaces.nauvis.get('logistics', {}).get('player', {})
             .get('items', {}).get('iron-plate', 0) }}
      - name: "Factorio players online"
        value_template: "{{ value_json.meta.player_count }}"
```

Note the `.get(..., {})` chaining on the iron-plate sensor: the mod omits empty
sections entirely, so intermediate keys like `logistics` are absent until you build
your first roboport — a plain dotted path would throw a template error until then.
The same applies to `power`, `production`, and `science_packs`.

Swap `nauvis` for any surface name (e.g. `vulcanus`, or a platform) to monitor other
worlds. Platform names contain hyphens, so use bracket syntax for them:
`value_json.surfaces['platform-1'].power.totals.production_watts`.

### Option C: RCON (no file, no bridge)

If your server has RCON enabled, poll the mod directly — the command prints the same
JSON snapshot to the RCON client:

```bash
# any rcon client, e.g. `pip install factorio-rcon-py`
python3 -c "
import factorio_rcon
client = factorio_rcon.RCONClient('127.0.0.1', 27015, 'rconpassword')
print(client.send_command('/wiretap'))
"
```

Other mods can also call `remote.call("wiretap", "stats")` (table) or
`remote.call("wiretap", "stats_json")` (string). `/wiretap write` forces an
immediate file export.

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
`factorio_logistic_item_count{item=…}`, `factorio_accumulator_charge_joules`,
`factorio_science_pack_consumption_per_minute{item=…}` (your live SPM),
`factorio_technologies_researched`, `factorio_evolution_factor`,
`factorio_stats_age_seconds` (alert if it grows — the game is paused, crashed, or
the mod is disabled).
