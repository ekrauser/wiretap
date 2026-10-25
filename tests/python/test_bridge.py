"""Tests for companion/wiretap_bridge.py (stdlib only; no broker, no game)."""
import http.client
import importlib.util
import json
import os
import pathlib
import tempfile
import threading
import time
import types
import unittest
from http.server import ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
_spec = importlib.util.spec_from_file_location("wiretap_bridge", ROOT / "companion" / "wiretap_bridge.py")
wb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wb)

# Snapshot produced by the mod under tests/lua (regenerate with
# `lua tests/lua/dump_snapshot.lua > tests/python/fixtures/snapshot-mock.json`).
SNAP = json.loads((HERE / "fixtures" / "snapshot-mock.json").read_text())


def families_of(text):
    seen, current = [], None
    for line in text.splitlines():
        if line.startswith("# TYPE "):
            name = line.split()[2]
            if name in seen:
                raise AssertionError(f"family {name} emitted twice")
            seen.append(name)
            current = name
        elif line and not line.startswith("#"):
            name = line.split("{")[0].split(" ")[0]
            if name != current:
                raise AssertionError(f"sample outside its family: {line}")
    return seen


class RenderMetrics(unittest.TestCase):
    def setUp(self):
        self.text = wb.render_metrics(SNAP, time.time() - 5, time.time(), {"science"})

    def test_families_are_grouped_with_one_type_line(self):
        self.assertGreater(len(families_of(self.text)), 40)
        self.assertTrue(self.text.endswith("\n"))

    def test_values(self):
        for expected in [
            'factorio_mod_info{game_version="2.1.23",mod_version="1.1.0"} 1',
            "factorio_game_speed 1",
            "factorio_game_paused 0",
            'factorio_player_afk_seconds{player="drkraus"} 20',
            'factorio_surface_info{is_platform="0",planet="nauvis",pollutant="pollution",surface="nauvis"} 1',
            'factorio_surface_darkness{surface="nauvis"} 0.2',
            'factorio_pollution{pollutant="pollution",surface="nauvis"} 187771.506',
            'factorio_power_production_watts{surface="nauvis"} 1860000',
            'factorio_power_consumption_satisfaction{surface="nauvis"} 0.95',
            'factorio_power_accumulator_discharge_watts{surface="nauvis"} 200000',
            'factorio_power_maximum_production_watts{surface="nauvis"} 2700000',
            'factorio_network_sub_network_count{network="1",surface="nauvis"} 2',
            'factorio_network_consumption_satisfaction{network="1",surface="nauvis"} 0.95',
            'factorio_network_production_by_entity_watts{entity="steam-engine",network="1",surface="nauvis"} 1800000',
            'factorio_network_production_watts{network="global",surface="platform-1"} 400000',
            'factorio_entity_count{entity="lab",surface="nauvis"} 12',
            'factorio_logistic_item_count{force="player",item="nutrients",surface="nauvis"} -2',
            'factorio_logistic_item_quality_count{force="player",item="iron-plate",quality="rare",surface="nauvis"} 200',
            'factorio_robots{force="player",kind="logistic",state="available",surface="nauvis"} 41',
            'factorio_research_info{force="player",level="1",technology="logistics-2"} 1',
            'factorio_research_progress{force="player"} 0.62',
            'factorio_research_current_units{force="player"} 200',
            'factorio_research_queue_length{force="player"} 4',
            'factorio_science_per_minute{force="player"} 120',
            'factorio_science_total{force="player"} 7000',
            'factorio_labs{force="player",surface="nauvis"} 12',
            'factorio_technologies_available{force="player"} 3',
            'factorio_rockets_launched_total{force="player"} 3',
            'factorio_items_launched_total{force="player",item="space-science-pack"} 1000',
            'factorio_evolution_factor{force="enemy",surface="nauvis"} 0.14',
            'factorio_evolution_factor_part{force="enemy",part="pollution",surface="nauvis"} 0.1',
            'factorio_science_pack_consumption_per_minute{force="player",item="automation-science-pack",surface="nauvis"} 60',
            'factorio_science_pack_consumption_per_minute_10m{force="player",item="automation-science-pack",surface="nauvis"} 60.1',
            'factorio_kills_per_minute{force="player",surface="nauvis"} 3.5',
            'factorio_losses_total{entity="gun-turret",force="player",surface="nauvis"} 2',
            'factorio_items_produced_total{force="player",item="science",surface="nauvis"} 7000',
            'factorio_platform_info{destination="vulcanus",force="player",location="nauvis",name="Hauler",platform="platform-1",state="on_the_path"} 1',
            'factorio_platform_damaged_tiles{force="player",platform="platform-1"} 3',
            'factorio_platform_completed_trips_total{force="player",platform="platform-1"} 7',
            'factorio_platform_paused{force="player",platform="platform-1"} 0',
            "factorio_game_tick 29628000",
        ]:
            self.assertIn(expected, self.text, expected)
        self.assertIn("# TYPE factorio_rockets_launched_total counter", self.text)
        self.assertNotIn('item="iron-plate",surface="nauvis"} 51234', self.text)
        age = [l for l in self.text.splitlines() if l.startswith("factorio_stats_age_seconds ")][0]
        self.assertTrue(4 <= float(age.split()[1]) <= 15)

    def test_production_items_all(self):
        text = wb.render_metrics(SNAP, None, None, {"all"})
        self.assertIn('factorio_items_produced_total{force="player",item="iron-plate",surface="nauvis"} 51234', text)
        self.assertIn('factorio_fluids_consumed_total{force="player",item="water",surface="nauvis"} 1200000', text)

    def test_without_snapshot(self):
        txt = wb.render_metrics(None, None)
        self.assertIn("factorio_bridge_up 1", txt)
        self.assertNotIn("stats_age", txt)
        self.assertNotIn("game_tick", txt)
        txt = wb.render_metrics(None, time.time() - 100)
        self.assertIn("factorio_stats_age_seconds", txt)

    def test_label_escaping(self):
        txt = wb.render_metrics({"surfaces": {'we"ird\\x\n': {"pollution": 1}}}, None)
        self.assertIn('factorio_pollution{pollutant="none",surface="we\\"ird\\\\x\\n"} 1', txt)

    def test_num_and_null_values(self):
        self.assertEqual(wb._num(float("nan")), "NaN")
        self.assertEqual(wb._num(float("inf")), "+Inf")
        self.assertEqual(wb._num(True), "1")
        self.assertIsNone(wb._num(None))
        self.assertIsNone(wb._num("x"))
        self.assertEqual(wb._num(0.1), "0.1")
        txt = wb.render_metrics({"meta": {"tick": 5}, "surfaces": {"nauvis": {"pollution": None}}}, None)
        self.assertNotIn("factorio_pollution", txt)
        self.assertIn("factorio_game_tick 5", txt)

    def test_tolerates_lists_for_empty_tables(self):
        txt = wb.render_metrics({"surfaces": [], "forces": {"player": {"research": []}}}, None)
        self.assertIn("factorio_bridge_up 1", txt)


class PrometheusValidity(unittest.TestCase):
    def test_no_duplicate_series_and_numeric_values(self):
        text = wb.render_metrics(SNAP, time.time(), time.time(), {"all"})
        seen = set()
        for line in text.splitlines():
            if not line or line.startswith("#"):
                continue
            series, _, value = line.rpartition(" ")
            self.assertNotIn(series, seen, f"duplicate series {series}")
            seen.add(series)
            float(value)  # must parse
        self.assertGreater(len(seen), 150)


# Home Assistant discovery rules the bridge must obey (subset relevant here).
_HA_SENSOR_UNITS = {
    "power": {"W", "kW", "MW", "GW", "TW"},
    "battery": {"%"},
    "duration": {"d", "h", "min", "s", "ms"},
}
_HA_BINARY_CLASSES = {"problem", "running", "connectivity", "power", "motion", "safety", "occupancy"}


class DiscoveryPayloads(unittest.TestCase):
    def test_every_config_is_valid_for_home_assistant(self):
        pub = make_publisher()
        pub.publish(SNAP)
        configs = configs_of(pub)
        self.assertGreater(len(configs), 40)
        for topic, cfg in configs.items():
            component = topic.split("/")[1]
            self.assertIn(component, ("sensor", "binary_sensor"), topic)
            for key in ("name", "unique_id", "state_topic", "value_template", "availability_topic", "device"):
                self.assertIn(key, cfg, f"{topic} lacks {key}")
            self.assertTrue(cfg["unique_id"].startswith("wiretap_"), topic)
            self.assertEqual(cfg["device"]["identifiers"], ["wiretap"], topic)
            if "icon" in cfg:
                self.assertTrue(cfg["icon"].startswith("mdi:"), topic)
            if component == "binary_sensor":
                self.assertNotIn("unit_of_measurement", cfg, topic)
                self.assertNotIn("state_class", cfg, topic)
                self.assertEqual((cfg.get("payload_on"), cfg.get("payload_off")), ("ON", "OFF"), topic)
                if "device_class" in cfg:
                    self.assertIn(cfg["device_class"], _HA_BINARY_CLASSES, topic)
                self.assertIn("'ON'", cfg["value_template"], topic)
            else:
                dc = cfg.get("device_class")
                if dc:
                    self.assertIn(dc, _HA_SENSOR_UNITS, f"{topic}: unexpected device_class {dc}")
                    self.assertIn(cfg.get("unit_of_measurement"), _HA_SENSOR_UNITS[dc], topic)
                if "state_class" in cfg:
                    self.assertIn(cfg["state_class"], ("measurement", "total", "total_increasing"), topic)
                    # numeric sensors: the template must not yield free text
                    self.assertNotRegex(cfg["value_template"], r"default\('(none|unknown|in transit)'\)", topic)
                else:
                    # text sensors carry no unit
                    self.assertNotIn("unit_of_measurement", cfg, topic)


class Slugs(unittest.TestCase):
    def test_vanilla_names_map_plainly(self):
        self.assertEqual(wb._stable_slug("nauvis"), "nauvis")
        self.assertEqual(wb._stable_slug("platform-1"), "platform_1")
        self.assertEqual(wb._stable_slug("player"), "player")

    def test_lossy_names_get_a_hash_and_never_collide(self):
        a, b = wb._stable_slug("Platform 1"), wb._stable_slug("platform:1")
        self.assertTrue(a.startswith("platform_1_") and b.startswith("platform_1_"))
        self.assertNotEqual(a, b)
        self.assertEqual(a, wb._stable_slug("Platform 1"))


class ParseSnapshot(unittest.TestCase):
    def test_object(self):
        self.assertEqual(wb.parse_snapshot('{"meta": {"tick": 1}}')["meta"]["tick"], 1)

    def test_journal_uses_last_complete_line(self):
        text = '{"meta": {"tick": 1}}\n{"meta": {"tick": 2}}\n'
        self.assertEqual(wb.parse_snapshot(text)["meta"]["tick"], 2)
        torn = text + '{"meta": {"tick": 3'
        self.assertEqual(wb.parse_snapshot(torn)["meta"]["tick"], 2)

    def test_rejects_non_snapshots(self):
        for bad in ("[]", "null", '{"x": 1}', '"str"'):
            with self.assertRaises(ValueError):
                wb.parse_snapshot(bad)


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))
        return types.SimpleNamespace(rc=0)


def make_publisher(**overrides):
    args = types.SimpleNamespace(mqtt_prefix="wiretap", watch_items="iron-plate,copper-plate",
                                 ha_discovery=True, publish_full=False, ha_prefix="homeassistant", flat=True)
    for k, v in overrides.items():
        setattr(args, k, v)
    return wb.MqttPublisher(args, client=FakeClient())


def configs_of(pub):
    return {t: json.loads(p) for t, p, _ in pub.client.published
            if t.startswith(pub.ha_prefix + "/") and p}


class Mqtt(unittest.TestCase):
    def test_topics_payloads_and_flat_scalars(self):
        pub = make_publisher()
        pub.publish(SNAP)
        published = pub.client.published
        topics = {t for t, _, _ in published}
        for t in ["wiretap/meta", "wiretap/items", "wiretap/power/nauvis", "wiretap/power/platform_1",
                  "wiretap/pollution/nauvis", "wiretap/evolution/enemy/nauvis", "wiretap/science/nauvis",
                  "wiretap/research/player", "wiretap/platforms/player_platform_1", "wiretap/kills/player/nauvis",
                  "wiretap/robots/player/nauvis", "wiretap/rockets/player", "wiretap/surface/nauvis"]:
            self.assertIn(t, topics)
        self.assertNotIn("wiretap/state", topics)
        self.assertTrue(all(retain for _, _, retain in published))

        payloads = {t: p for t, p, _ in published}
        power = json.loads(payloads["wiretap/power/nauvis"])
        self.assertEqual(power["satisfaction_percent"], 95.0)
        self.assertEqual(power["satisfaction_min_percent"], 95.0)
        self.assertEqual(power["charge_percent"], 50.0)
        self.assertEqual(power["headroom_watts"], 1000000)
        self.assertTrue(power["brownout"])
        self.assertEqual(json.loads(payloads["wiretap/items"])["iron-plate"], 3200)
        self.assertEqual(json.loads(payloads["wiretap/platforms/player_platform_1"])["state"], "on_the_path")
        kills = json.loads(payloads["wiretap/kills/player/nauvis"])
        self.assertEqual(kills["kills_total"], 520)
        self.assertTrue(kills["under_attack"])
        # flat scalar topics for microcontrollers: plain text, no JSON
        self.assertEqual(payloads["wiretap/flat/nauvis/power/production_watts"], "1860000")
        self.assertEqual(payloads["wiretap/flat/nauvis/power/brownout"], "ON")
        self.assertEqual(payloads["wiretap/flat/nauvis/power/satisfaction_percent"], "95.0")
        self.assertEqual(payloads["wiretap/flat/player/research/current"], "logistics-2")
        self.assertEqual(payloads["wiretap/flat/player/research/progress_percent"], "62.0")
        self.assertEqual(payloads["wiretap/flat/items/iron_plate"], "3200")
        self.assertEqual(payloads["wiretap/flat/platforms/player_platform_1/state"], "on_the_path")
        self.assertEqual(payloads["wiretap/flat/meta/paused"], "OFF")
        self.assertEqual(payloads["wiretap/flat/nauvis/kills/losses_per_minute"], "0.25")
        self.assertEqual(payloads["wiretap/flat/nauvis/evolution"], "0.14")

    def test_discovery_configs(self):
        pub = make_publisher()
        pub.publish(SNAP)
        configs = configs_of(pub)
        sat = configs["homeassistant/sensor/wiretap/nauvis_satisfaction/config"]
        self.assertEqual(sat["unit_of_measurement"], "%")
        self.assertEqual(sat["state_topic"], "wiretap/power/nauvis")
        self.assertEqual(sat["availability_topic"], "wiretap/availability")
        self.assertEqual(sat["unique_id"], "wiretap_nauvis_satisfaction")
        self.assertEqual(sat["device"]["identifiers"], ["wiretap"])
        self.assertEqual(sat["device"]["sw_version"], "1.1.0")
        self.assertEqual(sat["device"]["model"], "Factorio 2.1.23")
        brownout = configs["homeassistant/binary_sensor/wiretap/nauvis_brownout/config"]
        self.assertEqual(brownout["device_class"], "problem")
        self.assertEqual((brownout["payload_on"], brownout["payload_off"]), ("ON", "OFF"))
        self.assertNotIn("state_class", brownout)
        for text_sensor in ("platform_player_platform_1_state", "platform_player_platform_1_location",
                            "platform_player_platform_1_destination", "player_research"):
            self.assertNotIn("state_class", configs[f"homeassistant/sensor/wiretap/{text_sensor}/config"], text_sensor)
        self.assertEqual(configs["homeassistant/sensor/wiretap/player_research_eta/config"]["device_class"], "duration")
        self.assertEqual(configs["homeassistant/sensor/wiretap/item_iron_plate/config"]["state_topic"], "wiretap/items")
        self.assertEqual(configs["homeassistant/sensor/wiretap/game_tick/config"]["state_class"], "total_increasing")
        self.assertIn("homeassistant/binary_sensor/wiretap/player_nauvis_under_attack/config", configs)
        self.assertIn("homeassistant/sensor/wiretap/nauvis_evolution/config", configs)
        self.assertIn("homeassistant/sensor/wiretap/player_nauvis_logistic_robots/config", configs)
        unique_ids = [c["unique_id"] for c in configs.values()]
        self.assertEqual(len(unique_ids), len(set(unique_ids)))
        self.assertTrue(all(u.startswith("wiretap_") for u in unique_ids))

    def test_prefix_namespaces_everything(self):
        pub = make_publisher(mqtt_prefix="factorio2")
        pub.publish(SNAP)
        topics = {t for t, _, _ in pub.client.published}
        self.assertIn("factorio2/power/nauvis", topics)
        configs = configs_of(pub)
        sat = configs["homeassistant/sensor/factorio2/nauvis_satisfaction/config"]
        self.assertEqual(sat["unique_id"], "factorio2_nauvis_satisfaction")
        self.assertEqual(sat["device"]["identifiers"], ["factorio2"])
        self.assertEqual(sat["state_topic"], "factorio2/power/nauvis")
        self.assertEqual(sat["availability_topic"], "factorio2/availability")

    def test_discovery_is_sent_once_until_reconnect(self):
        pub = make_publisher()

        def discovery_count():
            return sum(1 for t, _, _ in pub.client.published if t.startswith("homeassistant/"))

        pub.publish(SNAP)
        first = discovery_count()
        self.assertGreater(first, 0)
        pub.publish(SNAP)
        self.assertEqual(discovery_count(), first)
        pub.discovered.clear()  # what _on_connect does after a reconnect
        pub.publish(SNAP)
        self.assertEqual(discovery_count(), 2 * first)

    def test_removed_platform_is_retired(self):
        pub = make_publisher()
        pub.publish(SNAP)
        without = json.loads(json.dumps(SNAP))
        del without["forces"]["player"]["platforms"]
        pub.client.published.clear()
        pub.publish(without)
        cleared = {t for t, p, _ in pub.client.published if p == ""}
        self.assertIn("homeassistant/sensor/wiretap/platform_player_platform_1_state/config", cleared)
        self.assertNotIn("homeassistant/sensor/wiretap/nauvis_production/config", cleared)

    def test_no_discovery_and_no_flat_when_disabled(self):
        pub = make_publisher(ha_discovery=False, flat=False)
        pub.publish(SNAP)
        topics = {t for t, _, _ in pub.client.published}
        self.assertFalse(any(t.startswith("homeassistant/") for t in topics))
        self.assertFalse(any(t.startswith("wiretap/flat/") for t in topics))

    def test_publish_full(self):
        pub = make_publisher(publish_full=True)
        pub.publish(SNAP)
        self.assertIn("wiretap/state", {t for t, _, _ in pub.client.published})


class Watcher(unittest.TestCase):
    def run_watcher(self, path, script):
        """Run watch_file with time.sleep replaced by `script(n)`; stop with StopIteration."""
        updates = []
        original = wb.time.sleep
        wb.time.sleep = script
        try:
            with self.assertRaises(StopIteration):
                wb.watch_file(path, 0.01, updates.append)
        finally:
            wb.time.sleep = original
        return updates

    def test_torn_write_is_retried_then_published(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "stats.json"
            before = wb.STATE.parse_errors
            calls = {"n": 0}

            def script(_seconds):
                calls["n"] += 1
                if calls["n"] == 1:
                    path.write_text('{"meta": {"tick": 1', encoding="utf-8")  # torn mid-write
                elif calls["n"] == 2:
                    path.write_text(json.dumps({"meta": {"tick": 2}}), encoding="utf-8")
                elif calls["n"] >= 4:
                    raise StopIteration

            updates = self.run_watcher(path, script)
            self.assertEqual([u["meta"]["tick"] for u in updates], [2])
            self.assertEqual(wb.STATE.parse_errors, before + 1)
            self.assertEqual(wb.STATE.get()[0]["meta"]["tick"], 2)

    def test_unparseable_file_is_read_once_per_signature(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "stats.json"
            path.write_text("garbage", encoding="utf-8")
            before = wb.STATE.parse_errors
            calls = {"n": 0}

            def script(_seconds):
                calls["n"] += 1
                if calls["n"] >= 5:
                    raise StopIteration

            self.run_watcher(path, script)
            self.assertEqual(wb.STATE.parse_errors, before + 1)

    def test_age_comes_from_file_mtime(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "stats.json"
            path.write_text(json.dumps({"meta": {"tick": 9}}), encoding="utf-8")
            old = time.time() - 7200
            os.utime(path, (old, old))
            calls = {"n": 0}

            def script(_seconds):
                calls["n"] += 1
                if calls["n"] >= 2:
                    raise StopIteration

            self.run_watcher(path, script)
            snapshot, file_mtime, updated_at = wb.STATE.get()
            self.assertEqual(snapshot["meta"]["tick"], 9)
            self.assertAlmostEqual(file_mtime, old, places=0)
            self.assertGreater(updated_at, old + 7000)
            text = wb.render_metrics(snapshot, file_mtime, updated_at)
            age = [l for l in text.splitlines() if l.startswith("factorio_stats_age_seconds ")][0]
            self.assertGreater(float(age.split()[1]), 7000)


class HttpApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        wb.Handler.args = types.SimpleNamespace(cors_origin="*", token="s3cret", stale_after=None,
                                                production_item_set={"science"}, verbose=False)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), wb.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        wb.Handler.args = None

    def request(self, method, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, headers=headers or {})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp, body

    def test_health_fresh_and_stale(self):
        wb.STATE.set(SNAP, time.time())
        resp, body = self.request("GET", "/health")
        self.assertEqual(resp.status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertEqual(data["stale_after_seconds"], 30.0)  # 3 x 10 s interval, floor 30
        wb.STATE.set(SNAP, time.time() - 7200)
        resp, body = self.request("GET", "/health")
        self.assertEqual(resp.status, 503)
        data = json.loads(body)
        self.assertFalse(data["ok"])
        self.assertEqual(data["reason"], "snapshot stale")
        self.assertGreater(float(resp.getheader("X-Stats-Age-Seconds")), 7000)

    def test_stats_requires_token_and_supports_head(self):
        wb.STATE.set(SNAP, time.time())
        resp, _ = self.request("GET", "/stats")
        self.assertEqual(resp.status, 401)
        resp, body = self.request("GET", "/stats", {"Authorization": "Bearer s3cret"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(body)["meta"]["tick"], 29628000)
        self.assertEqual(resp.getheader("Access-Control-Allow-Origin"), "*")
        resp, body = self.request("HEAD", "/stats", {"Authorization": "Bearer s3cret"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(body, b"")
        self.assertGreater(int(resp.getheader("Content-Length")), 1000)

    def test_metrics_and_root(self):
        wb.STATE.set(SNAP, time.time())
        resp, body = self.request("GET", "/metrics", {"Authorization": "Bearer s3cret"})
        self.assertEqual(resp.status, 200)
        self.assertIn("text/plain; version=0.0.4; charset=utf-8", resp.getheader("Content-Type"))
        self.assertIn(b"# TYPE factorio_game_tick gauge", body)
        resp, body = self.request("GET", "/")
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(body)["service"], "wiretap-bridge")
        resp, _ = self.request("GET", "/nope")
        self.assertEqual(resp.status, 404)
        resp, _ = self.request("OPTIONS", "/stats")
        self.assertEqual(resp.status, 204)


class Versions(unittest.TestCase):
    def test_changelog_has_entry_for_manifest_version(self):
        info = json.loads((ROOT / "mod" / "wiretap" / "info.json").read_text())
        changelog = (ROOT / "mod" / "wiretap" / "changelog.txt").read_text()
        self.assertIn(f"\nVersion: {info['version']}\n", "\n" + changelog)
        self.assertEqual(changelog.split("\n")[1], f"Version: {info['version']}")
        self.assertEqual(info["factorio_version"], "2.1")


if __name__ == "__main__":
    unittest.main()
