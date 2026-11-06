#!/usr/bin/env python3
"""Check the approved runtime before Compose or game-package updates."""

import argparse
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "admin"))
import runtime_security


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("env_file")
    parser.add_argument("--expected-tag")
    args = parser.parse_args()
    settings = runtime_security.settings_from_file(args.env_file)
    settings.update({key: os.environ[key] for key in runtime_security.KEYS if key in os.environ})
    approval = runtime_security.configuration(ROOT, settings, args.expected_tag)
    if approval is None:
        print(json.dumps({"enabled": False}))
        return
    def image_id(reference):
        return subprocess.check_output(
            ["docker", "image", "inspect", reference, "--format", "{{.Id}}"],
            stderr=subprocess.PIPE, timeout=15, text=True,
        ).strip()
    vendor = "registry.funcom.com/funcom/self-hosting/seabass-server:" + approval["vendorTag"]
    runtime_security.verify_loaded_images(approval, image_id(approval["image"]), image_id(vendor))
    print(json.dumps({"enabled": True, "image": approval["image"], "imageId": approval["imageId"],
                      "vendorTag": approval["vendorTag"]}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) else "required image or approval is unavailable"
        print("runtime security preflight rejected: " + reason, file=sys.stderr)
        raise SystemExit(2)
