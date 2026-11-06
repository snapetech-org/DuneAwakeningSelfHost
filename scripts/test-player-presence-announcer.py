#!/usr/bin/env python3
import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
import unittest.mock


SCRIPT_PATH = pathlib.Path(__file__).with_name("player-presence-announcer.py")
SPEC = importlib.util.spec_from_file_location("player_presence_announcer", SCRIPT_PATH)
player_presence_announcer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(player_presence_announcer)


def load_announcer_with_env(env):
    spec = importlib.util.spec_from_file_location("player_presence_announcer_reloaded", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    with unittest.mock.patch.dict(os.environ, env, clear=False):
        spec.loader.exec_module(module)
    return module


def fake_container_discovery(command, **kwargs):
    if command[1] == "ps":
        output = "postgres-fixture\n"
    elif command[1] == "inspect":
        output = "postgres\n"
    else:
        raise AssertionError(f"unexpected container operation: {command[1]}")
    return player_presence_announcer.subprocess.CompletedProcess(command, 0, stdout=output, stderr="")


class DatabaseConfigurationTests(unittest.TestCase):
    def test_database_discovery_rejects_missing_or_ambiguous_containers(self):
        for output in ("", "one\ntwo\n"):
            result = player_presence_announcer.subprocess.CompletedProcess([], 0, stdout=output, stderr="")
            with self.subTest(output=output), \
                 unittest.mock.patch.object(player_presence_announcer.subprocess, "run", return_value=result) as discovery:
                with self.assertRaisesRegex(RuntimeError, "expected one running PostgreSQL container"):
                    player_presence_announcer.compose_cmd("exec", "-T", "postgres", "psql")
                self.assertEqual(discovery.call_count, 1)

    def test_database_commands_refuse_other_services_without_discovery(self):
        with unittest.mock.patch.object(player_presence_announcer.subprocess, "run") as discovery:
            with self.assertRaisesRegex(ValueError, "postgres service"):
                player_presence_announcer.compose_cmd("exec", "-T", "gateway", "sh")
            discovery.assert_not_called()

    def test_database_name_comes_from_environment(self):
        module = load_announcer_with_env({
            "DUNE_DB_NAME": "dune_sb_current",
            "DUNE_ADMIN_BOT_ENV_FILE": "/tmp/dune-announcer-test-missing.env",
        })

        self.assertEqual(module.DB, "dune_sb_current")

    def test_database_name_comes_from_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = pathlib.Path(tmp) / ".env"
            env_path.write_text("DUNE_DB_NAME=dune_sb_from_file\n", encoding="utf-8")
            module = load_announcer_with_env({
                "DUNE_ADMIN_BOT_ENV_FILE": str(env_path),
            })

        self.assertEqual(module.DB, "dune_sb_from_file")

    def test_online_players_queries_configured_database(self):
        module = load_announcer_with_env({
            "DUNE_DB_NAME": "dune_sb_query_target",
            "DUNE_ADMIN_BOT_ENV_FILE": "/tmp/dune-announcer-test-missing.env",
        })
        captured = {}

        def fake_run(command, timeout=30):
            captured["command"] = command

            class Result:
                returncode = 0
                stdout = ""
                stderr = ""

            return Result()

        with unittest.mock.patch.object(module, "run", fake_run), \
             unittest.mock.patch.object(module.subprocess, "run", side_effect=fake_container_discovery):
            self.assertEqual(module.online_players(), {})

        db_arg = captured["command"].index("-d") + 1
        self.assertEqual(captured["command"][db_arg], "dune_sb_query_target")


class PrivateMessageRoutingTests(unittest.TestCase):
    def test_private_message_uses_whisper_exchange_and_target_queue(self):
        captured = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            captured["env"] = kwargs["env"]

            class Result:
                returncode = 0
                stdout = "{}"
                stderr = ""

            return Result()

        player = {
            "name": "AdminUser",
            "flsId": "TEST_FLS_ID",
        }
        file_env = {
            "DUNE_PLAYER_PRESENCE_PRIVATE_MESSAGE_EXCHANGE": "chat.whispers",
            "DUNE_PLAYER_PRESENCE_PRIVATE_MESSAGE_CHANNEL": "Whispers",
            "DUNE_PLAYER_PRESENCE_PRIVATE_MESSAGE_ROUTING_KEY": "",
            "DUNE_PLAYER_PRESENCE_PRIVATE_MESSAGE_COMMAND": "/bin/echo",
            "DUNE_PLAYER_PRESENCE_ANNOUNCE_TIMEOUT_SECONDS": "10",
            "DUNE_PLAYER_PRESENCE_PRIVATE_DEDUPE_ENABLED": "false",
        }

        with unittest.mock.patch.object(player_presence_announcer, "FILE_ENV", file_env), \
             unittest.mock.patch.object(player_presence_announcer.subprocess, "run", fake_run):
            result = player_presence_announcer.private_message(player, "private test")

        self.assertTrue(result["ok"])
        self.assertEqual(captured["command"], ["/bin/echo", "private test"])
        self.assertEqual(captured["env"]["DUNE_ANNOUNCE_ENV_OVERRIDES_FILE"], "true")
        self.assertEqual(captured["env"]["DUNE_ANNOUNCE_CHAT_EXCHANGE"], "chat.whispers")
        self.assertEqual(captured["env"]["DUNE_ANNOUNCE_CHAT_CHANNEL"], "Whispers")
        self.assertEqual(captured["env"]["DUNE_ANNOUNCE_CHAT_USER_NAME_TO"], "AdminUser")
        self.assertEqual(captured["env"]["DUNE_ANNOUNCE_CHAT_TARGET_FLS_IDS"], "TEST_FLS_ID")
        self.assertEqual(captured["env"]["DUNE_ANNOUNCE_CHAT_TARGET_QUEUES"], "TEST_FLS_ID_queue")
        self.assertEqual(captured["env"]["DUNE_ANNOUNCE_CHAT_ROUTING_KEYS"], "TEST_FLS_ID")
        self.assertEqual(captured["env"]["DUNE_ANNOUNCE_CHAT_CLEANUP_TARGET_BINDINGS"], "true")
        self.assertEqual(captured["env"]["DUNE_ANNOUNCE_WRAP_DASHBOARD_MESSAGES"], "false")

    def test_private_message_dedupes_same_target_job_and_message(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append({"command": command, "env": kwargs["env"]})

            class Result:
                returncode = 0
                stdout = "{}"
                stderr = ""

            return Result()

        player = {
            "name": "AdminUser",
            "flsId": "TEST_FLS_ID",
        }
        file_env = {
            "DUNE_PLAYER_PRESENCE_PRIVATE_MESSAGE_COMMAND": "/bin/echo",
            "DUNE_PLAYER_PRESENCE_PRIVATE_DEDUPE_ENABLED": "true",
            "DUNE_PLAYER_PRESENCE_PRIVATE_DEDUPE_SECONDS": "3600",
        }

        with tempfile.TemporaryDirectory() as tmp, \
             unittest.mock.patch.object(player_presence_announcer, "FILE_ENV", file_env), \
             unittest.mock.patch.object(player_presence_announcer, "DELIVERY_FILE", pathlib.Path(tmp) / "deliveries.json"), \
             unittest.mock.patch.object(player_presence_announcer.subprocess, "run", fake_run):
            first = player_presence_announcer.private_message(player, "same message", "same-job")
            second = player_presence_announcer.private_message(player, "same message", "same-job")

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertTrue(second["deduped"])
        self.assertEqual(len(calls), 1)


class BaseRecoveryReminderTests(unittest.TestCase):
    def test_status_requires_owner_and_non_staged_totem(self):
        module = load_announcer_with_env({
            "DUNE_ADMIN_BOT_ENV_FILE": "/tmp/dune-announcer-test-missing.env",
        })
        captured = {}

        def fake_run(command, timeout=30):
            captured["command"] = command

            class Result:
                returncode = 0
                stdout = "f\tt\tf\tt\n"
                stderr = ""

            return Result()

        config = {"account_id": 5247, "backup_id": 127, "totem_id": 22323, "message": "recover"}
        with unittest.mock.patch.object(module, "run", fake_run), \
             unittest.mock.patch.object(module.subprocess, "run", side_effect=fake_container_discovery):
            status = module.base_recovery_reminder_status(config)

        self.assertTrue(status["ok"])
        self.assertTrue(status["restored"])
        self.assertFalse(status["backupExists"])
        sql = captured["command"][-1]
        self.assertIn("bb.id = 127", sql)
        self.assertIn("par.permission_actor_id = 22323", sql)
        self.assertIn("ps.account_id = 5247", sql)

    def test_reminder_repeats_on_new_session_and_stops_after_restore(self):
        state = {"onlinePlayers": {}}
        player = {
            "name": "Mara Jade Skywalker",
            "flsId": "MARA_FLS",
            "lastLoginTime": "2026-07-31 19:00:00+00",
        }
        snapshots = [
            {"5247": dict(player)},
            {"5247": dict(player)},
            {"5247": {**player, "lastLoginTime": "2026-08-01 19:00:00+00"}},
            {"5247": {**player, "lastLoginTime": "2026-08-01 19:00:00+00"}},
        ]
        statuses = [
            {"ok": True, "restored": False, "backupExists": True, "ownerActive": False, "totemBaseBackup": True, "totemExists": True},
            {"ok": True, "restored": False, "backupExists": True, "ownerActive": False, "totemBaseBackup": True, "totemExists": True},
            {"ok": True, "restored": False, "backupExists": True, "ownerActive": False, "totemBaseBackup": True, "totemExists": True},
            {"ok": True, "restored": True, "backupExists": False, "ownerActive": True, "totemBaseBackup": False, "totemExists": True},
        ]
        sent = []

        def fake_save_state(next_state):
            state.clear()
            state.update(next_state)

        def fake_private_message(target, message, job_id="player-presence-private-message"):
            sent.append({"target": target, "message": message, "jobId": job_id})
            return {"ok": True}

        file_env = {
            "DUNE_PLAYER_PRESENCE_BASE_RECOVERY_REMINDER_ENABLED": "true",
            "DUNE_PLAYER_PRESENCE_BASE_RECOVERY_REMINDER_ACCOUNT_ID": "5247",
            "DUNE_PLAYER_PRESENCE_BASE_RECOVERY_REMINDER_BACKUP_ID": "127",
            "DUNE_PLAYER_PRESENCE_BASE_RECOVERY_REMINDER_TOTEM_ID": "22323",
            "DUNE_PLAYER_PRESENCE_BASE_RECOVERY_REMINDER_MESSAGE": "Mara, contact a server admin to recover your base.",
            "DUNE_PLAYER_PRESENCE_REPO_STAR_THIRD_JOIN_ENABLED": "false",
            "DUNE_PLAYER_PRESENCE_STARTER_BASE_TOOL_ENABLED": "false",
            "DUNE_PLAYER_PRESENCE_STARTER_EMOTES_ENABLED": "false",
            "DUNE_PLAYER_PRESENCE_ADMIN_FIRST_LOGIN_DAILY_ENABLED": "false",
        }

        with unittest.mock.patch.object(player_presence_announcer, "FILE_ENV", file_env), \
             unittest.mock.patch.dict(player_presence_announcer.os.environ, {}, clear=True), \
             unittest.mock.patch.object(player_presence_announcer, "online_players", lambda: snapshots.pop(0)), \
             unittest.mock.patch.object(player_presence_announcer, "base_recovery_reminder_status", lambda config: statuses.pop(0)), \
             unittest.mock.patch.object(player_presence_announcer, "load_state", lambda: state.copy()), \
             unittest.mock.patch.object(player_presence_announcer, "save_state", fake_save_state), \
             unittest.mock.patch.object(player_presence_announcer, "private_message", fake_private_message):
            first = player_presence_announcer.check_once()
            second = player_presence_announcer.check_once()
            third = player_presence_announcer.check_once()
            fourth = player_presence_announcer.check_once()

        self.assertEqual(len(first["baseRecoveryReminderMessages"]), 1)
        self.assertEqual(second["baseRecoveryReminderMessages"], [])
        self.assertEqual(len(third["baseRecoveryReminderMessages"]), 1)
        self.assertEqual(fourth["baseRecoveryReminderMessages"], [])
        self.assertEqual(len(sent), 2)
        self.assertNotEqual(sent[0]["jobId"], sent[1]["jobId"])
        self.assertFalse(state["baseRecoveryReminders"]["5247"]["active"])
        self.assertIn("restoredAt", state["baseRecoveryReminders"]["5247"])


class VehicleRecoveryReminderTests(unittest.TestCase):
    def test_status_uses_native_vehicle_backup_json_contract(self):
        module = load_announcer_with_env({
            "DUNE_ADMIN_BOT_ENV_FILE": "/tmp/dune-announcer-test-missing.env",
        })
        captured = {}

        def fake_run(command, timeout=30):
            captured["command"] = command

            class Result:
                returncode = 0
                stdout = '[{"vehicleId":22279,"actorExists":true,"backupExists":true,"vehicleBackupState":true,"ownerActive":true,"restored":false}]\n'
                stderr = ""

            return Result()

        config = {"accountId": 5247, "vehicleIds": [22279], "message": "recover"}
        with unittest.mock.patch.object(module, "run", fake_run), \
             unittest.mock.patch.object(module.subprocess, "run", side_effect=fake_container_discovery):
            status = module.vehicle_recovery_reminder_status(config)
        self.assertTrue(status["ok"])
        self.assertFalse(status["restored"])
        self.assertIn("state='VehicleBackup'", captured["command"][-1])
        self.assertIn("array[22279]::bigint[]", captured["command"][-1])

    def test_reminder_repeats_per_session_and_stops_after_all_vehicles_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = pathlib.Path(tmp) / "vehicle-reminders.json"
            registry_path.write_text(json.dumps({
                "schemaVersion": 1,
                "reminders": {"5247": {
                    "accountId": 5247,
                    "vehicleIds": [22279, 22296],
                    "message": "Mara, contact a server admin to recover your ornithopters.",
                }},
            }), encoding="utf-8")
            state = {}
            player = {
                "name": "Mara Jade Skywalker",
                "flsId": "MARA_FLS",
                "lastLoginTime": "2026-07-31 19:00:00+00",
            }
            statuses = [
                {"ok": True, "restored": False, "vehicles": [{"vehicleId": 22279, "restored": False}]},
                {"ok": True, "restored": False, "vehicles": [{"vehicleId": 22279, "restored": False}]},
                {"ok": True, "restored": False, "vehicles": [{"vehicleId": 22279, "restored": False}]},
                {"ok": True, "restored": True, "vehicles": [{"vehicleId": 22279, "restored": True}, {"vehicleId": 22296, "restored": True}]},
            ]
            sent = []

            def fake_private_message(target, message, job_id="player-presence-private-message"):
                sent.append((target, message, job_id))
                return {"ok": True}

            file_env = {"DUNE_PLAYER_PRESENCE_VEHICLE_RECOVERY_REMINDERS_FILE": str(registry_path)}
            with unittest.mock.patch.object(player_presence_announcer, "FILE_ENV", file_env), \
                 unittest.mock.patch.object(player_presence_announcer, "vehicle_recovery_reminder_status", lambda config: statuses.pop(0)), \
                 unittest.mock.patch.object(player_presence_announcer, "private_message", fake_private_message):
                first = player_presence_announcer.vehicle_recovery_reminder({"5247": player}, state)
                second = player_presence_announcer.vehicle_recovery_reminder({"5247": player}, state)
                third = player_presence_announcer.vehicle_recovery_reminder({"5247": {**player, "lastLoginTime": "2026-08-01 19:00:00+00"}}, state)
                fourth = player_presence_announcer.vehicle_recovery_reminder({"5247": {**player, "lastLoginTime": "2026-08-01 19:00:00+00"}}, state)

            self.assertEqual(len(first), 1)
            self.assertEqual(second, [])
            self.assertEqual(len(third), 1)
            self.assertEqual(fourth, [])
            self.assertEqual(len(sent), 2)
            self.assertFalse(state["vehicleRecoveryReminders"]["5247"]["active"])
            self.assertIn("restoredAt", state["vehicleRecoveryReminders"]["5247"])


class PublicAnnouncementRoutingTests(unittest.TestCase):
    def test_public_presence_announcement_defaults_to_single_routing_key(self):
        captured = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            captured["env"] = kwargs["env"]

            class Result:
                returncode = 0
                stdout = "{}"
                stderr = ""

            return Result()

        file_env = {
            "DUNE_PLAYER_PRESENCE_ANNOUNCE_COMMAND": "/bin/echo",
            "DUNE_PLAYER_PRESENCE_ANNOUNCE_TIMEOUT_SECONDS": "10",
            "DUNE_ANNOUNCE_CHAT_ROUTING_KEYS": "HaggaBasin.0,Survival_1.dim_0,<empty>",
        }

        with unittest.mock.patch.object(player_presence_announcer, "FILE_ENV", file_env), \
             unittest.mock.patch.object(player_presence_announcer.subprocess, "run", fake_run):
            result = player_presence_announcer.announce("joined")

        self.assertTrue(result["ok"])
        self.assertEqual(captured["command"], ["/bin/echo", "joined"])
        self.assertEqual(captured["env"]["DUNE_ANNOUNCE_ENV_OVERRIDES_FILE"], "true")
        self.assertEqual(captured["env"]["DUNE_ANNOUNCE_CHAT_EXCHANGE"], "chat.map")
        self.assertEqual(captured["env"]["DUNE_ANNOUNCE_CHAT_CHANNEL"], "Map")
        self.assertEqual(captured["env"]["DUNE_ANNOUNCE_CHAT_ROUTING_KEYS"], "<empty>")
        self.assertEqual(result["routingKeys"], "<empty>")

    def test_public_presence_announcement_can_opt_into_multiple_routing_keys(self):
        captured = {}

        def fake_run(command, **kwargs):
            captured["env"] = kwargs["env"]

            class Result:
                returncode = 0
                stdout = "{}"
                stderr = ""

            return Result()

        file_env = {
            "DUNE_PLAYER_PRESENCE_ANNOUNCE_COMMAND": "/bin/echo",
            "DUNE_PLAYER_PRESENCE_ANNOUNCE_ROUTING_KEYS": "HaggaBasin.0,<empty>",
        }

        with unittest.mock.patch.object(player_presence_announcer, "FILE_ENV", file_env), \
             unittest.mock.patch.object(player_presence_announcer.subprocess, "run", fake_run):
            result = player_presence_announcer.announce("joined")

        self.assertTrue(result["ok"])
        self.assertEqual(captured["env"]["DUNE_ANNOUNCE_CHAT_ROUTING_KEYS"], "HaggaBasin.0,<empty>")
        self.assertEqual(result["routingKeys"], "HaggaBasin.0,<empty>")

    def test_join_announcement_splits_first_timer_and_returning_players(self):
        snapshots = [
            {"123": {"name": "FirstTimer", "flsId": "FIRST_FLS"}, "456": {"name": "Returner", "flsId": "RETURN_FLS"}},
        ]
        state = {
            "onlinePlayers": {},
            "seenAccounts": ["456"],
        }
        announced = []

        def fake_online_players():
            return snapshots.pop(0)

        def fake_save_state(next_state):
            state.clear()
            state.update(next_state)

        def fake_announce(message):
            announced.append(message)
            return {"ok": True}

        file_env = {
            "DUNE_PLAYER_PRESENCE_ANNOUNCE_ENABLED": "true",
            "DUNE_PLAYER_PRESENCE_JOIN_TEMPLATE": "Welcome {playername}! Current player count is now {count}.",
            "DUNE_PLAYER_PRESENCE_RETURN_JOIN_TEMPLATE": "Welcome back {playername}! Current player count is now {count}.",
            "DUNE_PLAYER_PRESENCE_STARTER_BASE_TOOL_ENABLED": "false",
            "DUNE_PLAYER_PRESENCE_STARTER_EMOTES_ENABLED": "false",
            "DUNE_PLAYER_PRESENCE_ADMIN_FIRST_LOGIN_DAILY_ENABLED": "false",
        }

        with unittest.mock.patch.object(player_presence_announcer, "FILE_ENV", file_env), \
             unittest.mock.patch.dict(player_presence_announcer.os.environ, {}, clear=True), \
             unittest.mock.patch.object(player_presence_announcer, "online_players", fake_online_players), \
             unittest.mock.patch.object(player_presence_announcer, "load_state", lambda: state.copy()), \
             unittest.mock.patch.object(player_presence_announcer, "save_state", fake_save_state), \
             unittest.mock.patch.object(player_presence_announcer, "announce", fake_announce):
            result = player_presence_announcer.check_once()

        messages = {item["accountId"]: item for item in result["announcements"]}
        self.assertEqual(messages["123"]["event"], "join-first-time")
        self.assertEqual(messages["123"]["message"], "Welcome FirstTimer! Current player count is now 2.")
        self.assertEqual(messages["456"]["event"], "join-returning")
        self.assertEqual(messages["456"]["message"], "Welcome back Returner! Current player count is now 2.")
        self.assertEqual(
            announced,
            [
                "Welcome FirstTimer! Current player count is now 2.",
                "Welcome back Returner! Current player count is now 2.",
            ],
        )
        self.assertEqual(state["seenAccounts"], ["123", "456"])


class FactionChatSeedingTests(unittest.TestCase):
    def test_faction_channel_map_supports_multiple_channels(self):
        parsed = player_presence_announcer.parse_faction_channel_map(
            "Atreides=AtreidesChannel;Atreides,Harkonnen=HarkonnenChannel"
        )

        self.assertEqual(parsed["Atreides"], ["AtreidesChannel", "Atreides"])
        self.assertEqual(parsed["Harkonnen"], ["HarkonnenChannel"])

    def test_seed_faction_chat_dry_run_only_plans_candidates(self):
        candidate = {
            "accountId": "2",
            "characterName": "Lukano",
            "onlineStatus": "Online",
            "flsId": "TEST_FLS_ID",
            "playerControllerId": "17",
            "playerPawnId": "19",
            "controllerFactionId": 1,
            "pawnFactionId": 3,
            "inferredFactionId": 1,
            "factionName": "Atreides",
            "atreidesRep": 4025,
            "harkonnenRep": 0,
        }

        with unittest.mock.patch.object(player_presence_announcer, "faction_chat_candidates", lambda online_only=True: [candidate]), \
             unittest.mock.patch.object(player_presence_announcer, "seed_player_faction", side_effect=AssertionError("should not execute")):
            result = player_presence_announcer.seed_faction_chat(online_only=True, execute=False)

        self.assertEqual(result["count"], 1)
        self.assertFalse(result["execute"])
        self.assertEqual(result["results"][0]["candidate"], candidate)
        self.assertNotIn("playerFaction", result["results"][0])

    def test_seed_faction_chat_execute_applies_faction_and_binding(self):
        candidate = {
            "accountId": "2",
            "characterName": "Lukano",
            "onlineStatus": "Online",
            "flsId": "TEST_FLS_ID",
            "playerControllerId": "17",
            "playerPawnId": "19",
            "controllerFactionId": 1,
            "pawnFactionId": 3,
            "inferredFactionId": 1,
            "factionName": "Atreides",
        }
        file_env = {
            "DUNE_PLAYER_PRESENCE_FACTION_CHAT_BIND_QUEUES": "true",
            "DUNE_PLAYER_PRESENCE_FACTION_CHAT_TUNE_COMMUNINET": "false",
        }

        with unittest.mock.patch.object(player_presence_announcer, "FILE_ENV", file_env), \
             unittest.mock.patch.dict(player_presence_announcer.os.environ, {}, clear=True), \
             unittest.mock.patch.object(player_presence_announcer, "faction_chat_candidates", lambda online_only=True: [candidate]), \
             unittest.mock.patch.object(player_presence_announcer, "seed_player_faction", lambda row: {"ok": True, "afterFactionId": row["inferredFactionId"]}), \
             unittest.mock.patch.object(player_presence_announcer, "bind_faction_chat_queue", lambda row: {"ok": True, "exchange": "chat.faction.1"}):
            result = player_presence_announcer.seed_faction_chat(online_only=True, execute=True)

        self.assertTrue(result["execute"])
        self.assertTrue(result["results"][0]["playerFaction"]["ok"])
        self.assertEqual(result["results"][0]["binding"]["exchange"], "chat.faction.1")


class StarterEmoteGrantTests(unittest.TestCase):
    def test_joined_player_gets_quiet_starter_emotes_once(self):
        player = {"name": "FirstTimer", "flsId": "FIRST_FLS"}
        state = {
            "onlinePlayers": {},
            "seenAccounts": [],
        }
        grants = []
        sent = []

        def fake_save_state(next_state):
            state.clear()
            state.update(next_state)

        def fake_grant(account_id):
            grants.append(account_id)
            return {"ok": True, "accountId": account_id, "granted": 4}

        def fake_private_message(target, message, job_id="player-presence-private-message"):
            sent.append({"target": target, "message": message, "jobId": job_id})
            return {"ok": True}

        file_env = {
            "DUNE_PLAYER_PRESENCE_STARTER_BASE_TOOL_ENABLED": "false",
            "DUNE_PLAYER_PRESENCE_STARTER_EMOTES_ENABLED": "true",
            "DUNE_PLAYER_PRESENCE_REPO_STAR_THIRD_JOIN_ENABLED": "false",
            "DUNE_PLAYER_PRESENCE_ADMIN_FIRST_LOGIN_DAILY_ENABLED": "false",
        }

        with unittest.mock.patch.object(player_presence_announcer, "FILE_ENV", file_env), \
             unittest.mock.patch.dict(player_presence_announcer.os.environ, {}, clear=True), \
             unittest.mock.patch.object(player_presence_announcer, "online_players", lambda: {"123": player}), \
             unittest.mock.patch.object(player_presence_announcer, "load_state", lambda: state.copy()), \
             unittest.mock.patch.object(player_presence_announcer, "save_state", fake_save_state), \
             unittest.mock.patch.object(player_presence_announcer, "grant_starter_emotes", fake_grant), \
             unittest.mock.patch.object(player_presence_announcer, "private_message", fake_private_message):
            result = player_presence_announcer.check_once()

        self.assertEqual(grants, ["123"])
        self.assertEqual(result["starterEmoteGrants"][0]["grant"]["granted"], 4)
        self.assertEqual(state["starterEmotesGranted"], ["123"])
        self.assertEqual(sent, [])


    def test_starter_emote_templates_are_configurable(self):
        file_env = {
            "DUNE_PLAYER_PRESENCE_STARTER_EMOTE_TEMPLATES": " Emote_A , Emote_B ,, ",
        }
        with unittest.mock.patch.object(player_presence_announcer, "FILE_ENV", file_env), \
             unittest.mock.patch.dict(player_presence_announcer.os.environ, {}, clear=True):
            self.assertEqual(player_presence_announcer.starter_emote_templates(), ["Emote_A", "Emote_B"])

    def test_quick_rejoin_session_change_triggers_join_and_private_messages(self):
        player = {"name": "Returner", "flsId": "RETURN_FLS", "lastLoginTime": "2026-05-22 19:05:00+00"}
        snapshots = [
            {**player, "lastLoginTime": "2026-05-22 19:06:00+00"},
        ]
        state = {
            "onlinePlayers": {"456": player},
            "seenAccounts": ["456"],
            "recentLeaves": {},
        }
        announced = []
        sent = []

        def fake_online_players():
            return {"456": snapshots.pop(0)}

        def fake_save_state(next_state):
            state.clear()
            state.update(next_state)

        def fake_announce(message):
            announced.append(message)
            return {"ok": True}

        def fake_private_message(target, message, job_id="player-presence-private-message"):
            sent.append({"target": target, "message": message, "jobId": job_id})
            return {"ok": True}

        file_env = {
            "DUNE_PLAYER_PRESENCE_ANNOUNCE_ENABLED": "true",
            "DUNE_PLAYER_PRESENCE_RETURN_JOIN_TEMPLATE": "Welcome back {playername}! Current player count is now {count}.",
            "DUNE_PLAYER_PRESENCE_PRIVATE_WELCOME_ENABLED": "true",
            "DUNE_PLAYER_PRESENCE_PRIVATE_WELCOME_TEMPLATE": "Private welcome {playername}",
            "DUNE_PLAYER_PRESENCE_RECONNECT_RECOVERY_ENABLED": "true",
            "DUNE_PLAYER_PRESENCE_RECONNECT_RECOVERY_TEMPLATE": "Reconnect help {playername}",
            "DUNE_PLAYER_PRESENCE_STARTER_BASE_TOOL_ENABLED": "false",
            "DUNE_PLAYER_PRESENCE_STARTER_EMOTES_ENABLED": "false",
            "DUNE_PLAYER_PRESENCE_ADMIN_FIRST_LOGIN_DAILY_ENABLED": "false",
        }

        with unittest.mock.patch.object(player_presence_announcer, "FILE_ENV", file_env), \
             unittest.mock.patch.dict(player_presence_announcer.os.environ, {}, clear=True), \
             unittest.mock.patch.object(player_presence_announcer, "online_players", fake_online_players), \
             unittest.mock.patch.object(player_presence_announcer, "load_state", lambda: state.copy()), \
             unittest.mock.patch.object(player_presence_announcer, "save_state", fake_save_state), \
             unittest.mock.patch.object(player_presence_announcer, "announce", fake_announce), \
             unittest.mock.patch.object(player_presence_announcer, "private_message", fake_private_message):
            result = player_presence_announcer.check_once()

        self.assertEqual(result["joined"], ["Returner"])
        self.assertEqual(result["sessionRejoined"], ["Returner"])
        self.assertEqual(result["announcements"][0]["event"], "join-returning")
        self.assertEqual(announced, ["Welcome back Returner! Current player count is now 1."])
        self.assertEqual(result["privateWelcomeMessages"][0]["message"], "Private welcome Returner")
        self.assertEqual(result["automatedPrivateMessages"][0]["event"], "reconnect-recovery")
        self.assertEqual(result["automatedPrivateMessages"][0]["message"], "Reconnect help Returner")
        self.assertEqual([item["jobId"] for item in sent], ["player-presence-private-welcome", "player-presence-reconnect-recovery"])

    def test_join_messages_can_be_delayed_until_player_is_stably_online(self):
        player = {"name": "Returner", "flsId": "RETURN_FLS", "lastLoginTime": "2026-05-22 19:06:00+00"}
        state = {
            "onlinePlayers": {},
            "seenAccounts": ["456"],
            "recentLeaves": {"456": 1000},
        }
        announced = []
        sent = []
        fake_time = [1010]

        def fake_save_state(next_state):
            state.clear()
            state.update(next_state)

        def fake_announce(message):
            announced.append(message)
            return {"ok": True}

        def fake_private_message(target, message, job_id="player-presence-private-message"):
            sent.append({"target": target, "message": message, "jobId": job_id})
            return {"ok": True}

        file_env = {
            "DUNE_PLAYER_PRESENCE_ANNOUNCE_ENABLED": "true",
            "DUNE_PLAYER_PRESENCE_RETURN_JOIN_TEMPLATE": "Welcome back {playername}! Current player count is now {count}.",
            "DUNE_PLAYER_PRESENCE_PRIVATE_WELCOME_ENABLED": "true",
            "DUNE_PLAYER_PRESENCE_PRIVATE_WELCOME_TEMPLATE": "Private welcome {playername}",
            "DUNE_PLAYER_PRESENCE_RECONNECT_RECOVERY_ENABLED": "true",
            "DUNE_PLAYER_PRESENCE_RECONNECT_RECOVERY_TEMPLATE": "Reconnect help {playername}",
            "DUNE_PLAYER_PRESENCE_JOIN_MESSAGE_DELAY_SECONDS": "30",
            "DUNE_PLAYER_PRESENCE_STARTER_BASE_TOOL_ENABLED": "false",
            "DUNE_PLAYER_PRESENCE_STARTER_EMOTES_ENABLED": "false",
            "DUNE_PLAYER_PRESENCE_ADMIN_FIRST_LOGIN_DAILY_ENABLED": "false",
        }

        with unittest.mock.patch.object(player_presence_announcer, "FILE_ENV", file_env), \
             unittest.mock.patch.dict(player_presence_announcer.os.environ, {}, clear=True), \
             unittest.mock.patch.object(player_presence_announcer, "online_players", lambda: {"456": player}), \
             unittest.mock.patch.object(player_presence_announcer, "load_state", lambda: state.copy()), \
             unittest.mock.patch.object(player_presence_announcer, "save_state", fake_save_state), \
             unittest.mock.patch.object(player_presence_announcer, "announce", fake_announce), \
             unittest.mock.patch.object(player_presence_announcer, "private_message", fake_private_message), \
             unittest.mock.patch.object(player_presence_announcer, "now_ts", lambda: fake_time[0]):
            first = player_presence_announcer.check_once()
            self.assertIn("456", state.get("pendingJoinMessages", {}))
            fake_time[0] = 1041
            second = player_presence_announcer.check_once()

        self.assertEqual(first["joined"], [])
        self.assertEqual(first["announcements"], [])
        self.assertEqual(first["privateWelcomeMessages"], [])
        self.assertEqual(first["automatedPrivateMessages"], [])
        self.assertEqual(second["joined"], ["Returner"])
        self.assertEqual(second["announcements"][0]["message"], "Welcome back Returner! Current player count is now 1.")
        self.assertEqual(second["privateWelcomeMessages"][0]["message"], "Private welcome Returner")
        self.assertEqual(second["automatedPrivateMessages"][0]["message"], "Reconnect help Returner")
        self.assertEqual(announced, ["Welcome back Returner! Current player count is now 1."])
        self.assertEqual([item["jobId"] for item in sent], ["player-presence-private-welcome", "player-presence-reconnect-recovery"])
        self.assertNotIn("456", state.get("pendingJoinMessages", {}))

    def test_existing_online_player_without_old_session_marker_is_not_reannounced(self):
        state = {
            "onlinePlayers": {"456": {"name": "Returner", "flsId": "RETURN_FLS"}},
            "seenAccounts": ["456"],
        }

        def fake_save_state(next_state):
            state.clear()
            state.update(next_state)

        file_env = {
            "DUNE_PLAYER_PRESENCE_ANNOUNCE_ENABLED": "true",
            "DUNE_PLAYER_PRESENCE_PRIVATE_WELCOME_ENABLED": "true",
            "DUNE_PLAYER_PRESENCE_STARTER_BASE_TOOL_ENABLED": "false",
            "DUNE_PLAYER_PRESENCE_STARTER_EMOTES_ENABLED": "false",
            "DUNE_PLAYER_PRESENCE_ADMIN_FIRST_LOGIN_DAILY_ENABLED": "false",
        }

        with unittest.mock.patch.object(player_presence_announcer, "FILE_ENV", file_env), \
             unittest.mock.patch.dict(player_presence_announcer.os.environ, {}, clear=True), \
             unittest.mock.patch.object(player_presence_announcer, "online_players", lambda: {"456": {"name": "Returner", "flsId": "RETURN_FLS", "lastLoginTime": "2026-05-22 19:06:00+00"}}), \
             unittest.mock.patch.object(player_presence_announcer, "load_state", lambda: state.copy()), \
             unittest.mock.patch.object(player_presence_announcer, "save_state", fake_save_state):
            result = player_presence_announcer.check_once()

        self.assertEqual(result["joined"], [])
        self.assertEqual(result["sessionRejoined"], [])
        self.assertEqual(result["announcements"], [])
        self.assertEqual(result["privateWelcomeMessages"], [])
        self.assertEqual(state["onlinePlayers"]["456"]["lastLoginTime"], "2026-05-22 19:06:00+00")

    def test_session_change_during_map_transfer_is_not_reannounced(self):
        previous_player = {
            "name": "Traveler",
            "flsId": "TRAVELER_FLS",
            "lastLoginTime": "2026-05-22 19:05:00+00",
            "map": "HaggaBasin",
            "partitionLabel": "Hagga Basin",
            "partitionId": "1",
        }
        current_player = {
            **previous_player,
            "lastLoginTime": "2026-05-22 19:06:00+00",
            "map": "Arrakeen",
            "partitionLabel": "Arrakeen",
            "partitionId": "2",
        }
        state = {
            "onlinePlayers": {"456": previous_player},
            "seenAccounts": ["456"],
        }
        announced = []
        sent = []

        def fake_save_state(next_state):
            state.clear()
            state.update(next_state)

        def fake_announce(message):
            announced.append(message)
            return {"ok": True}

        def fake_private_message(target, message, job_id="player-presence-private-message"):
            sent.append({"target": target, "message": message, "jobId": job_id})
            return {"ok": True}

        file_env = {
            "DUNE_PLAYER_PRESENCE_ANNOUNCE_ENABLED": "true",
            "DUNE_PLAYER_PRESENCE_PRIVATE_WELCOME_ENABLED": "true",
            "DUNE_PLAYER_PRESENCE_RECONNECT_RECOVERY_ENABLED": "true",
            "DUNE_PLAYER_PRESENCE_STARTER_BASE_TOOL_ENABLED": "false",
            "DUNE_PLAYER_PRESENCE_STARTER_EMOTES_ENABLED": "false",
            "DUNE_PLAYER_PRESENCE_ADMIN_FIRST_LOGIN_DAILY_ENABLED": "false",
        }

        with unittest.mock.patch.object(player_presence_announcer, "FILE_ENV", file_env), \
             unittest.mock.patch.dict(player_presence_announcer.os.environ, {}, clear=True), \
             unittest.mock.patch.object(player_presence_announcer, "online_players", lambda: {"456": current_player}), \
             unittest.mock.patch.object(player_presence_announcer, "load_state", lambda: state.copy()), \
             unittest.mock.patch.object(player_presence_announcer, "save_state", fake_save_state), \
             unittest.mock.patch.object(player_presence_announcer, "announce", fake_announce), \
             unittest.mock.patch.object(player_presence_announcer, "private_message", fake_private_message):
            result = player_presence_announcer.check_once()

        self.assertEqual(result["joined"], [])
        self.assertEqual(result["sessionRejoined"], [])
        self.assertEqual(result["announcements"], [])
        self.assertEqual(result["privateWelcomeMessages"], [])
        self.assertEqual(result["automatedPrivateMessages"], [])
        self.assertEqual(announced, [])
        self.assertEqual(sent, [])

    def test_brief_missing_player_with_new_map_is_treated_as_transfer(self):
        previous_player = {
            "name": "Traveler",
            "flsId": "TRAVELER_FLS",
            "map": "HaggaBasin",
            "partitionLabel": "Hagga Basin",
            "partitionId": "1",
        }
        current_player = {
            "name": "Traveler",
            "flsId": "TRAVELER_FLS",
            "map": "Arrakeen",
            "partitionLabel": "Arrakeen",
            "partitionId": "2",
        }
        snapshots = [
            {},
            {"456": current_player},
        ]
        state = {
            "onlinePlayers": {"456": previous_player},
            "seenAccounts": ["456"],
            "recentLeaves": {},
        }
        fake_time = [1000]
        announced = []
        sent = []

        def fake_online_players():
            return snapshots.pop(0)

        def fake_save_state(next_state):
            state.clear()
            state.update(next_state)

        def fake_announce(message):
            announced.append(message)
            return {"ok": True}

        def fake_private_message(target, message, job_id="player-presence-private-message"):
            sent.append({"target": target, "message": message, "jobId": job_id})
            return {"ok": True}

        file_env = {
            "DUNE_PLAYER_PRESENCE_ANNOUNCE_ENABLED": "true",
            "DUNE_PLAYER_PRESENCE_PRIVATE_WELCOME_ENABLED": "true",
            "DUNE_PLAYER_PRESENCE_RECONNECT_RECOVERY_ENABLED": "true",
            "DUNE_PLAYER_PRESENCE_TRANSFER_GRACE_SECONDS": "90",
            "DUNE_PLAYER_PRESENCE_STARTER_BASE_TOOL_ENABLED": "false",
            "DUNE_PLAYER_PRESENCE_STARTER_EMOTES_ENABLED": "false",
            "DUNE_PLAYER_PRESENCE_ADMIN_FIRST_LOGIN_DAILY_ENABLED": "false",
        }

        with unittest.mock.patch.object(player_presence_announcer, "FILE_ENV", file_env), \
             unittest.mock.patch.dict(player_presence_announcer.os.environ, {}, clear=True), \
             unittest.mock.patch.object(player_presence_announcer, "online_players", fake_online_players), \
             unittest.mock.patch.object(player_presence_announcer, "load_state", lambda: state.copy()), \
             unittest.mock.patch.object(player_presence_announcer, "save_state", fake_save_state), \
             unittest.mock.patch.object(player_presence_announcer, "announce", fake_announce), \
             unittest.mock.patch.object(player_presence_announcer, "private_message", fake_private_message), \
             unittest.mock.patch.object(player_presence_announcer, "now_ts", lambda: fake_time[0]):
            first = player_presence_announcer.check_once()
            fake_time[0] = 1030
            second = player_presence_announcer.check_once()

        self.assertEqual(first["left"], [])
        self.assertIn("456", state["onlinePlayers"])
        self.assertEqual(second["joined"], [])
        self.assertEqual(second["suppressedTransfers"], ["Traveler"])
        self.assertEqual(second["announcements"], [])
        self.assertEqual(second["privateWelcomeMessages"], [])
        self.assertEqual(second["automatedPrivateMessages"], [])
        self.assertEqual(announced, [])
        self.assertEqual(sent, [])
        self.assertEqual(state.get("pendingLeaves", {}), {})
        self.assertEqual(state.get("recentLeaves", {}), {})


class StarterBaseToolGrantTests(unittest.TestCase):
    def test_online_join_is_queued_until_player_is_offline(self):
        snapshots = [
            {"123": {"name": "FirstTimer", "flsId": "FIRST_FLS"}},
            {},
        ]
        state = {
            "onlinePlayers": {},
            "seenAccounts": [],
        }
        grants = []

        def fake_online_players():
            return snapshots.pop(0)

        def fake_save_state(next_state):
            state.clear()
            state.update(next_state)

        def fake_grant(account_id):
            grants.append(str(account_id))
            return {"ok": True, "accountId": str(account_id), "granted": True}

        file_env = {
            "DUNE_PLAYER_PRESENCE_STARTER_BASE_TOOL_ENABLED": "true",
            "DUNE_PLAYER_PRESENCE_STARTER_BASE_TOOL_MESSAGE_ENABLED": "false",
            "DUNE_PLAYER_PRESENCE_STARTER_EMOTES_ENABLED": "false",
            "DUNE_PLAYER_PRESENCE_ANNOUNCE_ENABLED": "false",
            "DUNE_PLAYER_PRESENCE_TRANSFER_GRACE_SECONDS": "0",
            "DUNE_PLAYER_PRESENCE_ADMIN_FIRST_LOGIN_DAILY_ENABLED": "false",
        }

        with unittest.mock.patch.object(player_presence_announcer, "FILE_ENV", file_env), \
             unittest.mock.patch.dict(os.environ, {}, clear=True), \
             unittest.mock.patch.object(player_presence_announcer, "online_players", fake_online_players), \
             unittest.mock.patch.object(player_presence_announcer, "load_state", lambda: state.copy()), \
             unittest.mock.patch.object(player_presence_announcer, "save_state", fake_save_state), \
             unittest.mock.patch.object(player_presence_announcer, "grant_starter_base_tool", fake_grant):
            first = player_presence_announcer.check_once()
            second = player_presence_announcer.check_once()

        self.assertEqual(grants, ["123"])
        self.assertEqual(first["starterBaseToolGrants"], [])
        self.assertEqual(second["starterBaseToolGrants"][0]["accountId"], "123")
        self.assertEqual(state["starterBaseToolGranted"], ["123"])
        self.assertEqual(state["starterBaseToolPending"], {})


class DeepDesertJoinMessageTests(unittest.TestCase):
    def test_deep_desert_casual_message_sends_on_entry_not_every_poll(self):
        state = {
            "onlinePlayers": {
                "123": {"name": "Traveler", "flsId": "TRAVELER_FLS", "map": "HaggaBasin", "partitionLabel": "Hagga Basin"}
            },
            "seenAccounts": ["123"],
        }
        current_player = {
            "name": "Traveler",
            "flsId": "TRAVELER_FLS",
            "map": "DeepDesert",
            "partitionLabel": "01 Recommended PVE Casual",
            "partitionId": "8",
        }
        sent = []

        def fake_save_state(next_state):
            state.clear()
            state.update(next_state)

        def fake_private_message(target, message, job_id="player-presence-private-message"):
            sent.append({"target": target, "message": message, "jobId": job_id})
            return {"ok": True}

        file_env = {
            "DUNE_PLAYER_PRESENCE_DEEP_DESERT_JOIN_MESSAGES_ENABLED": "true",
            "DUNE_PLAYER_PRESENCE_DEEP_DESERT_CASUAL_JOIN_TEMPLATE": "Casual DD notice",
            "DUNE_PLAYER_PRESENCE_DEEP_DESERT_FIRST_ENABLED": "false",
            "DUNE_PLAYER_PRESENCE_REPO_STAR_THIRD_JOIN_ENABLED": "false",
            "DUNE_PLAYER_PRESENCE_STARTER_BASE_TOOL_ENABLED": "false",
            "DUNE_PLAYER_PRESENCE_STARTER_EMOTES_ENABLED": "false",
            "DUNE_PLAYER_PRESENCE_ADMIN_FIRST_LOGIN_DAILY_ENABLED": "false",
        }

        with unittest.mock.patch.object(player_presence_announcer, "FILE_ENV", file_env), \
             unittest.mock.patch.dict(player_presence_announcer.os.environ, {}, clear=True), \
             unittest.mock.patch.object(player_presence_announcer, "online_players", lambda: {"123": current_player}), \
             unittest.mock.patch.object(player_presence_announcer, "load_state", lambda: state.copy()), \
             unittest.mock.patch.object(player_presence_announcer, "save_state", fake_save_state), \
             unittest.mock.patch.object(player_presence_announcer, "private_message", fake_private_message):
            first_result = player_presence_announcer.check_once()
            second_result = player_presence_announcer.check_once()

        self.assertEqual(first_result["automatedPrivateMessages"][0]["event"], "deep-desert-casual-join")
        self.assertEqual(sent, [{"target": current_player, "message": "Casual DD notice", "jobId": "player-presence-deep-desert-casual-join"}])
        self.assertEqual(second_result["automatedPrivateMessages"], [])

    def test_deep_desert_hardcore_message_sends_on_login_by_partition(self):
        player = {
            "name": "Builder",
            "flsId": "BUILDER_FLS",
            "map": "DeepDesert",
            "partitionLabel": "02 PVE Hardcore",
            "partitionId": "31",
        }
        state = {
            "onlinePlayers": {},
            "seenAccounts": ["123"],
        }
        sent = []

        def fake_save_state(next_state):
            state.clear()
            state.update(next_state)

        def fake_private_message(target, message, job_id="player-presence-private-message"):
            sent.append({"target": target, "message": message, "jobId": job_id})
            return {"ok": True}

        file_env = {
            "DUNE_PLAYER_PRESENCE_DEEP_DESERT_JOIN_MESSAGES_ENABLED": "true",
            "DUNE_PLAYER_PRESENCE_DEEP_DESERT_HARDCORE_JOIN_TEMPLATE": "Hardcore DD notice",
            "DUNE_PLAYER_PRESENCE_DEEP_DESERT_FIRST_ENABLED": "false",
            "DUNE_PLAYER_PRESENCE_REPO_STAR_THIRD_JOIN_ENABLED": "false",
            "DUNE_PLAYER_PRESENCE_STARTER_BASE_TOOL_ENABLED": "false",
            "DUNE_PLAYER_PRESENCE_STARTER_EMOTES_ENABLED": "false",
            "DUNE_PLAYER_PRESENCE_ADMIN_FIRST_LOGIN_DAILY_ENABLED": "false",
        }

        with unittest.mock.patch.object(player_presence_announcer, "FILE_ENV", file_env), \
             unittest.mock.patch.dict(player_presence_announcer.os.environ, {}, clear=True), \
             unittest.mock.patch.object(player_presence_announcer, "online_players", lambda: {"123": player}), \
             unittest.mock.patch.object(player_presence_announcer, "load_state", lambda: state.copy()), \
             unittest.mock.patch.object(player_presence_announcer, "save_state", fake_save_state), \
             unittest.mock.patch.object(player_presence_announcer, "private_message", fake_private_message):
            result = player_presence_announcer.check_once()

        self.assertEqual(result["automatedPrivateMessages"][0]["event"], "deep-desert-hardcore-join")
        self.assertEqual(sent[0]["message"], "Hardcore DD notice")
        self.assertEqual(sent[0]["jobId"], "player-presence-deep-desert-hardcore-join")


class ServiceHealthCheckTests(unittest.TestCase):
    def test_default_freshness_check_excludes_derived_hagga_map_svg(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            static = root / "public-site" / "static"
            static.mkdir(parents=True)
            (static / "players.json").write_text("{}", encoding="utf-8")

            file_env = {
                "DUNE_PLAYER_PRESENCE_SERVICE_HEALTH_UNITS": ",",
                "DUNE_PLAYER_PRESENCE_FRESHNESS_MAX_AGE_SECONDS": "300",
                "DUNE_PLAYER_PRESENCE_FLS_PUBLICATION_HEALTH_ENABLED": "false",
            }
            with unittest.mock.patch.object(player_presence_announcer, "ROOT", root), \
                 unittest.mock.patch.object(player_presence_announcer, "FILE_ENV", file_env), \
                 unittest.mock.patch.dict(player_presence_announcer.os.environ, {}, clear=True):
                checks = player_presence_announcer.service_health_checks()

        self.assertEqual([check["name"] for check in checks], ["public-site/static/players.json"])
        self.assertTrue(checks[0]["ok"])

    def test_configured_freshness_files_can_still_include_hagga_map_svg(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            static = root / "public-site" / "static"
            static.mkdir(parents=True)
            (static / "players.json").write_text("{}", encoding="utf-8")

            file_env = {
                "DUNE_PLAYER_PRESENCE_SERVICE_HEALTH_UNITS": ",",
                "DUNE_PLAYER_PRESENCE_FRESHNESS_FILES": "public-site/static/players.json,public-site/static/hagga-map.svg",
                "DUNE_PLAYER_PRESENCE_FRESHNESS_MAX_AGE_SECONDS": "300",
                "DUNE_PLAYER_PRESENCE_FLS_PUBLICATION_HEALTH_ENABLED": "false",
            }
            with unittest.mock.patch.object(player_presence_announcer, "ROOT", root), \
                 unittest.mock.patch.object(player_presence_announcer, "FILE_ENV", file_env), \
                 unittest.mock.patch.dict(player_presence_announcer.os.environ, {}, clear=True):
                checks = player_presence_announcer.service_health_checks()

        by_name = {check["name"]: check for check in checks}
        self.assertTrue(by_name["public-site/static/players.json"]["ok"])
        self.assertFalse(by_name["public-site/static/hagga-map.svg"]["ok"])
        self.assertEqual(by_name["public-site/static/hagga-map.svg"]["status"], "missing")


class AdminAnomalyDigestTests(unittest.TestCase):
    def test_default_digest_omits_zero_value_categories(self):
        self.assertEqual(
            player_presence_announcer.admin_anomaly_digest_template(1, "AdminUser", 0),
            "Admin digest: stuck/recent anomalies=1 (AdminUser).",
        )
        self.assertEqual(
            player_presence_announcer.admin_anomaly_digest_template(0, "none", 2),
            "Admin digest: over base cap=2.",
        )
        self.assertEqual(
            player_presence_announcer.admin_anomaly_digest_template(0, "none", 0),
            "",
        )

    def test_digest_signature_tracks_actionable_values(self):
        stuck = [
            {"accountId": "2", "name": "AdminUser"},
            {"accountId": "10", "name": "Other"},
        ]
        self.assertEqual(
            player_presence_announcer.admin_anomaly_digest_signature(stuck, 0),
            '{"overBaseCap": 0, "stuck": ["10", "2"]}',
        )
        self.assertEqual(
            player_presence_announcer.admin_anomaly_digest_signature([], 0),
            '{"overBaseCap": 0, "stuck": []}',
        )


class BaseCapConfigTests(unittest.TestCase):
    def test_base_cap_defaults_to_usergame_hagga_basin_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            config = root / "config"
            config.mkdir()
            (config / "UserGame.ini").write_text(
                'm_MaxLandclaimSegmentsPerMap=(((Name="HaggaBasin"), 8),((Name="Survival_1"), 4))\n',
                encoding="utf-8",
            )

            with unittest.mock.patch.object(player_presence_announcer, "ROOT", root), \
                 unittest.mock.patch.object(player_presence_announcer, "FILE_ENV", {}), \
                 unittest.mock.patch.dict(player_presence_announcer.os.environ, {}, clear=True):
                self.assertEqual(player_presence_announcer.configured_base_cap(), 8)

    def test_legacy_base_cap_env_is_only_a_fallback_when_config_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)

            with unittest.mock.patch.object(player_presence_announcer, "ROOT", root), \
                 unittest.mock.patch.object(player_presence_announcer, "FILE_ENV", {"DUNE_PLAYER_PRESENCE_BASE_CAP": "10"}), \
                 unittest.mock.patch.dict(player_presence_announcer.os.environ, {}, clear=True):
                self.assertEqual(player_presence_announcer.configured_base_cap(), 10)


class SubfiefBonusTests(unittest.TestCase):
    def test_subfief_bonus_prefers_explicit_bonus(self):
        file_env = {
            "DUNE_SUBFIEF_LIMIT": "6",
            "DUNE_SUBFIEF_BASE_LIMIT": "3",
            "DUNE_SUBFIEF_LIMIT_BONUS": "6",
        }
        with unittest.mock.patch.object(player_presence_announcer, "FILE_ENV", file_env), \
             unittest.mock.patch.dict(player_presence_announcer.os.environ, {}, clear=True):
            self.assertEqual(player_presence_announcer.configured_subfief_limit_bonus(), 6)

    def test_subfief_bonus_falls_back_to_limit_minus_base(self):
        file_env = {
            "DUNE_SUBFIEF_LIMIT": "6",
            "DUNE_SUBFIEF_BASE_LIMIT": "3",
        }
        with unittest.mock.patch.object(player_presence_announcer, "FILE_ENV", file_env), \
             unittest.mock.patch.dict(player_presence_announcer.os.environ, {}, clear=True):
            self.assertEqual(player_presence_announcer.configured_subfief_limit_bonus(), 3)

    def test_joined_player_triggers_subfief_bonus_repair(self):
        state = {
            "onlinePlayers": {},
            "seenAccounts": [],
        }
        repairs = []

        def fake_save_state(next_state):
            state.clear()
            state.update(next_state)

        def fake_repair(account_ids):
            repairs.extend(account_ids)
            return [{"account_id": "123", "applied_bonus": 6}]

        file_env = {
            "DUNE_SUBFIEF_LIMIT_BONUS": "6",
            "DUNE_PLAYER_PRESENCE_STARTER_BASE_TOOL_ENABLED": "false",
            "DUNE_PLAYER_PRESENCE_STARTER_EMOTES_ENABLED": "false",
            "DUNE_PLAYER_PRESENCE_ADMIN_FIRST_LOGIN_DAILY_ENABLED": "false",
        }

        with unittest.mock.patch.object(player_presence_announcer, "FILE_ENV", file_env), \
             unittest.mock.patch.dict(player_presence_announcer.os.environ, {}, clear=True), \
             unittest.mock.patch.object(player_presence_announcer, "online_players", lambda: {"123": {"name": "Joiner", "flsId": "JOINER_FLS"}}), \
             unittest.mock.patch.object(player_presence_announcer, "load_state", lambda: state.copy()), \
             unittest.mock.patch.object(player_presence_announcer, "save_state", fake_save_state), \
             unittest.mock.patch.object(player_presence_announcer, "ensure_subfief_limit_bonus", fake_repair):
            result = player_presence_announcer.check_once()

        self.assertEqual(repairs, ["123"])
        self.assertEqual(result["subfiefBonusRepairs"], [{"account_id": "123", "applied_bonus": 6}])


class RepoStarThirdJoinTests(unittest.TestCase):
    def test_repo_star_private_message_sends_once_on_third_join(self):
        player = {
            "name": "AdminUser",
            "flsId": "TEST_FLS_ID",
        }
        snapshots = [
            {"123": player},
            {},
            {"123": player},
            {},
            {"123": player},
            {},
            {"123": player},
        ]
        state = {"onlinePlayers": {}}
        sent = []

        def fake_online_players():
            return snapshots.pop(0)

        def fake_save_state(next_state):
            state.clear()
            state.update(next_state)

        def fake_private_message(target, message, job_id="player-presence-private-message"):
            sent.append({"target": target, "message": message, "jobId": job_id})
            return {"ok": True}

        file_env = {
            "DUNE_PLAYER_PRESENCE_REPO_STAR_THIRD_JOIN_ENABLED": "true",
            "DUNE_PLAYER_PRESENCE_REPO_STAR_JOIN_COUNT": "3",
            "DUNE_PLAYER_PRESENCE_REPO_STAR_TEMPLATE": "Please star https://github.com/snapetech/DuneAwakeningSelfHost",
            "DUNE_PLAYER_PRESENCE_STARTER_BASE_TOOL_ENABLED": "false",
            "DUNE_PLAYER_PRESENCE_STARTER_EMOTES_ENABLED": "false",
            "DUNE_PLAYER_PRESENCE_ADMIN_FIRST_LOGIN_DAILY_ENABLED": "false",
        }

        with unittest.mock.patch.object(player_presence_announcer, "FILE_ENV", file_env), \
             unittest.mock.patch.dict(player_presence_announcer.os.environ, {}, clear=True), \
             unittest.mock.patch.object(player_presence_announcer, "online_players", fake_online_players), \
             unittest.mock.patch.object(player_presence_announcer, "load_state", lambda: state.copy()), \
             unittest.mock.patch.object(player_presence_announcer, "save_state", fake_save_state), \
             unittest.mock.patch.object(player_presence_announcer, "private_message", fake_private_message):
            results = [player_presence_announcer.check_once() for _ in range(7)]

        third_join_messages = [
            item
            for result in results
            for item in result["automatedPrivateMessages"]
            if item["event"] == "repo-star-third-join"
        ]
        self.assertEqual(len(third_join_messages), 1)
        self.assertEqual(third_join_messages[0]["accountId"], "123")
        self.assertEqual(sent[0]["jobId"], "player-presence-repo-star-third-join")
        self.assertEqual(state["joinCounts"]["123"], 4)
        self.assertEqual(state["repoStarMessaged"], ["123"])


if __name__ == "__main__":
    unittest.main()
