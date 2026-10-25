# wiretap

A Factorio 2.1 mod plus a Python bridge that export the state of a factory to
Home Assistant, Prometheus/Grafana and MQTT.

## How it fits together

```
Factorio (mod/wiretap)  --write_file-->  script-output/wiretap/stats.json
                                                 |
                        companion/wiretap_bridge.py (polls the file)
                           |              |                |
                        /stats        /metrics          MQTT + HA discovery
```

- The mod runs inside the game's Lua sandbox, which can only write files under
  `script-output`. Every interval it builds one snapshot table, encodes it to JSON
  itself and writes it. It also answers `/wiretap` over RCON and exposes a remote
  interface for other mods.
- The bridge is a stdlib-only Python process that watches the file and re-serves
  it. MQTT and Home Assistant discovery need `paho-mqtt`.

## Pages

- [Snapshot schema](schema.md): every field the mod writes, its unit and when it is
  absent. Read when building a dashboard or template.
- [Testing](testing.md): unit tests, the real-engine smoke test in the free headless
  server, and checking your own save.
- [Releasing](releasing.md): versioning, packaging, uploading to the mod portal,
  deploying to a local install.

The repository README has the install and Home Assistant walkthroughs.

## Supported game versions

| wiretap | Factorio |
| --- | --- |
| 1.1.x | 2.1 (2.1.15 or newer) |
| 1.0.x | 2.0 |

A mod zip declares one `factorio_version`; the game refuses to load a 2.0 mod on 2.1
("Incompatible Factorio version"), so the two lines cannot be merged.
