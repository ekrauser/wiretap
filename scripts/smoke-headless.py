#!/usr/bin/env python3
"""Run the wiretap mod inside a real headless Factorio server.

Downloads the free headless Linux server if needed (no account required), installs
the newest mod zip from dist/, creates a fresh Space Age map, seeds a tiny factory
over RCON (power with a deliberate brownout network, a lab doing research, a
roboport with robots, a kill, a space platform with solar panels) and checks the
snapshot the mod writes. Exit status 0 means the real engine agrees with the schema.

  ./scripts/package.sh && ./scripts/smoke-headless.py --download

Everything lives under --factorio-dir (default ~/.cache/wiretap/headless/factorio).
"""
import argparse
import glob
import json
import os
import pathlib
import shutil
import socket
import struct
import subprocess
import sys
import tarfile
import time
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
DOWNLOAD = "https://factorio.com/get-download/{channel}/headless/linux64"

SEED = r"""
local s = game.surfaces.nauvis
local f = game.forces.player
local made = {}
local function mk(surface, name, x, y, fn)
  local e = surface.create_entity{name = name, position = {x, y}, force = f}
  made[#made + 1] = name .. (e and "" or "(FAILED)")
  if e and fn then fn(e) end
  return e
end
for _, e in pairs(s.find_entities_filtered{area = {{-14, -18}, {16, 12}}}) do if e.type ~= "character" then e.destroy() end end
-- network 1: a 10 MW source (electric-energy-interface counts as accumulator-tier output), a lab, a roboport, a chest
mk(s, "medium-electric-pole", 0, 0)
mk(s, "electric-energy-interface", -2, 0, function(e) e.electric_buffer_size = 2e7; e.power_production = 1e7 / 60; e.power_usage = 0 end)
mk(s, "accumulator", 0, 2)
mk(s, "lab", 0, -2, function(e) e.insert{name = "automation-science-pack", count = 50} end)
mk(s, "medium-electric-pole", 5, 0)
mk(s, "roboport", 8, 0, function(e) e.insert{name = "logistic-robot", count = 5}; e.insert{name = "construction-robot", count = 3} end)
mk(s, "storage-chest", 8, 5, function(e) e.insert{name = "iron-plate", count = 100} end)
-- network 2: a lab with packs and no power source at all (a permanent brownout)
mk(s, "medium-electric-pole", 0, -12)
mk(s, "lab", 0, -15, function(e) e.insert{name = "automation-science-pack", count = 10} end)
-- research: the first 2.x technologies are trigger techs, so mark the prerequisites done
local function unlock(name) local t = f.technologies[name]; if t then for _, pre in pairs(t.prerequisites) do unlock(pre.name) end; t.researched = true end end
for _, pre in pairs(f.technologies["automation"].prerequisites) do unlock(pre.name) end
made[#made + 1] = "research=" .. tostring(f.add_research("automation"))
s.pollute({0, 0}, 500)
local b = s.create_entity{name = "small-biter", position = {12, 10}, force = "enemy"}
if b then b.die(f) end
local ok, err = pcall(function()
  local p = f.create_space_platform{name = "Smoke", planet = "nauvis", starter_pack = "space-platform-starter-pack"}
  p.apply_starter_pack()
  local ps = p.surface
  local tiles = {}
  for x = -10, 10 do for y = -10, 10 do tiles[#tiles + 1] = {name = "space-platform-foundation", position = {x, y}} end end
  ps.set_tiles(tiles)
  mk(ps, "solar-panel", -7, 0); mk(ps, "solar-panel", 7, 0); mk(ps, "accumulator", 0, 7)
  made[#made + 1] = "platform " .. p.name .. " on " .. ps.name
end)
if not ok then made[#made + 1] = "platform failed: " .. tostring(err) end
rcon.print(table.concat(made, ", "))
"""


def rcon(host, port, password, command, timeout=15):
    def packet(pid, ptype, body):
        data = struct.pack("<ii", pid, ptype) + body.encode() + b"\x00\x00"
        return struct.pack("<i", len(data)) + data

    def read(sock):
        size = struct.unpack("<i", sock.recv(4))[0]
        data = b""
        while len(data) < size:
            chunk = sock.recv(size - len(data))
            if not chunk:
                break
            data += chunk
        pid, _ = struct.unpack("<ii", data[:8])
        return pid, data[8:-2].decode(errors="replace")

    with socket.create_connection((host, port), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall(packet(1, 3, password))
        pid, _ = read(s)
        if pid == -1:
            raise RuntimeError("rcon auth failed")
        s.sendall(packet(2, 2, command))
        _, body = read(s)
        return body


def ensure_server(root: pathlib.Path, channel: str) -> pathlib.Path:
    binary = root / "bin" / "x64" / "factorio"
    if binary.exists():
        return binary
    root.parent.mkdir(parents=True, exist_ok=True)
    tarball = root.parent / f"factorio-headless-{channel}.tar.xz"
    if not tarball.exists():
        print(f"downloading the {channel} headless server...", flush=True)
        urllib.request.urlretrieve(DOWNLOAD.format(channel=channel), tarball)
    with tarfile.open(tarball) as tf:
        tf.extractall(root.parent)  # creates <parent>/factorio
    extracted = root.parent / "factorio"
    if extracted != root:
        shutil.move(str(extracted), str(root))
    return binary


def install_mod(root: pathlib.Path, mod_zip: pathlib.Path, extra_mods: list[str]) -> None:
    mods = root / "mods"
    mods.mkdir(exist_ok=True)
    for old in mods.glob("wiretap_*.zip"):
        old.unlink()
    shutil.copy(mod_zip, mods)
    names = ["base", "space-age", "quality", "elevated-rails", "wiretap"] + extra_mods
    (mods / "mod-list.json").write_text(json.dumps({"mods": [{"name": n, "enabled": True} for n in names]}, indent=2))


def write_server_settings(root: pathlib.Path) -> pathlib.Path:
    example = json.loads((root / "data" / "server-settings.example.json").read_text())
    settings = {k: v for k, v in example.items() if not k.startswith("_comment")}
    settings.update({"name": "wiretap smoke", "description": "local smoke test", "visibility": {"public": False, "lan": False},
                     "auto_pause": False, "auto_pause_when_players_connect": False, "autosave_interval": 0,
                     "autosave_slots": 1, "non_blocking_saving": False, "require_user_verification": False, "max_players": 1})
    path = root / "server-settings.json"
    path.write_text(json.dumps(settings, indent=2))
    return path


def wait_for_rcon(proc, port, password, deadline_s):
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        if proc.poll() is not None:
            return None
        try:
            return rcon("127.0.0.1", port, password, "/version").strip()
        except OSError:
            time.sleep(1)
    return None


def wait_for_ticking(port, password):
    """Commands sent in the first moments after load can be queued past their
    RCON reply; wait until the clock is visibly running."""
    last = -1
    for _ in range(60):
        tick = int(rcon("127.0.0.1", port, password, "/sc rcon.print(game.tick)").strip() or 0)
        if last >= 0 and tick > last + 30:
            return tick
        last = tick
        time.sleep(1)
    return last


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--factorio-dir", default=os.path.expanduser("~/.cache/wiretap/headless/factorio"))
    ap.add_argument("--download", action="store_true", help="download and extract the headless server if missing")
    ap.add_argument("--channel", default="stable", choices=["stable", "experimental"])
    ap.add_argument("--mod-zip", default=None, help="mod zip to install (default: newest dist/wiretap_*.zip)")
    ap.add_argument("--extra-mod", action="append", default=[], help="enable another mod already present in mods/")
    ap.add_argument("--no-seed", dest="seed", action="store_false", help="do not build the test factory")
    ap.add_argument("--wait", type=float, default=38.0, help="seconds to let the factory run (two 10 s exports)")
    ap.add_argument("--rcon-port", type=int, default=27015)
    ap.add_argument("--game-port", type=int, default=34197)
    ap.add_argument("--password", default="wiretap")
    ap.add_argument("--keep-save", action="store_true", help="reuse saves/smoke.zip instead of a fresh map")
    args = ap.parse_args()

    root = pathlib.Path(args.factorio_dir)
    if not (root / "bin" / "x64" / "factorio").exists() and not args.download:
        sys.exit(f"no headless server at {root}; pass --download")
    binary = ensure_server(root, args.channel)
    mod_zip = pathlib.Path(args.mod_zip) if args.mod_zip else pathlib.Path(sorted(glob.glob(str(REPO / "dist" / "wiretap_*.zip")))[-1])
    install_mod(root, mod_zip, args.extra_mod)
    settings = write_server_settings(root)
    save = root / "saves" / "smoke.zip"
    save.parent.mkdir(exist_ok=True)
    out_json = root / "script-output" / "wiretap" / "stats.json"
    if out_json.exists():
        out_json.unlink()
    if save.exists() and not args.keep_save:
        save.unlink()
    if not save.exists():
        print("creating a fresh map...", flush=True)
        subprocess.run([str(binary), "--create", str(save)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    log_path = root / "smoke-server.log"
    log = open(log_path, "w")
    proc = subprocess.Popen([str(binary), "--start-server", str(save), "--server-settings", str(settings),
                             "--port", str(args.game_port), "--rcon-bind", f"127.0.0.1:{args.rcon_port}",
                             "--rcon-password", args.password], stdout=log, stderr=subprocess.STDOUT)
    failures: list[str] = []
    cmd = lambda c: rcon("127.0.0.1", args.rcon_port, args.password, c)  # noqa: E731
    try:
        version = wait_for_rcon(proc, args.rcon_port, args.password, 180)
        if version is None:
            sys.exit(f"server did not come up, see {log_path}")
        print(f"server up: Factorio {version}, mod {mod_zip.name}")
        wait_for_ticking(args.rcon_port, args.password)
        snap = json.loads(cmd("/wiretap"))
        print(f"/wiretap ok: mod {snap['meta']['mod_version']} game {snap['meta']['game_version']} tick {snap['meta']['tick']}")
        if args.seed:
            made = ""
            for _ in range(3):
                made = cmd("/silent-command " + SEED.strip()).strip()  # newlines kept: a comment must not eat the rest
                if made:
                    break
                time.sleep(2)
            print("seeded:", made)
            if not made or "FAILED" in made or "failed" in made:
                failures.append(f"seed problem: {made or 'no output'}")
        print(f"running {args.wait:.0f} s for the timer exports...", flush=True)
        time.sleep(args.wait)
        if out_json.exists():
            timer = json.loads(out_json.read_text(encoding="utf-8"))
            print(f"timer export: tick {timer['meta']['tick']}, export_ticks {timer['meta'].get('export_ticks')}, {out_json.stat().st_size} bytes")
            if timer["meta"].get("export_ticks") is None:
                failures.append("timer export lacks meta.export_ticks")
        else:
            failures.append("no timer export within the wait")
        reply = cmd("/wiretap write").strip()
        if "wrote" not in reply:
            failures.append(f"/wiretap write replied: {reply}")
        time.sleep(1)
        data = json.loads(out_json.read_text(encoding="utf-8"))
        nauvis = data["surfaces"]["nauvis"]
        totals = nauvis["power"]["totals"]
        nets = nauvis["power"].get("networks") or {}
        research = data["forces"]["player"]["research"]
        plats = [n for n, s_ in data["surfaces"].items() if s_.get("is_platform")]
        print("nauvis totals:", json.dumps(totals))
        print("research:", json.dumps({k: research.get(k) for k in ("current", "progress", "eta_seconds", "progress_per_minute", "science_per_minute", "labs")}))
        print("platforms:", json.dumps(data["forces"]["player"].get("platforms")))
        if args.seed:
            if len(nets) != 2:
                failures.append(f"expected 2 networks on nauvis, got {list(nets)}")
            if not totals["production_watts"] > 1e5:
                failures.append(f"production_watts {totals['production_watts']} too low")
            if abs(totals["production_watts"] - totals["consumption_watts"]) > 0.02 * totals["production_watts"]:
                failures.append("production and consumption disagree on a source-limited grid")
            if not (0 < totals["consumption_satisfaction"] < 1):
                failures.append(f"surface satisfaction {totals['consumption_satisfaction']} should show the unpowered lab")
            if not any((n.get("flow") or {}).get("consumption_satisfaction") == 0 for n in nets.values()):
                failures.append("no network with zero satisfaction")
            if not all((n.get("flow") or {}).get("consumption_satisfaction_min") is not None for n in nets.values()):
                failures.append("a network lacks consumption_satisfaction_min")
            if totals.get("consumption_satisfaction_min") is None:
                failures.append("surface lacks consumption_satisfaction_min")
            if totals["accumulator_capacity_joules"] <= 0:
                failures.append("no accumulator capacity")
            if research.get("current") != "automation" or not research.get("progress", 0) > 0:
                failures.append(f"research not progressing: {research.get('current')} {research.get('progress')}")
            if research.get("eta_seconds") is None:
                failures.append("no research ETA after two exports")
            if (research.get("science_per_minute") or 0) <= 0:
                failures.append("science_per_minute not positive")
            items = (nauvis.get("logistics") or {}).get("player", {}).get("items", {})
            if items.get("iron-plate") != 100:
                failures.append(f"logistic iron-plate {items.get('iron-plate')} != 100")
            kills = (data["forces"]["player"].get("kills") or {}).get("nauvis", {})
            if (kills.get("kills_total") or {}).get("small-biter") != 1:
                failures.append(f"kills: {kills}")
            if not plats:
                failures.append("no platform surface")
            else:
                gnet = (data["surfaces"][plats[0]]["power"].get("networks") or {}).get("global")
                if not gnet or gnet.get("accumulator_capacity_joules", 0) <= 0:
                    failures.append(f"platform {plats[0]} global network missing or without accumulators: {gnet}")
                if not data["forces"]["player"].get("platforms", {}).get(plats[0]):
                    failures.append("platform missing from forces.player.platforms")
    finally:
        try:
            cmd("/quit")
        except OSError:
            pass
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
    lines = log_path.read_text(errors="replace").splitlines()
    errors = [l for l in lines if "wiretap" in l.lower() and ("error" in l.lower() or "failed" in l.lower())]
    errors += [l for l in lines if "Error" in l and "ModManager" in l]
    if errors:
        failures.append("log errors: " + " | ".join(errors[:5]))
    if failures:
        print("SMOKE FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
