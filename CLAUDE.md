# wiretap

Factorio 2.1 mod (`mod/wiretap`, Lua 5.2) plus a stdlib-only Python bridge
(`companion/wiretap_bridge.py`) that export game stats to Home Assistant,
Prometheus and MQTT. Read `README.md` first for what is exported and how it is
consumed.

## Working rules

- **The snapshot schema is a contract.** Dashboards and templates read it by key.
  Add fields; never rename, re-unit or restructure an existing one. Empty tables
  are omitted, never emitted. Document every new field in `docs/schema.md`.
- **The export must be deterministic.** In multiplayer the handler runs on every
  client; it may only read game state, and the only `storage` write is
  `interval_ticks` from a setting event. No wall clock, no `math.random`, nothing
  conditional on the local machine.
- **Every value the mod exports must reach MQTT and Home Assistant.** The owner
  watches the factory from HA and from ESPHome/MQTT devices, so a new snapshot field
  gets a JSON topic, a flat scalar topic under `<prefix>/flat/` for microcontrollers,
  a discovery sensor where it is a dashboard value, and a Prometheus family. Update
  the README topic table and `tests/python/test_bridge.py` with it.
- **Encode JSON in the mod, not with `helpers.table_to_json`.** The engine prints
  lossy 30+ digit expansions for most fractional doubles (open Factorio issue), which
  defeats the precision setting and bloats the file.
- **Electric networks are 2.1 objects.** Enumerate `LuaElectricNetwork`s through
  pole `electric_network.parent_network` and `surface.global_electric_network`;
  key them by the lowest sub network id. Per-pole statistics double count a
  network joined by a closed power switch.
- **Check the API against the game's own dump** before using a member:
  `<Factorio install>/doc-html/runtime-api.json` for the installed version (on the
  owner's machine `/mnt/e/SteamLibrary/steamapps/common/Factorio/doc-html/`), or
  <https://lua-api.factorio.com/latest/>. `info.json` `factorio_version` must equal
  the game's major.minor or the mod is refused at load.
- **Run `./scripts/test.sh` before packaging.** It needs Lua 5.2 and Python 3; no
  game. Regenerate `tests/python/fixtures/snapshot-mock.json` when the mod's output
  changes (`docs/releasing.md`).
- Bump `version` in `info.json` (the mod reads it at runtime), add a
  `changelog.txt` entry in Factorio's strict format, then `./scripts/package.sh`.
  `dist/` is not tracked; releases go to the mod portal and GitHub releases.
- The owner plays on Windows (Steam) and develops in WSL; Windows executables cannot
  be run from this WSL, so test in-game behaviour by reading
  `factorio-current.log` and the written `stats.json`, never by launching the game.
- The catalog entry for this repo lives in the handbook
  (`catalog/components-projects.yaml`), not in a `catalog-info.yaml` here.

## Docs map

- `README.md` — read first: what is exported, install, bridge, Home Assistant,
  Prometheus.
- `docs/index.md` — TechDocs landing page and version matrix.
- `docs/schema.md` — read when adding or consuming a snapshot field: every field,
  unit and when it is absent.
- `docs/releasing.md` — read before cutting a release: tests, versioning,
  changelog format, packaging, portal upload, local deploy.
- `mod/wiretap/changelog.txt` — user-facing changelog, shown in game and on the
  portal.
- `tests/lua/mock.lua` — read when the mod needs a new API member: the mocked
  runtime must learn it too.
- `LICENSE` — MIT, matching the mod portal's declared license.
