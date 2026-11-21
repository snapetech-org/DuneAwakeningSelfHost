"""Produce OpenVEX for the reviewed sid fixes present in this exact SteamCMD image."""
import datetime
import json
from pathlib import Path
import subprocess
import sys
from urllib.parse import parse_qs, unquote
import uuid

# Reviewed Debian source-package/advisory pairs for the SteamCMD sid overlay.
REVIEWED = {
    "ncurses": {"CVE-2025-69720"},
    "sqlite3": {"CVE-2026-119373", "CVE-2026-119374", "CVE-2026-119379"},
    "systemd": {"CVE-2026-16742"},
    "acl": {"CVE-2026-54369"},
    "util-linux": {"CVE-2026-76642", "CVE-2026-78408", "CVE-2026-78409", "CVE-2026-78410"},
    "perl": {"CVE-2026-9538"},
}


def assess(finding, installed, tracker):
    purl = (finding.get("PkgIdentifier") or {}).get("PURL", "")
    product, _, query = purl.partition("?")
    package, _, version = product.removeprefix("pkg:deb/debian/").partition("@")
    qualifiers = parse_qs(query)
    arch = qualifiers.get("arch", [])
    if not product.startswith("pkg:deb/debian/") or len(arch) != 1:
        return
    version = unquote(version)
    if "epoch" in qualifiers:
        epoch = qualifiers["epoch"]
        if len(epoch) != 1 or not epoch[0].isdigit():
            return
        version = epoch[0] + ":" + version
    if package != finding["PkgName"] or version != finding["InstalledVersion"]:
        return
    source = installed.get((package, arch[0], version))
    if not source:
        return
    name, source_version = source
    cve = finding["VulnerabilityID"]
    if cve not in REVIEWED.get(name, set()):
        return
    sid = tracker.get(name, {}).get(cve, {}).get("releases", {}).get("sid", {})
    fixed = sid.get("fixed_version")
    if (sid.get("status") != "resolved" or not fixed or fixed == "0"
            or sid.get("repositories", {}).get("sid") != source_version):
        return
    if subprocess.run(["dpkg", "--compare-versions", source_version, "ge", fixed]).returncode:
        return
    return {"purl": purl, "cve": cve, "source": name, "version": source_version, "fixed": fixed}


def main():
    image, raw, tracker_file, destination = sys.argv[1:]
    report = json.loads(Path(raw).read_text())
    tracker = json.loads(Path(tracker_file).read_bytes())
    installed = {}
    inventory = subprocess.check_output(
        ["docker", "run", "--rm", "--read-only", "--network", "none", "--cap-drop", "ALL",
         "--security-opt", "no-new-privileges", "--entrypoint", "dpkg-query", image,
         "-W", "-f=${Package}\\t${Architecture}\\t${Version}\\t${source:Package}\\t${source:Version}\\n"],
        text=True)
    for row in inventory.splitlines():
        package, arch, version, source, source_version = row.split("\t")
        installed[package, arch, version] = (source, source_version)
    statements = []
    for result in report["Results"]:
        if (result.get("Class"), result.get("Type")) != ("os-pkgs", "debian"):
            continue
        for finding in result.get("Vulnerabilities", []):
            proof = assess(finding, installed, tracker)
            if proof:
                statements.append({
                    "vulnerability": {"name": proof["cve"]}, "products": [{"@id": proof["purl"]}],
                    "status": "not_affected", "justification": "vulnerable_code_not_present",
                    "impact_statement": (f"{image} contains Debian {proof['source']} {proof['version']}. "
                                         f"The Debian tracker lists sid as resolved in {proof['fixed']}."),
                    "action_statement": "https://security-tracker.debian.org/tracker/" + proof["cve"],
                })
    if not statements:
        raise SystemExit("No reviewed Debian sid fixes match this image")
    vex = {"@context": "https://openvex.dev/ns/v0.2.0", "@id": "urn:uuid:" + str(uuid.uuid4()),
           "author": "Dune self-host local image qualification", "version": 1,
           "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(), "statements": statements}
    Path(destination).write_text(json.dumps(vex, indent=2) + "\n")


if __name__ == "__main__":
    main()
