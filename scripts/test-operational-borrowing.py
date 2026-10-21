#!/usr/bin/env python3
import os
import json
import shutil
import subprocess
import tempfile
import tarfile
import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "admin"))

import desired_state
import change_intelligence
import deployment_assurance


def create_desired_state_fixture(backup_dir):
    config_dir = backup_dir / "config"
    secret_dir = config_dir / "secrets"
    secret_dir.mkdir(parents=True, exist_ok=True)
    policy = config_dir / "desired-state.json"
    policy.write_text((ROOT / "config" / "desired-state.json").read_text(encoding="utf-8"), encoding="utf-8")
    secret = secret_dir / "desired-state-hmac.secret"
    secret.write_text("b" * 64 + "\n", encoding="utf-8")
    secret.chmod(0o600)
    database = backup_dir / "desired-state.sqlite3"
    store = desired_state.Store(database, policy, secret)
    store.initialize()
    store.seal({"schemaVersion": 1, "files": {}, "containers": {}}, "test", "backup fixture", at=1000)
    return database


def create_change_intelligence_fixture(backup_dir):
    config_dir = backup_dir / "config"
    secret_dir = config_dir / "secrets"
    secret_dir.mkdir(parents=True, exist_ok=True)
    policy = config_dir / "change-intelligence.json"
    policy.write_text((ROOT / "config" / "change-intelligence.json").read_text(encoding="utf-8"), encoding="utf-8")
    secret = secret_dir / "change-intelligence-hmac.secret"
    secret.write_text("e" * 64 + "\n", encoding="utf-8")
    secret.chmod(0o600)
    database = backup_dir / "change-intelligence.sqlite3"
    store = change_intelligence.Store(database, policy, secret)
    store.initialize()
    store.record({"action": "settings-write", "ts": 1000, "ok": True, "method": "POST", "eventId": "fixture"}, ingested_at=1001)
    store.record({"action": "slo-incident-opened", "ts": 1100, "ok": False, "incident_id": "fixture", "objective_id": "database_availability", "eventId": "fixture-open"}, ingested_at=1101)
    store.record({"action": "slo-incident-resolved", "ts": 1200, "ok": True, "incident_id": "fixture", "objective_id": "database_availability", "eventId": "fixture-resolved"}, ingested_at=1201)
    capsule = backup_dir / "fixture.signed.json"
    capsule.write_text(json.dumps(store.signed_capsule("slo:fixture", at=1300)), encoding="utf-8")
    with tarfile.open(backup_dir / "operator-evidence.tgz", "w:gz") as archive:
        archive.add(capsule, arcname="operator-evidence/fixture.signed.json")
    capsule.unlink()
    return database


def write_self_signed_cert(tmp_path, sans):
    key = tmp_path / "server.key"
    csr = tmp_path / "server.csr"
    cert = tmp_path / "server.crt"
    ext = tmp_path / "server.ext"
    ext.write_text(
        f"subjectAltName = {sans}\n"
        "extendedKeyUsage = serverAuth\n",
        encoding="utf-8",
    )
    subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(
        ["openssl", "req", "-new", "-key", str(key), "-subj", "/CN=game-rmq", "-out", str(csr)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        [
            "openssl",
            "x509",
            "-req",
            "-in",
            str(csr),
            "-signkey",
            str(key),
            "-days",
            "1",
            "-out",
            str(cert),
            "-extfile",
            str(ext),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return cert


class RabbitMqCertSanTests(unittest.TestCase):
    def test_cert_san_checker_reports_expected_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env_file = tmp_path / "dune.env"
            cert = write_self_signed_cert(tmp_path, "DNS:game-rmq,DNS:localhost,DNS:rmq.example.test,IP:127.0.0.1")
            env_file.write_text("GAME_RMQ_PUBLIC_HOST=rmq.example.test\n", encoding="utf-8")

            result = subprocess.run(
                [str(ROOT / "scripts" / "check-rabbitmq-cert-sans.sh"), str(env_file)],
                cwd=ROOT,
                env={**os.environ, "RABBITMQ_CERT_PATH": str(cert)},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("DNS:rmq.example.test", result.stdout)

    def test_cert_san_checker_warns_on_missing_public_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env_file = tmp_path / "dune.env"
            cert = write_self_signed_cert(tmp_path, "DNS:game-rmq,DNS:localhost,IP:127.0.0.1")
            env_file.write_text("GAME_RMQ_PUBLIC_HOST=rmq.example.test\n", encoding="utf-8")

            result = subprocess.run(
                [str(ROOT / "scripts" / "check-rabbitmq-cert-sans.sh"), str(env_file)],
                cwd=ROOT,
                env={**os.environ, "RABBITMQ_CERT_PATH": str(cert)},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("SAN missing DNS:rmq.example.test", result.stderr)

    def test_cert_generator_includes_public_host_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tls_dir = tmp_path / "tls"
            env_file = tmp_path / "dune.env"
            env_file.write_text("GAME_RMQ_PUBLIC_HOST=rmq.example.test\n", encoding="utf-8")

            result = subprocess.run(
                [str(ROOT / "scripts" / "generate-rabbitmq-cert.sh"), str(env_file)],
                cwd=ROOT,
                env={**os.environ, "RABBITMQ_TLS_DIR": str(tls_dir)},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("DNS:rmq.example.test", result.stdout)

            check = subprocess.run(
                [str(ROOT / "scripts" / "check-rabbitmq-cert-sans.sh"), str(env_file)],
                cwd=ROOT,
                env={**os.environ, "RABBITMQ_CERT_PATH": str(tls_dir / "server.crt")},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

            refused = subprocess.run(
                [str(ROOT / "scripts" / "generate-rabbitmq-cert.sh"), str(env_file)],
                cwd=ROOT,
                env={**os.environ, "RABBITMQ_TLS_DIR": str(tls_dir)},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(refused.returncode, 1)
            self.assertIn("refusing to overwrite", refused.stderr)


class RunServerSafeTests(unittest.TestCase):
    def test_workspace_probe_env_overrides_inherited_container_env(self):
        source = (ROOT / "scripts" / "run_server_safe.sh").read_text(encoding="utf-8")
        self.assertIn("workspace_env_overrides_runtime()", source)
        self.assertIn("DUNE_PROBE_LOADER_*)", source)
        self.assertIn("DUNE_ENABLE_LINUX_SERVER_PRELOAD|DUNE_LINUX_SERVER_PRELOAD|DUNE_LINUX_SERVER_PRELOAD_PARTITIONS", source)
        self.assertIn('if [ -n "${!name:-}" ] && ! workspace_env_overrides_runtime "$name"; then', source)
        self.assertIn('value="$(workspace_env_value "$name")"', source)

    def test_dry_run_preserves_args_and_writes_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            server_root = tmp_path / "server"
            dune_home = tmp_path / "home"
            args_out = tmp_path / "args.nul"
            server_root.mkdir()

            env = {
                **os.environ,
                "DUNE_SERVER_ROOT": str(server_root),
                "DUNE_HOME": str(dune_home),
                "DUNE_RUN_SERVER_SAFE_DRY_RUN": "true",
                "DUNE_RUN_SERVER_SAFE_ARGS_OUT": str(args_out),
                "POD_IP": "172.31.240.40",
                "DUNE_SERVER_LOGIN_PASSWORD": "pass with spaces",
                "DUNE_SERVER_DISPLAY_NAME": "Display With Spaces",
                "DUNE_SERVER_STARTUP_EXECCMDS": "ScheduleMTXEvent SpecializationXPBonus 60 604800,ListMTXEvents",
            }
            subprocess.run(
                [
                    str(ROOT / "scripts" / "run_server_safe.sh"),
                    "-MultiHome=$POD_IP",
                    "-ini:engine:[ConsoleVariables]:Bgd.ServerLoginPassword=old",
                    "/Game/Dune/Maps/Test Map",
                ],
                cwd=ROOT,
                env=env,
                check=True,
            )

            raw_args = args_out.read_bytes().rstrip(b"\0").split(b"\0")
            decoded = [item.decode("utf-8") for item in raw_args]
            self.assertIn("-MultiHome=172.31.240.40", decoded)
            self.assertIn("-ini:engine:[ConsoleVariables]:Bgd.ServerLoginPassword=pass with spaces", decoded)
            self.assertIn("/Game/Dune/Maps/Test Map", decoded)
            self.assertIn("-ExecCmds=ScheduleMTXEvent SpecializationXPBonus 60 604800,ListMTXEvents", decoded)
            self.assertNotIn("-IGWBindAddress=172.31.240.40", decoded)

            engine_ini = server_root / "DuneSandbox" / "Saved" / "Config" / "LinuxServer" / "Engine.ini"
            user_engine_ini = server_root / "DuneSandbox" / "Saved" / "UserSettings" / "UserEngine.ini"
            self.assertIn('Bgd.ServerLoginPassword="pass with spaces"', engine_ini.read_text(encoding="utf-8"))
            self.assertIn('Bgd.ServerDisplayName="Display With Spaces"', user_engine_ini.read_text(encoding="utf-8"))

            config_link = dune_home / ".config" / "Epic" / "Unreal Engine" / "Engine" / "Config"
            self.assertTrue(config_link.is_symlink())
            self.assertEqual(config_link.resolve(), server_root / "DuneSandbox" / "Saved" / "UserSettings")

    def test_dry_run_copies_server_custom_settings_to_both_saved_locations(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            server_root = tmp_path / "server"
            dune_home = tmp_path / "home"
            args_out = tmp_path / "args.nul"
            custom_source = tmp_path / "UserServerCustomSettings.ini"
            server_root.mkdir()
            custom_source.write_text(
                "[/Script/DuneSandbox.UserServerCustomSettings]\n"
                "GatheringAmount=3.000000\n",
                encoding="utf-8",
            )

            env = {
                **os.environ,
                "DUNE_SERVER_ROOT": str(server_root),
                "DUNE_HOME": str(dune_home),
                "DUNE_RUN_SERVER_SAFE_DRY_RUN": "true",
                "DUNE_RUN_SERVER_SAFE_ARGS_OUT": str(args_out),
                "DUNE_SERVER_CUSTOM_SETTINGS_PATH": str(custom_source),
                "DUNE_BUILDING_PIECE_LIMIT_SETTINGS_ENABLED": "false",
                "DUNE_BUILDING_PIECE_LIMIT_PATCH_ENABLED": "false",
            }
            subprocess.run(
                [str(ROOT / "scripts" / "run_server_safe.sh"), "/Game/Dune/Maps/Test"],
                cwd=ROOT,
                env=env,
                check=True,
            )

            config_copy = server_root / "DuneSandbox" / "Saved" / "Config" / "LinuxServer" / "ServerCustomSettings.ini"
            user_copy = server_root / "DuneSandbox" / "Saved" / "UserSettings" / "ServerCustomSettings.ini"
            self.assertEqual(config_copy.read_text(encoding="utf-8"), custom_source.read_text(encoding="utf-8"))
            self.assertEqual(user_copy.read_text(encoding="utf-8"), custom_source.read_text(encoding="utf-8"))

    def test_dry_run_can_force_private_igw_bind_address(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            server_root = tmp_path / "server"
            dune_home = tmp_path / "home"
            args_out = tmp_path / "args.nul"
            server_root.mkdir()

            env = {
                **os.environ,
                "DUNE_SERVER_ROOT": str(server_root),
                "DUNE_HOME": str(dune_home),
                "DUNE_RUN_SERVER_SAFE_DRY_RUN": "true",
                "DUNE_RUN_SERVER_SAFE_ARGS_OUT": str(args_out),
                "POD_IP": "172.31.240.40",
                "DUNE_FORCE_PRIVATE_IGW_BIND_ADDRESS": "true",
            }
            subprocess.run(
                [
                    str(ROOT / "scripts" / "run_server_safe.sh"),
                    "-MultiHome=$POD_IP",
                ],
                cwd=ROOT,
                env=env,
                check=True,
            )

            raw_args = args_out.read_bytes().rstrip(b"\0").split(b"\0")
            decoded = [item.decode("utf-8") for item in raw_args]
            self.assertIn("-IGWBindAddress=172.31.240.40", decoded)

    def test_dry_run_can_set_explicit_igw_bind_address(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            server_root = tmp_path / "server"
            dune_home = tmp_path / "home"
            args_out = tmp_path / "args.nul"
            server_root.mkdir()

            env = {
                **os.environ,
                "DUNE_SERVER_ROOT": str(server_root),
                "DUNE_HOME": str(dune_home),
                "DUNE_RUN_SERVER_SAFE_DRY_RUN": "true",
                "DUNE_RUN_SERVER_SAFE_ARGS_OUT": str(args_out),
                "POD_IP": "172.31.240.40",
                "DUNE_IGW_BIND_ADDRESS": "24.109.206.134",
            }
            subprocess.run(
                [
                    str(ROOT / "scripts" / "run_server_safe.sh"),
                    "-MultiHome=$POD_IP",
                ],
                cwd=ROOT,
                env=env,
                check=True,
            )

            raw_args = args_out.read_bytes().rstrip(b"\0").split(b"\0")
            decoded = [item.decode("utf-8") for item in raw_args]
            self.assertIn("-IGWBindAddress=24.109.206.134", decoded)

    def test_private_igw_bind_address_overrides_explicit_igw_bind_address(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            server_root = tmp_path / "server"
            dune_home = tmp_path / "home"
            args_out = tmp_path / "args.nul"
            server_root.mkdir()

            env = {
                **os.environ,
                "DUNE_SERVER_ROOT": str(server_root),
                "DUNE_HOME": str(dune_home),
                "DUNE_RUN_SERVER_SAFE_DRY_RUN": "true",
                "DUNE_RUN_SERVER_SAFE_ARGS_OUT": str(args_out),
                "POD_IP": "172.31.240.40",
                "DUNE_FORCE_PRIVATE_IGW_BIND_ADDRESS": "true",
                "DUNE_IGW_BIND_ADDRESS": "24.109.206.134",
            }
            subprocess.run(
                [
                    str(ROOT / "scripts" / "run_server_safe.sh"),
                    "-MultiHome=$POD_IP",
                ],
                cwd=ROOT,
                env=env,
                check=True,
            )

            raw_args = args_out.read_bytes().rstrip(b"\0").split(b"\0")
            decoded = [item.decode("utf-8") for item in raw_args]
            self.assertIn("-IGWBindAddress=172.31.240.40", decoded)
            self.assertNotIn("-IGWBindAddress=24.109.206.134", decoded)

    def test_dry_run_can_enable_linux_server_preload(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            server_root = tmp_path / "server"
            dune_home = tmp_path / "home"
            args_out = tmp_path / "args.nul"
            env_out = tmp_path / "env.txt"
            loader = tmp_path / "libdune_server_probe_loader.so"
            server_root.mkdir()
            loader.write_bytes(b"placeholder")

            env = {
                **os.environ,
                "DUNE_SERVER_ROOT": str(server_root),
                "DUNE_HOME": str(dune_home),
                "DUNE_RUN_SERVER_SAFE_DRY_RUN": "true",
                "DUNE_RUN_SERVER_SAFE_ARGS_OUT": str(args_out),
                "DUNE_RUN_SERVER_SAFE_ENV_OUT": str(env_out),
                "DUNE_ENABLE_LINUX_SERVER_PRELOAD": "true",
                "DUNE_LINUX_SERVER_PRELOAD": str(loader),
                "DUNE_LINUX_SERVER_PRELOAD_PARTITIONS": "7,8",
                "DUNE_PROBE_LOADER_LOG": "/tmp/dune-preload-test.log",
                "DUNE_PROBE_LOADER_TARGET": "DuneSandboxServer;DuneSandbox",
                "DUNE_PROBE_LOADER_FORCE": "false",
                "DUNE_PROBE_LOADER_SNAPSHOT_DELAY_SECONDS": "0",
                "DUNE_PROBE_LOADER_UE_DELAYED_PROBE_SECONDS": "30",
                "DUNE_PROBE_LOADER_SCAN_ENABLED": "true",
                "DUNE_PROBE_LOADER_SCAN_PRESETS": "brt,building",
                "DUNE_PROBE_LOADER_SCAN_STRINGS": "DeepDesert;ServerRequestBaseBackup",
                "DUNE_PROBE_LOADER_SCAN_SIGNATURES": "test=48 85 ?? 74",
                "DUNE_PROBE_LOADER_SCAN_SIGNATURES_FILE": "/tmp/server-signatures.txt",
                "DUNE_PROBE_LOADER_SCAN_PATH_FILTER": "DuneSandboxServer",
                "DUNE_PROBE_LOADER_SCAN_LOG_MAPPINGS": "true",
                "DUNE_PROBE_LOADER_SCAN_MAX_HITS_PER_NEEDLE": "4",
                "DUNE_PROBE_LOADER_SCAN_MAX_MAPPING_BYTES": "1048576",
                "DUNE_PROBE_LOADER_UE_ANCHORS": "FNamePool=0x1000;GUObjectArray=0x2000",
                "DUNE_PROBE_LOADER_UE_ANCHOR_SIGNATURES": "GWorldRef=48 8d 0d ?? ?? ?? ??",
                "DUNE_PROBE_LOADER_UE_ANCHOR_SIGNATURES_FILE": "/tmp/server-anchor-signatures.txt",
                "DUNE_PROBE_LOADER_UE_AUTO_DISCOVER_ROOTS": "true",
                "DUNE_PROBE_LOADER_UE_AUTO_DISCOVER_INCLUDE_ANONYMOUS_RW": "true",
                "DUNE_PROBE_LOADER_UE_AUTO_DISCOVER_INCLUDE_PRIVATE_RW": "true",
                "DUNE_PROBE_LOADER_UE_AUTO_DISCOVER_MAX_MAPPING_BYTES": "268435456",
                "DUNE_PROBE_LOADER_UE_AUTO_DISCOVER_MAX_CANDIDATES": "8",
                "DUNE_PROBE_LOADER_UE_AUTO_DISCOVER_MIN_OBJECT_ARRAY_ELEMENTS": "128",
                "DUNE_PROBE_LOADER_UE_AUTO_DISCOVER_MAX_REJECTED_FNAME_SAMPLES": "16",
                "DUNE_PROBE_LOADER_UE_POINTER_PROBE": "true",
                "DUNE_PROBE_LOADER_UE_LAYOUT_PROBE": "true",
                "DUNE_PROBE_LOADER_UE_LAYOUT_SLOTS": "6",
                "DUNE_PROBE_LOADER_UE_UOBJECT_PROBE": "true",
                "DUNE_PROBE_LOADER_UE_OBJECT_ARRAY_PROBE": "true",
                "DUNE_PROBE_LOADER_UE_OBJECT_ARRAY_MAX_OBJECTS": "16",
                "DUNE_PROBE_LOADER_UE_OBJECT_ARRAY_OFFSET": "0x20",
                "DUNE_PROBE_LOADER_UE_OBJECT_ARRAY_ITEM_SIZE": "24",
                "DUNE_PROBE_LOADER_UE_OBJECT_ARRAY_OBJECT_OFFSET": "0",
                "DUNE_PROBE_LOADER_UE_OBJECT_ARRAY_CHUNK_SIZE": "65536",
                "DUNE_PROBE_LOADER_UE_FNAME_PROBE": "true",
                "DUNE_PROBE_LOADER_UE_FNAME_POOL": "0x3000",
                "DUNE_PROBE_LOADER_UE_FNAME_POOL_ADDR": "0x3000",
                "DUNE_PROBE_LOADER_UE_FNAME_POOL_OFFSET": "0",
                "DUNE_PROBE_LOADER_UE_FNAME_BLOCKS_OFFSET": "0x10",
                "DUNE_PROBE_LOADER_UE_FNAME_STRIDE": "2",
                "DUNE_PROBE_LOADER_UE_FNAME_MAX_LENGTH": "128",
                "DUNE_PROBE_LOADER_UE_FNAME_DIAGNOSTICS": "true",
                "DUNE_PROBE_LOADER_UE_FNAME_DIAGNOSTICS_MAX": "32",
                "DUNE_PROBE_LOADER_UE_SELF_TEST_ANCHOR": "true",
                "DUNE_PROBE_LOADER_HOOK_SELF_TEST": "true",
                "DUNE_PROBE_LOADER_MOD_SELF_TEST": "true",
                "DUNE_PROBE_LOADER_LUA_SELF_TEST": "true",
                "DUNE_PROBE_LOADER_LUA_LIBRARY": "liblua5.4.so",
                "DUNE_PROBE_LOADER_LUA_REFLECTION_SELF_TEST": "true",
                "DUNE_PROBE_LOADER_UE_PROCESS_EVENT_HOOK_PROBE": "true",
                "DUNE_PROBE_LOADER_UE_PROCESS_EVENT_HOOK_ADDRESS": "0x4000",
                "DUNE_PROBE_LOADER_UE_PROCESS_EVENT_ADDRESS": "0x4000",
                "DUNE_PROBE_LOADER_UE_PROCESS_EVENT_HOOK_SELF_TEST_TARGET": "false",
                "DUNE_PROBE_LOADER_UE_PROCESS_EVENT_HOOK_INSTALL": "false",
                "DUNE_PROBE_LOADER_UE_PROCESS_EVENT_HOOK_CALL_SELF_TEST": "false",
                "DUNE_PROBE_LOADER_UE_PROCESS_EVENT_LIVE_HOOK": "true",
                "DUNE_PROBE_LOADER_UE_PROCESS_EVENT_LIVE_HOOK_ADDRESS": "0x4000",
                "DUNE_PROBE_LOADER_UE_PROCESS_EVENT_LIVE_HOOK_SELF_TEST_TARGET": "false",
                "DUNE_PROBE_LOADER_UE_PROCESS_EVENT_LIVE_HOOK_CALL_SELF_TEST": "false",
                "DUNE_PROBE_LOADER_UE_PROCESS_EVENT_LIVE_HOOK_LOG_CALLS": "false",
                "DUNE_PROBE_LOADER_UE_PROCESS_EVENT_LIVE_HOOK_CALL_LOG_LIMIT": "8",
                "DUNE_PROBE_LOADER_UE_PROCESS_EVENT_DISPATCH_SELF_TEST": "false",
                "DUNE_PROBE_LOADER_UE_PROCESS_EVENT_LIVE_LUA_DISPATCH": "true",
                "DUNE_PROBE_LOADER_UE_PROCESS_EVENT_LIVE_LUA_SCRIPT": "return RegisterHook('/Script/DuneServerProbe.LiveProcessEvent:Function', function() return 11 end, function() return 31 end)",
                "DUNE_PROBE_LOADER_LUA_PROCESS_EVENT_SELF_TEST": "true",
                "DUNE_PROBE_LOADER_LUA_MODS_ENABLED": "true",
                "DUNE_PROBE_LOADER_LUA_MOD_SCRIPTS": "/tmp/mod.lua",
                "DUNE_PROBE_LOADER_LUA_MOD_DISPATCH_SELF_TEST": "true",
            }
            subprocess.run(
                [
                    str(ROOT / "scripts" / "run_server_safe.sh"),
                    "/Game/Dune/Maps/Test",
                    "-PartitionIndex=7",
                ],
                cwd=ROOT,
                env=env,
                check=True,
            )

            env_text = env_out.read_text(encoding="utf-8")
            self.assertIn(f"LD_PRELOAD={loader}", env_text)
            self.assertIn("DUNE_PROBE_LOADER_LOG=/tmp/dune-preload-test.log", env_text)
            self.assertIn("DUNE_PROBE_LOADER_TARGET=DuneSandboxServer;DuneSandbox", env_text)
            self.assertIn("DUNE_PROBE_LOADER_FORCE=false", env_text)
            self.assertIn("DUNE_PROBE_LOADER_SNAPSHOT_DELAY_SECONDS=0", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_DELAYED_PROBE_SECONDS=30", env_text)
            self.assertIn("DUNE_PROBE_LOADER_SCAN_ENABLED=true", env_text)
            self.assertIn("DUNE_PROBE_LOADER_SCAN_PRESETS=brt,building", env_text)
            self.assertIn("DUNE_PROBE_LOADER_SCAN_STRINGS=DeepDesert;ServerRequestBaseBackup", env_text)
            self.assertIn("DUNE_PROBE_LOADER_SCAN_SIGNATURES=test=48 85 ?? 74", env_text)
            self.assertIn("DUNE_PROBE_LOADER_SCAN_SIGNATURES_FILE=/tmp/server-signatures.txt", env_text)
            self.assertIn("DUNE_PROBE_LOADER_SCAN_PATH_FILTER=DuneSandboxServer", env_text)
            self.assertIn("DUNE_PROBE_LOADER_SCAN_LOG_MAPPINGS=true", env_text)
            self.assertIn("DUNE_PROBE_LOADER_SCAN_MAX_HITS_PER_NEEDLE=4", env_text)
            self.assertIn("DUNE_PROBE_LOADER_SCAN_MAX_MAPPING_BYTES=1048576", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_ANCHORS=FNamePool=0x1000;GUObjectArray=0x2000", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_ANCHOR_SIGNATURES=GWorldRef=48 8d 0d ?? ?? ?? ??", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_ANCHOR_SIGNATURES_FILE=/tmp/server-anchor-signatures.txt", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_AUTO_DISCOVER_ROOTS=true", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_AUTO_DISCOVER_INCLUDE_ANONYMOUS_RW=true", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_AUTO_DISCOVER_INCLUDE_PRIVATE_RW=true", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_AUTO_DISCOVER_MAX_MAPPING_BYTES=268435456", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_AUTO_DISCOVER_MAX_CANDIDATES=8", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_AUTO_DISCOVER_MIN_OBJECT_ARRAY_ELEMENTS=128", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_AUTO_DISCOVER_MAX_REJECTED_FNAME_SAMPLES=16", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_POINTER_PROBE=true", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_LAYOUT_PROBE=true", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_LAYOUT_SLOTS=6", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_UOBJECT_PROBE=true", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_OBJECT_ARRAY_PROBE=true", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_OBJECT_ARRAY_MAX_OBJECTS=16", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_OBJECT_ARRAY_OFFSET=0x20", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_OBJECT_ARRAY_ITEM_SIZE=24", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_OBJECT_ARRAY_OBJECT_OFFSET=0", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_OBJECT_ARRAY_CHUNK_SIZE=65536", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_FNAME_PROBE=true", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_FNAME_POOL=0x3000", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_FNAME_POOL_ADDR=0x3000", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_FNAME_POOL_OFFSET=0", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_FNAME_BLOCKS_OFFSET=0x10", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_FNAME_STRIDE=2", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_FNAME_MAX_LENGTH=128", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_FNAME_DIAGNOSTICS=true", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_FNAME_DIAGNOSTICS_MAX=32", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_SELF_TEST_ANCHOR=true", env_text)
            self.assertIn("DUNE_PROBE_LOADER_HOOK_SELF_TEST=true", env_text)
            self.assertIn("DUNE_PROBE_LOADER_MOD_SELF_TEST=true", env_text)
            self.assertIn("DUNE_PROBE_LOADER_LUA_SELF_TEST=true", env_text)
            self.assertIn("DUNE_PROBE_LOADER_LUA_LIBRARY=liblua5.4.so", env_text)
            self.assertIn("DUNE_PROBE_LOADER_LUA_REFLECTION_SELF_TEST=true", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_PROCESS_EVENT_HOOK_PROBE=true", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_PROCESS_EVENT_HOOK_ADDRESS=0x4000", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_PROCESS_EVENT_ADDRESS=0x4000", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_PROCESS_EVENT_HOOK_SELF_TEST_TARGET=false", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_PROCESS_EVENT_HOOK_INSTALL=false", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_PROCESS_EVENT_HOOK_CALL_SELF_TEST=false", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_PROCESS_EVENT_LIVE_HOOK=true", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_PROCESS_EVENT_LIVE_HOOK_ADDRESS=0x4000", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_PROCESS_EVENT_LIVE_HOOK_SELF_TEST_TARGET=false", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_PROCESS_EVENT_LIVE_HOOK_CALL_SELF_TEST=false", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_PROCESS_EVENT_LIVE_HOOK_LOG_CALLS=false", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_PROCESS_EVENT_LIVE_HOOK_CALL_LOG_LIMIT=8", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_PROCESS_EVENT_DISPATCH_SELF_TEST=false", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_PROCESS_EVENT_LIVE_LUA_DISPATCH=true", env_text)
            self.assertIn("DUNE_PROBE_LOADER_UE_PROCESS_EVENT_LIVE_LUA_SCRIPT=return RegisterHook('/Script/DuneServerProbe.LiveProcessEvent:Function', function() return 11 end, function() return 31 end)", env_text)
            self.assertIn("DUNE_PROBE_LOADER_LUA_PROCESS_EVENT_SELF_TEST=true", env_text)
            self.assertIn("DUNE_PROBE_LOADER_LUA_MODS_ENABLED=true", env_text)
            self.assertIn("DUNE_PROBE_LOADER_LUA_MOD_SCRIPTS=/tmp/mod.lua", env_text)
            self.assertIn("DUNE_PROBE_LOADER_LUA_MOD_DISPATCH_SELF_TEST=true", env_text)

    def test_dry_run_skips_linux_server_preload_for_non_matching_partition(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            server_root = tmp_path / "server"
            dune_home = tmp_path / "home"
            args_out = tmp_path / "args.nul"
            env_out = tmp_path / "env.txt"
            loader = tmp_path / "libdune_server_probe_loader.so"
            server_root.mkdir()
            loader.write_bytes(b"placeholder")

            env = {
                **os.environ,
                "DUNE_SERVER_ROOT": str(server_root),
                "DUNE_HOME": str(dune_home),
                "DUNE_RUN_SERVER_SAFE_DRY_RUN": "true",
                "DUNE_RUN_SERVER_SAFE_ARGS_OUT": str(args_out),
                "DUNE_RUN_SERVER_SAFE_ENV_OUT": str(env_out),
                "DUNE_ENABLE_LINUX_SERVER_PRELOAD": "true",
                "DUNE_LINUX_SERVER_PRELOAD": str(loader),
                "DUNE_LINUX_SERVER_PRELOAD_PARTITIONS": "7",
            }
            subprocess.run(
                [
                    str(ROOT / "scripts" / "run_server_safe.sh"),
                    "-PartitionIndex=8",
                ],
                cwd=ROOT,
                env=env,
                check=True,
            )

            env_text = env_out.read_text(encoding="utf-8")
            self.assertIn("LD_PRELOAD=", env_text)
            self.assertNotIn(str(loader), env_text)


class ComposeCommandTests(unittest.TestCase):
    def compose_config(self, *files, env_file=".env.example", profiles=None):
        cmd = ["docker", "compose"]
        for file_name in files:
            cmd.extend(["-f", file_name])
        for profile in profiles or []:
            cmd.extend(["--profile", profile])
        cmd.extend(["--env-file", str(env_file), "config", "--format", "json"])
        result = subprocess.run(
            cmd,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return json.loads(result.stdout)

    def test_default_compose_passes_fls_environment(self):
        config = self.compose_config("compose.yaml")
        command = config["services"]["survival"]["command"]
        self.assertIn("-ini:engine:[FuncomLiveServices]:DefaultFlsEnvironment=retail", command)
        self.assertEqual(config["services"]["director"]["environment"]["FuncomLiveServices__DefaultFlsEnvironment"], "retail")
        self.assertEqual(config["services"]["text-router"]["environment"]["FuncomLiveServices__DefaultFlsEnvironment"], "retail")
        self.assertEqual(config["services"]["gateway"]["environment"]["FuncomLiveServices__DefaultFlsEnvironment"], "retail")

    def test_deep_desert_uses_dedicated_engine_config(self):
        config = self.compose_config("compose.yaml")
        environment = config["services"]["deep-desert"]["environment"]
        self.assertEqual(environment["DUNE_USERENGINE_CONFIG_PATH"], "/workspace/config/UserEngine.deep-desert.ini")

    def test_partition_31_deep_desert_uses_dedicated_engine_config(self):
        config = self.compose_config(
            "compose.yaml",
            "compose.allmaps.yaml",
            profiles=["disabled-deep-desert-pvp"],
        )
        environment = config["services"]["deep-desert-pvp"]["environment"]
        self.assertEqual(environment["DUNE_USERENGINE_CONFIG_PATH"], "/workspace/config/UserEngine.deep-desert-pvp.ini")

    def test_deep_desert_uses_dimension_routing(self):
        director_ini = (ROOT / "config" / "director.ini").read_text(encoding="utf-8")
        self.assertIn("DeepDesert_1=Dimension", director_ini)
        self.assertNotIn("DeepDesert_1=ClassicalInstancing", director_ini)

    def test_allmaps_overlay_passes_fls_environment(self):
        config = self.compose_config("compose.yaml", "compose.allmaps.yaml")
        command = config["services"]["lostharvest-ecolab-a"]["command"]
        self.assertIn("-ini:engine:[FuncomLiveServices]:DefaultFlsEnvironment=retail", command)

    def test_compose_exposes_linux_loader_runtime_discovery_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "compose.env"
            env_text = (ROOT / ".env.example").read_text(encoding="utf-8")
            env_text += "\n".join(
                [
                    "",
                    "DUNE_PROBE_LOADER_TARGET=DuneSandboxServer;DuneSandbox",
                    "DUNE_PROBE_LOADER_UE_DELAYED_PROBE_SECONDS=30",
                    "DUNE_PROBE_LOADER_UE_FNAME_DIAGNOSTICS=true",
                    "DUNE_PROBE_LOADER_UE_FNAME_DIAGNOSTICS_MAX=32",
                    "DUNE_PROBE_LOADER_UE_AUTO_DISCOVER_ROOTS=true",
                    "DUNE_PROBE_LOADER_UE_AUTO_DISCOVER_INCLUDE_ANONYMOUS_RW=true",
                    "DUNE_PROBE_LOADER_UE_AUTO_DISCOVER_INCLUDE_PRIVATE_RW=true",
                    "DUNE_PROBE_LOADER_UE_AUTO_DISCOVER_MAX_MAPPING_BYTES=268435456",
                    "DUNE_PROBE_LOADER_UE_AUTO_DISCOVER_MAX_CANDIDATES=8",
                    "DUNE_PROBE_LOADER_UE_AUTO_DISCOVER_MIN_OBJECT_ARRAY_ELEMENTS=128",
                    "DUNE_PROBE_LOADER_UE_AUTO_DISCOVER_MAX_REJECTED_FNAME_SAMPLES=16",
                    "",
                ]
            )
            env_file.write_text(env_text, encoding="utf-8")

            config = self.compose_config("compose.yaml", env_file=env_file)
            environment = config["services"]["survival"]["environment"]

        self.assertEqual(environment["DUNE_PROBE_LOADER_TARGET"], "DuneSandboxServer;DuneSandbox")
        self.assertEqual(environment["DUNE_PROBE_LOADER_UE_DELAYED_PROBE_SECONDS"], "30")
        self.assertEqual(environment["DUNE_PROBE_LOADER_UE_FNAME_DIAGNOSTICS"], "true")
        self.assertEqual(environment["DUNE_PROBE_LOADER_UE_FNAME_DIAGNOSTICS_MAX"], "32")
        self.assertEqual(environment["DUNE_PROBE_LOADER_UE_AUTO_DISCOVER_ROOTS"], "true")
        self.assertEqual(environment["DUNE_PROBE_LOADER_UE_AUTO_DISCOVER_INCLUDE_ANONYMOUS_RW"], "true")
        self.assertEqual(environment["DUNE_PROBE_LOADER_UE_AUTO_DISCOVER_INCLUDE_PRIVATE_RW"], "true")
        self.assertEqual(environment["DUNE_PROBE_LOADER_UE_AUTO_DISCOVER_MAX_MAPPING_BYTES"], "268435456")
        self.assertEqual(environment["DUNE_PROBE_LOADER_UE_AUTO_DISCOVER_MAX_CANDIDATES"], "8")
        self.assertEqual(environment["DUNE_PROBE_LOADER_UE_AUTO_DISCOVER_MIN_OBJECT_ARRAY_ELEMENTS"], "128")
        self.assertEqual(environment["DUNE_PROBE_LOADER_UE_AUTO_DISCOVER_MAX_REJECTED_FNAME_SAMPLES"], "16")

    def test_compose_propagates_non_default_fls_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "compose.env"
            env_text = (ROOT / ".env.example").read_text(encoding="utf-8")
            env_text = env_text.replace("DUNE_FLS_ENV=retail", "DUNE_FLS_ENV=beta")
            env_file.write_text(env_text, encoding="utf-8")

            config = self.compose_config("compose.yaml", env_file=env_file)
            command = config["services"]["survival"]["command"]
            self.assertIn("-ini:engine:[FuncomLiveServices]:DefaultFlsEnvironment=beta", command)
            self.assertEqual(config["services"]["director"]["environment"]["FuncomLiveServices__DefaultFlsEnvironment"], "beta")
            self.assertEqual(config["services"]["text-router"]["environment"]["FuncomLiveServices__DefaultFlsEnvironment"], "beta")
            self.assertEqual(config["services"]["gateway"]["environment"]["FuncomLiveServices__DefaultFlsEnvironment"], "beta")


class RestoreStateTests(unittest.TestCase):
    def test_restore_dry_run_reports_world_identity_and_optional_layers(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "backups") as tmp:
            backup_dir = Path(tmp)
            env_file = backup_dir / "restore.env"
            env_file.write_text("WORLD_UNIQUE_NAME=sh-current\n", encoding="utf-8")
            (backup_dir / "postgres-dune_sb_1_4_0_0.dump").write_bytes(b"not-read-in-dry-run")
            (backup_dir / "manifest.txt").write_text(
                "world_unique_name=sh-backed-up\n"
                "config_archive=config.tgz\n"
                "config_tls_archive=config-tls.tgz\n",
                encoding="utf-8",
            )
            create_desired_state_fixture(backup_dir)
            create_change_intelligence_fixture(backup_dir)
            subprocess.run(
                ["tar", "-czf", str(backup_dir / "config.tgz"), "-C", str(backup_dir), "manifest.txt", "config"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            for archive_name in ("config-tls.tgz", "rabbitmq-admin.tgz", "rabbitmq-game.tgz", "server-saved.tgz"):
                subprocess.run(
                    ["tar", "-czf", str(backup_dir / archive_name), "-C", str(backup_dir), "manifest.txt"],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            import sqlite3
            connection = sqlite3.connect(backup_dir / "community-rewards.sqlite3")
            connection.execute("create table test(id integer primary key)")
            connection.commit()
            connection.close()
            connection = sqlite3.connect(backup_dir / "base-gallery.sqlite3")
            connection.execute("create table test(id integer primary key)")
            connection.commit()
            connection.close()
            connection = sqlite3.connect(backup_dir / "moderation.sqlite3")
            connection.execute("create table test(id integer primary key)")
            connection.commit()
            connection.close()
            connection = sqlite3.connect(backup_dir / "operational-slo.sqlite3")
            connection.execute("create table incident_events(sequence integer primary key,incident_id text,objective_id text,event_type text,created_at real,actor text,note text,payload_json text,previous_hash text,event_hash text)")
            connection.commit()
            connection.close()
            connection = sqlite3.connect(backup_dir / "capacity-intelligence.sqlite3")
            connection.execute("create table applications(id text primary key,applied_at real,actor text,source text,changes_json text,sha256 text)")
            connection.execute("create trigger capacity_applications_no_update before update on applications begin select raise(abort,'append-only'); end")
            connection.execute("create trigger capacity_applications_no_delete before delete on applications begin select raise(abort,'append-only'); end")
            connection.commit()
            connection.close()

            result = subprocess.run(
                [
                    str(ROOT / "scripts" / "restore-state.sh"),
                    "--dry-run",
                    "--rabbitmq",
                    "--server-saved",
                    "--config",
                    "--tls",
                    "--community-rewards",
                    "--moderation",
                    "--base-gallery",
                    "--operational-slo",
                    "--capacity-intelligence",
                    "--desired-state",
                    "--change-intelligence",
                    str(env_file),
                    str(backup_dir.relative_to(ROOT)),
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("restore_config=true", result.stdout)
            self.assertIn("restore_tls=true", result.stdout)
            self.assertIn("restore_community_rewards=true", result.stdout)
            self.assertIn("restore_moderation=true", result.stdout)
            self.assertIn("restore_base_gallery=true", result.stdout)
            self.assertIn("restore_operational_slo=true", result.stdout)
            self.assertIn("restore_capacity_intelligence=true", result.stdout)
            self.assertIn("restore_desired_state=true", result.stdout)
            self.assertIn("restore_change_intelligence=true", result.stdout)
            self.assertIn("backup_world_unique_name=sh-backed-up", result.stdout)
            self.assertIn("current_world_unique_name=sh-current", result.stdout)
            self.assertIn("differs from current", result.stderr)


class BackupStateTests(unittest.TestCase):
    def test_backup_dry_run_reports_identity_layers_without_docker(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env_file = tmp_path / "backup.env"
            env_file.write_text(
                "WORLD_UNIQUE_NAME=sh-backup\n"
                "DUNE_FLS_ENV=retail\n"
                "GAME_RMQ_PUBLIC_HOST=rmq.example.test\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [str(ROOT / "scripts" / "backup-state.sh"), "--dry-run", str(env_file)],
                cwd=ROOT,
                env={**os.environ, "CONTAINER_RUNTIME": "definitely-not-a-runtime"},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("backup dry run OK", result.stdout)
            self.assertIn("world_unique_name=sh-backup", result.stdout)
            self.assertIn("dune_fls_env=retail", result.stdout)
            self.assertIn("game_rmq_public_host=rmq.example.test", result.stdout)

    def test_backup_dry_run_reports_portable_operator_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env_file = tmp_path / "backup.env"
            evidence = tmp_path / "evidence"
            evidence.mkdir()
            (evidence / "one.signed.json").write_text("{}\n", encoding="utf-8")
            env_file.write_text("WORLD_UNIQUE_NAME=sh-backup\n", encoding="utf-8")
            result = subprocess.run(
                [str(ROOT / "scripts" / "backup-state.sh"), "--dry-run", str(env_file)],
                cwd=ROOT,
                env={**os.environ, "CONTAINER_RUNTIME": "definitely-not-a-runtime", "DUNE_CHANGE_INTELLIGENCE_HOST_EVIDENCE_DIR": str(evidence)},
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("operator_evidence_archive=operator-evidence.tgz", result.stdout)
            self.assertIn("operator_evidence_files=1", result.stdout)


class OperationalIdentityCheckTests(unittest.TestCase):
    def test_operational_identity_check_reports_rendered_fls_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "identity.env"
            env_text = (ROOT / ".env.example").read_text(encoding="utf-8")
            env_text = env_text.replace("WORLD_UNIQUE_NAME=sh-example-dune", "WORLD_UNIQUE_NAME=sh-identity-test")
            env_text = env_text.replace("DUNE_FLS_ENV=retail", "DUNE_FLS_ENV=beta")
            env_text = env_text.replace("GAME_RMQ_PUBLIC_HOST=127.0.0.1", "GAME_RMQ_PUBLIC_HOST=rmq.example.test")
            env_file.write_text(env_text, encoding="utf-8")

            result = subprocess.run(
                [str(ROOT / "scripts" / "check-operational-identity.sh"), str(env_file)],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("survival command renders beta FLS environment", result.stdout)
            self.assertIn("service layer renders beta FLS environment", result.stdout)


class OperationalReportTests(unittest.TestCase):
    def test_operational_report_redacts_secret_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env_file = tmp_path / "report.env"
            report_file = tmp_path / "report.txt"
            env_text = (ROOT / ".env.example").read_text(encoding="utf-8")
            env_text = env_text.replace("WORLD_UNIQUE_NAME=sh-example-dune", "WORLD_UNIQUE_NAME=sh-report-test")
            fls_key = "FLS" + "_SECRET"
            env_text = env_text.replace(f"{fls_key}=", f"{fls_key}=super-secret-token")
            env_text = env_text.replace("DUNE_ADMIN_TOKEN=change-me-admin-token", "DUNE_ADMIN_TOKEN=admin-secret-token")
            env_file.write_text(env_text, encoding="utf-8")

            result = subprocess.run(
                [str(ROOT / "scripts" / "operational-report.sh"), str(env_file), str(report_file)],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = report_file.read_text(encoding="utf-8")
            self.assertIn("WORLD_UNIQUE_NAME=sh-report-test", report)
            self.assertIn(f"{fls_key}=<set length=18>", report)
            self.assertIn("DUNE_ADMIN_TOKEN=<set length=18>", report)
            self.assertNotIn("super-secret-token", report)
            self.assertNotIn("admin-secret-token", report)
            self.assertIn("compose_config=OK", report)


class OperationalBundleTests(unittest.TestCase):
    def test_operational_bundle_excludes_raw_secrets(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "backups") as tmp:
            tmp_path = Path(tmp)
            env_file = tmp_path / "bundle.env"
            bundle_file = tmp_path / "bundle.tgz"
            extract_dir = tmp_path / "extract"
            env_text = (ROOT / ".env.example").read_text(encoding="utf-8")
            env_text = env_text.replace("WORLD_UNIQUE_NAME=sh-example-dune", "WORLD_UNIQUE_NAME=sh-bundle-test")
            fls_key = "FLS" + "_SECRET"
            env_text = env_text.replace(f"{fls_key}=", f"{fls_key}=bundle-secret-token")
            env_file.write_text(env_text, encoding="utf-8")

            result = subprocess.run(
                [str(ROOT / "scripts" / "operational-bundle.sh"), str(env_file), str(bundle_file.relative_to(ROOT))],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            extract_dir.mkdir()
            subprocess.run(["tar", "-xzf", str(bundle_file), "-C", str(extract_dir)], check=True)
            names = {path.name for path in extract_dir.iterdir()}
            self.assertIn("operational-report.txt", names)
            self.assertIn("operational-identity-check.txt", names)
            self.assertIn("backup-dry-run.txt", names)
            self.assertIn("compose-summary.txt", names)
            self.assertIn("manifest.txt", names)
            self.assertNotIn("bundle.env", names)

            combined = "\n".join(path.read_text(encoding="utf-8") for path in extract_dir.iterdir() if path.is_file())
            self.assertIn("contains_env=false", combined)
            self.assertIn("WORLD_UNIQUE_NAME=sh-bundle-test", combined)
            self.assertNotIn("bundle-secret-token", combined)

    def test_operational_bundle_verifier_accepts_generated_bundle(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "backups") as tmp:
            tmp_path = Path(tmp)
            env_file = tmp_path / "bundle.env"
            bundle_file = tmp_path / "bundle.tgz"
            env_text = (ROOT / ".env.example").read_text(encoding="utf-8")
            env_text = env_text.replace("WORLD_UNIQUE_NAME=sh-example-dune", "WORLD_UNIQUE_NAME=sh-bundle-verify")
            env_file.write_text(env_text, encoding="utf-8")

            subprocess.run(
                [str(ROOT / "scripts" / "operational-bundle.sh"), str(env_file), str(bundle_file.relative_to(ROOT))],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            result = subprocess.run(
                [str(ROOT / "scripts" / "verify-operational-bundle.sh"), str(bundle_file.relative_to(ROOT))],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("operational bundle verification complete: OK", result.stdout)

    def test_operational_bundle_verifier_rejects_env_file(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "backups") as tmp:
            tmp_path = Path(tmp)
            bundle_file = tmp_path / "bad-bundle.tgz"
            payload_dir = tmp_path / "payload"
            payload_dir.mkdir()
            for name in ("operational-report.txt", "operational-identity-check.txt", "backup-dry-run.txt", "compose-summary.txt"):
                (payload_dir / name).write_text("ok\n", encoding="utf-8")
            (payload_dir / "manifest.txt").write_text(
                "contains_env=false\n"
                "contains_tls_keys=false\n"
                "contains_database_dump=false\n"
                "contains_rabbitmq_state=false\n"
                "contains_raw_compose=false\n",
                encoding="utf-8",
            )
            (payload_dir / ".env").write_text("WORLD_UNIQUE_NAME=sh-bad\n", encoding="utf-8")
            subprocess.run(["tar", "-czf", str(bundle_file), "-C", str(payload_dir), "."], check=True)

            result = subprocess.run(
                [str(ROOT / "scripts" / "verify-operational-bundle.sh"), str(bundle_file.relative_to(ROOT))],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("forbidden file types", result.stderr)


class VerifyBackupTests(unittest.TestCase):
    def test_verify_backup_reports_identity_layers_without_pg_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp)
            bin_dir = backup_dir / "bin"
            bin_dir.mkdir()
            for tool in ("bash", "tar", "gzip", "rg", "grep", "find", "python3"):
                target = shutil.which(tool)
                self.assertIsNotNone(target, tool)
                os.symlink(target, bin_dir / tool)
            (backup_dir / "postgres-dune_sb_1_4_0_0.dump").write_bytes(b"placeholder")
            (backup_dir / ".env").write_text("WORLD_UNIQUE_NAME=sh-test\n", encoding="utf-8")
            (backup_dir / "manifest.txt").write_text("world_unique_name=sh-test\n", encoding="utf-8")
            create_desired_state_fixture(backup_dir)
            create_change_intelligence_fixture(backup_dir)
            subprocess.run(
                ["tar", "-czf", str(backup_dir / "config.tgz"), "-C", str(backup_dir), "manifest.txt", "config"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            for archive_name in ("config-tls.tgz",):
                subprocess.run(
                    ["tar", "-czf", str(backup_dir / archive_name), "-C", str(backup_dir), "manifest.txt"],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            import sqlite3
            connection = sqlite3.connect(backup_dir / "community-rewards.sqlite3")
            connection.execute("create table test(id integer primary key)")
            connection.commit()
            connection.close()
            connection = sqlite3.connect(backup_dir / "base-gallery.sqlite3")
            connection.execute("create table test(id integer primary key)")
            connection.commit()
            connection.close()
            connection = sqlite3.connect(backup_dir / "moderation.sqlite3")
            connection.execute("create table test(id integer primary key)")
            connection.commit()
            connection.close()
            connection = sqlite3.connect(backup_dir / "operational-slo.sqlite3")
            connection.execute("create table incident_events(sequence integer primary key,incident_id text,objective_id text,event_type text,created_at real,actor text,note text,payload_json text,previous_hash text,event_hash text)")
            connection.commit()
            connection.close()
            connection = sqlite3.connect(backup_dir / "capacity-intelligence.sqlite3")
            connection.execute("create table applications(id text primary key,applied_at real,actor text,source text,changes_json text,sha256 text)")
            connection.execute("create trigger capacity_applications_no_update before update on applications begin select raise(abort,'append-only'); end")
            connection.execute("create trigger capacity_applications_no_delete before delete on applications begin select raise(abort,'append-only'); end")
            connection.commit()
            connection.close()

            env = {**os.environ, "PATH": str(bin_dir)}
            result = subprocess.run(
                [str(ROOT / "scripts" / "verify-backup.sh"), str(backup_dir)],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("OK manifest world identity present", result.stdout)
            self.assertIn("OK archive", result.stdout)
            self.assertIn("OK env copy present", result.stdout)
            self.assertIn("OK community rewards SQLite snapshot", result.stdout)
            self.assertIn("OK moderation SQLite snapshot", result.stdout)
            self.assertIn("OK base gallery SQLite snapshot", result.stdout)
            self.assertIn("OK operational SLO SQLite snapshot and incident hash chain", result.stdout)
            self.assertIn("OK capacity intelligence SQLite snapshot and application receipts", result.stdout)
            self.assertIn("OK desired-state SQLite snapshot, baseline/observation/finding HMACs, and event chain", result.stdout)
            self.assertIn("OK change-intelligence SQLite snapshot and HMAC event chain", result.stdout)
            self.assertIn("OK 1 portable signed operator evidence capsule(s)", result.stdout)

            with tarfile.open(backup_dir / "operator-evidence.tgz", "r:gz") as archive:
                document = json.load(archive.extractfile("operator-evidence/fixture.signed.json"))
            evidence_dir = backup_dir / "deployment-evidence"
            assurance = deployment_assurance.Store(
                backup_dir / "deployment-windows", evidence_dir, backup_dir,
                change_intelligence.read_secret(backup_dir / "config" / "secrets" / "change-intelligence-hmac.secret"),
            )
            policy_path = backup_dir / "config" / "change-intelligence.json"
            manifest = [{"path": "config/change-intelligence.json", "sha256": deployment_assurance.file_sha256(policy_path)}]
            snapshot = {"survival": {"containerId": "a" * 64, "state": "running", "startedAt": "2026-07-16T10:00:00Z"}}
            window = assurance.start(
                commit="b" * 40, reason="Shell backup deployment evidence fixture", manifest=manifest,
                principal_id="owner", snapshot=snapshot, protected_services=["survival"], strict_services=["survival"],
                recovery_backup={"ok": True, "path": "before", "exitCode": 0},
                source_rollback={"verified": True, "path": "before.tgz", "sha256": "e" * 64, "bytes": 1}, now=1000,
            )
            assured = assurance.finish(
                window["id"], principal_id="owner", snapshot=snapshot,
                health={"desiredStateAttested": True, "readinessCurrent": True, "sloHealthy": True, "changeIntegrity": True, "prometheusReadiness": True, "adminHealthy": True, "backupVerified": True},
                backup={"ok": True, "path": "after", "exitCode": 0}, now=1100,
            )["evidencePath"]
            fixture_path = backup_dir / "fixture.signed.json"
            fixture_path.write_text(json.dumps(document), encoding="utf-8")
            with tarfile.open(backup_dir / "operator-evidence.tgz", "w:gz") as archive:
                archive.add(fixture_path, arcname="operator-evidence/fixture.signed.json")
                archive.add(assured, arcname=f"operator-evidence/{Path(assured).name}")
            mixed = subprocess.run(
                [str(ROOT / "scripts" / "verify-backup.sh"), str(backup_dir)], cwd=ROOT, env=env,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(0, mixed.returncode, mixed.stdout + mixed.stderr)
            self.assertIn("OK 2 portable signed operator evidence capsule(s)", mixed.stdout)

            document["capsule"]["responsePlan"]["title"] = "tampered response plan"
            payload = backup_dir / "fixture.signed.json"
            payload.write_text(json.dumps(document), encoding="utf-8")
            with tarfile.open(backup_dir / "operator-evidence.tgz", "w:gz") as archive:
                archive.add(payload, arcname="operator-evidence/fixture.signed.json")
            payload.unlink()
            tampered = subprocess.run(
                [str(ROOT / "scripts" / "verify-backup.sh"), str(backup_dir)], cwd=ROOT, env=env,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(1, tampered.returncode)
            self.assertIn("FAIL portable signed operator evidence capsules", tampered.stderr)


class FailoverScriptTests(unittest.TestCase):
    def test_compose_files_adds_director_hostnet_overlays_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "dune.env"
            env_file.write_text(
                "DUNE_DIRECTOR_HOSTNET_ENABLED=true\n"
                "DUNE_DIRECTOR_HOSTNET_PORT_COMPOSE_FILE=compose.director-hostnet-port.yaml\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [str(ROOT / "scripts" / "compose-files.sh"), str(env_file)],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            files = result.stdout.strip().split(":")
            self.assertIn("compose.fls-ipv4-hosts.yaml", files)
            self.assertIn("compose.director-hostnet-cutover.yaml", files)
            self.assertIn("compose.director-hostnet-port.yaml", files)
            self.assertLess(files.index("compose.fls-ipv4-hosts.yaml"), files.index("compose.director-hostnet-cutover.yaml"))

    def test_router_cutover_dry_run_rewrites_env_driven_ports(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir = tmp_path / "bin"
            backup_dir = tmp_path / "router-backups"
            bin_dir.mkdir()
            ssh = bin_dir / "ssh"
            ssh.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ \"$*\" == *\"nvram get vts_rulelist\"* ]]; then\n"
                "  printf '<duneA1>7000:7002>10.0.0.10>>UDP><duneA2>7100:7102>10.0.0.10>>UDP><DuneRMQ>32000>10.0.0.10>32000>TCP>'\n"
                "  exit 0\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            ssh.chmod(0o755)
            env_file = tmp_path / "failover.env"
            env_file.write_text(
                "DUNE_FAILOVER_ROUTER_SSH=router.example.test\n"
                "DUNE_FAILOVER_PRIMARY_LAN_IP=10.0.0.10\n"
                "DUNE_FAILOVER_STANDBY_LAN_IP=10.0.0.20\n"
                "EXTERNAL_ADDRESS=203.0.113.10\n"
                "GAME_RMQ_PUBLIC_PORT=32000\n"
                "GAME_UDP_PORT_RANGE=7000:7002\n"
                "IGW_UDP_PORT_RANGE=7100:7102\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [str(ROOT / "scripts" / "router-cutover-asuswrt.sh"), str(env_file), "10.0.0.20"],
                cwd=ROOT,
                env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "DUNE_ROUTER_BACKUP_DIR": str(backup_dir)},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("<duneA1>7000:7002>10.0.0.20>>UDP>", result.stdout)
            self.assertIn("<duneA2>7100:7102>10.0.0.20>>UDP>", result.stdout)
            self.assertIn("<DuneRMQ>32000>10.0.0.20>32000>TCP>", result.stdout)
            self.assertIn("Dry run only", result.stdout)

    def test_staged_rabbitmq_install_dry_run_does_not_touch_live_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            live_dir = tmp_path / "live-rabbitmq"
            stage_dir = tmp_path / "staged-rabbitmq"
            env_file = tmp_path / "dune.env"
            env_file.write_text("GAME_RMQ_PUBLIC_HOST=rmq.example.test\n", encoding="utf-8")
            live_dir.mkdir()
            (live_dir / "server.crt").write_text("live\n", encoding="utf-8")

            generated = subprocess.run(
                [str(ROOT / "scripts" / "generate-rabbitmq-cert.sh"), str(env_file)],
                cwd=ROOT,
                env={**os.environ, "RABBITMQ_TLS_DIR": str(stage_dir)},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)

            result = subprocess.run(
                [str(ROOT / "scripts" / "install-staged-rabbitmq-cert.sh"), str(env_file)],
                cwd=ROOT,
                env={**os.environ, "RABBITMQ_STAGED_TLS_DIR": str(stage_dir), "RABBITMQ_TLS_DIR": str(live_dir)},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("dry-run: would back up", result.stdout)
            self.assertEqual((live_dir / "server.crt").read_text(encoding="utf-8"), "live\n")

    def test_failover_orchestrate_apply_refuses_failed_preflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir = tmp_path / "bin"
            log_file = tmp_path / "make.log"
            bin_dir.mkdir()
            make = bin_dir / "make"
            make.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$MAKE_LOG\"\n"
                "case \"$*\" in\n"
                "  *standby-status*) exit 2 ;;\n"
                "  *) exit 0 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            make.chmod(0o755)
            env_file = tmp_path / "failover.env"
            env_file.write_text(
                "POSTGRES_REMOTE_REPLICA_HOST=standby.example.test\n"
                "DUNE_FAILOVER_PRIMARY_LAN_IP=10.0.0.10\n"
                "DUNE_FAILOVER_STANDBY_LAN_IP=10.0.0.20\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [str(ROOT / "scripts" / "failover-orchestrate.sh"), str(env_file), "standby", "--apply"],
                cwd=ROOT,
                env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "MAKE_LOG": str(log_file)},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("Refusing apply", result.stderr)
            self.assertNotIn("sync-standby-files", log_file.read_text(encoding="utf-8"))

    def test_rabbitmq_tls_recreate_dry_run_allows_invalid_current_cert(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env_file = tmp_path / "dune.env"
            cert = write_self_signed_cert(tmp_path, "DNS:game-rmq,DNS:localhost,IP:127.0.0.1")
            env_file.write_text("GAME_RMQ_PUBLIC_HOST=rmq.example.test\n", encoding="utf-8")

            result = subprocess.run(
                [str(ROOT / "scripts" / "recreate-rabbitmq-tls-stack.sh"), str(env_file)],
                cwd=ROOT,
                env={**os.environ, "RABBITMQ_CERT_PATH": str(cert)},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Dry run only", result.stdout)

    def test_rabbitmq_tls_recreate_apply_refuses_invalid_current_cert(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env_file = tmp_path / "dune.env"
            cert = write_self_signed_cert(tmp_path, "DNS:game-rmq,DNS:localhost,IP:127.0.0.1")
            env_file.write_text("GAME_RMQ_PUBLIC_HOST=rmq.example.test\n", encoding="utf-8")

            result = subprocess.run(
                [str(ROOT / "scripts" / "recreate-rabbitmq-tls-stack.sh"), str(env_file)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "RABBITMQ_CERT_PATH": str(cert),
                    "CONFIRM_RECREATE_RMQ_TLS_STACK": "yes",
                },
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("refusing recreate", result.stderr)


if __name__ == "__main__":
    unittest.main()
