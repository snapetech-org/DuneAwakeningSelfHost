#!/usr/bin/env python3
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import map_catalog


def worker(partition_id, asset, *, profiles=None, labels=None):
    service = {
        "command": ["/run", asset, f"-PartitionIndex={partition_id}"],
        "networks": {"default": {"ipv4_address": f"172.31.250.{partition_id}"}},
    }
    if profiles:
        service["profiles"] = profiles
    if labels:
        service["labels"] = labels
    return service


class MapCatalogTest(unittest.TestCase):
    def test_discovers_new_noncontiguous_worker_and_extracts_routing_metadata(self):
        config = {"services": {
            "survival": worker(1, "/Game/Dune/Maps/Survival_1.Survival_1", labels={
                "dune.map.database-name": "Survival_1",
            }),
            "new-quest-map": worker(
                47,
                "/Game/Dune/Maps/Story/NewQuestMap.NewQuestMap",
            ),
        }}

        catalog = map_catalog.parse_compose_config(config, env_values={"DUNE_WORLD_PARTITION_COUNT": "auto"})

        self.assertEqual(["survival", "new-quest-map"], catalog["active_services"])
        self.assertEqual([1, 47], catalog["active_partition_ids"])
        destination = catalog["maps"][1]
        self.assertEqual("NewQuestMap", destination["map"])
        self.assertEqual("/Game/Dune/Maps/Story/NewQuestMap.NewQuestMap", destination["asset_path"])

    def test_profile_worker_needs_explicit_profile_or_legacy_opt_in(self):
        config = {"services": {
            "survival": worker(1, "/Game/Maps/Survival_1.Survival_1"),
            "deep-desert-pvp": worker(
                31,
                "/Game/Dune/Maps/Arrakis/DeepDesert_1/DeepDesert_1.DeepDesert_1",
                profiles=["disabled-deep-desert-pvp"],
                labels={
                    "dune.map.dimension-index": "1",
                    "dune.map.enabled-by-partition-count": "true",
                },
            ),
        }}

        disabled = map_catalog.parse_compose_config(config, env_values={"DUNE_WORLD_PARTITION_COUNT": "30"})
        enabled = map_catalog.parse_compose_config(config, env_values={"DUNE_WORLD_PARTITION_COUNT": "31"})
        explicitly_enabled = map_catalog.parse_compose_config(
            config, env_values={"DUNE_WORLD_PARTITION_COUNT": "auto"},
            active_profiles={"disabled-deep-desert-pvp"},
        )

        self.assertEqual(["survival"], disabled["active_services"])
        self.assertEqual(["survival", "deep-desert-pvp"], enabled["active_services"])
        self.assertEqual([1, 31], enabled["active_partition_ids"])
        self.assertEqual(["disabled-deep-desert-pvp"], enabled["active_profiles"])
        self.assertEqual(["survival", "deep-desert-pvp"], explicitly_enabled["active_services"])

    def test_duplicate_partition_ids_fail_closed(self):
        config = {"services": {
            "map-a": worker(4, "/Game/Maps/MapA.MapA"),
            "map-b": worker(4, "/Game/Maps/MapB.MapB"),
        }}
        with self.assertRaisesRegex(map_catalog.CatalogError, "duplicate map partition id: 4"):
            map_catalog.parse_compose_config(config)

    def test_duplicate_map_dimension_fails_closed(self):
        asset = "/Game/Maps/DeepDesert_1.DeepDesert_1"
        config = {"services": {
            "dd-one": worker(8, asset),
            "dd-two": worker(31, asset),
        }}
        with self.assertRaisesRegex(map_catalog.CatalogError, "duplicate database map/dimension"):
            map_catalog.parse_compose_config(config)

    def test_map_worker_without_cooked_asset_or_explicit_db_name_fails(self):
        config = {"services": {"broken-map": {"command": ["/run", "-PartitionIndex=12"]}}}
        with self.assertRaisesRegex(map_catalog.CatalogError, "no cooked map argument"):
            map_catalog.parse_compose_config(config)


if __name__ == "__main__":
    unittest.main()
