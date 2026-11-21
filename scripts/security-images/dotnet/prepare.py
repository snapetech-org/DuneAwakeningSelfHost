"""Convert the known self-contained vendor layout to the patched shared runtime."""
import json
from pathlib import Path
import re
import shutil
import sys

app = Path(sys.argv[1])
name = sys.argv[2]
shared = Path(sys.argv[3]) / "shared"
config_path = app / f"{name}.runtimeconfig.json"
config = json.loads(config_path.read_text())
options = config["runtimeOptions"]
frameworks = options.pop("includedFrameworks")
expected = {"Microsoft.NETCore.App", "Microsoft.AspNetCore.App"}
if {entry["name"] for entry in frameworks} != expected or len(frameworks) != 2:
    raise ValueError("Unexpected vendor framework layout")
deps_path = app / f"{name}.deps.json"
deps = json.loads(deps_path.read_text())
target = deps["targets"][deps["runtimeTarget"]["name"]]
manifest_path = app / "extraction-manifest.json"
entries = set(json.loads(manifest_path.read_text())["files"])
updated = set()
versions = set()
for framework in frameworks:
    pack = framework["name"]
    patch_dir, = (shared / pack).iterdir()
    patch = patch_dir.name
    old = framework["version"]
    if not all(re.fullmatch(r"10\.0\.\d+", v) for v in (old, patch)):
        raise ValueError("Only .NET 10.0 patch upgrades are supported")
    if int(patch.split(".")[-1]) < max(15, int(old.split(".")[-1])):
        raise ValueError("Runtime below the vendor or qualified patch level")
    prefix = f"runtimepack.{pack}.Runtime.linux-musl-x64/"
    assets = target[prefix + old]["runtime"]
    if not assets or set(assets) - entries or updated.intersection(assets):
        raise ValueError("Missing or ambiguous framework assets")
    for asset in assets:
        if Path(asset).name != asset:
            raise ValueError("Unexpected framework asset path")
        shutil.copyfile(patch_dir / asset, app / asset)
    updated.update(assets)
    for group in (target, deps["libraries"]):
        if old != patch and prefix + patch in group:
            raise ValueError("Runtime package collision")
        group[prefix + patch] = group.pop(prefix + old)
    versions.add(patch)
    framework["version"] = patch
if len(versions) != 1:
    raise ValueError("ASP.NET and .NET runtime versions differ")
options["frameworks"] = frameworks
config_path.write_text(json.dumps(config, indent=2) + "\n")
deps_path.write_text(json.dumps(deps, separators=(",", ":")) + "\n")
manifest_path.unlink()
for path in app.rglob("*"):
    path.chmod(0o755 if path.is_dir() else 0o644)
app.chmod(0o755)
