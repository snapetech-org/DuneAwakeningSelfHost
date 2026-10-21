#!/usr/bin/env python3
import importlib.util
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "sync-map-workers.py"
SPEC = importlib.util.spec_from_file_location("sync_map_workers", MODULE_PATH)
sync_map_workers = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(sync_map_workers)


def map_worker(partition_id, map_name, game_port, igw_port, ip, *, dimension=0):
    return {
        "command": [
            "/run", f"/Game/Maps/{map_name}.{map_name}",
            f"-Port={game_port}", f"-IGWPort={igw_port}",
            f"-PartitionIndex={partition_id}",
        ],
        "labels": {"dune.map.dimension-index": str(dimension)},
        "networks": {"default": {"ipv4_address": ip}},
    }


def official_template(map_names):
    return {
        "spec": {
            "world": {
                "partitions": [
                    {
                        "map": name,
                        "partitions": [{"dimension": 0, "id": index, "disable": False}],
                    }
                    for index, name in enumerate(map_names, 1)
                ]
            }
        }
    }


class MapWorkerDiscoveryTest(unittest.TestCase):
    def test_port_ranges_parse_and_reject_invalid_bounds(self):
        self.assertEqual(
            (7777, 7871),
            sync_map_workers._parse_port_range("7777:7871", "GAME_UDP_PORT_RANGE", (1, 1)),
        )
        self.assertEqual(
            (7888, 7982),
            sync_map_workers._parse_port_range(None, "IGW_UDP_PORT_RANGE", (7888, 7982)),
        )
        for value in ("7777", "0:10", "20:10", "10:65536"):
            with self.subTest(value=value), self.assertRaises(sync_map_workers.DiscoveryError):
                sync_map_workers._parse_port_range(value, "GAME_UDP_PORT_RANGE", (1, 1))

    def test_port_allocator_fails_closed_at_configured_range_limit(self):
        with self.assertRaisesRegex(sync_map_workers.DiscoveryError, "GAME_UDP_PORT_RANGE"):
            sync_map_workers._next_port(
                7871, {7871}, end=7871, range_name="GAME_UDP_PORT_RANGE",
            )

    def test_port_pools_reject_overlap(self):
        with self.assertRaisesRegex(sync_map_workers.DiscoveryError, "must not overlap"):
            sync_map_workers._validate_port_pools((7777, 7890), (7888, 7982))

    def test_game_allocator_uses_holes_before_requesting_growth(self):
        used = {7777, 7779}
        occupied = set(used)
        self.assertEqual(
            7778,
            sync_map_workers._next_game_port(
                7780, occupied, start=7777, end=7779,
            ),
        )

    def test_generated_overlay_and_cache_are_readable_by_compose(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            overlay = root / "compose.auto-maps.yaml"
            cache = root / "map-auto-discovery-cache.json"

            sync_map_workers.write_text_atomic(overlay, "services: {}\n")
            sync_map_workers._write_json_atomic(cache, {"assets": {}})

            self.assertEqual(0o644, overlay.stat().st_mode & 0o777)
            self.assertEqual(0o644, cache.stat().st_mode & 0o777)

    def test_asset_index_ignores_unrequested_package_name_collisions(self):
        assets = sync_map_workers._asset_entries(
            "SharedName\t/Game/Maps/Old/SharedName.SharedName\n"
            "SharedName\t/Game/Maps/New/SharedName.SharedName\n"
            "WantedMap\t/Game/Story/WantedMap.WantedMap\n",
            required_map_names={"WantedMap"},
        )

        self.assertEqual({"WantedMap": "/Game/Story/WantedMap.WantedMap"}, assets)

    def test_asset_index_rejects_ambiguous_requested_package_name(self):
        with self.assertRaisesRegex(sync_map_workers.DiscoveryError, "multiple cooked map assets"):
            sync_map_workers._asset_entries(
                "WantedMap\t/Game/Maps/One/WantedMap.WantedMap\n"
                "WantedMap\t/Game/Maps/Two/WantedMap.WantedMap\n",
                required_map_names={"WantedMap"},
            )

    def test_extracts_enabled_maps_and_ignores_disabled_or_empty_roster_entries(self):
        template = {
            "spec": {"world": {"partitions": [
                {"map": "MapA", "partitions": [{"dimension": 0, "id": 1, "disable": False}]},
                {"map": "MapB", "partitions": [{"dimension": 1, "id": 2, "disable": True}]},
                {"map": "MapC", "partitions": []},
            ]}}
        }

        maps = sync_map_workers.extract_template_maps(template)

        self.assertEqual([("MapA", 0)], [(row["map"], row["dimension_index"]) for row in maps])

    def test_allocates_new_partitions_ports_and_addresses_after_existing_farm(self):
        names = [
            "CB_Story_DestroyedZanovar",
            "CB_Story_OrbitalMonitor",
            "CB_Arrakis_Story_Paranoid_PrayerRoom",
            "CB_Arrakis_Story_Glutton_DiningRoom",
            "CB_Arrakis_Generic_Sietch_Room",
        ]
        config = {"services": {
            "survival": map_worker(1, "Survival_1", 7780, 7890, "172.31.240.10"),
            "deep-desert-pvp": map_worker(
                31, "DeepDesert_1", 7807, 7918, "172.31.240.43", dimension=1,
            ),
        }}
        existing, partitions, game_ports, igw_ports, worker_ips = sync_map_workers.collect_existing_workers(config)
        assets = {
            name: f"/Game/Maps/{name}.{name}"
            for name in names
        }
        planned, pending = sync_map_workers.plan_new_workers(
            sync_map_workers.extract_template_maps(official_template(names)),
            existing,
            assets,
            partitions,
            game_ports,
            igw_ports,
            worker_ips,
            all_used_ips={"172.31.240.10", "172.31.240.40", "172.31.240.43"},
            occupied_host_ports=set(range(7780, 7808)) | set(range(7890, 7919)),
            network_config={"ipam": {"config": [{
                "subnet": "172.31.240.0/24",
                "ip_range": "172.31.240.128/25",
            }]}},
        )

        self.assertEqual([], pending)
        self.assertEqual(list(range(32, 37)), [row["partition_id"] for row in planned])
        self.assertEqual(list(range(7808, 7813)), [row["port"] for row in planned])
        self.assertEqual(list(range(7919, 7924)), [row["igw_port"] for row in planned])
        self.assertEqual(
            [f"172.31.240.{address}" for address in range(44, 49)],
            [row["ipv4_address"] for row in planned],
        )
        self.assertEqual(
            ["cb-story-destroyed-zanovar", "cb-story-orbital-monitor",
             "cb-arrakis-story-paranoid-prayer-room", "cb-arrakis-story-glutton-dining-room",
             "cb-arrakis-generic-sietch-room"],
            [row["service"] for row in planned],
        )

    def test_waits_for_assets_in_the_selected_game_build(self):
        rows = sync_map_workers.extract_template_maps(official_template(["MapNotCookedYet"]))
        planned, pending = sync_map_workers.plan_new_workers(
            rows, {}, {}, set(), set(), set(), set(),
            all_used_ips=set(), occupied_host_ports=set(),
            network_config={"ipam": {"config": [{"subnet": "172.31.240.0/24"}]}},
        )

        self.assertEqual([], planned)
        self.assertEqual(["MapNotCookedYet"], pending)

    def test_new_workers_cannot_escape_configured_port_pools(self):
        rows = sync_map_workers.extract_template_maps(official_template(["NewMap"]))
        with self.assertRaisesRegex(sync_map_workers.DiscoveryError, "GAME_UDP_PORT_RANGE"):
            sync_map_workers.plan_new_workers(
                rows, {}, {"NewMap": "/Game/Maps/NewMap.NewMap"}, set(), set(), set(), set(),
                all_used_ips=set(), occupied_host_ports={7777},
                network_config={"ipam": {"config": [{"subnet": "172.31.240.0/24"}]}},
                game_port_range=(7777, 7777), igw_port_range=(7888, 7888),
            )

    def test_growth_widens_only_exhausted_pools_and_preserves_existing_assignments(self):
        old_map = "OldMap"
        new_map = "NewMap"
        old_worker = {
            "service": "old-map",
            "map": old_map,
            "dimension_index": 0,
            "asset_path": "/Game/Maps/OldMap.OldMap",
            "auto_discovered": True,
            "partition_id": 1,
            "port": 7777,
            "igw_port": 7888,
            "ipv4_addresses": ["172.31.240.10"],
        }
        planned, pending, game_range, igw_range = sync_map_workers.plan_new_workers_with_port_growth(
            sync_map_workers.extract_template_maps(official_template([old_map, new_map])),
            {(old_map, 0): old_worker},
            {old_map: "/Game/Maps/OldMap.OldMap", new_map: "/Game/Maps/NewMap.NewMap"},
            {1},
            {7777},
            {7888},
            {"172.31.240.10"},
            all_used_ips={"172.31.240.10"},
            occupied_host_ports=set(),
            network_config={"ipam": {"config": [{"subnet": "172.31.240.0/24"}]}},
            game_port_range=(7777, 7777),
            igw_port_range=(7888, 7888),
            growth_step=64,
        )

        self.assertEqual([], pending)
        self.assertEqual((7713, 7777), game_range)
        self.assertEqual((7888, 7952), igw_range)
        self.assertEqual((7777, 7888), (planned[0]["port"], planned[0]["igw_port"]))
        self.assertEqual((7713, 7889), (planned[1]["port"], planned[1]["igw_port"]))

    def test_preserves_generated_assignments_when_template_changes(self):
        old_map = "CB_Story_DestroyedZanovar"
        new_map = "CB_Story_NewChapter"
        old_asset = f"/Game/DLC/Old/{old_map}.{old_map}"
        config = {"services": {
            "deep-desert-pvp": map_worker(
                31, "DeepDesert_1", 7807, 7918, "172.31.240.43", dimension=1,
            ),
            "cb-story-destroyed-zanovar": {
                **map_worker(32, old_map, 7808, 7919, "172.31.240.44"),
                "labels": {
                    "dune.map.database-name": old_map,
                    "dune.map.auto-discovered": "true",
                },
            },
        }}
        existing, partitions, game_ports, igw_ports, worker_ips = sync_map_workers.collect_existing_workers(config)
        assets = {
            old_map: f"/Game/DLC/Updated/{old_map}.{old_map}",
            new_map: f"/Game/DLC/New/{new_map}.{new_map}",
        }

        planned, pending = sync_map_workers.plan_new_workers(
            sync_map_workers.extract_template_maps(official_template([old_map, new_map])),
            existing,
            assets,
            partitions,
            game_ports,
            igw_ports,
            worker_ips,
            all_used_ips={"172.31.240.43", "172.31.240.44"},
            occupied_host_ports={7807, 7808, 7918, 7919},
            network_config={"ipam": {"config": [{"subnet": "172.31.240.0/24"}]}},
        )

        self.assertEqual([], pending)
        self.assertEqual([32, 33], [row["partition_id"] for row in planned])
        self.assertEqual(old_asset.replace("Old", "Updated"), planned[0]["asset_path"])
        self.assertEqual("cb-story-destroyed-zanovar", planned[0]["service"])
        self.assertEqual(7808, planned[0]["port"])
        self.assertEqual("172.31.240.44", planned[0]["ipv4_address"])
        self.assertEqual(33, planned[1]["partition_id"])

    def test_renders_override_lists_without_credential_arguments(self):
        worker = {
            "service": "cb-story-example",
            "map": "CB_Story_Example",
            "dimension_index": 0,
            "partition_id": 32,
            "asset_path": "/Game/DLC/Story/CB_Story_Example.CB_Story_Example",
            "port": 7808,
            "igw_port": 7919,
            "ipv4_address": "172.31.240.44",
        }
        template_service = {"command": [
            "/workspace/scripts/run_server_safe.sh",
            "-MultiHome=$$POD_IP",
            "/Game/Dungeons/OldMap.OldMap",
            "-Port=7806", "-IGWPort=7917", "-PartitionIndex=30",
        ]}

        output = sync_map_workers.render_overlay(
            [worker],
            root=pathlib.Path("/tmp/dune-test-workspace"),
            template_compose_file=pathlib.Path("compose.allmaps.yaml"),
            template_service=template_service,
        )

        self.assertIn("ports: !override", output)
        self.assertIn('"7808:7808/udp"', output)
        self.assertIn('"7919:7919/udp"', output)
        self.assertNotIn("${FLS_SECRET}", output)
        self.assertNotIn("${POSTGRES_DUNE_PASSWORD}", output)
        self.assertNotIn("ServiceAuthToken", output)
        self.assertNotIn("DatabasePassword", output)
        self.assertIn("/Game/DLC/Story/CB_Story_Example.CB_Story_Example", output)


if __name__ == "__main__":
    unittest.main()
