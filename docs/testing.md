# Testing

Three levels, from seconds to minutes. All run from WSL or any Linux box; none needs
the Windows game or a Factorio account.

## 1. Unit tests (seconds)

```bash
./scripts/test.sh
```

`tests/lua/` runs `control.lua` under a mocked Factorio 2.1 runtime built from the
game's `runtime-api.json`; `tests/python/` tests the bridge. Fast and exact, but a
mock only knows what we taught it.

## 2. Real engine, fresh map (about a minute)

```bash
./scripts/package.sh
./scripts/smoke-headless.py --download
```

Factorio's headless Linux server is a free 61 MB download that ships with the Space
Age, quality and elevated-rails mods. The script extracts it to
`~/.cache/wiretap/headless/factorio`, installs the newest `dist/wiretap_*.zip`,
creates a fresh map, starts a server with RCON and `auto_pause` off, and over RCON:

- asks `/wiretap` (the synchronous path),
- seeds a small factory: a 10 MW source with a lab, an accumulator, a roboport with
  robots and a chest of iron plates; a second pole with a powered-off lab (a permanent
  brownout); research of `automation` with its trigger prerequisites marked done; a
  killed biter; a space platform with foundation, two solar panels and an accumulator,
- waits for two timer exports (so the research ETA has two samples), then asserts on
  `stats.json`: two networks on nauvis, source-limited production equals consumption,
  surface satisfaction between 0 and 1, a network at 0, minimums present, research
  progressing with an ETA and a positive science rate, 100 iron plates in logistics,
  the kill, the platform's `global` network with accumulators, no wiretap errors in the
  server log.

`SMOKE OK` means the real engine agrees with `docs/schema.md`. Facts the script
encodes that cost an afternoon to learn: RCON commands sent in the first second after
load can be answered before they run (wait for `game.tick` to advance); multi-line
Lua must keep its newlines or a `--` comment eats the rest; `automation` cannot be
queued until the 2.x trigger technologies before it are done; a new platform has
foundation only under its hub; `electric-energy-interface` is a tertiary source and
shows up as accumulator-tier output.

## 3. Real engine, real save (about a minute, plus the save's load time)

```bash
./scripts/check-save.py --download \
    --save "/mnt/c/Users/<you>/AppData/Roaming/Factorio/saves/<save>.zip" \
    --mods-from /mnt/c/Users/<you>/AppData/Roaming/Factorio/mods \
    --disable even-distribution
```

Copies the save, the enabled mod zips and `mod-settings.dat` into a second headless
install (`~/.cache/wiretap/headless-real/factorio`), loads it, prints what `/wiretap`
returns for every surface and force, waits for a timer export and checks the log. The
original save is never written. Mods that crash on the current game version must be
disabled; on 2026-10-25 `even-distribution 2.1.0` failed in `on_configuration_changed`
on Factorio 2.1.23.

Observed on 2026-10-25 with a 137-hour Space Age save (21 mods, 7 surfaces, 3
platforms): the server was up in 4 s, `/wiretap` took 0.46 s for a 46 KB snapshot, the
timer export spread over 11 ticks, platforms were keyed `global`, Gleba showed a real
brownout (5 MW produced against 256 MW demand), and the asteroid kills on a platform
appeared under `kills`. That run is what motivated the demand-weighted surface
minimum in 1.1.1: one unpowered 5 MW block on Nauvis had pinned the per-network
minimum at 0 while 98.5% of demand was met.

## 4. The Windows game

Copy `dist/wiretap_<version>.zip` into `%APPDATA%\Factorio\mods\`, launch, load a
save, then check `%APPDATA%\Factorio\factorio-current.log` for `wiretap` lines and
`script-output\wiretap\stats.json` for `"mod_version"`. Run the bridge against that
file to see the Home Assistant side. From WSL the paths are under
`/mnt/c/Users/<you>/AppData/Roaming/Factorio/`.

## Cleaning up

`rm -rf ~/.cache/wiretap` removes both headless installs and the cached download.
The headless server listens on UDP 34197 (smoke) and 34198 (check-save) and RCON on
127.0.0.1:27015 and 27017 while running.
