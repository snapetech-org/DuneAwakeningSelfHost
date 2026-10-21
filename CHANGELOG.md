# Changelog

All notable DASH changes are recorded here. Versions follow Semantic Versioning;
prereleases are used until the clean-host installation contract has been proven
by operators outside the development environment.

## [0.1.0-beta.1] - 2026-07-17

### Added

- First packaged DASH release for Linux x86_64 hosts with AVX2 and Docker
  Compose.
- Immutable commit-bound installer with persistent state, atomic activation,
  and no-restart rollback.
- Automated tag pipeline producing deterministic source, SPDX 2.3 SBOM,
  checksums, release manifest, in-toto/SLSA provenance, and GitHub artifact
  attestations.
- Reproducible experimental Linux server, Linux client, and Windows client
  loader archives with independent verification receipts.
- Ansible, Proxmox, cloud-init, Pelican/Pterodactyl, and active/passive HA
  deployment packages.
- Full ecosystem-parity dashboard, adaptive map lifecycle, governance,
  backups/restore drills, native offline recovery/teleport, and native
  character transfer backup/restore functionality.

### Distribution constraints

- Funcom server binaries, Steam package files, container images, game assets,
  credentials, saves, databases, and runtime logs are not distributed.
- The supported server target is Linux x86_64 with AVX2. Windows and macOS are
  operator workstations, not native server targets.

[0.1.0-beta.1]: https://github.com/snapetech/DuneAwakeningSelfHost/releases/tag/v0.1.0-beta.1
