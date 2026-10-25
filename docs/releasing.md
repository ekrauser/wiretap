# Testing and releasing

## Tests (no game needed)

```bash
./scripts/test.sh
```

- `tests/lua/` runs `mod/wiretap/control.lua` under a mocked Factorio 2.1 runtime
  (`mock.lua`) against a representative world (`world.lua`): a planet with a
  switch-joined electric network, accumulators and logistics, a platform with a
  global network, two forces, research and a travelling platform.
  `test_control.lua` asserts on the JSON text, the timer wiring, multiplayer
  `for_player` handling, setting toggles, encoder edge cases and error handling.
  Factorio embeds Lua 5.2, so the tests run under Lua 5.2 (`lua5.2`, or
  `mise exec lua@5.2.4 -- lua`).
- `tests/python/` tests the bridge: Prometheus rendering (family grouping, values,
  escaping), slug collisions, MQTT topics and Home Assistant discovery payloads
  through a fake client, and the file watcher's torn-write retry. The fixture
  `tests/python/fixtures/snapshot-mock.json` is what the Lua world produces;
  regenerate it after changing the mod's output:

  ```bash
  mise exec lua@5.2.4 -- lua tests/lua/dump_snapshot.lua > tests/python/fixtures/snapshot-mock.json
  ```

The authoritative API reference is the `runtime-api.json` shipped with the game
(`<Factorio install>/doc-html/runtime-api.json`); the online copy is
<https://lua-api.factorio.com/latest/>.

## Versioning

- `mod/wiretap/info.json` `version` is the single source: the mod reads its own
  version from the mod list at runtime, and the Python tests check that the newest
  `changelog.txt` entry matches it.
- `factorio_version` is the game's major.minor (`"2.1"`); `dependencies` pins the oldest
  2.1 build whose API the mod uses (`base >= 2.1.15`). A zip loads only on that
  line, so a 2.0 and a 2.1 release are separate uploads.
- Every release gets a `changelog.txt` entry in Factorio's strict format: a line of
  99 dashes, `Version: x.y.z`, `Date: YYYY-MM-DD`, categories indented two spaces
  with a trailing colon, entries as `    - text`, continuation lines indented six
  spaces. Malformed files are rejected by the portal.

## Package

```bash
./scripts/package.sh      # dist/wiretap_<version>.zip (dist/ is not tracked)
unzip -l dist/wiretap_<version>.zip
```

The zip contains `wiretap_<version>/` with `info.json`, `control.lua`,
`settings.lua`, `changelog.txt`, `LICENSE`, `thumbnail.png` (144x144) and `locale/`.

## Upload to the mod portal

1. <https://mods.factorio.com/mod/wiretap/downloads> → "Upload a new version"
   (account `drkraus`), pick the zip. The portal reads version, factorio_version
   and changelog from the zip.
2. Check the mod page renders the changelog entry.
3. Tag the commit (`git tag v<version>`) and attach the zip to a GitHub release.

## Deploy to a local install

Windows (Steam), developed from WSL:

```bash
cp dist/wiretap_<version>.zip /mnt/c/Users/<you>/AppData/Roaming/Factorio/mods/
```

Factorio picks the highest version present at the next launch, so older zips can
stay until the game is closed (it locks the loaded zip). The snapshot lands in
`/mnt/c/Users/<you>/AppData/Roaming/Factorio/script-output/wiretap/stats.json`;
`factorio-current.log` records load failures such as
`Incompatible Factorio version`.
