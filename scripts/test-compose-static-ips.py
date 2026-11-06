#!/usr/bin/env python3
import contextlib
import importlib.util
import io
import pathlib
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("static_ips", pathlib.Path(__file__).with_name("check-compose-static-ips.py"))
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


class StaticIPTests(unittest.TestCase):
    def check(self, services):
        config = {"services": services, "networks": {"default": {"ipam": {"config": [{"ip_range": "172.31.240.128/25"}]}}}}
        with mock.patch.object(CHECK, "compose_config", return_value=config), \
             mock.patch.object(CHECK.sys, "argv", ["check"]), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return CHECK.main()

    def test_isolated_and_host_helpers_do_not_require_static_addresses(self):
        self.assertEqual(self.check({"broker": {"network_mode": "none"}, "host": {"network_mode": "host"}}), 0)

    def test_bridged_service_still_requires_default_attachment(self):
        self.assertEqual(self.check({"game": {}}), 1)

    def test_duplicate_static_addresses_are_rejected(self):
        attachment = {"networks": {"default": {"ipv4_address": "172.31.240.5"}}}
        self.assertEqual(self.check({"one": attachment, "two": attachment}), 1)

    def test_dynamic_range_is_not_valid_for_a_static_address(self):
        self.assertEqual(self.check({"game": {"networks": {"default": {"ipv4_address": "172.31.240.130"}}}}), 1)


if __name__ == "__main__":
    unittest.main()
