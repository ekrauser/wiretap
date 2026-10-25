#!/usr/bin/env python3
"""wiretap bridge — ship Factorio stats to Home Assistant and friends.

Watches the JSON snapshot written by the wiretap Factorio mod
(script-output/wiretap/stats.json, or the NDJSON journal) and re-exposes it as:

  * HTTP JSON API .......... GET /stats (full snapshot), /health, /
  * Prometheus metrics ..... GET /metrics (HELP/TYPE per family)
  * MQTT (optional) ........ JSON topics, flat scalar topics for ESPHome and
                             other microcontrollers, Home Assistant discovery

Only the Python standard library is required for HTTP/metrics (Python 3.8+).
MQTT support needs `pip install paho-mqtt` (1.x or 2.x) and --mqtt-host.

Examples:
  # Serve HTTP on :8787, reading the default Factorio script-output path
  python3 wiretap_bridge.py --file ~/.factorio/script-output/wiretap/stats.json

  # Also publish to an MQTT broker with Home Assistant discovery
  WIRETAP_MQTT_PASSWORD=secret python3 wiretap_bridge.py --file .../stats.json \
      --mqtt-host 192.168.1.10 --mqtt-user ha \
      --ha-discovery --watch-items iron-plate,copper-plate,uranium-235
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BRIDGE_VERSION = "1.1.0"

# ----------------------------------------------------------------------------
# Shared state
# ----------------------------------------------------------------------------


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.snapshot: dict | None = None
        self.updated_at: float | None = None  # wall clock of last successful parse
        self.file_mtime: float | None = None  # mtime of the parsed file: when the mod wrote it
        self.parse_errors = 0

    def set(self, snapshot: dict, mtime: float) -> None:
        with self.lock:
            self.snapshot = snapshot
            self.updated_at = time.time()
            self.file_mtime = mtime

    def get(self) -> tuple[dict | None, float | None, float | None]:
        """Return (snapshot, file_mtime, updated_at)."""
        with self.lock:
            return self.snapshot, self.file_mtime, self.updated_at


STATE = State()


def _age(timestamp: float | None) -> float | None:
    """Seconds since `timestamp`, clamped at 0 (remote mounts can run ahead)."""
    return None if timestamp is None else max(0.0, time.time() - timestamp)


def _d(x) -> dict:
    """Coerce to dict: the mod omits empty sections, and 1.0.x files written
    with helpers.table_to_json could emit [] for empty tables."""
    return x if isinstance(x, dict) else {}


# ----------------------------------------------------------------------------
# File watcher
# ----------------------------------------------------------------------------


def parse_snapshot(text: str) -> dict:
    """Parse a snapshot file. A journal (NDJSON, one snapshot per line) yields
    its last complete line, so --file can point at stats.ndjson too."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        lines = [line for line in text.splitlines() if line.strip()]
        if len(lines) < 2:
            raise
        data = None
        for line in (lines[-1], lines[-2]):  # the last line may be mid-write
            try:
                data = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
        if data is None:
            raise
    if not isinstance(data, dict) or not isinstance(data.get("meta"), dict):
        raise ValueError("not a wiretap snapshot (no meta object)")
    return data


def watch_file(path: Path, poll_seconds: float, on_update) -> None:
    """Poll `path` for changes; parse and publish new snapshots.

    Factorio's write_file is not atomic from the reader's point of view, so a
    parse failure is expected occasionally: the signature changes once the
    write completes and the next poll retries. A file that stays unparseable
    is read once per signature, not on every poll.
    """
    last_sig = None
    bad_sig = None
    warned_missing = False
    while True:
        try:
            try:
                st = path.stat()
            except OSError as exc:  # missing file, but also transient EACCES/EIO
                if not warned_missing:  # (e.g. WSL drvfs or antivirus holding it)
                    print(f"[bridge] waiting for {path} ({exc.__class__.__name__}) "
                          "— is the mod installed and the save loaded?", flush=True)
                    warned_missing = True
                time.sleep(poll_seconds)
                continue
            warned_missing = False
            sig = (st.st_mtime_ns, st.st_size)
            if sig == last_sig or sig == bad_sig:
                time.sleep(poll_seconds)
                continue
            try:
                data = parse_snapshot(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError) as exc:
                STATE.parse_errors += 1
                after = path.stat()
                if (after.st_mtime_ns, after.st_size) != sig:
                    time.sleep(min(poll_seconds, 0.5))  # torn read mid-write; retry soon
                    continue
                bad_sig = sig
                print(f"[bridge] {path} is not a parseable snapshot ({exc}); "
                      "waiting for it to change", flush=True)
                time.sleep(poll_seconds)
                continue
            after = path.stat()
            if (after.st_mtime_ns, after.st_size) != sig:
                time.sleep(min(poll_seconds, 0.5))  # file changed under us; re-read
                continue
            last_sig = sig
            bad_sig = None
            STATE.set(data, st.st_mtime)
            try:
                on_update(data)
            except Exception as exc:  # MQTT hiccups must not kill the watcher
                print(f"[bridge] on_update error: {exc}", flush=True)
            time.sleep(poll_seconds)
        except Exception as exc:  # the watcher must never die silently
            print(f"[bridge] watcher error: {exc!r}", flush=True)
            time.sleep(max(poll_seconds, 1.0))


# ----------------------------------------------------------------------------
# Prometheus rendering
# ----------------------------------------------------------------------------

_LABEL_BAD = re.compile(r'([\\"\n])')


def _lv(value) -> str:
    """Escape a Prometheus label value."""
    return _LABEL_BAD.sub(lambda m: {"\\": r"\\", '"': r"\"", "\n": r"\n"}[m.group(1)], str(value))


def _num(x) -> str | None:
    """Format a sample value; None means 'no sample' (unknown is not zero)."""
    if isinstance(x, bool):
        return "1" if x else "0"
    if isinstance(x, int):
        return str(x)
    if isinstance(x, float):
        if x != x:
            return "NaN"
        if x in (float("inf"), float("-inf")):
            return "+Inf" if x > 0 else "-Inf"
        return repr(x)
    return None


# Metric families: name -> (type, help). Samples are grouped per family in the
# output so the exposition stays valid with HELP/TYPE lines. Names are kept
# stable across releases; new fields get new families.
_FAMILIES: dict[str, tuple[str, str]] = {
    "bridge_up": ("gauge", "1 while the bridge process is running."),
    "bridge_parse_errors_total": ("counter", "Snapshot reads that failed to parse (torn writes, retried on the next poll)."),
    "bridge_last_parse_timestamp_seconds": ("gauge", "Unix time the bridge last parsed the snapshot; resets when the bridge restarts."),
    "stats_age_seconds": ("gauge", "Seconds since the mod last wrote the snapshot (file mtime); alert if it grows: the game is paused, crashed or the mod is disabled."),
    "stats_file_mtime_timestamp_seconds": ("gauge", "Unix time the snapshot file was last written."),
    "mod_info": ("gauge", "1; labels carry the mod and game versions."),
    "game_tick": ("gauge", "Game tick of the latest snapshot (60 ticks per second of game time)."),
    "game_time_seconds": ("gauge", "Seconds of game time played on this save."),
    "game_speed": ("gauge", "Game speed; 1 is 60 updates per second."),
    "game_paused": ("gauge", "1 while the game tick is paused."),
    "players_online": ("gauge", "Connected players."),
    "player_afk_seconds": ("gauge", "Seconds since the player's last action."),
    "player_online_seconds": ("gauge", "Seconds the player has spent in this save."),
    "surface_info": ("gauge", "1 per surface; labels carry planet, platform flag and pollutant."),
    "surface_darkness": ("gauge", "Darkness on the surface, 0 (day) to 1 (night)."),
    "surface_daytime": ("gauge", "Time of day on the surface, 0 to 1."),
    "surface_solar_multiplier": ("gauge", "Solar power multiplier of the surface."),
    "pollution": ("gauge", "Total pollution (or spores) on the surface."),
    "power_production_watts": ("gauge", "Electric production on the surface, 5-second average, summed over networks."),
    "power_consumption_watts": ("gauge", "Electric consumption on the surface, 5-second average, summed over networks."),
    "accumulator_charge_joules": ("gauge", "Energy stored in accumulators on the surface."),
    "accumulator_capacity_joules": ("gauge", "Accumulator capacity on the surface."),
    "electric_network_count": ("gauge", "Electric networks with anything attached on the surface."),
    "power_consumption_satisfaction": ("gauge", "Demand-weighted fraction of electric demand met on the export tick; below 1 is a brownout."),
    "power_consumption_satisfaction_min": ("gauge", "Lowest consumption satisfaction of any network sampled every second since the previous export."),
    "power_maximum_production_watts": ("gauge", "Electric production available last tick, including accumulator discharge."),
    "power_maximum_consumption_watts": ("gauge", "Electric demand last tick, including accumulator charging."),
    "power_solar_watts": ("gauge", "Solar output offered last tick."),
    "power_accumulator_charge_watts": ("gauge", "Power flowing into accumulators last tick."),
    "power_accumulator_discharge_watts": ("gauge", "Power drawn from accumulators last tick."),
    "network_production_watts": ("gauge", "Production of one electric network, 5-second average."),
    "network_consumption_watts": ("gauge", "Consumption of one electric network, 5-second average."),
    "network_accumulator_charge_joules": ("gauge", "Energy stored in the network's accumulators."),
    "network_accumulator_capacity_joules": ("gauge", "Capacity of the network's accumulators."),
    "network_consumption_satisfaction": ("gauge", "Fraction of the network's demand met last tick."),
    "network_consumption_satisfaction_min": ("gauge", "Lowest consumption satisfaction of the network sampled since the previous export."),
    "network_production_satisfaction": ("gauge", "Fraction of the network's available production used last tick."),
    "network_maximum_production_watts": ("gauge", "Production available to the network last tick."),
    "network_maximum_consumption_watts": ("gauge", "Demand on the network last tick."),
    "network_solar_watts": ("gauge", "Solar output offered to the network last tick."),
    "network_solar_used_watts": ("gauge", "Solar output actually used last tick."),
    "network_primary_output_watts": ("gauge", "Primary generation (steam, nuclear, fusion) used last tick."),
    "network_secondary_output_watts": ("gauge", "Secondary generation used last tick."),
    "network_accumulator_charge_watts": ("gauge", "Power flowing into the network's accumulators last tick."),
    "network_accumulator_discharge_watts": ("gauge", "Power drawn from the network's accumulators last tick."),
    "network_transfer_watts": ("gauge", "Energy moved from producers to consumers last tick."),
    "network_sub_network_count": ("gauge", "Pole groups (sub networks) joined into this network by power switches or a global network."),
    "network_production_by_entity_watts": ("gauge", "Production of one entity type in the network, 5-second average."),
    "network_consumption_by_entity_watts": ("gauge", "Consumption of one entity type in the network, 5-second average."),
    "entity_count": ("gauge", "Entities of this type on the surface, all forces."),
    "logistic_network_count": ("gauge", "Logistic networks of the force on the surface."),
    "robots": ("gauge", "Robots in the force's logistic networks on the surface."),
    "logistic_item_count": ("gauge", "Items in the force's logistic networks on the surface (briefly negative while robots reserve items)."),
    "logistic_item_quality_count": ("gauge", "Items of one quality in the force's logistic networks on the surface."),
    "research_info": ("gauge", "1 per force with active research; labels carry the technology and level."),
    "research_progress": ("gauge", "Progress of the current research, 0 to 1."),
    "research_progress_per_minute": ("gauge", "Research progress gained per minute, from the last two exports."),
    "research_eta_seconds": ("gauge", "Estimated seconds until the current research completes."),
    "research_current_units": ("gauge", "Research units the current technology needs."),
    "research_queue_length": ("gauge", "Technologies in the research queue, including the current one."),
    "research_enabled": ("gauge", "1 if the force can research."),
    "labs": ("gauge", "Labs of the force on the surface."),
    "science_per_minute": ("gauge", "Research units completed per minute (the game's hidden science item), 1-minute average."),
    "science_per_minute_10m": ("gauge", "Research units completed per minute, 10-minute average."),
    "science_total": ("counter", "Research units completed, all time."),
    "technologies_researched": ("gauge", "Technologies researched by the force."),
    "technologies_total": ("gauge", "Technologies known to the force."),
    "technologies_available": ("gauge", "Enabled, visible, finite technologies of the force."),
    "technologies_infinite": ("gauge", "Infinite (level-based) technologies of the force."),
    "rockets_launched": ("counter", "Rockets launched by the force."),
    "rockets_launched_total": ("counter", "Rockets launched by the force."),
    "items_launched_total": ("counter", "Items launched in rockets by the force."),
    "evolution_factor": ("gauge", "Enemy evolution factor on the surface."),
    "evolution_factor_part": ("gauge", "Share of the evolution factor driven by pollution, time or spawner kills."),
    "science_pack_production_per_minute": ("gauge", "Science packs produced per minute, 1-minute average."),
    "science_pack_consumption_per_minute": ("gauge", "Science packs consumed per minute, 1-minute average."),
    "science_pack_production_per_minute_10m": ("gauge", "Science packs produced per minute, 10-minute average."),
    "science_pack_consumption_per_minute_10m": ("gauge", "Science packs consumed per minute, 10-minute average."),
    "science_pack_produced_total": ("counter", "Science packs produced, all time."),
    "science_pack_consumed_total": ("counter", "Science packs consumed, all time."),
    "kills_per_minute": ("gauge", "Enemies killed by the force on the surface per minute, 1-minute average."),
    "losses_per_minute": ("gauge", "Entities of the force lost on the surface per minute, 1-minute average."),
    "kills_total": ("counter", "Enemies of one type killed, all time."),
    "losses_total": ("counter", "Entities of one type lost, all time."),
    "items_produced_total": ("counter", "Items produced by the force on the surface, all time."),
    "items_consumed_total": ("counter", "Items consumed by the force on the surface, all time."),
    "fluids_produced_total": ("counter", "Fluid produced by the force on the surface, all time."),
    "fluids_consumed_total": ("counter", "Fluid consumed by the force on the surface, all time."),
    "entities_built_total": ("counter", "Entities of one type built, all time (needs the build counts setting)."),
    "entities_mined_total": ("counter", "Entities of one type mined, all time (needs the build counts setting)."),
    "platform_info": ("gauge", "1 per space platform; labels carry its name, state, location and destination."),
    "platform_speed": ("gauge", "Space platform speed."),
    "platform_weight": ("gauge", "Space platform weight."),
    "platform_distance": ("gauge", "Position along the current space connection, 0 to 1 (absent while parked)."),
    "platform_completed_trips_total": ("counter", "Trips the platform has completed."),
    "platform_damaged_tiles": ("gauge", "Damaged tiles on the platform."),
    "platform_paused": ("gauge", "1 while the platform's thrust is paused."),
    "platform_has_hub": ("gauge", "1 once the platform has a hub."),
}


def render_metrics(snapshot: dict | None, file_mtime: float | None, updated_at: float | None = None,
                   production_items: set[str] | None = None) -> str:
    families: dict[str, list[str]] = {}
    production_items = production_items or set()

    def emit(name: str, labels: dict, value) -> None:
        text = _num(value)
        if text is None:
            return
        if labels:
            lbl = ",".join(f'{k}="{_lv(v)}"' for k, v in sorted(labels.items()))
            families.setdefault(name, []).append(f"factorio_{name}{{{lbl}}} {text}")
        else:
            families.setdefault(name, []).append(f"factorio_{name} {text}")

    def render() -> str:
        out: list[str] = []
        for name, lines in families.items():
            mtype, help_ = _FAMILIES.get(name, ("gauge", ""))
            if help_:
                out.append(f"# HELP factorio_{name} {help_}")
            out.append(f"# TYPE factorio_{name} {mtype}")
            out.extend(lines)
        return "\n".join(out) + "\n"

    emit("bridge_up", {}, 1)
    emit("bridge_parse_errors_total", {}, STATE.parse_errors)
    if file_mtime is not None:
        emit("stats_age_seconds", {}, _age(file_mtime))
        emit("stats_file_mtime_timestamp_seconds", {}, file_mtime)
    if updated_at is not None:
        emit("bridge_last_parse_timestamp_seconds", {}, updated_at)
    if not snapshot:
        return render()

    meta = _d(snapshot.get("meta"))
    emit("mod_info", {"mod_version": str(meta.get("mod_version", "")),
                      "game_version": str(meta.get("game_version", ""))}, 1)
    emit("game_tick", {}, meta.get("tick", 0))
    emit("game_time_seconds", {}, meta.get("game_time_seconds"))
    emit("game_speed", {}, meta.get("game_speed"))
    emit("game_paused", {}, meta.get("tick_paused"))
    emit("players_online", {}, meta.get("player_count", 0))
    for pname, p in _d(meta.get("players")).items():
        p = _d(p)
        emit("player_afk_seconds", {"player": pname}, p.get("afk_seconds"))
        emit("player_online_seconds", {"player": pname}, p.get("online_seconds"))

    for sname, surf in _d(snapshot.get("surfaces")).items():
        surf = _d(surf)
        s = {"surface": sname}
        emit("surface_info", {**s, "planet": str(surf.get("planet") or ""),
                              "is_platform": "1" if surf.get("is_platform") else "0",
                              "pollutant": str(surf.get("pollutant") or "")}, 1)
        emit("surface_darkness", s, surf.get("darkness"))
        emit("surface_daytime", s, surf.get("daytime"))
        emit("surface_solar_multiplier", s, surf.get("solar_multiplier"))
        if "pollution" in surf:
            emit("pollution", {**s, "pollutant": str(surf.get("pollutant") or "none")}, surf["pollution"])

        power = _d(surf.get("power"))
        totals = _d(power.get("totals"))
        for key, metric in (
            ("production_watts", "power_production_watts"),
            ("consumption_watts", "power_consumption_watts"),
            ("accumulator_charge_joules", "accumulator_charge_joules"),
            ("accumulator_capacity_joules", "accumulator_capacity_joules"),
            ("network_count", "electric_network_count"),
            ("consumption_satisfaction", "power_consumption_satisfaction"),
            ("consumption_satisfaction_min", "power_consumption_satisfaction_min"),
            ("maximum_production_watts", "power_maximum_production_watts"),
            ("maximum_consumption_watts", "power_maximum_consumption_watts"),
            ("solar_watts", "power_solar_watts"),
            ("accumulator_charge_watts", "power_accumulator_charge_watts"),
            ("accumulator_discharge_watts", "power_accumulator_discharge_watts"),
        ):
            if key in totals:
                emit(metric, s, totals[key])
        for nid, net in _d(power.get("networks")).items():
            net = _d(net)
            ns = {"surface": sname, "network": str(nid)}
            for key, metric in (
                ("production_watts", "network_production_watts"),
                ("consumption_watts", "network_consumption_watts"),
                ("accumulator_charge_joules", "network_accumulator_charge_joules"),
                ("accumulator_capacity_joules", "network_accumulator_capacity_joules"),
                ("sub_network_count", "network_sub_network_count"),
            ):
                if key in net:
                    emit(metric, ns, net[key])
            flow = _d(net.get("flow"))
            for key in ("consumption_satisfaction", "consumption_satisfaction_min", "production_satisfaction",
                        "maximum_production_watts", "maximum_consumption_watts", "solar_watts", "solar_used_watts",
                        "primary_output_watts", "secondary_output_watts", "accumulator_charge_watts",
                        "accumulator_discharge_watts", "transfer_watts"):
                if key in flow:
                    emit(f"network_{key}", ns, flow[key])
            for ename, watts in _d(net.get("production_by_entity")).items():
                emit("network_production_by_entity_watts", {**ns, "entity": ename}, watts)
            for ename, watts in _d(net.get("consumption_by_entity")).items():
                emit("network_consumption_by_entity_watts", {**ns, "entity": ename}, watts)
        for ename, cnt in _d(power.get("count_by_entity")).items():
            emit("entity_count", {"surface": sname, "entity": ename}, cnt)

        for fname, log in _d(surf.get("logistics")).items():
            log = _d(log)
            fs = {"surface": sname, "force": fname}
            emit("logistic_network_count", fs, log.get("network_count", 0))
            robots = _d(log.get("robots"))
            for key, (kind, state_) in {
                "logistic_total": ("logistic", "total"),
                "logistic_available": ("logistic", "available"),
                "construction_total": ("construction", "total"),
                "construction_available": ("construction", "available"),
            }.items():
                if key in robots:
                    emit("robots", {**fs, "kind": kind, "state": state_}, robots[key])
            for item, count in _d(log.get("items")).items():
                emit("logistic_item_count", {**fs, "item": item}, count)
            for key, count in _d(log.get("items_by_quality")).items():
                item, _, quality = str(key).rpartition(":")
                emit("logistic_item_quality_count", {**fs, "item": item or key, "quality": quality or "normal"}, count)

    for fname, force in _d(snapshot.get("forces")).items():
        force = _d(force)
        f = {"force": fname}
        research = _d(force.get("research"))
        if research.get("current"):
            emit("research_info", {**f, "technology": str(research["current"]),
                                   "level": str(research.get("current_level", ""))}, 1)
        for key, metric in (
            ("progress", "research_progress"),
            ("progress_per_minute", "research_progress_per_minute"),
            ("eta_seconds", "research_eta_seconds"),
            ("current_units", "research_current_units"),
            ("enabled", "research_enabled"),
            ("science_per_minute", "science_per_minute"),
            ("science_per_minute_10m", "science_per_minute_10m"),
            ("science_total", "science_total"),
            ("technologies_researched", "technologies_researched"),
            ("technologies_total", "technologies_total"),
            ("technologies_available", "technologies_available"),
            ("technologies_infinite", "technologies_infinite"),
        ):
            if key in research:
                emit(metric, f, research[key])
        if research:
            queue = research.get("queue")
            emit("research_queue_length", f, len(queue) if isinstance(queue, list) else 0)
        for sname, n in _d(research.get("labs")).items():
            emit("labs", {**f, "surface": sname}, n)
        if "rockets_launched" in force:
            emit("rockets_launched", f, force["rockets_launched"])
            emit("rockets_launched_total", f, force["rockets_launched"])
        for item, count in _d(force.get("items_launched")).items():
            emit("items_launched_total", {**f, "item": item}, count)
        for sname, evo in _d(force.get("evolution")).items():
            emit("evolution_factor", {"force": fname, "surface": sname}, evo)
        for sname, parts in _d(force.get("evolution_by")).items():
            for part, value in _d(parts).items():
                emit("evolution_factor_part", {"force": fname, "surface": sname, "part": part}, value)
        for sname, packs in _d(force.get("science_packs")).items():
            for pack, v in _d(packs).items():
                v = _d(v)
                ps = {"force": fname, "surface": sname, "item": pack}
                for key, metric in (
                    ("produced_per_minute", "science_pack_production_per_minute"),
                    ("consumed_per_minute", "science_pack_consumption_per_minute"),
                    ("produced_per_minute_10m", "science_pack_production_per_minute_10m"),
                    ("consumed_per_minute_10m", "science_pack_consumption_per_minute_10m"),
                    ("produced_total", "science_pack_produced_total"),
                    ("consumed_total", "science_pack_consumed_total"),
                ):
                    if key in v:
                        emit(metric, ps, v[key])
        for sname, k in _d(force.get("kills")).items():
            k = _d(k)
            ks = {"force": fname, "surface": sname}
            emit("kills_per_minute", ks, k.get("kills_per_minute"))
            emit("losses_per_minute", ks, k.get("losses_per_minute"))
            for ename, count in _d(k.get("kills_total")).items():
                emit("kills_total", {**ks, "entity": ename}, count)
            for ename, count in _d(k.get("losses_total")).items():
                emit("losses_total", {**ks, "entity": ename}, count)
        for sname, prod in _d(force.get("production")).items():
            prod = _d(prod)
            ps = {"force": fname, "surface": sname}
            for key, metric in (
                ("items_produced_total", "items_produced_total"),
                ("items_consumed_total", "items_consumed_total"),
                ("fluids_produced_total", "fluids_produced_total"),
                ("fluids_consumed_total", "fluids_consumed_total"),
            ):
                for item, count in _d(prod.get(key)).items():
                    if "all" in production_items or item in production_items:
                        emit(metric, {**ps, "item": item}, count)
        for sname, b in _d(force.get("builds")).items():
            b = _d(b)
            bs = {"force": fname, "surface": sname}
            for ename, count in _d(b.get("built_total")).items():
                emit("entities_built_total", {**bs, "entity": ename}, count)
            for ename, count in _d(b.get("mined_total")).items():
                emit("entities_mined_total", {**bs, "entity": ename}, count)
        for key, plat in _d(force.get("platforms")).items():
            plat = _d(plat)
            pl = {"force": fname, "platform": key}
            emit("platform_info", {**pl, "name": str(plat.get("name", key)),
                                   "state": str(plat.get("state", "")),
                                   "location": str(plat.get("location") or plat.get("last_location") or ""),
                                   "destination": str(plat.get("destination") or "")}, 1)
            for pkey, metric in (
                ("speed", "platform_speed"),
                ("weight", "platform_weight"),
                ("distance", "platform_distance"),
                ("completed_trips", "platform_completed_trips_total"),
                ("damaged_tiles", "platform_damaged_tiles"),
                ("paused", "platform_paused"),
                ("has_hub", "platform_has_hub"),
            ):
                if pkey in plat:
                    emit(metric, pl, plat[pkey])

    return render()


# ----------------------------------------------------------------------------
# HTTP server
# ----------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = f"wiretap-bridge/{BRIDGE_VERSION}"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 15
    args: argparse.Namespace | None = None  # set by main()

    def _cors(self) -> None:
        origin = getattr(self.args, "cors_origin", "*") if self.args else "*"
        if origin and origin != "none":
            self.send_header("Access-Control-Allow-Origin", origin)

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._cors()
            _, file_mtime, _ = STATE.get()
            age = _age(file_mtime)
            if age is not None:
                self.send_header("X-Stats-Age-Seconds", f"{age:.1f}")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away mid-response; not worth a traceback

    def _authorized(self) -> bool:
        token = getattr(self.args, "token", None) if self.args else None
        if not token:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {token}"

    def _stale_after(self, meta: dict) -> float:
        configured = getattr(self.args, "stale_after", None) if self.args else None
        if configured:
            return float(configured)
        interval = meta.get("interval_seconds")
        if isinstance(interval, (int, float)) and interval > 0:
            return max(30.0, 3.0 * float(interval))
        return 60.0

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        snapshot, file_mtime, updated_at = STATE.get()
        age = _age(file_mtime)
        if path == "/":
            body = json.dumps({
                "service": "wiretap-bridge",
                "version": BRIDGE_VERSION,
                "endpoints": ["/stats", "/metrics", "/health"],
                "have_data": snapshot is not None,
                "stats_age_seconds": age,
            }).encode()
            self._send(200, body, "application/json")
        elif path == "/stats":
            if not self._authorized():
                self._send(401, b'{"error":"unauthorized"}', "application/json")
            elif snapshot is None:
                self._send(503, b'{"error":"no snapshot yet"}', "application/json")
            else:
                self._send(200, json.dumps(snapshot).encode(), "application/json")
        elif path == "/health":
            meta = _d(_d(snapshot).get("meta"))
            stale_after = self._stale_after(meta)
            stale = age is None or age > stale_after
            body = json.dumps({
                "ok": not stale,
                "reason": None if not stale else ("no snapshot yet" if age is None else "snapshot stale"),
                "stats_age_seconds": age,
                "stale_after_seconds": stale_after,
                "file_mtime": file_mtime,
                "last_parse": updated_at,
                "parse_errors": STATE.parse_errors,
                "tick": meta.get("tick"),
                "mod_version": meta.get("mod_version"),
                "game_version": meta.get("game_version"),
            }).encode()
            self._send(200 if not stale else 503, body, "application/json")
        elif path == "/metrics":
            if not self._authorized():
                self._send(401, b"unauthorized\n", "text/plain; charset=utf-8")
            else:
                items = getattr(self.args, "production_item_set", None) if self.args else None
                body = render_metrics(snapshot, file_mtime, updated_at, items).encode()
                self._send(200, body, "text/plain; version=0.0.4; charset=utf-8")
        else:
            self._send(404, b'{"error":"not found"}', "application/json")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt: str, *args) -> None:  # keep stdout quiet unless asked
        if self.args is not None and getattr(self.args, "verbose", False):
            print("[http] " + fmt % args, flush=True)

    def log_error(self, fmt: str, *args) -> None:
        print("[http] " + fmt % args, file=sys.stderr, flush=True)


# ----------------------------------------------------------------------------
# MQTT publisher (optional)
# ----------------------------------------------------------------------------


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", str(s).lower()).strip("_")


def _stable_slug(name: str) -> str:
    """Topic/object-id slug that depends only on the name itself.

    Vanilla names (nauvis, platform-1, player) map plainly, so entity ids from
    1.0.x survive. Any name that loses information in slugging (upper case,
    spaces, symbols) gets a short hash so two such names can never collide."""
    base = _slug(name) or "x"
    if base == str(name).replace("-", "_"):
        return base
    return base + "_" + hashlib.sha1(str(name).encode("utf-8")).hexdigest()[:6]


def _pct(value, digits: int = 1):
    try:
        return round(100.0 * float(value), digits)
    except (TypeError, ValueError):
        return None


class MqttPublisher:
    """Publishes snapshots as JSON topics, flat scalar topics and Home
    Assistant discovery configs. `client` may be injected (tests)."""

    def __init__(self, args, client=None) -> None:
        self.args = args
        self.prefix = args.mqtt_prefix.rstrip("/") or "wiretap"
        self.inst = _slug(self.prefix) or "wiretap"
        self.ha_prefix = (getattr(args, "ha_prefix", None) or "homeassistant").rstrip("/")
        self.flat = getattr(args, "flat", True)
        self.watch_items = [i.strip() for i in (args.watch_items or "").split(",") if i.strip()]
        self.discovered: set[str] = set()      # object ids with a config on the broker
        self.present: set[str] = set()         # object ids seen in the current publish
        self.previous_present: set[str] = set()
        self.device_versions: tuple = ()
        self.publish_failed = False
        if client is not None:
            self.client = client
            return
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            sys.exit("MQTT requested but paho-mqtt is not installed: pip install paho-mqtt")
        client_id = getattr(args, "mqtt_client_id", None) or f"{self.inst}-bridge"
        try:  # paho-mqtt 2.x requires an api-version argument; 1.x lacks it
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        except AttributeError:
            self.client = mqtt.Client(client_id=client_id)
        if getattr(args, "mqtt_tls", False):
            self.client.tls_set()
        if args.mqtt_user:
            self.client.username_pw_set(args.mqtt_user, args.mqtt_password or None)
        self.client.will_set(f"{self.prefix}/availability", "offline", qos=1, retain=True)
        self.client.on_connect = self._on_connect
        # connect_async + loop_start: paho retries in the background until the
        # broker is reachable, so a broker outage at startup (or any later
        # disconnect) never kills the bridge.
        self.client.connect_async(args.mqtt_host, args.mqtt_port, keepalive=60)
        self.client.loop_start()
        print(f"[bridge] MQTT connecting to {args.mqtt_host}:{args.mqtt_port}, "
              f"prefix '{self.prefix}', client id '{client_id}'", flush=True)

    def _on_connect(self, client, userdata, *args) -> None:
        """Runs on every (re)connect; *args absorbs paho 1.x/2.x signature diff.

        The broker served our retained LWT "offline" while we were away, and a
        restarted broker may have lost discovery configs — so re-announce both.
        Clearing `discovered` makes the next publish() re-send discovery; a
        benign race with the watcher thread at worst duplicates a retained
        config message.
        """
        rc = args[1] if len(args) > 1 else 0  # (flags, rc[, properties]) in 1.x/2.x
        failed = getattr(rc, "is_failure", False)  # paho 2.x ReasonCode
        if not isinstance(failed, bool):
            failed = bool(failed)
        if not failed and isinstance(rc, int) and rc != 0:  # paho 1.x int code
            failed = True
        if failed:
            print(f"[bridge] MQTT connection refused by broker: {rc}", flush=True)
            return
        client.publish(f"{self.prefix}/availability", "online", qos=1, retain=True)
        self.discovered.clear()
        self.publish_failed = False
        print("[bridge] MQTT connected", flush=True)

    def close(self) -> None:
        try:
            self.client.publish(f"{self.prefix}/availability", "offline", qos=1, retain=True)
            time.sleep(0.2)
            self.client.disconnect()
            self.client.loop_stop()
        except Exception:
            pass

    # -- low level ---------------------------------------------------------

    def _raw(self, topic: str, payload, retain: bool = True, qos: int = 0) -> None:
        if payload is None:
            return
        if isinstance(payload, bool):
            payload = "ON" if payload else "OFF"
        elif isinstance(payload, (dict, list)):
            payload = json.dumps(payload)
        elif not isinstance(payload, (str, bytes)):
            payload = str(payload)
        info = self.client.publish(topic, payload, qos=qos, retain=retain)
        rc = getattr(info, "rc", 0)
        if rc not in (0, None):
            if not self.publish_failed:
                print(f"[bridge] MQTT publish failed (rc={rc}); will retry after reconnect", flush=True)
                self.publish_failed = True

    def _pub(self, topic: str, payload, retain: bool = True) -> None:
        self._raw(f"{self.prefix}/{topic}", payload, retain=retain)

    def _flat(self, topic: str, value) -> None:
        """Scalar leaf topic for microcontrollers (ESPHome mqtt_subscribe)."""
        if not self.flat or value is None:
            return
        if isinstance(value, float):
            value = round(value, 3)
        self._raw(f"{self.prefix}/flat/{topic}", value, retain=True)

    # -- Home Assistant discovery ------------------------------------------

    def _device(self) -> dict:
        mod_version, game_version = (self.device_versions + ("", ""))[:2]
        device = {
            "identifiers": [self.inst],
            "name": "Factorio (wiretap)" if self.inst == "wiretap" else f"Factorio ({self.prefix})",
            "manufacturer": "wiretap mod",
        }
        if mod_version:
            device["sw_version"] = str(mod_version)
        if game_version:
            device["model"] = f"Factorio {game_version}"
        return device

    def _discover(self, object_id: str, name: str, state_topic: str,
                  value_template: str, unit: str | None = None,
                  device_class: str | None = None, icon: str | None = None,
                  state_class: str | None = "measurement", component: str = "sensor",
                  extra: dict | None = None) -> None:
        self.present.add(object_id)
        if object_id in self.discovered:
            return
        self.discovered.add(object_id)
        cfg = {
            "name": name,
            "unique_id": f"{self.inst}_{object_id}",
            "state_topic": f"{self.prefix}/{state_topic}",
            "value_template": value_template,
            "availability_topic": f"{self.prefix}/availability",
            "device": self._device(),
        }
        if component == "binary_sensor":
            cfg["payload_on"] = "ON"
            cfg["payload_off"] = "OFF"
        if unit:
            cfg["unit_of_measurement"] = unit
        if device_class:
            cfg["device_class"] = device_class
        if icon:
            cfg["icon"] = icon
        if state_class and component == "sensor":
            cfg["state_class"] = state_class
        if extra:
            cfg.update(extra)
        self._raw(f"{self.ha_prefix}/{component}/{self.inst}/{object_id}/config", cfg, retain=True, qos=1)

    def _retire(self, object_id: str) -> None:
        """Remove an entity whose source (surface, platform, force) is gone."""
        for component in ("sensor", "binary_sensor"):
            self._raw(f"{self.ha_prefix}/{component}/{self.inst}/{object_id}/config", "", retain=True, qos=1)
        self.discovered.discard(object_id)

    # -- per-snapshot publish ----------------------------------------------

    def publish(self, snap: dict) -> None:
        meta = _d(snap.get("meta"))
        versions = (str(meta.get("mod_version", "")), str(meta.get("game_version", "")))
        if versions != self.device_versions:
            self.device_versions = versions
            self.discovered.clear()  # re-announce with the new device info
        self.present = set()
        discovery = self.args.ha_discovery

        self._pub("meta", meta)
        if self.args.publish_full:
            self._pub("state", snap)
        self._flat("meta/tick", meta.get("tick"))
        self._flat("meta/player_count", meta.get("player_count"))
        self._flat("meta/players_online", ",".join(meta.get("players_online") or []) if isinstance(meta.get("players_online"), list) else "")
        self._flat("meta/game_time_seconds", meta.get("game_time_seconds"))
        self._flat("meta/game_speed", meta.get("game_speed"))
        self._flat("meta/paused", meta.get("tick_paused"))
        self._flat("meta/mod_version", meta.get("mod_version"))
        self._flat("meta/game_version", meta.get("game_version"))

        # Aggregate logistic item counts across all surfaces and forces.
        items_total: dict[str, int] = {}
        for surf in _d(snap.get("surfaces")).values():
            for log in _d(_d(surf).get("logistics")).values():
                for item, count in _d(_d(log).get("items")).items():
                    try:
                        items_total[item] = items_total.get(item, 0) + int(count)
                    except (TypeError, ValueError):
                        continue
        self._pub("items", items_total)
        for item in self.watch_items:
            self._flat(f"items/{_stable_slug(item)}", items_total.get(item, 0))

        # -- surfaces --------------------------------------------------------
        for sname, surf in _d(snap.get("surfaces")).items():
            surf = _d(surf)
            slug = _stable_slug(sname)
            self._pub(f"surface/{slug}", {
                "name": sname, "planet": surf.get("planet"), "is_platform": bool(surf.get("is_platform")),
                "pollutant": surf.get("pollutant"), "daytime": surf.get("daytime"),
                "darkness": surf.get("darkness"), "solar_multiplier": surf.get("solar_multiplier"),
            })
            self._flat(f"{slug}/darkness", surf.get("darkness"))
            if discovery and surf.get("darkness") is not None:
                self._discover(f"{slug}_darkness", f"{sname} darkness", f"surface/{slug}",
                               "{{ ((value_json.darkness | default(0)) * 100) | round(0) }}",
                               unit="%", icon="mdi:weather-night")

            power = _d(_d(surf.get("power")).get("totals"))
            if power:
                payload = dict(power)
                cap = power.get("accumulator_capacity_joules") or 0
                charge = power.get("accumulator_charge_joules") or 0
                payload["charge_percent"] = round(100.0 * charge / cap, 1) if cap else 0.0
                satisfaction = power.get("consumption_satisfaction")
                satisfaction_min = power.get("consumption_satisfaction_min", satisfaction)
                if satisfaction is not None:
                    payload["satisfaction_percent"] = _pct(satisfaction)
                    payload["satisfaction_min_percent"] = _pct(satisfaction_min)
                    payload["brownout"] = bool(float(satisfaction_min if satisfaction_min is not None else satisfaction) < 0.999)
                maximum = power.get("maximum_production_watts")
                if maximum is not None:
                    payload["headroom_watts"] = max(0.0, float(maximum) - float(power.get("consumption_watts") or 0))
                self._pub(f"power/{slug}", payload)
                for key in ("production_watts", "consumption_watts", "charge_percent", "satisfaction_percent",
                            "satisfaction_min_percent", "brownout", "accumulator_charge_joules",
                            "accumulator_capacity_joules", "maximum_production_watts", "maximum_consumption_watts",
                            "headroom_watts", "solar_watts", "accumulator_charge_watts", "accumulator_discharge_watts",
                            "network_count"):
                    if key in payload:
                        self._flat(f"{slug}/power/{key}", payload[key])
                if discovery:
                    self._discover(f"{slug}_production", f"{sname} power production", f"power/{slug}",
                                   "{{ value_json.production_watts | round(0) }}", unit="W", device_class="power")
                    self._discover(f"{slug}_consumption", f"{sname} power consumption", f"power/{slug}",
                                   "{{ value_json.consumption_watts | round(0) }}", unit="W", device_class="power")
                    self._discover(f"{slug}_accu_charge", f"{sname} accumulator charge", f"power/{slug}",
                                   "{{ value_json.charge_percent }}", unit="%", device_class="battery")
                    if satisfaction is not None:
                        self._discover(f"{slug}_satisfaction", f"{sname} power satisfaction", f"power/{slug}",
                                       "{{ value_json.satisfaction_percent }}", unit="%", icon="mdi:transmission-tower")
                        self._discover(f"{slug}_satisfaction_min", f"{sname} power satisfaction minimum", f"power/{slug}",
                                       "{{ value_json.satisfaction_min_percent }}", unit="%", icon="mdi:transmission-tower-off")
                        self._discover(f"{slug}_brownout", f"{sname} brownout", f"power/{slug}",
                                       "{{ 'ON' if value_json.brownout else 'OFF' }}", device_class="problem",
                                       component="binary_sensor")
                    if maximum is not None:
                        self._discover(f"{slug}_headroom", f"{sname} power headroom", f"power/{slug}",
                                       "{{ value_json.headroom_watts | round(0) }}", unit="W", device_class="power",
                                       icon="mdi:gauge")
                        self._discover(f"{slug}_solar", f"{sname} solar output", f"power/{slug}",
                                       "{{ value_json.solar_watts | default(0) | round(0) }}", unit="W",
                                       device_class="power", icon="mdi:solar-power")
                    if "accumulator_discharge_watts" in payload:
                        self._discover(f"{slug}_accu_discharge", f"{sname} accumulator discharge", f"power/{slug}",
                                       "{{ value_json.accumulator_discharge_watts | round(0) }}", unit="W",
                                       device_class="power", icon="mdi:battery-arrow-down")
                        self._discover(f"{slug}_accu_charging", f"{sname} accumulator charging", f"power/{slug}",
                                       "{{ value_json.accumulator_charge_watts | round(0) }}", unit="W",
                                       device_class="power", icon="mdi:battery-arrow-up")

            if "pollution" in surf:
                self._pub(f"pollution/{slug}", {"pollution": surf["pollution"], "pollutant": surf.get("pollutant")})
                self._flat(f"{slug}/pollution", surf["pollution"])
                if discovery:
                    self._discover(f"{slug}_pollution", f"{sname} pollution", f"pollution/{slug}",
                                   "{{ value_json.pollution | round(0) }}", icon="mdi:factory")

            for fname, log in _d(surf.get("logistics")).items():
                log = _d(log)
                fslug = _stable_slug(fname)
                robots = dict(_d(log.get("robots")))
                robots["network_count"] = log.get("network_count", 0)
                self._pub(f"robots/{fslug}/{slug}", robots)
                for key in ("logistic_available", "logistic_total", "construction_available", "construction_total"):
                    if key in robots:
                        self._flat(f"{slug}/robots/{key}", robots[key])
                if discovery:
                    self._discover(f"{fslug}_{slug}_logistic_robots", f"{sname} logistic robots available",
                                   f"robots/{fslug}/{slug}", "{{ value_json.logistic_available | default(0) }}",
                                   icon="mdi:robot")
                    self._discover(f"{fslug}_{slug}_construction_robots", f"{sname} construction robots available",
                                   f"robots/{fslug}/{slug}", "{{ value_json.construction_available | default(0) }}",
                                   icon="mdi:robot-industrial")

        # -- forces ----------------------------------------------------------
        for fname, force in _d(snap.get("forces")).items():
            force = _d(force)
            fslug = _stable_slug(fname)
            evolution_by = _d(force.get("evolution_by"))
            for sname, evo in _d(force.get("evolution")).items():
                slug = _stable_slug(sname)
                payload = {"evolution": evo}
                payload.update(_d(evolution_by.get(sname)))
                self._pub(f"evolution/{fslug}/{slug}", payload)
                self._flat(f"{slug}/evolution", evo)
                if discovery:
                    self._discover(f"{slug}_evolution", f"{sname} evolution", f"evolution/{fslug}/{slug}",
                                   "{{ ((value_json.evolution | default(0)) * 100) | round(1) }}", unit="%", icon="mdi:bug")

            research = _d(force.get("research"))
            if research:
                self._pub(f"research/{fslug}", research)
                self._flat(f"{fslug}/research/current", research.get("current") or "none")
                self._flat(f"{fslug}/research/progress_percent", _pct(research.get("progress", 0)))
                self._flat(f"{fslug}/research/eta_seconds", research.get("eta_seconds"))
                self._flat(f"{fslug}/research/science_per_minute", research.get("science_per_minute"))
                self._flat(f"{fslug}/research/science_per_minute_10m", research.get("science_per_minute_10m"))
                self._flat(f"{fslug}/research/technologies_researched", research.get("technologies_researched"))
                if discovery:
                    self._discover(f"{fslug}_research", f"{fname} current research", f"research/{fslug}",
                                   "{{ value_json.current | default('none') }}", icon="mdi:flask-outline", state_class=None,
                                   extra={"json_attributes_topic": f"{self.prefix}/research/{fslug}",
                                          "json_attributes_template": "{{ {'level': value_json.current_level | default(0), 'queue': value_json.queue | default([]), 'ingredients': value_json.current_ingredients | default({})} | tojson }}"})
                    self._discover(f"{fslug}_research_progress", f"{fname} research progress", f"research/{fslug}",
                                   "{{ ((value_json.progress | default(0)) * 100) | round(1) }}", unit="%", icon="mdi:progress-check")
                    self._discover(f"{fslug}_research_eta", f"{fname} research ETA", f"research/{fslug}",
                                   "{{ value_json.eta_seconds | default(0) | round(0) }}", unit="s", device_class="duration",
                                   icon="mdi:timer-sand")
                    self._discover(f"{fslug}_science_rate", f"{fname} science per minute", f"research/{fslug}",
                                   "{{ value_json.science_per_minute | default(0) | round(1) }}", unit="units/min",
                                   icon="mdi:flask")
                    self._discover(f"{fslug}_science_rate_10m", f"{fname} science per minute (10 min)", f"research/{fslug}",
                                   "{{ value_json.science_per_minute_10m | default(0) | round(1) }}", unit="units/min",
                                   icon="mdi:flask")
                    self._discover(f"{fslug}_technologies", f"{fname} technologies researched", f"research/{fslug}",
                                   "{{ value_json.technologies_researched | default(0) }}", icon="mdi:school",
                                   state_class="total_increasing")

            rockets = {"rockets_launched": force.get("rockets_launched"), "items_launched": _d(force.get("items_launched"))}
            if rockets["rockets_launched"] is not None:
                self._pub(f"rockets/{fslug}", rockets)
                self._flat(f"{fslug}/rockets_launched", rockets["rockets_launched"])
                if discovery:
                    self._discover(f"{fslug}_rockets", f"{fname} rockets launched", f"rockets/{fslug}",
                                   "{{ value_json.rockets_launched }}", icon="mdi:rocket", state_class="total_increasing")

            for sname, k in _d(force.get("kills")).items():
                k = _d(k)
                slug = _stable_slug(sname)
                payload = {"kills_per_minute": k.get("kills_per_minute", 0), "losses_per_minute": k.get("losses_per_minute", 0),
                           "kills_total": sum(v for v in _d(k.get("kills_total")).values() if isinstance(v, (int, float))),
                           "losses_total": sum(v for v in _d(k.get("losses_total")).values() if isinstance(v, (int, float))),
                           "under_attack": bool((k.get("losses_per_minute") or 0) > 0)}
                self._pub(f"kills/{fslug}/{slug}", payload)
                for key in ("kills_per_minute", "losses_per_minute", "under_attack"):
                    self._flat(f"{slug}/kills/{key}", payload[key])
                if discovery:
                    self._discover(f"{fslug}_{slug}_kills_rate", f"{sname} kills per minute", f"kills/{fslug}/{slug}",
                                   "{{ value_json.kills_per_minute | round(1) }}", unit="/min", icon="mdi:sword-cross")
                    self._discover(f"{fslug}_{slug}_losses_rate", f"{sname} losses per minute", f"kills/{fslug}/{slug}",
                                   "{{ value_json.losses_per_minute | round(2) }}", unit="/min", icon="mdi:shield-alert")
                    self._discover(f"{fslug}_{slug}_under_attack", f"{sname} under attack", f"kills/{fslug}/{slug}",
                                   "{{ 'ON' if value_json.under_attack else 'OFF' }}", device_class="problem",
                                   component="binary_sensor")

            for key, plat in _d(force.get("platforms")).items():
                plat = _d(plat)
                pslug = _stable_slug(f"{fname}_{key}")
                self._pub(f"platforms/{pslug}", plat)
                pname = str(plat.get("name") or key)
                for pkey in ("state", "location", "destination", "speed", "completed_trips", "damaged_tiles", "paused", "distance"):
                    self._flat(f"platforms/{pslug}/{pkey}", plat.get(pkey))
                if discovery:
                    self._discover(f"platform_{pslug}_state", f"{pname} state", f"platforms/{pslug}",
                                   "{{ value_json.state | default('unknown') }}", icon="mdi:rocket-launch-outline", state_class=None)
                    self._discover(f"platform_{pslug}_location", f"{pname} location", f"platforms/{pslug}",
                                   "{{ value_json.location | default(value_json.last_location) | default('in transit') }}",
                                   icon="mdi:map-marker", state_class=None)
                    self._discover(f"platform_{pslug}_destination", f"{pname} destination", f"platforms/{pslug}",
                                   "{{ value_json.destination | default('none') }}", icon="mdi:map-marker-path", state_class=None)
                    self._discover(f"platform_{pslug}_speed", f"{pname} speed", f"platforms/{pslug}",
                                   "{{ value_json.speed | default(0) | round(1) }}", icon="mdi:speedometer")
                    self._discover(f"platform_{pslug}_trips", f"{pname} completed trips", f"platforms/{pslug}",
                                   "{{ value_json.completed_trips | default(0) }}", icon="mdi:counter",
                                   state_class="total_increasing")
                    self._discover(f"platform_{pslug}_damage", f"{pname} damaged tiles", f"platforms/{pslug}",
                                   "{{ value_json.damaged_tiles | default(0) }}", icon="mdi:meteor")
                    self._discover(f"platform_{pslug}_paused", f"{pname} paused", f"platforms/{pslug}",
                                   "{{ 'ON' if value_json.paused else 'OFF' }}", component="binary_sensor",
                                   icon="mdi:pause-circle")

        # Science: per-surface pack rates summed across forces (usually just
        # "player"), so dashboards get one topic per surface.
        science: dict[str, dict] = {}
        for force in _d(snap.get("forces")).values():
            for sname, packs in _d(_d(force).get("science_packs")).items():
                agg = science.setdefault(sname, {"packs": {}, "total_consumed_per_minute": 0.0,
                                                 "total_produced_per_minute": 0.0,
                                                 "total_consumed_per_minute_10m": 0.0,
                                                 "total_produced_per_minute_10m": 0.0})
                for pack, v in _d(packs).items():
                    v = _d(v)
                    entry = agg["packs"].setdefault(pack, {"produced_per_minute": 0.0, "consumed_per_minute": 0.0,
                                                           "produced_per_minute_10m": 0.0, "consumed_per_minute_10m": 0.0,
                                                           "produced_total": 0, "consumed_total": 0})
                    for field in entry:
                        entry[field] += v.get(field, 0) or 0
                    agg["total_produced_per_minute"] += v.get("produced_per_minute", 0) or 0
                    agg["total_consumed_per_minute"] += v.get("consumed_per_minute", 0) or 0
                    agg["total_produced_per_minute_10m"] += v.get("produced_per_minute_10m", 0) or 0
                    agg["total_consumed_per_minute_10m"] += v.get("consumed_per_minute_10m", 0) or 0
        for sname, agg in science.items():
            slug = _stable_slug(sname)
            self._pub(f"science/{slug}", agg)
            self._flat(f"{slug}/science/consumed_per_minute", agg["total_consumed_per_minute"])
            self._flat(f"{slug}/science/consumed_per_minute_10m", agg["total_consumed_per_minute_10m"])
            if discovery:
                self._discover(f"{slug}_science_rate", f"{sname} science consumed/min", f"science/{slug}",
                               "{{ value_json.total_consumed_per_minute | round(1) }}", unit="packs/min", icon="mdi:flask")
                self._discover(f"{slug}_science_rate_10m", f"{sname} science consumed/min (10 min)", f"science/{slug}",
                               "{{ value_json.total_consumed_per_minute_10m | round(1) }}", unit="packs/min", icon="mdi:flask")

        if discovery:
            self._discover("players_online", "Factorio players online", "meta",
                           "{{ value_json.player_count }}", icon="mdi:account-group",
                           extra={"json_attributes_topic": f"{self.prefix}/meta",
                                  "json_attributes_template": "{{ {'players': value_json.players_online | default([])} | tojson }}"})
            self._discover("game_tick", "Factorio game tick", "meta",
                           "{{ value_json.tick }}", icon="mdi:clock-fast", state_class="total_increasing")
            self._discover("play_time", "Factorio play time", "meta",
                           "{{ value_json.game_time_seconds | default(0) }}", unit="s", device_class="duration",
                           icon="mdi:timer-outline", state_class="total_increasing")
            self._discover("game_speed", "Factorio game speed", "meta",
                           "{{ value_json.game_speed | default(1) }}", icon="mdi:fast-forward")
            self._discover("game_paused", "Factorio paused", "meta",
                           "{{ 'ON' if value_json.tick_paused else 'OFF' }}", component="binary_sensor",
                           icon="mdi:pause")
            for item in self.watch_items:
                self._discover(
                    f"item_{_slug(item)}", f"Factorio {item} in logistics", "items",
                    # json.dumps quotes/escapes the name into a valid Jinja literal
                    "{{ value_json[%s] | default(0) }}" % json.dumps(item),
                    icon="mdi:package-variant-closed")

        # Entities whose source disappeared (deleted platform, renamed surface)
        # get their discovery config cleared so HA drops them.
        for gone in self.previous_present - self.present:
            self._retire(gone)
        self.previous_present = set(self.present)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------


def positive_float(text: str) -> float:
    value = float(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return value


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", required=True,
                    help="path to script-output/wiretap/stats.json (or the .ndjson journal)")
    ap.add_argument("--bind", default="0.0.0.0",
                    help="HTTP bind address (default 0.0.0.0: reachable from the whole LAN; no auth unless --token)")
    ap.add_argument("--port", type=int, default=8787, help="HTTP port (default 8787)")
    ap.add_argument("--poll", type=positive_float, default=2.0,
                    help="seconds between file checks (default 2)")
    ap.add_argument("--stale-after", type=positive_float, default=None,
                    help="seconds after which /health reports 503 (default: 3x the mod's export interval, at least 30)")
    ap.add_argument("--token", default=os.environ.get("WIRETAP_TOKEN"),
                    help="require 'Authorization: Bearer <token>' on /stats and /metrics (env WIRETAP_TOKEN)")
    ap.add_argument("--cors-origin", default="*",
                    help="Access-Control-Allow-Origin value (default *; 'none' disables the header)")
    ap.add_argument("--production-items", default="all",
                    help="items to expose as factorio_items_*_total / factorio_fluids_*_total series: 'all' (default) "
                         "or a comma-separated list such as 'science,iron-plate' to limit series cardinality")
    ap.add_argument("--verbose", action="store_true", help="log every HTTP request")
    ap.add_argument("--mqtt-host", help="MQTT broker host (enables MQTT)")
    ap.add_argument("--mqtt-port", type=int, default=1883, help="MQTT broker port (default 1883, 8883 with --mqtt-tls)")
    ap.add_argument("--mqtt-tls", action="store_true", help="connect to the broker with TLS (system CA store)")
    ap.add_argument("--mqtt-user")
    ap.add_argument("--mqtt-password", default=os.environ.get("WIRETAP_MQTT_PASSWORD"),
                    help="broker password (prefer env WIRETAP_MQTT_PASSWORD or --mqtt-password-file)")
    ap.add_argument("--mqtt-password-file", help="file whose first line is the broker password")
    ap.add_argument("--mqtt-client-id", help="MQTT client id (default '<prefix>-bridge')")
    ap.add_argument("--mqtt-prefix", default="wiretap",
                    help="MQTT topic prefix (default 'wiretap'); also namespaces HA entities, so two bridges can share a broker")
    ap.add_argument("--ha-discovery", action="store_true",
                    help="publish Home Assistant MQTT discovery configs")
    ap.add_argument("--ha-prefix", default="homeassistant",
                    help="Home Assistant discovery prefix (default 'homeassistant')")
    ap.add_argument("--watch-items", default="",
                    help="comma-separated item names to expose as individual HA sensors and flat topics")
    ap.add_argument("--publish-full", action="store_true",
                    help="also publish the entire snapshot to <prefix>/state")
    ap.add_argument("--no-flat", dest="flat", action="store_false",
                    help="do not publish the flat scalar topics under <prefix>/flat/ (meant for ESPHome)")
    return ap


def main() -> None:
    ap = build_parser()
    args = ap.parse_args()
    if not args.mqtt_host and (args.ha_discovery or args.watch_items or args.publish_full):
        ap.error("--ha-discovery, --watch-items and --publish-full need --mqtt-host")
    if args.mqtt_password_file:
        args.mqtt_password = Path(args.mqtt_password_file).expanduser().read_text(encoding="utf-8").splitlines()[0].strip()
    if args.mqtt_tls and args.mqtt_port == 1883:
        args.mqtt_port = 8883
    args.production_item_set = {i.strip() for i in args.production_items.split(",") if i.strip()}
    args.production_item_set |= {i.strip() for i in (args.watch_items or "").split(",") if i.strip()}

    publisher = MqttPublisher(args) if args.mqtt_host else None
    on_update = publisher.publish if publisher else (lambda snap: None)

    path = Path(args.file).expanduser()
    threading.Thread(target=watch_file, args=(path, args.poll, on_update),
                     daemon=True).start()

    Handler.args = args
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    server.daemon_threads = True

    def stop(signum, _frame) -> None:
        print(f"[bridge] signal {signum}, shutting down", flush=True)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    print(f"[bridge] wiretap bridge {BRIDGE_VERSION}: HTTP on http://{args.bind}:{args.port} "
          f"(endpoints: /stats /metrics /health)", flush=True)
    print(f"[bridge] watching {path}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if publisher:
            publisher.close()
        print("[bridge] stopped", flush=True)


if __name__ == "__main__":
    main()
