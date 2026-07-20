#!/usr/bin/env python3
"""wiretap bridge — ship Factorio stats to Home Assistant and friends.

Watches the JSON snapshot written by the wiretap Factorio mod
(script-output/wiretap/stats.json) and re-exposes it as:

  * HTTP JSON API .......... GET /stats (full snapshot), /health, /
  * Prometheus metrics ..... GET /metrics
  * MQTT (optional) ........ per-topic JSON + Home Assistant MQTT discovery

Only the Python standard library is required for HTTP/metrics.
MQTT support needs `pip install paho-mqtt` and --mqtt-host.

Examples:
  # Serve HTTP on :8787, reading the default Factorio script-output path
  python3 wiretap_bridge.py --file ~/.factorio/script-output/wiretap/stats.json

  # Also publish to an MQTT broker with Home Assistant discovery
  python3 wiretap_bridge.py --file .../stats.json \
      --mqtt-host 192.168.1.10 --mqtt-user ha --mqtt-password secret \
      --ha-discovery --watch-items iron-plate,copper-plate,uranium-235
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ----------------------------------------------------------------------------
# Shared state
# ----------------------------------------------------------------------------


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.snapshot: dict | None = None
        self.updated_at: float | None = None  # wall clock of last successful parse
        self.file_mtime: float | None = None
        self.parse_errors = 0

    def set(self, snapshot: dict, mtime: float) -> None:
        with self.lock:
            self.snapshot = snapshot
            self.updated_at = time.time()
            self.file_mtime = mtime

    def get(self) -> tuple[dict | None, float | None]:
        with self.lock:
            return self.snapshot, self.updated_at


STATE = State()

# ----------------------------------------------------------------------------
# File watcher
# ----------------------------------------------------------------------------


def watch_file(path: Path, poll_seconds: float, on_update) -> None:
    """Poll `path` for changes; parse and publish new snapshots.

    Factorio's write_file is not atomic from the reader's point of view, so a
    parse failure is expected occasionally — we simply retry on the next poll.
    """
    last_sig: tuple[float, int] | None = None
    warned_missing = False
    consecutive_failures = 0
    while True:
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
        sig = (st.st_mtime, st.st_size)
        if sig != last_sig:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
                STATE.parse_errors += 1
                consecutive_failures += 1
                if consecutive_failures % 25 == 0:  # a mid-write race clears in
                    print(f"[bridge] {path} still not parseable after "  # 1-2 polls
                          f"{consecutive_failures} attempts ({exc.__class__.__name__})"
                          " — is something else writing this file?", flush=True)
                time.sleep(min(poll_seconds, 0.5))  # likely mid-write; retry soon
                continue
            consecutive_failures = 0
            last_sig = sig
            STATE.set(data, st.st_mtime)
            try:
                on_update(data)
            except Exception as exc:  # MQTT hiccups must not kill the watcher
                print(f"[bridge] on_update error: {exc}", flush=True)
        time.sleep(poll_seconds)


# ----------------------------------------------------------------------------
# Prometheus rendering
# ----------------------------------------------------------------------------

def _d(x) -> dict:
    """Coerce to dict — Lua's JSON serializer may emit [] for empty tables."""
    return x if isinstance(x, dict) else {}


_LABEL_BAD = re.compile(r'([\\"\n])')


def _lv(value: str) -> str:
    """Escape a Prometheus label value."""
    return _LABEL_BAD.sub(lambda m: {"\\": r"\\", '"': r"\"", "\n": r"\n"}[m.group(1)], str(value))


def _num(x) -> str:
    if isinstance(x, bool):
        return "1" if x else "0"
    if isinstance(x, (int, float)):
        return repr(float(x)) if isinstance(x, float) else str(x)
    return "0"


def render_metrics(snapshot: dict | None, updated_at: float | None) -> str:
    out: list[str] = []

    def emit(name: str, labels: dict[str, str], value) -> None:
        if labels:
            lbl = ",".join(f'{k}="{_lv(v)}"' for k, v in sorted(labels.items()))
            out.append(f"factorio_{name}{{{lbl}}} {_num(value)}")
        else:
            out.append(f"factorio_{name} {_num(value)}")

    emit("bridge_up", {}, 1)
    emit("bridge_parse_errors_total", {}, STATE.parse_errors)
    if updated_at is not None:
        emit("stats_age_seconds", {}, time.time() - updated_at)
    if not snapshot:
        return "\n".join(out) + "\n"

    meta = _d(snapshot.get("meta"))
    emit("game_tick", {}, meta.get("tick", 0))
    emit("players_online", {}, meta.get("player_count", 0))

    for sname, surf in _d(snapshot.get("surfaces")).items():
        s = {"surface": sname}
        if "pollution" in surf:
            emit("pollution", s, surf["pollution"])

        power = _d(surf.get("power"))
        totals = _d(power.get("totals"))
        for key, metric in (
            ("production_watts", "power_production_watts"),
            ("consumption_watts", "power_consumption_watts"),
            ("accumulator_charge_joules", "accumulator_charge_joules"),
            ("accumulator_capacity_joules", "accumulator_capacity_joules"),
            ("network_count", "electric_network_count"),
        ):
            if key in totals:
                emit(metric, s, totals[key])
        for nid, net in _d(power.get("networks")).items():
            ns = {"surface": sname, "network": str(nid)}
            for key, metric in (
                ("production_watts", "network_production_watts"),
                ("consumption_watts", "network_consumption_watts"),
                ("accumulator_charge_joules", "network_accumulator_charge_joules"),
                ("accumulator_capacity_joules", "network_accumulator_capacity_joules"),
            ):
                if key in net:
                    emit(metric, ns, net[key])
        for ename, cnt in _d(power.get("count_by_entity")).items():
            emit("entity_count", {"surface": sname, "entity": ename}, cnt)

        for fname, log in _d(surf.get("logistics")).items():
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

    for fname, force in _d(snapshot.get("forces")).items():
        f = {"force": fname}
        research = _d(force.get("research"))
        if research.get("progress") is not None:
            emit("research_progress", f, research["progress"])
        if research.get("technologies_researched") is not None:
            emit("technologies_researched", f, research["technologies_researched"])
            emit("technologies_total", f, research.get("technologies_total", 0))
        if "rockets_launched" in force:
            emit("rockets_launched", f, force["rockets_launched"])
        for sname, evo in _d(force.get("evolution")).items():
            emit("evolution_factor", {"force": fname, "surface": sname}, evo)
        for sname, packs in _d(force.get("science_packs")).items():
            for pack, v in _d(packs).items():
                ps = {"force": fname, "surface": sname, "item": pack}
                for key, metric in (
                    ("produced_per_minute", "science_pack_production_per_minute"),
                    ("consumed_per_minute", "science_pack_consumption_per_minute"),
                    ("produced_total", "science_pack_produced_total"),
                    ("consumed_total", "science_pack_consumed_total"),
                ):
                    if key in _d(v):
                        emit(metric, ps, v[key])

    return "\n".join(out) + "\n"


# ----------------------------------------------------------------------------
# HTTP server
# ----------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "wiretap-bridge/1.0"

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            snapshot, updated_at = STATE.get()
            if updated_at is not None:
                self.send_header("X-Stats-Age-Seconds", f"{time.time() - updated_at:.1f}")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away mid-response; not worth a traceback

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        snapshot, updated_at = STATE.get()
        if path == "/":
            body = json.dumps({
                "service": "wiretap-bridge",
                "endpoints": ["/stats", "/metrics", "/health"],
                "have_data": snapshot is not None,
            }).encode()
            self._send(200, body, "application/json")
        elif path == "/stats":
            if snapshot is None:
                self._send(503, b'{"error":"no snapshot yet"}', "application/json")
            else:
                self._send(200, json.dumps(snapshot).encode(), "application/json")
        elif path == "/health":
            age = None if updated_at is None else time.time() - updated_at
            ok = age is not None
            body = json.dumps({
                "ok": ok,
                "stats_age_seconds": age,
                "parse_errors": STATE.parse_errors,
            }).encode()
            self._send(200 if ok else 503, body, "application/json")
        elif path == "/metrics":
            body = render_metrics(snapshot, updated_at).encode()
            self._send(200, body, "text/plain; version=0.0.4")
        else:
            self._send(404, b'{"error":"not found"}', "application/json")

    def log_message(self, fmt: str, *args) -> None:  # keep stdout quiet
        pass


# ----------------------------------------------------------------------------
# MQTT publisher (optional)
# ----------------------------------------------------------------------------


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", s.lower()).strip("_")


def _slug_map(names) -> dict[str, str]:
    """Stable name -> slug map; distinct names that slug identically
    (e.g. "platform-1" and "platform 1") get deterministic numeric suffixes
    so topics and unique_ids never collide."""
    out: dict[str, str] = {}
    used: set[str] = set()
    for name in sorted(set(names)):
        base = _slug(name) or "surface"
        slug = base
        n = 2
        while slug in used:
            slug = f"{base}_{n}"
            n += 1
        used.add(slug)
        out[name] = slug
    return out


class MqttPublisher:
    def __init__(self, args) -> None:
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            sys.exit("MQTT requested but paho-mqtt is not installed: pip install paho-mqtt")
        self.args = args
        self.prefix = args.mqtt_prefix.rstrip("/")
        self.watch_items = [i.strip() for i in (args.watch_items or "").split(",") if i.strip()]
        self.discovered: set[str] = set()
        try:  # paho-mqtt 2.x requires an api-version argument; 1.x lacks it
            self.client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2, client_id="wiretap-bridge")
        except AttributeError:
            self.client = mqtt.Client(client_id="wiretap-bridge")
        if args.mqtt_user:
            self.client.username_pw_set(args.mqtt_user, args.mqtt_password or None)
        self.client.will_set(f"{self.prefix}/availability", "offline", retain=True)
        self.client.on_connect = self._on_connect
        # connect_async + loop_start: paho retries in the background until the
        # broker is reachable, so a broker outage at startup (or any later
        # disconnect) never kills the bridge.
        self.client.connect_async(args.mqtt_host, args.mqtt_port, keepalive=60)
        self.client.loop_start()
        print(f"[bridge] MQTT connecting to {args.mqtt_host}:{args.mqtt_port}, "
              f"prefix '{self.prefix}'", flush=True)

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
        client.publish(f"{self.prefix}/availability", "online", retain=True)
        self.discovered.clear()
        print("[bridge] MQTT connected", flush=True)

    def _pub(self, topic: str, payload, retain: bool = True) -> None:
        if not isinstance(payload, (str, bytes)):
            payload = json.dumps(payload)
        self.client.publish(f"{self.prefix}/{topic}", payload, retain=retain)

    # -- Home Assistant discovery ------------------------------------------

    def _discover(self, object_id: str, name: str, state_topic: str,
                  value_template: str, unit: str | None = None,
                  device_class: str | None = None, icon: str | None = None,
                  state_class: str | None = "measurement") -> None:
        if object_id in self.discovered:
            return
        self.discovered.add(object_id)
        cfg = {
            "name": name,
            "unique_id": f"wiretap_{object_id}",
            "state_topic": f"{self.prefix}/{state_topic}",
            "value_template": value_template,
            "availability_topic": f"{self.prefix}/availability",
            "device": {
                "identifiers": ["wiretap"],
                "name": "Factorio (wiretap)",
                "manufacturer": "wiretap mod",
            },
        }
        if unit:
            cfg["unit_of_measurement"] = unit
        if device_class:
            cfg["device_class"] = device_class
        if icon:
            cfg["icon"] = icon
        if state_class:
            cfg["state_class"] = state_class
        self.client.publish(
            f"homeassistant/sensor/wiretap/{object_id}/config",
            json.dumps(cfg), retain=True)

    # -- per-snapshot publish ----------------------------------------------

    def publish(self, snap: dict) -> None:
        meta = _d(snap.get("meta"))
        self._pub("meta", meta)
        if self.args.publish_full:
            self._pub("state", snap)

        # Aggregate logistic item counts across all surfaces and forces.
        items_total: dict[str, int] = {}
        for surf in _d(snap.get("surfaces")).values():
            for log in _d(surf.get("logistics")).values():
                for item, count in _d(log.get("items")).items():
                    items_total[item] = items_total.get(item, 0) + int(count)
        self._pub("items", items_total)

        # One collision-free slug per surface name, stable across publishes.
        surface_names = set(_d(snap.get("surfaces")))
        for force in _d(snap.get("forces")).values():
            surface_names |= set(_d(force.get("evolution")))
            surface_names |= set(_d(force.get("science_packs")))
        slugs = _slug_map(surface_names)

        for sname, surf in _d(snap.get("surfaces")).items():
            slug = slugs[sname]
            power = _d(_d(surf.get("power")).get("totals"))
            if power:
                payload = dict(power)
                cap = power.get("accumulator_capacity_joules") or 0
                charge = power.get("accumulator_charge_joules") or 0
                payload["charge_percent"] = round(100.0 * charge / cap, 1) if cap else 0.0
                self._pub(f"power/{slug}", payload)
                if self.args.ha_discovery:
                    self._discover(f"{slug}_production", f"{sname} power production",
                                   f"power/{slug}",
                                   "{{ value_json.production_watts | round(0) }}",
                                   unit="W", device_class="power")
                    self._discover(f"{slug}_consumption", f"{sname} power consumption",
                                   f"power/{slug}",
                                   "{{ value_json.consumption_watts | round(0) }}",
                                   unit="W", device_class="power")
                    self._discover(f"{slug}_accu_charge", f"{sname} accumulator charge",
                                   f"power/{slug}",
                                   "{{ value_json.charge_percent }}",
                                   unit="%", device_class="battery")
            if "pollution" in surf:
                self._pub(f"pollution/{slug}", {"pollution": surf["pollution"]})
                if self.args.ha_discovery:
                    self._discover(f"{slug}_pollution", f"{sname} pollution",
                                   f"pollution/{slug}",
                                   "{{ value_json.pollution | round(0) }}",
                                   icon="mdi:factory")

        for fname, force in _d(snap.get("forces")).items():
            for sname, evo in _d(force.get("evolution")).items():
                self._pub(f"evolution/{_slug(fname)}/{slugs[sname]}",
                          {"evolution": evo})

        # Science: per-surface pack rates summed across forces (usually just
        # "player"), so dashboards get one topic per surface.
        science: dict[str, dict] = {}
        for force in _d(snap.get("forces")).values():
            for sname, packs in _d(force.get("science_packs")).items():
                agg = science.setdefault(
                    sname, {"packs": {}, "total_consumed_per_minute": 0.0,
                            "total_produced_per_minute": 0.0})
                for pack, v in _d(packs).items():
                    v = _d(v)
                    entry = agg["packs"].setdefault(
                        pack, {"produced_per_minute": 0.0, "consumed_per_minute": 0.0})
                    entry["produced_per_minute"] += v.get("produced_per_minute", 0) or 0
                    entry["consumed_per_minute"] += v.get("consumed_per_minute", 0) or 0
                    agg["total_produced_per_minute"] += v.get("produced_per_minute", 0) or 0
                    agg["total_consumed_per_minute"] += v.get("consumed_per_minute", 0) or 0
        for sname, agg in science.items():
            slug = slugs[sname]
            self._pub(f"science/{slug}", agg)
            if self.args.ha_discovery:
                self._discover(f"{slug}_science_rate", f"{sname} science consumed/min",
                               f"science/{slug}",
                               "{{ value_json.total_consumed_per_minute | round(1) }}",
                               unit="packs/min", icon="mdi:flask")

        if self.args.ha_discovery:
            self._discover("players_online", "Factorio players online", "meta",
                           "{{ value_json.player_count }}", icon="mdi:account-group")
            self._discover("game_tick", "Factorio game tick", "meta",
                           "{{ value_json.tick }}", icon="mdi:clock-fast",
                           state_class="total_increasing")
            for item in self.watch_items:
                self._discover(
                    f"item_{_slug(item)}", f"Factorio {item} in logistics", "items",
                    # json.dumps quotes/escapes the name into a valid Jinja literal
                    "{{ value_json[%s] | default(0) }}" % json.dumps(item),
                    icon="mdi:package-variant-closed")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", required=True,
                    help="path to script-output/wiretap/stats.json")
    ap.add_argument("--bind", default="0.0.0.0", help="HTTP bind address")
    ap.add_argument("--port", type=int, default=8787, help="HTTP port (default 8787)")
    ap.add_argument("--poll", type=float, default=2.0,
                    help="seconds between file checks (default 2)")
    ap.add_argument("--mqtt-host", help="MQTT broker host (enables MQTT)")
    ap.add_argument("--mqtt-port", type=int, default=1883)
    ap.add_argument("--mqtt-user")
    ap.add_argument("--mqtt-password")
    ap.add_argument("--mqtt-prefix", default="wiretap",
                    help="MQTT topic prefix (default 'wiretap')")
    ap.add_argument("--ha-discovery", action="store_true",
                    help="publish Home Assistant MQTT discovery configs")
    ap.add_argument("--watch-items", default="",
                    help="comma-separated item names to expose as individual HA sensors")
    ap.add_argument("--publish-full", action="store_true",
                    help="also publish the entire snapshot to <prefix>/state")
    args = ap.parse_args()

    publisher = MqttPublisher(args) if args.mqtt_host else None
    on_update = publisher.publish if publisher else (lambda snap: None)

    path = Path(args.file).expanduser()
    threading.Thread(target=watch_file, args=(path, args.poll, on_update),
                     daemon=True).start()

    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"[bridge] HTTP listening on http://{args.bind}:{args.port} "
          f"(endpoints: /stats /metrics /health)", flush=True)
    print(f"[bridge] watching {path}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
