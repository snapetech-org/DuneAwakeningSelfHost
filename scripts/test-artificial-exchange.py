#!/usr/bin/env python3
import csv
import importlib.util
import json
import pathlib
import types
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


catalog = load_script("build_exchange_catalog_under_test", "scripts/build-exchange-catalog.py")
bot = load_script("artificial_exchange_bot_under_test", "scripts/artificial-exchange-bot.py")
research = load_script("research_exchange_prices_under_test", "scripts/research-exchange-prices.py")
dune_exchange_import = load_script("import_dune_exchange_prices_under_test", "scripts/import-dune-exchange-prices.py")


class ArtificialExchangeEnvironmentTest(unittest.TestCase):
    def test_dotenv_file_reads_can_be_disabled_for_systemd_environment(self):
        with mock.patch.dict(bot.os.environ, {"DUNE_ARTIFICIAL_EXCHANGE_SKIP_DOTENV_FILE": "true"}):
            self.assertFalse(bot.should_read_env_files())
            self.assertFalse(catalog.should_read_env_files())

        with mock.patch.dict(bot.os.environ, {"DUNE_ARTIFICIAL_EXCHANGE_SKIP_DOTENV_FILE": "0"}):
            self.assertTrue(bot.should_read_env_files())
            self.assertTrue(catalog.should_read_env_files())

    def test_seeded_stats_owner_filter_uses_bound_parameters(self):
        for owner_id in (0, 124):
            with self.subTest(owner_id=owner_id):
                conn = mock.MagicMock()
                cursor = conn.cursor.return_value.__enter__.return_value
                cursor.fetchall.return_value = []
                args = types.SimpleNamespace(exchange_id=2, populator_owner_id=owner_id, limit=50, report_skips=3)

                bot.audit_seeded_stats(conn, args, {})

                query, params = cursor.execute.call_args.args
                self.assertIn("and (%s <= 0 or o.owner_id=%s)", query)
                if owner_id > 0:
                    self.assertNotIn(str(owner_id), query)
                self.assertEqual(params, (2, owner_id, owner_id, 50))


class ArtificialExchangeCatalogTest(unittest.TestCase):
    def write_csv(self, path, rows):
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=catalog.FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    def base_row(self, template_id="ItemA", **overrides):
        row = {
            "template_id": template_id,
            "display_name": template_id,
            "category": "materials",
            "category_mask": "0",
            "category_depth": "0",
            "sellable_status": "validated",
            "baseline_price": "100",
            "max_buy_price": "80",
            "price_floor": "",
            "price_ceiling": "",
            "liquidity_tier": "medium",
            "enabled": "true",
            "source": "manual",
            "confidence": "moderate",
            "notes": "tier=2",
        }
        row.update(overrides)
        return row

    def test_manual_rows_validate_and_override_snapshot_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            manual = tmp_path / "manual.csv"
            snapshot_dir = tmp_path / "snapshots"
            output_dir = tmp_path / "out"
            snapshot_dir.mkdir()
            self.write_csv(snapshot_dir / "snapshot.csv", [
                self.base_row("ItemA", baseline_price="50", max_buy_price="40", enabled="false", source="snapshot", confidence="low"),
                self.base_row("ItemB", baseline_price="200", max_buy_price="", enabled="false", source="snapshot", confidence="low"),
            ])
            self.write_csv(manual, [self.base_row("ItemA", baseline_price="120", max_buy_price="90", confidence="high")])

            merged = {}
            snapshots = []
            for path in snapshot_dir.glob("*.csv"):
                snapshots.extend(catalog.read_csv_rows(path))
            for row in catalog.merge_snapshot_rows(snapshots):
                merged.setdefault(row["template_id"], row)
            for row in catalog.read_csv_rows(manual):
                merged[row["template_id"]] = row
            latest_json, latest_csv = catalog.write_outputs([merged[tid] for tid in sorted(merged)], output_dir)

            self.assertTrue(latest_json.exists())
            self.assertTrue(latest_csv.exists())
            self.assertEqual(merged["ItemA"]["baseline_price"], 120)
            self.assertEqual(merged["ItemA"]["max_buy_price"], 90)
            self.assertTrue(merged["ItemA"]["enabled"])
            self.assertFalse(merged["ItemB"]["enabled"])
            self.assertEqual(merged["ItemB"]["max_buy_price"], 160)

    def test_snapshot_median_requires_three_rows_for_moderate_confidence(self):
        rows = [
            catalog.clean_row(self.base_row("ItemA", baseline_price="100", enabled="false"), "row1"),
            catalog.clean_row(self.base_row("ItemA", baseline_price="200", enabled="false"), "row2"),
            catalog.clean_row(self.base_row("ItemA", baseline_price="300", enabled="false"), "row3"),
        ]
        merged = catalog.merge_snapshot_rows(rows)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["baseline_price"], 200)
        self.assertEqual(merged[0]["max_buy_price"], 160)
        self.assertEqual(merged[0]["confidence"], "moderate")
        self.assertEqual(merged[0]["liquidity_tier"], "medium")

    def test_malformed_price_duplicate_and_unknown_confidence_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "bad.csv"
            self.write_csv(path, [self.base_row("ItemA", baseline_price="nope")])
            with self.assertRaises(ValueError):
                catalog.read_csv_rows(path)

            self.write_csv(path, [self.base_row("ItemA"), self.base_row("ItemA")])
            with self.assertRaises(ValueError):
                catalog.read_csv_rows(path)

            self.write_csv(path, [self.base_row("ItemB", confidence="certain")])
            with self.assertRaises(ValueError):
                catalog.read_csv_rows(path)

    def test_reconcile_known_category_masks_updates_exchange_categories(self):
        rows, stats = catalog.reconcile_known_category_masks([
            self.base_row("ItemA", category="weapons/ranged", category_mask=0, category_depth=0, notes="tier=2"),
            self.base_row("ItemB", category="not/a/category", category_mask=0, category_depth=0),
        ])

        self.assertEqual(stats, {"updated": 1, "missing": 1})
        self.assertEqual(rows[0]["category_mask"], 0x01010700)
        self.assertEqual(rows[0]["category_depth"], 3)
        self.assertIn("category mask reconciled from exchange_category_map", rows[0]["notes"])
        self.assertEqual(rows[1]["category_mask"], 0)
        self.assertEqual(rows[1]["category_depth"], 0)

    def test_category_override_wins_then_mask_recomputes(self):
        rows, stats = catalog.apply_category_overrides(
            [
                self.base_row("SpicedFuelCell", category="resources/refined", category_mask=0x05010000, category_depth=2),
                self.base_row("MelangeSpice", category="resources/refined", category_mask=0x05010000, category_depth=2),
            ],
            {"SpicedFuelCell": "resources/fuel"},
        )
        self.assertEqual(stats, {"updated": 1})
        self.assertEqual(rows[0]["category"], "resources/fuel")
        self.assertEqual(rows[0]["category_mask"], 0)  # cleared for recompute
        self.assertIn("category set from reviewed override", rows[0]["notes"])
        self.assertEqual(rows[1]["category"], "resources/refined")  # untouched
        # mask reconcile then derives the correct fuel mask from the override
        rows, _ = catalog.reconcile_known_category_masks(rows)
        self.assertEqual(rows[0]["category_mask"], 0x05030000)
        self.assertEqual(rows[0]["category_depth"], 2)


class ArtificialExchangePriceResearchTest(unittest.TestCase):
    def test_extract_item_data_reads_wiki_price_and_item_id(self):
        wikitext = """
<!-- |ITEMID|ScrapMetalKnife|ITEMID| -->
| Name || Scrap Metal Knife
| Base Vendor Price || 50
[[Category:Weapons]]
[[Category:Daggers]]
"""
        data = research.extract_item_data(wikitext, "Scrap Metal Knife")
        self.assertEqual(data["template_id"], "ScrapMetalKnife")
        self.assertEqual(data["display_name"], "Scrap Metal Knife")
        self.assertEqual(data["base_vendor_price"], 50)
        self.assertEqual(data["categories"], ["Daggers", "Weapons"])

    def test_humanize_template_id_expands_common_template_shapes(self):
        self.assertEqual(research.humanize_template_id("PowerPack5"), "Power Pack 5")
        self.assertEqual(research.humanize_template_id("ScrapMetalKnife"), "Scrap Metal Knife")


class ArtificialExchangeBotTest(unittest.TestCase):
    def setUp(self):
        self.original_env = dict(bot.FILE_ENV)
        bot.FILE_ENV.clear()
        bot.SOURCE_CATEGORY_MAP_CACHE.clear()
        bot.VERIFIED_CATEGORY_MAP_CACHE.clear()
        bot.STATS_LIBRARY_CACHE.clear()
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_REQUIRE_SOURCE_CATEGORY"] = "false"

    def tearDown(self):
        bot.FILE_ENV.clear()
        bot.FILE_ENV.update(self.original_env)
        bot.SOURCE_CATEGORY_MAP_CACHE.clear()
        bot.VERIFIED_CATEGORY_MAP_CACHE.clear()
        bot.STATS_LIBRARY_CACHE.clear()

    def order(self, **overrides):
        row = {"id": 1, "owner_id": 10, "template_id": "ItemA", "item_price": 75, "revision": "1"}
        row.update(overrides)
        return row

    def catalog_row(self, template_id="ItemA", **overrides):
        row = {"template_id": template_id, "max_buy_price": 80, "baseline_price": 100, "price_floor": None, "price_ceiling": None, "enabled": True, "liquidity_tier": "medium", "sellable_status": "validated", "category_mask": 0, "category_depth": 0, "source": "dune.exchange+midpoint-floor", "notes": "tier=2; price_ceiling=dune.exchange averagePrice"}
        row.update(overrides)
        return row

    def test_price_and_daily_caps_are_enforced(self):
        state = {"spent_global": 0, "spent_by_seller": {}, "spent_by_template": {}}
        ok, reason = bot.spend_available(state, self.order(item_price=89), self.catalog_row(max_buy_price=80))
        self.assertFalse(ok)
        self.assertEqual(reason, "above max_buy_price tolerance")

        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_DAILY_SOLARI_CAP"] = "100"
        state["spent_global"] = 50
        ok, reason = bot.spend_available(state, self.order(item_price=75), self.catalog_row(max_buy_price=100))
        self.assertFalse(ok)
        self.assertEqual(reason, "global daily cap")

        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_DAILY_SOLARI_CAP"] = "1000"
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_DAILY_SELLER_CAP"] = "100"
        state = {"spent_global": 0, "spent_by_seller": {"10": 50}, "spent_by_template": {}}
        ok, reason = bot.spend_available(state, self.order(item_price=75), self.catalog_row(max_buy_price=100))
        self.assertFalse(ok)
        self.assertEqual(reason, "seller daily cap")

        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_DAILY_SELLER_CAP"] = "1000"
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_DAILY_TEMPLATE_CAP"] = "100"
        state = {"spent_global": 0, "spent_by_seller": {}, "spent_by_template": {"ItemA": 50}}
        ok, reason = bot.spend_available(state, self.order(item_price=75), self.catalog_row(max_buy_price=100))
        self.assertFalse(ok)
        self.assertEqual(reason, "template daily cap")

    def test_price_tolerance_allows_small_over_cap_purchases(self):
        state = {"spent_global": 0, "spent_by_seller": {}, "spent_by_template": {}}
        ok, reason = bot.spend_available(state, self.order(item_price=88), self.catalog_row(max_buy_price=80))
        self.assertTrue(ok)
        self.assertEqual(reason, "")

        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_MAX_BUY_PRICE_TOLERANCE_PCT"] = "0"
        ok, reason = bot.spend_available(state, self.order(item_price=81), self.catalog_row(max_buy_price=80))
        self.assertFalse(ok)
        self.assertEqual(reason, "above max_buy_price tolerance")

    def test_stack_purchase_total_is_used_for_caps_and_daily_buckets(self):
        state = {"spent_global": 0, "spent_by_seller": {}, "spent_by_template": {}}
        order = self.order(item_price=75, stack_size=4, initial_stack_size=10)
        self.assertEqual(bot.purchase_quantity(order), 4)
        self.assertEqual(bot.purchase_total_price(order), 300)
        bot.record_spend(state, order)
        self.assertEqual(state["spent_global"], 300)
        self.assertEqual(state["spent_by_seller"]["10"], 300)
        self.assertEqual(state["spent_by_template"]["ItemA"], 300)

        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_DAILY_SOLARI_CAP"] = "299"
        ok, reason = bot.spend_available(
            {"spent_global": 0, "spent_by_seller": {}, "spent_by_template": {}},
            order,
            self.catalog_row(max_buy_price=75),
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "global daily cap")

    def test_blocked_sellers_and_probability_env(self):
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_BLOCKED_SELLERS"] = "10, 20"
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_MEDIUM_BUY_PROBABILITY"] = "0.42"
        self.assertEqual(bot.blocked_sellers(), {"10", "20"})
        self.assertEqual(bot.buy_probability("medium"), 0.42)

    def test_purchase_notification_uses_private_whisper_route(self):
        captured = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            captured["env"] = kwargs["env"]

            class Result:
                returncode = 0
                stdout = "{}"
                stderr = ""

            return Result()

        order = self.order(
            id=77,
            item_price=1234,
            initial_stack_size=2,
            seller_character_name="Seller",
            seller_fls_id="TEST_FLS_ID",
            seller_online_status="Online",
        )
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_PURCHASE_NOTIFY_COMMAND"] = "/bin/echo"

        with mock.patch.object(bot.subprocess, "run", fake_run):
            result = bot.notify_purchase_seller(order)

        self.assertTrue(result["ok"])
        self.assertEqual(captured["command"][0], "/bin/echo")
        self.assertIn("2x ItemA", captured["command"][1])
        self.assertIn("2468 Solari", captured["command"][1])
        self.assertIn("bank after your next relog", captured["command"][1])
        self.assertEqual(captured["env"]["DUNE_ANNOUNCE_CHAT_EXCHANGE"], "chat.whispers")
        self.assertEqual(captured["env"]["DUNE_ANNOUNCE_CHAT_CHANNEL"], "Whispers")
        self.assertEqual(captured["env"]["DUNE_ANNOUNCE_CHAT_USER_NAME_TO"], "Seller")
        self.assertEqual(captured["env"]["DUNE_ANNOUNCE_CHAT_TARGET_FLS_IDS"], "TEST_FLS_ID")
        self.assertEqual(captured["env"]["DUNE_ANNOUNCE_CHAT_TARGET_QUEUES"], "TEST_FLS_ID_queue")
        self.assertEqual(captured["env"]["DUNE_ANNOUNCE_CHAT_ROUTING_KEYS"], "TEST_FLS_ID")
        self.assertEqual(captured["env"]["DUNE_ANNOUNCE_CHAT_CLEANUP_TARGET_BINDINGS"], "true")
        self.assertEqual(captured["env"]["DUNE_ANNOUNCE_WRAP_DASHBOARD_MESSAGES"], "false")

    def test_purchase_notification_can_be_disabled(self):
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_PURCHASE_NOTIFY_ENABLED"] = "false"
        result = bot.notify_purchase_seller(self.order(seller_character_name="Seller", seller_fls_id="TEST_FLS_ID"))
        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])

    def test_offline_purchase_notification_is_skipped_without_pending_retry(self):
        result = bot.notify_purchase_seller(self.order(seller_character_name="Seller", seller_fls_id="TEST_FLS_ID", seller_online_status="Offline"))
        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "seller offline")

    def test_settlement_status_blocks_missing_currency_balance(self):
        purchased = {"completion_type": 0, "item_id": 100, "currency_balance": None}
        self.assertEqual(bot.settlement_status(purchased), "purchased_item_storage")
        self.assertFalse(bot.settlement_claim_safe(purchased))

        unsafe = {"completion_type": 1, "item_id": None, "currency_balance": None}
        self.assertEqual(bot.settlement_status(unsafe), "unsafe_missing_base_solaris_balance")
        self.assertFalse(bot.settlement_claim_safe(unsafe))

        safe = {"completion_type": 1, "item_id": None, "currency_balance": 500}
        self.assertEqual(bot.settlement_status(safe), "seller_solari_claim_ready")
        self.assertTrue(bot.settlement_claim_safe(safe))

    def test_settlement_status_flags_unknown_completion_type(self):
        row = {"completion_type": 99, "item_id": None, "currency_balance": 500}
        self.assertEqual(bot.settlement_status(row), "unknown_completion_type")
        self.assertFalse(bot.settlement_claim_safe(row))

    def test_settlement_claim_key_includes_idempotency_fields(self):
        row = {"order_id": 7, "source_order_id": None, "original_order_id": 5, "completion_type": 1}
        self.assertEqual(bot.settlement_claim_key(row), "7::5:1")

    def test_settlement_report_uses_solaris_enum_label(self):
        class FakeCursor:
            def __init__(self):
                self.calls = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql, params=None):
                self.calls.append((sql, params))

            def fetchone(self):
                return {"rel": "dune.dune_exchange_fulfilled_orders"}

            def fetchall(self):
                return []

        class FakeConn:
            def __init__(self):
                self.fake_cursor = FakeCursor()

            def cursor(self):
                return self.fake_cursor

        conn = FakeConn()
        self.assertEqual(bot.settlement_report(conn, 20), [])
        sql = "\n".join(statement for statement, _ in conn.fake_cursor.calls)
        self.assertIn("'Solaris'::dune.virtualwallettype", sql)
        self.assertNotIn("dune.get_solaris_id()", sql)

    def test_seller_solari_credit_updates_bank_and_exchange_mirror(self):
        class FakeCursor:
            def __init__(self):
                self.calls = []
                self.responses = [
                    {"balance": 1000},
                    {"balance": 36001000},
                    {"solari_balance": 36001000},
                    {"balance": 36001000},
                ]

            def execute(self, sql, params=None):
                self.calls.append((sql, params))

            def fetchone(self):
                return self.responses.pop(0)

        cur = FakeCursor()
        result = bot.credit_solaris_bank(cur, 10, 36_000_000, owner_id=10)

        self.assertEqual(result, {"before_balance": 1000, "after_balance": 36001000, "credited": 36_000_000})
        sql = "\n".join(statement for statement, _ in cur.calls)
        self.assertIn("player_virtual_currency_balances", sql)
        self.assertIn("dune_exchange_users", sql)
        self.assertIn("'Solaris'::dune.virtualwallettype", sql)
        self.assertNotIn("dune.get_solaris_id()", sql)
        self.assertNotIn("dune.items", sql)
        self.assertNotIn("save_item", sql)

    def test_seller_claim_keeps_price_and_credits_bank_not_inventory(self):
        class FakeCursor:
            def __init__(self):
                self.calls = []
                self.responses = [
                    {
                        "owner_id": 10,
                        "item_id": None,
                        "item_price": 18_000_000,
                        "completion_type": 1,
                        "stack_size": 2,
                        "original_order_id": 5,
                    },
                    {"still_exists": False},
                ]

            def execute(self, sql, params=None):
                self.calls.append((sql, params))

            def fetchone(self):
                return self.responses.pop(0)

        cur = FakeCursor()
        row = {
            "order_id": 7,
            "owner_id": 10,
            "expected_solari": 36_000_000,
            "original_order_id": 5,
        }
        bank_credit = {"before_balance": 1000, "after_balance": 36001000, "credited": 36_000_000}
        with mock.patch.object(bot, "credit_solaris_bank", return_value=bank_credit) as credit:
            result = bot.execute_direct_seller_claim(cur, row)

        credit.assert_called_once_with(cur, 10, 36_000_000, owner_id=10)
        self.assertEqual(result["total_item_value"], 36_000_000)
        self.assertEqual(result["credited"], 36_000_000)
        self.assertEqual(result["method"], "validated_bank_solaris_balance")
        self.assertEqual(result["before_bank_balance"], 1000)
        self.assertEqual(result["after_bank_balance"], 36001000)
        sql = "\n".join(statement for statement, _ in cur.calls)
        self.assertNotIn("dune.items", sql)
        self.assertNotIn("save_item", sql)

    def test_env_bool_defaults_and_overrides(self):
        self.assertFalse(bot.env_bool("DUNE_ARTIFICIAL_EXCHANGE_ENABLED", False))
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_ENABLED"] = "true"
        self.assertTrue(bot.env_bool("DUNE_ARTIFICIAL_EXCHANGE_ENABLED", False))

    def test_live_buyer_loop_gets_service_confirmation(self):
        original = bot.FILE_ENV.get("DUNE_ARTIFICIAL_EXCHANGE_SERVICE_CONFIRM")
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_SERVICE_CONFIRM"] = "RUN ARTIFICIAL EXCHANGE"
        args = types.SimpleNamespace(loop=True, dry_run=False, confirm="")
        bot.apply_service_confirmation(args)
        self.assertEqual("RUN ARTIFICIAL EXCHANGE", args.confirm)
        if original is None:
            bot.FILE_ENV.pop("DUNE_ARTIFICIAL_EXCHANGE_SERVICE_CONFIRM", None)
        else:
            bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_SERVICE_CONFIRM"] = original

    def test_buyer_skips_npc_and_populator_owner_by_default(self):
        args = types.SimpleNamespace(include_npc_test_orders=False)
        self.assertEqual(bot.buyer_skip_reason(self.order(is_npc_order=True), args), "npc order skipped")

        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_OWNER_ID"] = "10"
        self.assertEqual(bot.buyer_skip_reason(self.order(is_npc_order=False), args), "populator owner skipped")

        args.include_npc_test_orders = True
        self.assertEqual(bot.buyer_skip_reason(self.order(is_npc_order=True), args), "")
        self.assertEqual(bot.buyer_skip_reason(self.order(is_npc_order=False), args), "")

    def test_buyer_identity_must_not_be_populator_owner(self):
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_OWNER_ID"] = "124"
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_OWNER_IDS"] = "124"
        self.assertTrue(bot.buyer_identity_conflict(types.SimpleNamespace(buyer_controller_id=124)))
        self.assertFalse(bot.buyer_identity_conflict(types.SimpleNamespace(buyer_controller_id=7)))

    def test_scan_dry_run_keeps_auto_claim_dry_run(self):
        class FakeConn:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def close(self):
                pass

        args = types.SimpleNamespace(
            dry_run=True,
            exchange_id=2,
            limit=200,
            settlement_limit=50,
            auto_claim_after_scan=True,
            catalog="catalog.json",
            report_skips=50,
            ignore_enabled_gate=False,
        )
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_ENABLED"] = "true"
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_AUTO_CLAIM_ENABLED"] = "true"
        with mock.patch.object(bot, "load_catalog", return_value={}), \
            mock.patch.object(bot, "load_state", return_value={"claimed_settlements": []}), \
            mock.patch.object(bot, "connect_db", return_value=FakeConn()), \
            mock.patch.object(bot, "inspect_settlement", return_value=[]), \
            mock.patch.object(bot, "fetch_orders", return_value=[]), \
            mock.patch.object(bot, "save_json"), \
            mock.patch.object(bot, "claim_all_settlements", return_value={"ok": True, "dryRun": True, "claimed": [], "skipped": []}) as claim_all:
            result = bot.scan_once(args)

        self.assertTrue(result["autoClaim"]["dryRun"])
        claim_all.assert_called_once()
        self.assertTrue(claim_all.call_args.kwargs["dry_run"])

    def test_scan_apply_sends_seller_purchase_notification(self):
        class FakeConn:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def close(self):
                pass

        args = types.SimpleNamespace(
            dry_run=False,
            exchange_id=2,
            buyer_controller_id=124,
            limit=200,
            settlement_limit=50,
            auto_claim_after_scan=False,
            catalog="catalog.json",
            report_skips=50,
            ignore_enabled_gate=False,
            confirm=bot.CONFIRM,
            include_npc_test_orders=False,
        )
        order = self.order(seller_character_name="Seller", seller_fls_id="TEST_FLS_ID", seller_online_status="Online")
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_ENABLED"] = "true"
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_PURCHASES_ENABLED"] = "true"
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_MEDIUM_BUY_PROBABILITY"] = "1"
        with mock.patch.object(bot, "load_catalog", return_value={"ItemA": self.catalog_row(max_buy_price=100)}), \
            mock.patch.object(bot, "load_state", return_value={"spent_global": 0, "spent_by_seller": {}, "spent_by_template": {}, "claimed_settlements": []}), \
            mock.patch.object(bot, "connect_db", return_value=FakeConn()), \
            mock.patch.object(bot, "inspect_settlement", return_value=[]), \
            mock.patch.object(bot, "fetch_orders", return_value=[order]), \
            mock.patch.object(bot, "revision_matches", return_value=True), \
            mock.patch.object(bot, "execute_purchase", return_value={"ok": True}), \
            mock.patch.object(bot, "notify_purchase_seller", return_value={"ok": True, "message": "sent"}) as notify, \
            mock.patch.object(bot, "save_json"), \
            mock.patch.object(bot.random, "random", return_value=0):
            result = bot.scan_once(args)

        self.assertEqual(len(result["selected"]), 1)
        self.assertEqual(result["selected"][0]["sellerNotification"], {"ok": True, "message": "sent"})
        notify.assert_called_once()

    def test_scan_apply_rejects_unfinalized_native_purchase(self):
        class FakeConn:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def close(self):
                pass

        args = types.SimpleNamespace(
            dry_run=False,
            exchange_id=2,
            buyer_controller_id=124,
            limit=200,
            settlement_limit=50,
            auto_claim_after_scan=False,
            catalog="catalog.json",
            report_skips=50,
            ignore_enabled_gate=False,
            confirm=bot.CONFIRM,
            include_npc_test_orders=False,
        )
        order = self.order(seller_character_name="Seller", seller_fls_id="TEST_FLS_ID", seller_online_status="Online")
        state = {"spent_global": 0, "spent_by_seller": {}, "spent_by_template": {}, "claimed_settlements": []}
        rejected = {
            "ok": False,
            "item_id": 0,
            "order_slots_used": 4113,
            "postcondition": {"activeOrderExists": True, "fulfilledRows": 0},
            "reason": "native fulfill did not finalize order",
        }
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_ENABLED"] = "true"
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_PURCHASES_ENABLED"] = "true"
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_MEDIUM_BUY_PROBABILITY"] = "1"
        with mock.patch.object(bot, "load_catalog", return_value={"ItemA": self.catalog_row(max_buy_price=100)}), \
            mock.patch.object(bot, "load_state", return_value=state), \
            mock.patch.object(bot, "connect_db", return_value=FakeConn()), \
            mock.patch.object(bot, "inspect_settlement", return_value=[]), \
            mock.patch.object(bot, "fetch_orders", return_value=[order]), \
            mock.patch.object(bot, "revision_matches", return_value=True), \
            mock.patch.object(bot, "execute_purchase", return_value=rejected), \
            mock.patch.object(bot, "notify_purchase_seller") as notify, \
            mock.patch.object(bot, "save_json"), \
            mock.patch.object(bot.random, "random", return_value=0):
            result = bot.scan_once(args)

        self.assertEqual(result["selected"], [])
        self.assertEqual(result["skipped"][0]["reason"], "native fulfill did not finalize order")
        self.assertEqual(state["spent_global"], 0)
        notify.assert_not_called()

    def test_purchase_postcondition_requires_full_listing_finalization(self):
        self.assertFalse(bot.purchase_postcondition_ok({
            "activeOrderExists": True,
            "activeItemId": 485,
            "fulfilledRows": 2,
        }))
        self.assertTrue(bot.purchase_postcondition_ok({
            "activeOrderExists": False,
            "activeItemId": None,
            "fulfilledRows": 2,
        }))

    def test_populator_catalog_filter_requires_enabled_baseline_and_validated(self):
        catalog_rows = {
            "enabled": self.catalog_row("enabled"),
            "disabled": self.catalog_row("disabled", enabled=False),
            "missing-price": self.catalog_row("missing-price", baseline_price=""),
            "unvalidated": self.catalog_row("unvalidated", sellable_status="observed"),
            "tier-one": self.catalog_row("tier-one", notes="tier=1"),
            "unknown-tier": self.catalog_row("unknown-tier", notes=""),
            "missing-market": self.catalog_row("missing-market", source="manual", notes="tier=2"),
        }
        self.assertEqual([row["template_id"] for row in bot.populator_catalog_rows(catalog_rows)], ["enabled"])

        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_REQUIRE_VALIDATED"] = "false"
        self.assertEqual(
            [row["template_id"] for row in bot.populator_catalog_rows(catalog_rows)],
            ["enabled", "unvalidated"],
        )

        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_REQUIRE_MARKET_PRICE"] = "false"
        self.assertEqual(
            [row["template_id"] for row in bot.populator_catalog_rows(catalog_rows)],
            ["enabled", "unvalidated"],
        )

        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_ALLOW_UNPRICED_SEEDING"] = "true"
        self.assertEqual(
            [row["template_id"] for row in bot.populator_catalog_rows(catalog_rows)],
            ["enabled", "unvalidated", "missing-market"],
        )

    def test_populator_price_and_expiry_jitter_bounds(self):
        with mock.patch.object(bot.random, "randint", side_effect=lambda low, high: low):
            self.assertEqual(bot.jitter_price(100, 20), 80)
            self.assertEqual(bot.jitter_expiration(1000, 60, 120), 1060)
        with mock.patch.object(bot.random, "randint", side_effect=lambda low, high: high):
            self.assertEqual(bot.jitter_price(100, 20), 120)
            self.assertEqual(bot.jitter_expiration(1000, 60, 120), 1120)
        self.assertEqual(bot.jitter_price_bounds(100, 20), (80, 120))

    def test_planned_unique_price_avoids_existing_template_prices(self):
        used = {"ItemA": {120, 121}}
        with mock.patch.object(bot.random, "choice", side_effect=lambda values: values[0]):
            self.assertEqual(bot.planned_unique_price(self.catalog_row(baseline_price=100), 20, used), 122)
        self.assertIn(122, used["ItemA"])
        with self.assertRaises(RuntimeError):
            bot.planned_unique_price(self.catalog_row(baseline_price=1), 0, {"ItemA": {1}})

    def test_planned_unique_price_uses_scaled_baseline_anchor(self):
        row = self.catalog_row(baseline_price=100, price_floor=100, price_ceiling=1100)
        with mock.patch.object(bot.random, "choice", side_effect=lambda values: values[0]):
            self.assertEqual(bot.planned_unique_price(row, 20, {}), 120)

    def test_planned_unique_price_keeps_stackables_at_unit_price(self):
        row = self.catalog_row(baseline_price=100, category="resources/raw")
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_FULL_STACK_SIZE"] = "100"
        with mock.patch.object(bot.random, "choice", side_effect=lambda values: values[0]):
            self.assertEqual(bot.planned_unique_price(row, 20, {}), 160)

    def test_planned_unique_price_dampens_game_file_outliers(self):
        row = self.catalog_row(
            baseline_price=53546,
            price_floor=53546,
            price_ceiling=106942,
            category="resources/components",
            notes="tier=5; game_file_price=150; price_ceiling=dune.exchange averagePrice",
        )
        self.assertEqual(round(bot.populator_price_anchor(row)), 2834)
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_STACKABLE_CATEGORIES"] = ""
        with mock.patch.object(bot.random, "choice", side_effect=lambda values: values[0]):
            self.assertEqual(bot.planned_unique_price(row, 20, {}), 3401)

    def test_planned_unique_price_can_expand_tight_fixed_ranges(self):
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MIN_PRICE_SPAN"] = "20"
        row = self.catalog_row(price_floor=100, price_ceiling=100)
        used = {"ItemA": set(range(120, 139))}
        with mock.patch.object(bot.random, "choice", side_effect=lambda values: values[0]):
            self.assertEqual(bot.planned_unique_price(row, 20, used), 139)

    def test_populator_category_gate_blocks_non_augments_from_augments_mask(self):
        row = self.catalog_row("Rifle_Schematic", category="schematics/weapons", category_mask=117506048, category_depth=2, notes="tier=4")
        self.assertEqual(bot.populator_category_skip_reason(row), "non-augment in augments category")
        augment = self.catalog_row("D_T4_WeaponMod_ExplosiveGel_Schematic", category="schematics/weapons", category_mask=117506048, category_depth=2, notes="tier=4")
        self.assertEqual(bot.populator_category_skip_reason(augment), "")
        unknown = self.catalog_row("MysteryItem", category="unknown", category_mask=0, category_depth=0, notes="tier=4")
        self.assertEqual(bot.populator_category_skip_reason(unknown), "unknown category")

    def test_staging_stats_gives_blueprints_learnable_payload(self):
        schematic = self.catalog_row("D_T4_Vehicle_BuggyChassis2_Schematic", category="schematics/vehicles", category_mask=117440512, category_depth=2, notes="tier=4")
        patent = self.catalog_row("LargeWaterCistern_Patent", category="building/patents", category_mask=50790400, category_depth=2, notes="tier=4")
        commodity = self.catalog_row("SpiceResidue", category="resources/raw", category_mask=83886080, category_depth=2, notes="tier=2")
        self.assertEqual(bot.staging_stats_for_row(schematic), bot.SCHEMATIC_DURABILITY_STATS)
        self.assertEqual(bot.staging_stats_for_row(patent), bot.SCHEMATIC_DURABILITY_STATS)
        # stateless commodities stay empty; only blueprint rows get the token payload
        self.assertEqual(bot.staging_stats_for_row(commodity), {})

    def test_populator_skip_blueprint_categories_gate(self):
        weapon = self.catalog_row("AtreLMG3", category="weapons/ranged", category_mask=16844544, category_depth=3, notes="tier=5")
        schematic = self.catalog_row("D_T4_Vehicle_BuggyChassis2_Schematic", category="schematics/vehicles", category_mask=117440512, category_depth=2, notes="tier=4")
        patent = self.catalog_row("LargeWaterCistern_Patent", category="building/patents", category_mask=50790400, category_depth=2, notes="tier=4")
        # This test isolates the blueprint-category switch; stateful-stat safety
        # is covered separately below.
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_REQUIRE_STATS_FOR_STATEFUL_ITEMS"] = "false"
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_SKIP_BLUEPRINT_CATEGORIES"] = "false"
        self.assertEqual(bot.populator_category_skip_reason(schematic), "")
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_SKIP_BLUEPRINT_CATEGORIES"] = "true"
        self.assertEqual(bot.populator_category_skip_reason(schematic), "blueprint category seeding disabled")
        self.assertEqual(bot.populator_category_skip_reason(patent), "blueprint category seeding disabled")
        self.assertEqual(bot.populator_category_skip_reason(weapon), "")

    def test_populator_category_gate_requires_deterministic_category(self):
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_REQUIRE_DETERMINISTIC_CATEGORY"] = "true"
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_REQUIRE_SOURCE_CATEGORY"] = "false"
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_PROTECT_AUGMENTS_CATEGORY"] = "false"
        row = self.catalog_row("SandbikeEngine_Schematic", category="schematics/weapons", category_mask=117506048, category_depth=2, notes="tier=4")
        self.assertEqual(bot.populator_category_skip_reason(row), "category mismatch expected schematics/vehicles/sandbike")
        mask, depth = bot.CATEGORY_MASKS["schematics/vehicles/sandbike"]
        fixed = self.catalog_row("SandbikeEngine_Schematic", category="schematics/vehicles/sandbike", category_mask=mask, category_depth=depth, notes="tier=4")
        self.assertEqual(bot.populator_category_skip_reason(fixed), "")

    def test_populator_category_gate_requires_source_category(self):
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_REQUIRE_SOURCE_CATEGORY"] = "true"
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_REQUIRE_STATS_FOR_STATEFUL_ITEMS"] = "false"
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "source-category-map.json"
            path.write_text('{"items":{"ItemA":{"category":"weapons/ranged","category_mask":1,"category_depth":2}}}', encoding="utf-8")
            bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_SOURCE_CATEGORY_MAP"] = str(path)
            row = self.catalog_row("ItemA", category="weapons/ranged", category_mask=1, category_depth=2)
            bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_REQUIRE_DETERMINISTIC_CATEGORY"] = "false"
            self.assertEqual(bot.populator_category_skip_reason(row), "")
            wrong = self.catalog_row("ItemA", category="tools/utility", category_mask=1, category_depth=2)
            self.assertEqual(bot.populator_category_skip_reason(wrong), "source category mismatch expected weapons/ranged")

    def test_verified_category_map_overrides_catalog_masks(self):
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_CATEGORY_SEEDING_VERIFIED"] = "true"
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_REQUIRE_SOURCE_CATEGORY"] = "false"
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_REQUIRE_STATS_FOR_STATEFUL_ITEMS"] = "false"
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "verified-category-map.json"
            mask, depth = bot.CATEGORY_MASKS["weapons/ranged"]
            path.write_text(f'{{"items":{{"ItemA":{{"category_mask":{mask},"category_depth":{depth}}},"ItemC":{{"category_mask":258,"category_depth":2}},"ItemD":{{"category_mask":-1,"category_depth":2}}}}}}', encoding="utf-8")
            bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_VERIFIED_CATEGORY_MAP"] = str(path)
            row = self.catalog_row("ItemA", category="weapons/ranged", category_mask=1, category_depth=1)
            bot.VERIFIED_CATEGORY_MAP_CACHE.clear()
            self.assertEqual(bot.populator_category_skip_reason(row), "")
            self.assertEqual(bot.populator_category_mask(row), mask)
            self.assertEqual(bot.populator_category_depth(row), depth)
            missing = self.catalog_row("ItemB", category_mask=1, category_depth=1)
            self.assertEqual(bot.populator_category_skip_reason(missing), "missing verified category")
            game_derived = self.catalog_row("ItemC", category="weapons/ranged", category_mask=1, category_depth=1)
            self.assertEqual(bot.populator_category_skip_reason(game_derived), "")
            self.assertEqual(bot.populator_category_mask(game_derived), 258)
            invalid = self.catalog_row("ItemD", category="weapons/ranged", category_mask=1, category_depth=1)
            self.assertEqual(bot.populator_category_skip_reason(invalid), "invalid verified category")

    def test_blueprint_identity_is_restricted_to_blueprint_categories(self):
        mask, depth = bot.CATEGORY_MASKS["weapons/ranged"]
        row = self.catalog_row("D_T4_Rifle_Blueprint", category="weapons/ranged", category_mask=mask, category_depth=depth, notes="tier=4")
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_REQUIRE_DETERMINISTIC_CATEGORY"] = "false"
        self.assertEqual(bot.populator_category_skip_reason(row), "blueprint outside blueprint category")

    def test_template_category_key_distinguishes_category(self):
        row = self.catalog_row("ItemA", category="weapons/ranged", category_mask=1, category_depth=2)
        self.assertEqual(bot.template_category_key(row), ("ItemA", "weapons/ranged", 1, 2))

    def test_planned_unique_price_samples_large_floor_ceiling_range(self):
        row = self.catalog_row(baseline_price=100, price_floor=100, price_ceiling=100000000)
        with mock.patch.object(bot.random, "choice", side_effect=lambda values: values[-1]):
            self.assertEqual(bot.planned_unique_price(row, 20, {}), 180)

    def test_planned_unique_price_requires_baseline_or_bounds(self):
        row = self.catalog_row(baseline_price="", price_floor="", price_ceiling="")
        with self.assertRaisesRegex(RuntimeError, "missing baseline_price"):
            bot.planned_unique_price(row, 20, {})

    def test_populator_target_count_and_expiry_selection(self):
        with mock.patch.object(bot.random, "randint", return_value=7):
            self.assertEqual(bot.desired_seed_count(active_count=5, target_min=10, target_max=20), 7)
        self.assertEqual(bot.desired_seed_count(active_count=10, target_min=10, target_max=20), 0)

        with mock.patch.object(bot.random, "random", side_effect=[0.05, 0.5, 0.09]):
            self.assertEqual(bot.expire_probability_selected([1, 2, 3], 0.10), [1, 3])

        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_FORCE_COUNT"] = "1"
        self.assertEqual(bot.desired_seed_count(active_count=99, target_min=10, target_max=20), 1)

    def test_category_population_requires_verified_masks(self):
        args = types.SimpleNamespace()
        with self.assertRaisesRegex(RuntimeError, "category seeding is disabled"):
            bot.populate_categories_once(args)

    def test_cleanup_candidates_select_over_cap_then_probability(self):
        active = [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}]
        with mock.patch.object(bot.random, "random", side_effect=[0.5, 0.05]):
            self.assertEqual(bot.cleanup_candidate_ids(active, target_max_orders=2, expire_probability=0.10), [1, 2, 4])

    def test_free_position_candidates_skip_occupied_slots(self):
        self.assertEqual(bot.free_position_candidates([0, 2, 5], needed_count=4, start=0, max_position=8), [1, 3, 4, 6])
        with self.assertRaises(RuntimeError):
            bot.free_position_candidates([0, 1, 2], needed_count=1, start=0, max_position=2)

    def test_seeded_order_ids_are_normalized(self):
        self.assertEqual(bot.seeded_order_ids([{"id": "7"}, {"id": 8}]), {7, 8})

    def test_populator_quality_defaults_to_tier_one_minimum(self):
        self.assertEqual(bot.populator_quality_level(self.catalog_row(notes="tier=4")), 4)
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_QUALITY_LEVEL"] = "1"
        self.assertEqual(bot.populator_quality_level(self.catalog_row(notes="")), 1)

        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MIN_QUALITY_LEVEL"] = "2"
        with self.assertRaises(RuntimeError):
            bot.populator_quality_level(self.catalog_row(notes=""))

    def test_populator_category_uses_row_before_env(self):
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_CATEGORY_MASK"] = "99"
        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_CATEGORY_DEPTH"] = "1"
        row = self.catalog_row(category_mask=258, category_depth=2)
        self.assertEqual(bot.populator_category_mask(row), 258)
        self.assertEqual(bot.populator_category_depth(row), 2)

    def test_populator_skips_stateful_items_without_stats(self):
        mask, depth = bot.CATEGORY_MASKS["weapons/ranged"]
        weapon = self.catalog_row(category="weapons/ranged", category_mask=mask, category_depth=depth)
        resource = self.catalog_row(category="resources/components")
        schematic = self.catalog_row(category="schematics/weapons")

        self.assertTrue(bot.populator_requires_stats(weapon))
        self.assertEqual(bot.populator_category_skip_reason(weapon), "stateful item stats unavailable")
        self.assertFalse(bot.populator_requires_stats(resource))
        self.assertFalse(bot.populator_requires_stats(schematic))

        bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_REQUIRE_STATS_FOR_STATEFUL_ITEMS"] = "false"
        self.assertFalse(bot.populator_requires_stats(weapon))

    def test_stats_library_allows_stateful_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "stats-library.json"
            path.write_text(json.dumps({
                "items": {
                    "RifleA": {
                        "selected": {
                            "stats": {
                                "FWeaponItemStats": [[], {}],
                                "FItemStackAndDurabilityStats": [[], {"CurrentDurability": 100.0, "MaxDurability": 100.0}],
                            }
                        }
                    }
                }
            }), encoding="utf-8")
            bot.FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_STATS_LIBRARY"] = str(path)
            bot.STATS_LIBRARY_CACHE.clear()
            mask, depth = bot.CATEGORY_MASKS["weapons/ranged"]
            weapon = self.catalog_row("RifleA", category="weapons/ranged", category_mask=mask, category_depth=depth)
            self.assertEqual(bot.populator_category_skip_reason(weapon), "")
            self.assertIn("FWeaponItemStats", bot.stats_payload_for_row(weapon))

    def test_normalized_seed_stats_restores_durability(self):
        stats = {"FItemStackAndDurabilityStats": [[], {"CurrentDurability": 5.0, "MaxDurability": 100.0, "DecayedMaxDurability": 80.0}]}
        normalized = bot.normalized_seed_stats(stats)
        values = normalized["FItemStackAndDurabilityStats"][1]
        self.assertEqual(values["CurrentDurability"], 100.0)
        self.assertEqual(values["DecayedMaxDurability"], 100.0)
        self.assertEqual(stats["FItemStackAndDurabilityStats"][1]["CurrentDurability"], 5.0)

    def test_generalized_inferred_stats_strips_customization(self):
        stats = {
            "FCustomizationStats": [[], {"SwatchId": "red", "VariantId": "skin"}],
            "FItemStackAndDurabilityStats": [[], {"CurrentDurability": 1.0, "MaxDurability": 10.0}],
        }
        generalized = bot.generalized_inferred_stats(stats)
        self.assertEqual(generalized["FCustomizationStats"][1], {})
        self.assertEqual(generalized["FItemStackAndDurabilityStats"][1]["CurrentDurability"], 10.0)

    def test_dune_exchange_import_sets_midpoint_floor(self):
        self.assertEqual(dune_exchange_import.midpoint_floor(100, 1100), 600)


if __name__ == "__main__":
    unittest.main()
