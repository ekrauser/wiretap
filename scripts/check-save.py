#!/usr/bin/env python3
"""Load a real save with its mods in a headless Factorio server and report what
wiretap exports: the strongest test there is, short of playing.

  ./scripts/check-save.py --save "/mnt/c/Users/me/AppData/Roaming/Factorio/saves/age of space.zip" \
      --mods-from /mnt/c/Users/me/AppData/Roaming/Factorio/mods --disable even-distribution

Copies the save, the enabled mod zips listed in that folder's mod-list.json and its
mod-settings.dat into --factorio-dir (default ~/.cache/wiretap/headless-real/factorio,
downloaded with --download), installs the newest dist/wiretap_*.zip, starts the
server, asks /wiretap over RCON, waits for a timer export and prints a summary.
Exit status 0 when the mod loaded, both snapshots parsed and the log has no
wiretap errors. The original save is never touched.
"""
import argparse
import glob
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from importlib import util as _util  # noqa: E402

_spec = _util.spec_from_file_location("smoke", pathlib.Path(__file__).resolve().parent / "smoke-headless.py")
smoke = _util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)

REPO = pathlib.Path(__file__).resolve().parent.parent
BUILTIN = {"base", "elevated-rails", "quality", "recycler", "space-age"}


def copy_mods(src: pathlib.Path, dst: pathlib.Path, disable: set[str], mod_zip: pathlib.Path) -> list[str]:
    dst.mkdir(parents=True, exist_ok=True)
    for old in dst.glob("*.zip"):
        old.unlink()
    listed = json.loads((src / "mod-list.json").read_text())["mods"]
    enabled = [m["name"] for m in listed if m["enabled"] and m["name"] not in disable]
    for name in enabled:
        if name in BUILTIN or name == "wiretap":
            continue
        zips = sorted(glob.glob(str(src / f"{name}_*.zip")))
        if not zips:
            print(f"warning: no zip for enabled mod {name} (folder install?)", file=sys.stderr)
            continue
        shutil.copy(zips[-1], dst)
    shutil.copy(mod_zip, dst)
    if "wiretap" not in enabled:
        enabled.append("wiretap")
    settings = src / "mod-settings.dat"
    if settings.exists():
        shutil.copy(settings, dst)
    (dst / "mod-list.json").write_text(json.dumps({"mods": [{"name": n, "enabled": True} for n in enabled]}, indent=2))
    return enabled


def summarize(snap: dict) -> None:
    meta = snap["meta"]
    print("meta:", json.dumps({k: meta.get(k) for k in ("mod_version", "game_version", "tick", "game_time_seconds", "player_count", "export_ticks")}),
          "mods:", len(meta.get("mods", {})))
    for name, s in snap["surfaces"].items():
        power = s.get("power", {})
        tot = power.get("totals", {})
        print(f"  {name}: planet={s.get('planet')} platform={s.get('is_platform')} pollutant={s.get('pollutant')} "
              f"networks={list((power.get('networks') or {}))[:5]} prod={tot.get('production_watts')} cons={tot.get('consumption_watts')} "
              f"sat={tot.get('consumption_satisfaction')} sat_min={tot.get('consumption_satisfaction_min')} "
              f"acc={tot.get('accumulator_charge_joules')}/{tot.get('accumulator_capacity_joules')}")
    for fname, force in snap.get("forces", {}).items():
        r = force.get("research")
        if r:
            print(f"  force {fname}: research={r.get('current')} {r.get('progress')} eta={r.get('eta_seconds')} spm10m={r.get('science_per_minute_10m')} "
                  f"techs={r.get('technologies_researched')}/{r.get('technologies_total')} rockets={force.get('rockets_launched')} "
                  f"platforms={[ (k, v.get('state'), v.get('destination')) for k, v in (force.get('platforms') or {}).items()]}")
        if force.get("evolution"):
            print(f"  force {fname}: evolution={force['evolution']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--save", required=True, help="save zip to copy and load")
    ap.add_argument("--mods-from", required=True, help="a Factorio mods folder with mod-list.json (and mod-settings.dat)")
    ap.add_argument("--disable", default="", help="comma-separated mods to leave disabled")
    ap.add_argument("--factorio-dir", default=os.path.expanduser("~/.cache/wiretap/headless-real/factorio"))
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--channel", default="stable", choices=["stable", "experimental"])
    ap.add_argument("--mod-zip", default=None)
    ap.add_argument("--wait", type=float, default=30.0, help="seconds to wait for timer exports")
    ap.add_argument("--rcon-port", type=int, default=27017)
    ap.add_argument("--game-port", type=int, default=34198)
    ap.add_argument("--password", default="wiretap")
    ap.add_argument("--load-timeout", type=float, default=900.0)
    args = ap.parse_args()

    root = pathlib.Path(args.factorio_dir)
    if not (root / "bin" / "x64" / "factorio").exists() and not args.download:
        sys.exit(f"no headless server at {root}; pass --download")
    binary = smoke.ensure_server(root, args.channel)
    mod_zip = pathlib.Path(args.mod_zip) if args.mod_zip else pathlib.Path(sorted(glob.glob(str(REPO / "dist" / "wiretap_*.zip")))[-1])
    enabled = copy_mods(pathlib.Path(args.mods_from), root / "mods", {d.strip() for d in args.disable.split(",") if d.strip()}, mod_zip)
    print(f"mods: {', '.join(enabled)}")
    (root / "saves").mkdir(exist_ok=True)
    save = root / "saves" / "check-save.zip"
    shutil.copy(args.save, save)
    settings = smoke.write_server_settings(root)
    out_json = root / "script-output" / "wiretap" / "stats.json"
    if out_json.exists():
        out_json.unlink()
    log_path = root / "check-save.log"
    log = open(log_path, "w")
    t0 = time.time()
    proc = subprocess.Popen([str(binary), "--start-server", str(save), "--server-settings", str(settings),
                             "--port", str(args.game_port), "--rcon-bind", f"127.0.0.1:{args.rcon_port}",
                             "--rcon-password", args.password], stdout=log, stderr=subprocess.STDOUT)
    cmd = lambda c: smoke.rcon("127.0.0.1", args.rcon_port, args.password, c, timeout=120)  # noqa: E731
    failures: list[str] = []
    try:
        version = smoke.wait_for_rcon(proc, args.rcon_port, args.password, args.load_timeout)
        if version is None:
            lines = log_path.read_text(errors="replace").splitlines()
            errs = [l for l in lines if "Error" in l]
            sys.exit("server did not come up: " + (" | ".join(errs[-3:]) if errs else f"see {log_path}"))
        print(f"server up after {time.time() - t0:.0f} s: Factorio {version}, mod {mod_zip.name}")
        smoke.wait_for_ticking(args.rcon_port, args.password)
        t1 = time.time()
        snap = json.loads(cmd("/wiretap"))
        print(f"/wiretap took {time.time() - t1:.2f} s, {len(json.dumps(snap))} bytes")
        summarize(snap)
        print(f"waiting {args.wait:.0f} s for timer exports...", flush=True)
        time.sleep(args.wait)
        if out_json.exists():
            timer = json.loads(out_json.read_text(encoding="utf-8"))
            print(f"timer export: {out_json.stat().st_size} bytes, tick {timer['meta']['tick']}, export_ticks {timer['meta'].get('export_ticks')}")
        else:
            failures.append("no timer export within the wait")
    finally:
        try:
            cmd("/quit")
        except OSError:
            pass
        try:
            proc.wait(timeout=180)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
    lines = log_path.read_text(errors="replace").splitlines()
    errors = [l for l in lines if "wiretap" in l.lower() and ("error" in l.lower() or "failed" in l.lower())]
    if errors:
        failures.append("log errors: " + " | ".join(errors[:5]))
    if failures:
        print("CHECK FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("CHECK OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
