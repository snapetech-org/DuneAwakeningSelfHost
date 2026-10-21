COMPOSE ?= docker compose
ENV_FILE ?= .env.example
CONTAINER ?= dune_server-deep-desert-1
TRACE_LOG ?= /tmp/brt-place-trace-lab.log
INCIDENT_KEY ?=
CAPSULE_FILE ?=
CAPSULE_OUTPUT ?= backups/operator-evidence/incident.signed.json
RELEASE_TAG ?= v$(shell tr -d '\r\n' < VERSION)
RELEASE_REF ?= $(shell git rev-parse HEAD)
RELEASE_DIR ?= dist/release

.PHONY: test-player-login-notices

.PHONY: validate compose-config check-compose-static-ips validate-research-build-tags secret-scan test-watch-maps test-admin-panel-safe-surfaces test-readme-screenshots readme-screenshots test-storage-cleanup test-character-slot-tool test-research-catalog test-discovery-tools test-admin-chat test-admin-grant-item test-operational-borrowing test-artificial-exchange test-artificial-exchange-service artificial-exchange-smoke artificial-exchange-bootstrap-catalog artificial-exchange-research-prices test-vehicle-fidelity-investigation gm-catalog gm-probe-preview gm-probe-safe research-catalog research-catalog-markdown surface-ledger surface-ledger-markdown discovery-queue binary-candidate-scores asset-reference-graph extract-build-surfaces diff-build-surfaces db-function-classifier fixture-runner knob-experiment capture-rmq-window diff-rmq-captures list-publishable preflight operational-identity-check operational-report operational-bundle verify-operational-bundle target-safety-audit reconcile-map-patch-overlays status storage-status storage-cleanup-dry-run standby-status failover-topology-status failover-bidirectional-audit sync-standby-files sync-standby-images promote-standby postgres-failover-seal postgres-cutback-proof rebuild-postgres-standby set-active-gameserver handoff-ready handoff-experiment summarize-handoff handoff-lab-config handoff-lab-up handoff-lab-seed handoff-lab-status handoff-lab-stop handoff-lab-remote-up handoff-lab-remote-status handoff-lab-remote-stop handoff-lab brt-dd-lab-config brt-dd-lab-images brt-dd-lab-up brt-dd-lab-seed brt-dd-lab-status brt-dd-lab-verify-config brt-dd-lab-logs brt-dd-lab-stop brt-dd-next-downtime-stage brt-dd-next-downtime-status test-brt-dd-tooling brt-dd-trace brt-dd-trace-stop brt-dd-live-preflight brt-dd-live-restart brt-dd-live-verify brt-dd-live-logs brt-dd-live-checklist failover-orchestrate failover-role-services cutover-check cutover-network-status browser-ping-proof browser-ping-diagnostics client-browser-ping-verifier client-browser-ping-verifier-external watch-browser-probe watch-client-browser-ping-log host-network-failover router-cutover install-dune-status-service check-steam-update backup-dry-run backup-state dd-pre-restore-backup restore-dry-run verify-backup start-full-warm-pool recover-survival recover-map watch-maps watch-maps-status install-map-watchdog-service install-artificial-exchange-service install-artificial-exchange-buyer-service install-artificial-exchange-populator-service install-full-farm-service install-daily-maintenance-timer full-world-partitions update-hagga-pois public-site-check public-site-package public-site-deploy public-site-verify admin-panel-deploy admin-panel-verify verify-local-state-ignored rabbitmq-cert-check rabbitmq-cert-generate rabbitmq-cert-stage rabbitmq-cert-install-staged rabbitmq-cert-recreate-stack build-linux-server-loader package-linux-server-loader smoke-linux-server-loader smoke-linux-server-loader-package-preflight build-linux-client-loader package-linux-client-loader smoke-linux-client-loader smoke-linux-client-loader-package-preflight build-windows-client-loader package-windows-client-loader smoke-windows-client-loader stage-windows-lua-runtime smoke-windows-client-loader-lua smoke-windows-client-loader-package-preflight smoke-windows-client-loader-full build-client-loaders package-client-loaders smoke-client-loaders smoke-client-loaders-full
.PHONY: summarize-linux-loader-scan summarize-linux-loader-xrefs summarize-linux-loader-anchors summarize-linux-client-loader-xrefs summarize-linux-client-loader-anchors validate-elf-signatures export-elf-signature-manifest test-linux-loader-scan-summary test-linux-loader-xrefs test-linux-loader-anchors test-elf-signatures test-elf-signature-manifest summarize-client-loader-scan summarize-client-loader-xrefs validate-client-pe-signatures export-client-pe-signature-manifest export-ue-anchor-env summarize-client-ue-anchors ue4ss-port-readiness plan-ue4ss-canary-env proton-proxy-candidates proton-dll-override-query proton-dll-override-set proton-dll-override-unset test-client-loader-scan-summary test-client-loader-xrefs test-client-pe-signatures test-client-pe-signature-manifest test-export-ue-anchor-env test-prepare-ue-anchor-canary test-canary-linux-server-loader test-plan-ue4ss-canary-env test-client-ue-anchors test-ue4ss-port-readiness test-loader-container-api-parity test-loader-scheduler-api-parity test-loader-modref-api-parity test-loader-mod-lifecycle-api-parity test-loader-unregister-api-parity test-loader-fname-api-parity test-loader-native-identity-parity test-loader-hook-path-alias-parity test-loader-custom-property-api-parity test-loader-compat-globals-api-parity test-loader-world-engine-api-parity test-loader-object-notify-api-parity test-loader-console-command-api-parity test-loader-anchor-group-parity test-loader-scan-preset-parity test-proton-proxy-candidates test-client-loader-tools test-public-ip-monitor install-public-ip-monitor
.PHONY: install-target-safety-audit-timer loader-build-toolchain-check loader-build-toolchain-install verify-loader-artifacts test-verify-loader-artifacts test-community-rewards community-rewards-check test-creator-canary test-moderation moderation-check test-base-creator test-base-retirement test-base-recovery-reminders test-gameplay-presets test-command-console test-federated-auth test-backup-encryption restore-drill restore-drill-status install-backup-restore-drill-timer test-restore-drill rabbitmq-restore-drill rabbitmq-restore-drill-status install-rabbitmq-restore-drill-timer test-rabbitmq-restore-drill slo-status slo-verify slo-metrics test-operational-slo capacity-status capacity-verify capacity-metrics test-capacity-intelligence test-maintenance-planner desired-state-status desired-state-verify desired-state-metrics test-desired-state change-intelligence-status change-intelligence-verify change-intelligence-metrics change-intelligence-plan change-intelligence-export-capsule change-intelligence-verify-capsule test-change-intelligence test-deployment-assurance test-update-readiness test-maintenance-outcomes test-hotfix-update-readiness test-cosmetics-admin test-install-release test-panel-command test-ha-packaging test-deployment-packaging
.PHONY: test-update-env-file test-update-steam-tool test-patch-dune-directory-caddy test-sietches test-configure-autoscaler-profile test-inventory-conflicts inventory-integrity-audit inventory-integrity-repair-preview test-cpu-affinity cpu-affinity-generate cpu-affinity-status cpu-affinity-preview test-host-tuning host-tuning-status host-tuning-plan test-admin-access-control admin-access-list test-outbound-webhooks webhooks-init test-offline-teleport test-character-backups webhooks-list test-discord-bot discord-bot-check install-discord-bot-service test-parity-diagnostics test-client-deployment test-windows-client-loader-package test-loader-package-reproducibility test-progression-admin test-player-life-recovery test-change-approvals test-change-contracts test-public-directory test-audit-ledger test-feature-readiness test-feature-readiness-history test-credential-lifecycle
.PHONY: test-public-ip-canary test-canary-autopilot test-operations-briefing test-operations-calendar test-alert-inbox test-restore-alert-inbox test-peer-watch test-release-packaging test-vendored-release-tools release-package release-verify
.PHONY: install-swap-warm-service test-swap-warm
.PHONY: test-control-plane-health install-autoheal-services
.PHONY: test-map-catalog test-map-network-autogrow

validate: test-control-plane-health
validate: test-map-catalog
validate: test-map-network-autogrow

validate: compose-config check-compose-static-ips validate-research-build-tags surface-ledger secret-scan test-update-env-file test-update-steam-tool test-patch-dune-directory-caddy test-watch-maps test-admin-panel-safe-surfaces test-readme-screenshots test-storage-cleanup test-public-ip-monitor test-sietches test-configure-autoscaler-profile test-inventory-conflicts test-cpu-affinity test-host-tuning test-admin-access-control test-change-approvals test-change-contracts test-public-directory test-feature-readiness test-feature-readiness-history test-credential-lifecycle test-audit-ledger test-federated-auth test-backup-encryption test-restore-drill test-rabbitmq-restore-drill test-operational-slo test-capacity-intelligence test-maintenance-planner test-desired-state test-change-intelligence test-deployment-assurance test-update-readiness test-maintenance-outcomes test-hotfix-update-readiness test-outbound-webhooks test-discord-bot test-community-rewards test-creator-canary test-moderation test-base-creator test-base-retirement test-base-recovery-reminders test-vehicle-retirement test-vehicle-recovery-reminders test-gameplay-presets test-command-console test-cosmetics-admin test-progression-admin test-player-life-recovery test-offline-teleport test-character-backups test-deployment-packaging test-parity-diagnostics test-client-deployment test-windows-client-loader-package test-loader-package-reproducibility test-character-slot-tool test-research-catalog test-discovery-tools test-admin-chat test-admin-grant-item test-smugglers-run-mp test-operational-borrowing test-artificial-exchange test-artificial-exchange-service test-vehicle-fidelity-investigation test-brt-dd-tooling public-site-check verify-local-state-ignored

readme-screenshots:
	python3 scripts/capture-readme-screenshots.py

test-readme-screenshots:
	python3 scripts/capture-readme-screenshots.py --check
validate: test-public-ip-canary test-canary-autopilot test-operations-briefing test-operations-calendar test-alert-inbox test-restore-alert-inbox test-peer-watch test-release-packaging test-vendored-release-tools test-swap-warm

test-release-packaging:
	python3 -W error::ResourceWarning -m unittest scripts/test-release-packaging.py
	python3 -m py_compile scripts/build-release.py scripts/finalize-release.py scripts/test-release-packaging.py
	bash -n scripts/build-release-assets.sh scripts/publish-github-release.sh scripts/install-release.sh

test-vendored-release-tools:
	./scripts/test-vendored-release-tools.sh

release-package:
	./scripts/build-release-assets.sh --version "$(RELEASE_TAG)" --ref "$(RELEASE_REF)" --output "$(RELEASE_DIR)"

release-verify:
	python3 scripts/finalize-release.py verify --version "$(RELEASE_TAG)" --ref "$(RELEASE_REF)" --asset-dir "$(RELEASE_DIR)"

test-outbound-webhooks:
	python3 -m unittest scripts/test-outbound-webhooks.py

webhooks-init:
	python3 scripts/outbound-webhooks.py init

webhooks-list:
	python3 scripts/outbound-webhooks.py list

test-discord-bot:
	python3 -m unittest scripts/test-discord-bot.py
	bash -n scripts/install-discord-bot-service.sh

test-community-rewards:
	python3 -m unittest scripts/test-community-rewards.py
	python3 -m unittest scripts/test-community-canary.py
	python3 -m unittest scripts/test-merge-community-engagement-policy.py
	python3 -m py_compile admin/community_rewards.py admin/community_canary.py scripts/test-community-canary.py

community-rewards-check:
	python3 -c 'import pathlib,sys; sys.path.insert(0,"admin"); import community_rewards; s=community_rewards.Store(pathlib.Path("backups/community-rewards/community.sqlite3"),pathlib.Path("config/community-rewards.json")); print(s.initialize())'

test-creator-canary:
	python3 -W error::ResourceWarning -m unittest scripts/test-creator-canary.py
	python3 -m py_compile admin/creator_canary.py scripts/test-creator-canary.py

test-public-ip-canary:
	python3 -W error::ResourceWarning -m unittest scripts/test-public-ip-canary.py
	python3 -m py_compile admin/public_ip_canary.py scripts/test-public-ip-canary.py

test-canary-autopilot:
	python3 -W error::ResourceWarning -m unittest scripts/test-canary-autopilot.py
	python3 -m py_compile admin/canary_autopilot.py scripts/test-canary-autopilot.py

test-operations-briefing:
	python3 -W error::ResourceWarning -m unittest scripts/test-operations-briefing.py
	python3 -m py_compile admin/operations_briefing.py scripts/test-operations-briefing.py

test-operations-calendar:
	python3 -W error::ResourceWarning -m unittest scripts/test-operations-calendar.py
	python3 -m py_compile admin/operations_calendar.py scripts/test-operations-calendar.py

test-alert-inbox:
	python3 -W error::ResourceWarning -m unittest scripts/test-alert-inbox.py
	python3 -m py_compile admin/alert_inbox.py scripts/test-alert-inbox.py
	./scripts/test-alert-inbox-backup.sh

test-restore-alert-inbox:
	./scripts/test-restore-alert-inbox.sh

test-peer-watch:
	python3 -W error::ResourceWarning -m unittest scripts/test-peer-watch.py
	python3 -m py_compile admin/peer_watch.py scripts/test-peer-watch.py
	./scripts/test-peer-watch-backup.sh

test-moderation:
	python3 -m unittest scripts/test-moderation.py

moderation-check:
	python3 -c 'import pathlib,sys; sys.path.insert(0,"admin"); import moderation; print(moderation.Store(pathlib.Path("backups/moderation/moderation.sqlite3")).initialize().status(limit=1))'

test-base-creator:
	python3 -m unittest scripts/test-base-creator.py

test-base-retirement:
	python3 -m unittest scripts/test-base-retirement.py

test-base-recovery-reminders:
	python3 scripts/test-base-recovery-reminders.py

test-player-login-notices:
	python3 scripts/test-player-login-notices.py

test-vehicle-retirement:
	python3 -m unittest scripts/test-vehicle-retirement.py

test-vehicle-recovery-reminders:
	python3 scripts/test-vehicle-recovery-reminders.py

.PHONY: test-vehicle-retirement test-vehicle-recovery-reminders

test-gameplay-presets:
	python3 -m unittest scripts/test-gameplay-presets.py

test-command-console:
	python3 -m unittest scripts/test-command-console.py

test-cosmetics-admin:
	python3 -m unittest scripts/test-cosmetics-admin.py

test-progression-admin:
	python3 -m unittest scripts/test-progression-admin.py
	python3 -m py_compile admin/progression_admin.py scripts/test-progression-admin.py

test-player-life-recovery:
	python3 -W error::ResourceWarning -m unittest scripts/test-player-life-recovery.py
	python3 -m py_compile admin/player_life_recovery.py scripts/test-player-life-recovery.py

test-offline-teleport:
	python3 -W error::ResourceWarning -m unittest scripts/test-offline-teleport.py
	python3 -m py_compile admin/offline_teleport.py scripts/test-offline-teleport.py

test-character-backups:
	python3 -W error::ResourceWarning scripts/test-character-backups.py
	python3 -m py_compile admin/character_backups.py scripts/test-character-backups.py

test-change-approvals:
	python3 -W error::ResourceWarning -m unittest scripts/test-change-approvals.py
	python3 -m py_compile admin/change_approvals.py scripts/test-change-approvals.py

test-change-contracts:
	python3 -W error::ResourceWarning -m unittest scripts/test-change-contracts.py
	python3 -m py_compile admin/change_contracts.py scripts/test-change-contracts.py

test-public-directory:
	python3 -W error::ResourceWarning -m unittest scripts/test-public-directory.py
	python3 -m py_compile admin/public_directory.py public-site/scripts/build-federated-directory.py public-site/scripts/configure-federated-directory-sources.py scripts/test-public-directory.py
	node --check public-site/directory/directory.js

test-feature-readiness:
	python3 -W error::ResourceWarning -m unittest scripts/test-feature-readiness.py
	python3 -m py_compile admin/feature_readiness.py scripts/test-feature-readiness.py
	python3 -m json.tool config/feature-readiness.json >/dev/null

test-feature-readiness-history:
	python3 -W error::ResourceWarning -m unittest scripts/test-feature-readiness-history.py
	python3 -m py_compile admin/feature_readiness_history.py scripts/test-feature-readiness-history.py

test-credential-lifecycle:
	python3 -W error::ResourceWarning -m unittest scripts/test-credential-lifecycle.py
	python3 -m py_compile admin/credential_lifecycle.py scripts/test-credential-lifecycle.py
	python3 -m json.tool config/credential-lifecycle.json >/dev/null

test-audit-ledger:
	python3 -W error::ResourceWarning -m unittest scripts/test-audit-ledger.py
	python3 -m py_compile admin/audit_ledger.py scripts/snapshot-audit-ledger.py scripts/test-audit-ledger.py

test-install-release:
	./scripts/test-install-release.sh

test-panel-command:
	./scripts/test-panel-command.sh

test-ha-packaging:
	./scripts/test-ha-packaging.sh

test-deployment-packaging:
	./scripts/test-deployment-packaging.sh

test-parity-diagnostics:
	python3 -m unittest scripts/test-parity-diagnostics.py
	python3 -m py_compile scripts/build-cvar-catalog.py scripts/query-cvar-catalog.py scripts/remote-targets.py scripts/game-peer-diagnostics.py

test-client-deployment:
	python3 -m unittest scripts/test-client-deployment.py
	python3 -m py_compile scripts/client-deployment.py

test-windows-client-loader-package:
	python3 -m unittest scripts/test-windows-client-loader-package.py
	python3 -m unittest scripts/test-verify-loader-artifacts.py
	bash -n scripts/package-windows-client-loader.sh scripts/package-linux-client-loader.sh scripts/package-linux-server-loader.sh scripts/loader-package-common.sh

test-loader-package-reproducibility:
	python3 -m unittest scripts/test-loader-package-reproducibility.py

test-federated-auth:
	python3 -m unittest scripts/test-federated-auth.py

test-backup-encryption:
	./scripts/test-backup-encryption.sh

test-restore-drill:
	python3 scripts/test-restore-drill.py
	bash -n scripts/install-backup-restore-drill-timer.sh
	python3 -m py_compile admin/restore_drill.py scripts/backup-restore-drill.py

test-rabbitmq-restore-drill:
	python3 -W error::ResourceWarning -m unittest scripts/test-rabbitmq-restore-drill.py
	bash -n scripts/install-rabbitmq-restore-drill-timer.sh
	python3 -m py_compile admin/rabbitmq_restore_drill.py scripts/rabbitmq-restore-drill.py scripts/test-rabbitmq-restore-drill.py

test-operational-slo:
	python3 scripts/test-operational-slo.py
	python3 -m py_compile admin/operational_slo.py scripts/operational-slo.py

test-capacity-intelligence:
	python3 scripts/test-capacity-intelligence.py
	python3 -m py_compile admin/capacity_intelligence.py scripts/capacity-intelligence.py

test-maintenance-planner:
	python3 scripts/test-maintenance-planner.py
	python3 -m py_compile admin/maintenance_planner.py scripts/test-maintenance-planner.py
	python3 -m json.tool config/maintenance-planner.json >/dev/null

test-desired-state:
	python3 scripts/test-desired-state.py
	python3 -m py_compile admin/desired_state.py scripts/desired-state.py

test-change-intelligence:
	python3 scripts/test-change-intelligence.py
	python3 -m py_compile admin/change_intelligence.py scripts/change-intelligence.py scripts/test-change-intelligence.py

test-deployment-assurance:
	python3 scripts/test-deployment-assurance.py
	python3 -m py_compile admin/deployment_assurance.py scripts/deployment-assurance.py scripts/test-deployment-assurance.py
	bash -n scripts/assured-control-plane-deploy.sh scripts/push-assured-control-plane.sh

test-update-readiness:
	python3 scripts/test-update-readiness.py
	python3 -m unittest scripts/test-daily-maintenance-scheduler.py
	python3 -m py_compile admin/update_readiness.py scripts/test-update-readiness.py
	bash -n scripts/schedule-daily-maintenance.sh scripts/restart-target.sh

test-maintenance-outcomes:
	python3 scripts/test-maintenance-outcomes.py
	python3 -m py_compile admin/maintenance_outcomes.py scripts/test-maintenance-outcomes.py

test-hotfix-update-readiness:
	bash scripts/test-hotfix-update-readiness.sh
	bash -n scripts/hotfix-auto-update-and-restart.sh scripts/test-hotfix-update-readiness.sh

change-intelligence-status:
	./scripts/change-intelligence.py status

change-intelligence-verify:
	./scripts/change-intelligence.py verify

change-intelligence-metrics:
	./scripts/change-intelligence.py metrics

change-intelligence-plan:
	@test -n "$(INCIDENT_KEY)" || { echo 'INCIDENT_KEY is required' >&2; exit 2; }
	./scripts/change-intelligence.py plan --incident-key "$(INCIDENT_KEY)"

change-intelligence-export-capsule:
	@test -n "$(INCIDENT_KEY)" || { echo 'INCIDENT_KEY is required' >&2; exit 2; }
	./scripts/change-intelligence.py export-capsule --incident-key "$(INCIDENT_KEY)" --output "$(CAPSULE_OUTPUT)"

change-intelligence-verify-capsule:
	@test -n "$(CAPSULE_FILE)" || { echo 'CAPSULE_FILE is required' >&2; exit 2; }
	./scripts/change-intelligence.py verify-capsule --capsule-file "$(CAPSULE_FILE)"

discord-bot-check:
	./scripts/discord-bot.py --env-file $(ENV_FILE) --check

install-discord-bot-service:
	./scripts/install-discord-bot-service.sh $(ENV_FILE)

build-linux-server-loader:
	./scripts/build-linux-server-loader.sh

loader-build-toolchain-check:
	./scripts/ensure-loader-build-toolchain.sh --check

loader-build-toolchain-install:
	./scripts/ensure-loader-build-toolchain.sh --install

verify-loader-artifacts:
	./scripts/verify-loader-artifacts.py

test-verify-loader-artifacts:
	python3 -m unittest scripts/test-verify-loader-artifacts.py

package-linux-server-loader:
	./scripts/package-linux-server-loader.sh

smoke-linux-server-loader:
	./scripts/smoke-linux-server-loader.sh

smoke-linux-server-loader-package-preflight:
	./scripts/smoke-linux-server-loader-package-preflight.sh

summarize-linux-loader-scan:
	@if [ -z "$(LOADER_SCAN_LOG)" ]; then \
		echo "Usage: make summarize-linux-loader-scan LOADER_SCAN_LOG=/path/to/loader.log"; \
		exit 2; \
	fi
	./scripts/summarize-linux-loader-scan.py "$(LOADER_SCAN_LOG)"

summarize-linux-loader-xrefs:
	@if [ -z "$(SERVER_BINARY)" ]; then \
		echo "Usage: make summarize-linux-loader-xrefs SERVER_BINARY=/path/to/DuneSandboxServer-Linux-Shipping LOADER_SCAN_LOG=/path/to/loader.log [CATEGORY=brt] [NAME=PerformCanBePlaced]"; \
		exit 2; \
	fi
	@if [ -z "$(LOADER_SCAN_LOG)" ]; then \
		echo "Usage: make summarize-linux-loader-xrefs SERVER_BINARY=/path/to/DuneSandboxServer-Linux-Shipping LOADER_SCAN_LOG=/path/to/loader.log [CATEGORY=brt] [NAME=PerformCanBePlaced]"; \
		exit 2; \
	fi
	./scripts/summarize-linux-loader-xrefs.py "$(SERVER_BINARY)" --loader-log "$(LOADER_SCAN_LOG)" $(if $(CATEGORY),--category "$(CATEGORY)",) $(if $(NAME),--name "$(NAME)",)

summarize-linux-loader-anchors:
	@if [ -z "$(SERVER_BINARY)" ]; then \
		echo "Usage: make summarize-linux-loader-anchors SERVER_BINARY=/path/to/DuneSandboxServer-Linux-Shipping LOADER_SCAN_LOG=/path/to/loader.log [CATEGORY=brt] [NAME=PerformCanBePlaced]"; \
		exit 2; \
	fi
	@if [ -z "$(LOADER_SCAN_LOG)" ]; then \
		echo "Usage: make summarize-linux-loader-anchors SERVER_BINARY=/path/to/DuneSandboxServer-Linux-Shipping LOADER_SCAN_LOG=/path/to/loader.log [CATEGORY=brt] [NAME=PerformCanBePlaced]"; \
		exit 2; \
	fi
	./scripts/summarize-linux-loader-anchors.py "$(SERVER_BINARY)" --loader-log "$(LOADER_SCAN_LOG)" $(if $(CATEGORY),--category "$(CATEGORY)",) $(if $(NAME),--name "$(NAME)",)

summarize-linux-client-loader-xrefs:
	@if [ -z "$(CLIENT_BINARY)" ]; then \
		echo "Usage: make summarize-linux-client-loader-xrefs CLIENT_BINARY=/path/to/DuneSandbox-Linux-Shipping CLIENT_LOADER_LOG=/path/to/client-loader.log [CATEGORY=cheat] [NAME=CheatManager] [CLIENT_EXE_SUBSTRING=DuneSandbox]"; \
		exit 2; \
	fi
	@if [ -z "$(CLIENT_LOADER_LOG)" ]; then \
		echo "Usage: make summarize-linux-client-loader-xrefs CLIENT_BINARY=/path/to/DuneSandbox-Linux-Shipping CLIENT_LOADER_LOG=/path/to/client-loader.log [CATEGORY=cheat] [NAME=CheatManager] [CLIENT_EXE_SUBSTRING=DuneSandbox]"; \
		exit 2; \
	fi
	./scripts/summarize-linux-loader-xrefs.py "$(CLIENT_BINARY)" --loader-log "$(CLIENT_LOADER_LOG)" --exe-substring "$(or $(CLIENT_EXE_SUBSTRING),DuneSandbox)" $(if $(CATEGORY),--category "$(CATEGORY)",) $(if $(NAME),--name "$(NAME)",)

summarize-linux-client-loader-anchors:
	@if [ -z "$(CLIENT_BINARY)" ]; then \
		echo "Usage: make summarize-linux-client-loader-anchors CLIENT_BINARY=/path/to/DuneSandbox-Linux-Shipping CLIENT_LOADER_LOG=/path/to/client-loader.log [CATEGORY=cheat] [NAME=CheatManager] [CLIENT_EXE_SUBSTRING=DuneSandbox]"; \
		exit 2; \
	fi
	@if [ -z "$(CLIENT_LOADER_LOG)" ]; then \
		echo "Usage: make summarize-linux-client-loader-anchors CLIENT_BINARY=/path/to/DuneSandbox-Linux-Shipping CLIENT_LOADER_LOG=/path/to/client-loader.log [CATEGORY=cheat] [NAME=CheatManager] [CLIENT_EXE_SUBSTRING=DuneSandbox]"; \
		exit 2; \
	fi
	./scripts/summarize-linux-loader-anchors.py "$(CLIENT_BINARY)" --loader-log "$(CLIENT_LOADER_LOG)" --exe-substring "$(or $(CLIENT_EXE_SUBSTRING),DuneSandbox)" $(if $(CATEGORY),--category "$(CATEGORY)",) $(if $(NAME),--name "$(NAME)",)

validate-elf-signatures:
	@if [ -z "$(ELF_BINARY)" ]; then \
		echo "Usage: make validate-elf-signatures ELF_BINARY=/path/to/ELF LOADER_SCAN_LOG=/path/to/loader.log [CATEGORY=brt] [NAME=PerformCanBePlaced]"; \
		echo "   or: make validate-elf-signatures ELF_BINARY=/path/to/ELF ELF_SIGNATURE_MANIFEST=build/linux-server-loader/elf-signature-manifest.json [IGNORE_EXPECTED_OFFSETS=1]"; \
		exit 2; \
	fi
	@if [ -z "$(LOADER_SCAN_LOG)" ] && [ -z "$(ELF_SIGNATURE_MANIFEST)" ]; then \
		echo "Usage: make validate-elf-signatures ELF_BINARY=/path/to/ELF LOADER_SCAN_LOG=/path/to/loader.log [CATEGORY=brt] [NAME=PerformCanBePlaced]"; \
		echo "   or: make validate-elf-signatures ELF_BINARY=/path/to/ELF ELF_SIGNATURE_MANIFEST=build/linux-server-loader/elf-signature-manifest.json [IGNORE_EXPECTED_OFFSETS=1]"; \
		exit 2; \
	fi
	./scripts/validate-elf-signatures.py "$(ELF_BINARY)" $(if $(LOADER_SCAN_LOG),--loader-log "$(LOADER_SCAN_LOG)",) $(if $(ELF_SIGNATURE_MANIFEST),--manifest-json "$(ELF_SIGNATURE_MANIFEST)",) $(if $(IGNORE_EXPECTED_OFFSETS),--ignore-expected-offsets,) $(if $(ELF_EXE_SUBSTRING),--exe-substring "$(ELF_EXE_SUBSTRING)",) $(if $(CATEGORY),--category "$(CATEGORY)",) $(if $(NAME),--name "$(NAME)",)

export-elf-signature-manifest:
	@if [ -z "$(ELF_BINARY)" ]; then \
		echo "Usage: make export-elf-signature-manifest ELF_BINARY=/path/to/ELF LOADER_SCAN_LOG=/path/to/loader.log [ELF_TARGET_LOADER=server|linux-client] [CATEGORY=brt] [NAME=PerformCanBePlaced] [SIGNATURE_FORMAT=json|env|signatures|markdown] [OUTPUT=path]"; \
		exit 2; \
	fi
	@if [ -z "$(LOADER_SCAN_LOG)" ]; then \
		echo "Usage: make export-elf-signature-manifest ELF_BINARY=/path/to/ELF LOADER_SCAN_LOG=/path/to/loader.log [ELF_TARGET_LOADER=server|linux-client] [CATEGORY=brt] [NAME=PerformCanBePlaced] [SIGNATURE_FORMAT=json|env|signatures|markdown] [OUTPUT=path]"; \
		exit 2; \
	fi
	./scripts/export-elf-signature-manifest.py "$(ELF_BINARY)" --loader-log "$(LOADER_SCAN_LOG)" --target-loader "$(or $(ELF_TARGET_LOADER),server)" --format "$(or $(SIGNATURE_FORMAT),markdown)" $(if $(ELF_EXE_SUBSTRING),--exe-substring "$(ELF_EXE_SUBSTRING)",) $(if $(CATEGORY),--category "$(CATEGORY)",) $(if $(NAME),--name "$(NAME)",) $(if $(OUTPUT),--output "$(OUTPUT)",)

test-linux-loader-scan-summary:
	python3 -m unittest scripts/test-linux-loader-scan-summary.py

test-linux-loader-xrefs:
	python3 -m unittest scripts/test-linux-loader-xrefs.py

test-linux-loader-anchors:
	python3 -m unittest scripts/test-linux-loader-anchors.py

test-elf-signatures:
	python3 -m unittest scripts/test-elf-signatures.py

test-elf-signature-manifest:
	python3 -m unittest scripts/test-elf-signature-manifest.py

summarize-client-loader-scan:
	@if [ -z "$(CLIENT_LOADER_LOG)" ]; then \
		echo "Usage: make summarize-client-loader-scan CLIENT_LOADER_LOG=/path/to/client-loader.log"; \
		exit 2; \
	fi
	./scripts/summarize-client-loader-scan.py "$(CLIENT_LOADER_LOG)"

summarize-client-loader-xrefs:
	@if [ -z "$(CLIENT_BINARY)" ]; then \
		echo "Usage: make summarize-client-loader-xrefs CLIENT_BINARY=/path/to/DuneSandbox-Win64-Shipping.exe CLIENT_LOADER_LOG=/path/to/client-loader.log [CATEGORY=cheat] [NAME=CheatManager]"; \
		exit 2; \
	fi
	@if [ -z "$(CLIENT_LOADER_LOG)" ]; then \
		echo "Usage: make summarize-client-loader-xrefs CLIENT_BINARY=/path/to/DuneSandbox-Win64-Shipping.exe CLIENT_LOADER_LOG=/path/to/client-loader.log [CATEGORY=cheat] [NAME=CheatManager]"; \
		exit 2; \
	fi
	./scripts/summarize-client-loader-xrefs.py "$(CLIENT_BINARY)" --loader-log "$(CLIENT_LOADER_LOG)" $(if $(CATEGORY),--category "$(CATEGORY)",) $(if $(NAME),--name "$(NAME)",)

validate-client-pe-signatures:
	@if [ -z "$(CLIENT_BINARY)" ]; then \
		echo "Usage: make validate-client-pe-signatures CLIENT_BINARY=/path/to/DuneSandbox-Win64-Shipping.exe CLIENT_LOADER_LOG=/path/to/client-loader.log [CATEGORY=brt] [NAME=PerformCanBePlaced]"; \
		echo "   or: make validate-client-pe-signatures CLIENT_BINARY=/path/to/DuneSandbox-Win64-Shipping.exe CLIENT_SIGNATURE_MANIFEST=build/windows-client-loader/client-pe-signature-manifest.json [IGNORE_EXPECTED_OFFSETS=1]"; \
		exit 2; \
	fi
	@if [ -z "$(CLIENT_LOADER_LOG)" ] && [ -z "$(CLIENT_SIGNATURE_MANIFEST)" ]; then \
		echo "Usage: make validate-client-pe-signatures CLIENT_BINARY=/path/to/DuneSandbox-Win64-Shipping.exe CLIENT_LOADER_LOG=/path/to/client-loader.log [CATEGORY=brt] [NAME=PerformCanBePlaced]"; \
		echo "   or: make validate-client-pe-signatures CLIENT_BINARY=/path/to/DuneSandbox-Win64-Shipping.exe CLIENT_SIGNATURE_MANIFEST=build/windows-client-loader/client-pe-signature-manifest.json [IGNORE_EXPECTED_OFFSETS=1]"; \
		exit 2; \
	fi
	./scripts/validate-client-pe-signatures.py "$(CLIENT_BINARY)" $(if $(CLIENT_LOADER_LOG),--loader-log "$(CLIENT_LOADER_LOG)",) $(if $(CLIENT_SIGNATURE_MANIFEST),--manifest-json "$(CLIENT_SIGNATURE_MANIFEST)",) $(if $(IGNORE_EXPECTED_OFFSETS),--ignore-expected-offsets,) $(if $(CATEGORY),--category "$(CATEGORY)",) $(if $(NAME),--name "$(NAME)",)

export-client-pe-signature-manifest:
	@if [ -z "$(CLIENT_BINARY)" ]; then \
		echo "Usage: make export-client-pe-signature-manifest CLIENT_BINARY=/path/to/DuneSandbox-Win64-Shipping.exe CLIENT_LOADER_LOG=/path/to/client-loader.log [CATEGORY=brt] [NAME=PerformCanBePlaced] [SIGNATURE_FORMAT=json|env|signatures|markdown] [OUTPUT=path]"; \
		exit 2; \
	fi
	@if [ -z "$(CLIENT_LOADER_LOG)" ]; then \
		echo "Usage: make export-client-pe-signature-manifest CLIENT_BINARY=/path/to/DuneSandbox-Win64-Shipping.exe CLIENT_LOADER_LOG=/path/to/client-loader.log [CATEGORY=brt] [NAME=PerformCanBePlaced] [SIGNATURE_FORMAT=json|env|signatures|markdown] [OUTPUT=path]"; \
		exit 2; \
	fi
	./scripts/export-client-pe-signature-manifest.py "$(CLIENT_BINARY)" --loader-log "$(CLIENT_LOADER_LOG)" --format "$(or $(SIGNATURE_FORMAT),markdown)" $(if $(CATEGORY),--category "$(CATEGORY)",) $(if $(NAME),--name "$(NAME)",) $(if $(OUTPUT),--output "$(OUTPUT)",)

export-ue-anchor-env:
	@if [ -z "$(CLIENT_LOADER_LOG)" ]; then \
		echo "Usage: make export-ue-anchor-env CLIENT_LOADER_LOG=/path/to/loader.log [CLIENT_LOADER=server|win-client|client|linux-client] [UE_ANCHOR_PLATFORM=auto|server|windows|linux] [NAME=FNamePool]"; \
		exit 2; \
	fi
	./scripts/export-ue-anchor-env.py "$(CLIENT_LOADER_LOG)" $(if $(CLIENT_LOADER),--loader "$(CLIENT_LOADER)",) $(if $(UE_ANCHOR_PLATFORM),--platform "$(UE_ANCHOR_PLATFORM)",) $(if $(NAME),--name "$(NAME)",)

summarize-client-ue-anchors:
	@if [ -z "$(CLIENT_LOADER_LOG)" ]; then \
		echo "Usage: make summarize-client-ue-anchors CLIENT_LOADER_LOG=/path/to/client-loader.log"; \
		exit 2; \
	fi
	./scripts/summarize-client-ue-anchors.py "$(CLIENT_LOADER_LOG)"

ue4ss-port-readiness:
	@if [ -z "$(CLIENT_LOADER_LOG)" ] && [ -z "$(LOADER_SCAN_LOG)" ]; then \
		echo "Usage: make ue4ss-port-readiness CLIENT_LOADER_LOG=/path/to/client-loader.log [CLIENT_SIGNATURE_VALIDATION=/path/to/validation.json] [CLIENT_LOADER=win-client|client|linux-client]"; \
		echo "   or: make ue4ss-port-readiness LOADER_SCAN_LOG=/path/to/server-loader.log"; \
		exit 2; \
	fi
	./scripts/ue4ss-port-readiness.py $(if $(CLIENT_LOADER_LOG),--client-log "$(CLIENT_LOADER_LOG)",) $(if $(LOADER_SCAN_LOG),--server-log "$(LOADER_SCAN_LOG)",) $(if $(CLIENT_LOADER),--loader "$(CLIENT_LOADER)",) $(if $(CLIENT_SIGNATURE_VALIDATION),--signature-validation-json "$(CLIENT_SIGNATURE_VALIDATION)",)

plan-ue4ss-canary-env:
	@if [ -z "$(UE4SS_CANARY_PLATFORM)" ]; then \
		echo "Usage: make plan-ue4ss-canary-env UE4SS_CANARY_PLATFORM=server|linux-client|windows CLIENT_LOADER_LOG=/path/to/client.log [CLIENT_SIGNATURE_VALIDATION=/path/to/validation.json] [UE4SS_MAX_STAGE=read-only|hook-probe|live-hook|lua-dispatch] [UE4SS_LIVE_CALL_LOG_LIMIT=8]"; \
		echo "   or: make plan-ue4ss-canary-env UE4SS_CANARY_PLATFORM=server LOADER_SCAN_LOG=/path/to/server.log"; \
		exit 2; \
	fi
	./scripts/plan-ue4ss-canary-env.py --platform "$(UE4SS_CANARY_PLATFORM)" $(if $(CLIENT_LOADER_LOG),--client-log "$(CLIENT_LOADER_LOG)",) $(if $(LOADER_SCAN_LOG),--server-log "$(LOADER_SCAN_LOG)",) $(if $(CLIENT_LOADER),--loader "$(CLIENT_LOADER)",) $(if $(CLIENT_SIGNATURE_VALIDATION),--signature-validation-json "$(CLIENT_SIGNATURE_VALIDATION)",) --max-stage "$(or $(UE4SS_MAX_STAGE),read-only)" $(if $(UE4SS_LIVE_CALL_LOG_LIMIT),--live-call-log-limit "$(UE4SS_LIVE_CALL_LOG_LIMIT)",)

proton-proxy-candidates:
	@if [ -z "$(CLIENT_EXE)" ]; then \
		echo "Usage: make proton-proxy-candidates CLIENT_EXE=/path/to/DuneSandbox-Win64-Shipping.exe"; \
		exit 2; \
	fi
	./scripts/proton-proxy-candidates.py "$(CLIENT_EXE)"

proton-dll-override-query:
	./scripts/proton-dll-override-control.sh --query

proton-dll-override-set:
	./scripts/proton-dll-override-control.sh --set

proton-dll-override-unset:
	./scripts/proton-dll-override-control.sh --unset

test-client-loader-scan-summary:
	python3 -m unittest scripts/test-client-loader-scan-summary.py

test-client-loader-xrefs:
	python3 -m unittest scripts/test-client-loader-xrefs.py

test-client-pe-signatures:
	python3 -m unittest scripts/test-client-pe-signatures.py

test-client-pe-signature-manifest:
	python3 -m unittest scripts/test-client-pe-signature-manifest.py

test-export-ue-anchor-env:
	python3 -m unittest scripts/test-export-ue-anchor-env.py

test-prepare-ue-anchor-canary:
	python3 -m unittest scripts/test-prepare-ue-anchor-canary.py

test-canary-linux-server-loader:
	python3 -m unittest scripts/test-canary-linux-server-loader.py

test-plan-ue4ss-canary-env:
	python3 -m unittest scripts/test-plan-ue4ss-canary-env.py

test-client-ue-anchors:
	python3 -m unittest scripts/test-client-ue-anchors.py

test-ue4ss-port-readiness:
	python3 -m unittest scripts/test-ue4ss-port-readiness.py

test-loader-container-api-parity:
	python3 -m unittest scripts/test-loader-container-api-parity.py

test-loader-scheduler-api-parity:
	python3 -m unittest scripts/test-loader-scheduler-api-parity.py

test-loader-modref-api-parity:
	python3 -m unittest scripts/test-loader-modref-api-parity.py

test-loader-mod-lifecycle-api-parity:
	python3 -m unittest scripts/test-loader-mod-lifecycle-api-parity.py

test-loader-unregister-api-parity:
	python3 -m unittest scripts/test-loader-unregister-api-parity.py

test-loader-fname-api-parity:
	python3 -m unittest scripts/test-loader-fname-api-parity.py

test-loader-native-identity-parity:
	python3 -m unittest scripts/test-loader-native-identity-parity.py

test-loader-hook-path-alias-parity:
	python3 -m unittest scripts/test-loader-hook-path-alias-parity.py

test-loader-custom-property-api-parity:
	python3 -m unittest scripts/test-loader-custom-property-api-parity.py

test-loader-compat-globals-api-parity:
	python3 -m unittest scripts/test-loader-compat-globals-api-parity.py

test-loader-world-engine-api-parity:
	python3 -m unittest scripts/test-loader-world-engine-api-parity.py

test-loader-object-notify-api-parity:
	python3 -m unittest scripts/test-loader-object-notify-api-parity.py

test-loader-console-command-api-parity:
	python3 -m unittest scripts/test-loader-console-command-api-parity.py

test-loader-anchor-group-parity:
	python3 -m unittest scripts/test-loader-anchor-group-parity.py

test-loader-scan-preset-parity:
	python3 -m unittest scripts/test-loader-scan-preset-parity.py

test-proton-proxy-candidates:
	python3 -m unittest scripts/test-proton-proxy-candidates.py

test-client-loader-tools: test-client-loader-scan-summary test-linux-loader-xrefs test-linux-loader-anchors test-elf-signatures test-elf-signature-manifest test-client-loader-xrefs test-client-pe-signatures test-client-pe-signature-manifest test-export-ue-anchor-env test-prepare-ue-anchor-canary test-canary-linux-server-loader test-plan-ue4ss-canary-env test-client-ue-anchors test-ue4ss-port-readiness test-loader-container-api-parity test-loader-scheduler-api-parity test-loader-modref-api-parity test-loader-mod-lifecycle-api-parity test-loader-unregister-api-parity test-loader-fname-api-parity test-loader-native-identity-parity test-loader-hook-path-alias-parity test-loader-custom-property-api-parity test-loader-compat-globals-api-parity test-loader-world-engine-api-parity test-loader-object-notify-api-parity test-loader-console-command-api-parity test-loader-anchor-group-parity test-loader-scan-preset-parity test-proton-proxy-candidates test-verify-loader-artifacts

build-linux-client-loader:
	./scripts/build-linux-client-loader.sh

package-linux-client-loader:
	./scripts/package-linux-client-loader.sh

smoke-linux-client-loader:
	./scripts/smoke-linux-client-loader.sh

smoke-linux-client-loader-package-preflight:
	./scripts/smoke-linux-client-loader-package-preflight.sh

build-windows-client-loader:
	./scripts/build-windows-client-loader.sh

package-windows-client-loader:
	./scripts/package-windows-client-loader.sh

smoke-windows-client-loader:
	./scripts/smoke-windows-client-loader.sh

stage-windows-lua-runtime:
	./scripts/stage-windows-lua-runtime.sh

smoke-windows-client-loader-lua:
	./scripts/smoke-windows-client-loader-lua.sh

smoke-windows-client-loader-package-preflight:
	./scripts/smoke-windows-client-loader-package-preflight.sh

smoke-windows-client-loader-full: smoke-windows-client-loader smoke-windows-client-loader-lua smoke-windows-client-loader-package-preflight

build-client-loaders: build-linux-client-loader build-windows-client-loader

package-client-loaders: package-linux-client-loader package-windows-client-loader

smoke-client-loaders: smoke-linux-client-loader smoke-windows-client-loader

smoke-client-loaders-full: smoke-linux-client-loader smoke-linux-client-loader-package-preflight smoke-windows-client-loader-full

validate-research-build-tags:
	./scripts/validate-research-build-tags.sh $(ENV_FILE)

preflight:
	./scripts/preflight.sh

operational-identity-check:
	./scripts/check-operational-identity.sh $(ENV_FILE)

operational-report:
	./scripts/operational-report.sh $(ENV_FILE) $(REPORT_FILE)

operational-bundle:
	./scripts/operational-bundle.sh $(ENV_FILE) $(BUNDLE_FILE)

verify-operational-bundle:
	@if [ -z "$(BUNDLE_FILE)" ]; then \
		echo "Usage: make verify-operational-bundle BUNDLE_FILE=backups/operational-bundle-<id>.tgz"; \
		exit 2; \
	fi
	./scripts/verify-operational-bundle.sh $(BUNDLE_FILE)

target-safety-audit:
	./scripts/live-target-safety-audit.sh $(ENV_FILE)

reconcile-map-patch-overlays:
	./scripts/reconcile-map-patch-overlays.sh $(ENV_FILE) $(if $(EXECUTE),--execute,)

install-target-safety-audit-timer:
	./scripts/install-target-safety-audit-timer.sh $(ENV_FILE)

rabbitmq-cert-check:
	./scripts/check-rabbitmq-cert-sans.sh $(ENV_FILE)

rabbitmq-cert-generate:
	./scripts/generate-rabbitmq-cert.sh $(ENV_FILE)

rabbitmq-cert-stage:
	./scripts/stage-rabbitmq-cert.sh $(ENV_FILE)

rabbitmq-cert-install-staged:
	./scripts/install-staged-rabbitmq-cert.sh $(ENV_FILE) $(REMOTE)

rabbitmq-cert-recreate-stack:
	./scripts/recreate-rabbitmq-tls-stack.sh $(ENV_FILE)

status:
	./scripts/status.sh $(ENV_FILE)

storage-status:
	./scripts/storage-cleanup.sh --env-file $(ENV_FILE) status

storage-cleanup-dry-run:
	./scripts/storage-cleanup.sh --env-file $(ENV_FILE) cleanup --dry-run

standby-status:
	./scripts/standby-status.sh $(ENV_FILE) $(REMOTE) $(ROOT)

failover-topology-status:
	./scripts/failover-topology-status.sh $(ENV_FILE)

failover-bidirectional-audit:
	./scripts/failover-bidirectional-audit.sh $(ENV_FILE)

sync-standby-files:
	./scripts/sync-standby-files.sh $(ENV_FILE) $(REMOTE)

sync-standby-images:
	./scripts/sync-standby-images.sh $(ENV_FILE) $(REMOTE)

promote-standby:
	./scripts/promote-standby.sh $(ENV_FILE) $(REMOTE)

postgres-failover-seal:
	./scripts/postgres-failover-seal.sh $(ENV_FILE) $(REMOTE) $(SEAL_FILE)

postgres-cutback-proof:
	./scripts/postgres-cutback-proof.sh $(ENV_FILE) $(TARGET) $(ROOT) $(SEAL_FILE)

rebuild-postgres-standby:
	./scripts/rebuild-postgres-standby.sh $(ENV_FILE) $(TARGET) $(ROOT)

set-active-gameserver:
	./scripts/set-active-gameserver.sh $(ENV_FILE) $(ACTIVE_HOST) $(ACTIVE_IP) $(STANDBY_HOST) $(STANDBY_IP) $(APPLY)

handoff-ready:
	./scripts/handoff-ready.sh $(ENV_FILE) $(ROLE)

handoff-experiment:
	./scripts/handoff-experiment.sh $(ENV_FILE) $(ROLE) $(APPLY)

summarize-handoff:
	./scripts/summarize-handoff-experiment.sh $(CAPTURE_DIR)

handoff-lab-config:
	./scripts/handoff-lab.sh config $(ENV_FILE)

handoff-lab-up:
	./scripts/handoff-lab.sh up $(ENV_FILE)

handoff-lab-seed:
	./scripts/handoff-lab.sh seed $(ENV_FILE)

handoff-lab-status:
	./scripts/handoff-lab.sh status $(ENV_FILE)

handoff-lab-settled-status:
	./scripts/handoff-lab.sh settled-status $(ENV_FILE)

handoff-lab-stop:
	./scripts/handoff-lab.sh stop $(ENV_FILE)

handoff-lab-remote-up:
	./scripts/handoff-lab.sh remote-up $(ENV_FILE) $(REMOTE)

handoff-lab-remote-status:
	./scripts/handoff-lab.sh remote-status $(ENV_FILE) $(REMOTE)

handoff-lab-remote-settled-status:
	./scripts/handoff-lab.sh remote-settled-status $(ENV_FILE) $(REMOTE)

handoff-lab-remote-stop:
	./scripts/handoff-lab.sh remote-stop $(ENV_FILE) $(REMOTE)

handoff-lab:
	./scripts/handoff-lab.sh handoff $(ENV_FILE) $(SRC) $(DST)

brt-dd-lab-config:
	./scripts/brt-dd-lab.sh config $(ENV_FILE)

brt-dd-lab-images:
	./scripts/brt-dd-lab.sh images $(ENV_FILE)

brt-dd-lab-up:
	./scripts/brt-dd-lab.sh up $(ENV_FILE)

brt-dd-lab-seed:
	./scripts/brt-dd-lab.sh seed $(ENV_FILE)

brt-dd-lab-status:
	./scripts/brt-dd-lab.sh status $(ENV_FILE)

brt-dd-lab-verify-config:
	./scripts/brt-dd-lab.sh verify-config $(ENV_FILE)

brt-dd-lab-logs:
	./scripts/brt-dd-lab.sh logs $(ENV_FILE)

brt-dd-lab-stop:
	./scripts/brt-dd-lab.sh stop $(ENV_FILE)

brt-dd-next-downtime-stage:
	./scripts/brt-dd-next-downtime.sh stage $(ENV_FILE)

brt-dd-next-downtime-status:
	./scripts/brt-dd-next-downtime.sh status $(ENV_FILE)

test-brt-dd-tooling:
	python3 -m unittest scripts/test-brt-dd-trace.py scripts/test-classify-brt-dd-trace.py scripts/test-dd1-brt-emulator.py scripts/test-brt-dd-trace-guards.py scripts/test-brt-dd-live-canary.py scripts/test-brt-dd-runtime-patch-site.py scripts/test-brt-dd-db-metadata-map-shim.py

brt-dd-trace:
	./scripts/brt-dd-trace.sh arm $(CONTAINER) $(TRACE_LOG) $(ENV_FILE)

brt-dd-trace-stop:
	./scripts/brt-dd-trace.sh stop $(CONTAINER) $(ENV_FILE)

brt-dd-live-preflight:
	./scripts/brt-dd-live-readiness.sh preflight $(ENV_FILE)

brt-dd-live-restart:
	./scripts/brt-dd-live-readiness.sh restart-deep-desert $(ENV_FILE) "$(CONFIRM)"

brt-dd-live-verify:
	./scripts/brt-dd-live-readiness.sh verify-after-restart $(ENV_FILE)

brt-dd-live-verify-stack:
	./scripts/brt-dd-live-readiness.sh verify-brt-stack $(ENV_FILE)

brt-dd-live-logs:
	./scripts/brt-dd-live-readiness.sh logs $(ENV_FILE)

brt-dd-live-checklist:
	./scripts/brt-dd-live-readiness.sh checklist $(ENV_FILE)

failover-orchestrate:
	./scripts/failover-orchestrate.sh $(ENV_FILE) $(ROLE) $(APPLY)

failover-role-services:
	./scripts/failover-role-services.sh $(ENV_FILE) $(ROLE) $(REMOTE)

cutover-check:
	./scripts/cutover-check.sh $(ENV_FILE)

cutover-network-status:
	./scripts/cutover-network-status.sh $(ENV_FILE) $(ROUTER) $(REMOTE)

browser-ping-diagnostics:
	./scripts/browser-ping-diagnostics.sh $(ENV_FILE)

browser-ping-proof: browser-ping-diagnostics client-browser-ping-verifier-external

client-browser-ping-verifier:
	./scripts/client-browser-ping-verifier.sh $(ENV_FILE)

client-browser-ping-verifier-external:
	DUNE_CLIENT_BROWSER_PING_EXTERNAL_ICMP=true ./scripts/client-browser-ping-verifier.sh $(ENV_FILE)

watch-browser-probe:
	./scripts/watch-browser-probe.sh $(ENV_FILE) $(SECONDS)

watch-client-browser-ping-log:
	./scripts/watch-client-browser-ping-log.sh $(ENV_FILE) $(SECONDS)

host-network-failover:
	REMOTE="$(REMOTE)" ROLE="$(ROLE)" ./scripts/host-network-failover.sh $(ENV_FILE)

router-cutover:
	./scripts/router-cutover-asuswrt.sh $(ENV_FILE) $(ROUTER) $(TARGET)

install-dune-status-service:
	./scripts/install-dune-status-service.sh $(ENV_FILE)

check-steam-update:
	./scripts/check-steam-update.sh $(ENV_FILE)

backup-dry-run:
	./scripts/backup-state.sh --dry-run $(ENV_FILE)

backup-state:
	./scripts/backup-state.sh $(ENV_FILE)

dd-pre-restore-backup:
	./scripts/dd-pre-restore-backup.sh --env-file $(ENV_FILE) $(if $(LABEL),--label "$(LABEL)",) $(if $(BRT_PLAYER_ID),--brt-player-id $(BRT_PLAYER_ID),) $(if $(BRT_CHARACTER),--brt-character "$(BRT_CHARACTER)",) $(if $(BRT_TOTEM_ID),--brt-totem-id $(BRT_TOTEM_ID),) $(if $(COMMIT_BRT_BACKUP),--commit-brt-backup,)

restore-dry-run:
	@if [ -z "$(BACKUP_DIR)" ]; then \
		echo "Usage: make restore-dry-run ENV_FILE=.env BACKUP_DIR=backups/<id> RESTORE_FLAGS='--rabbitmq --server-saved --config --tls'"; \
		exit 2; \
	fi
	./scripts/restore-state.sh --dry-run $(RESTORE_FLAGS) $(ENV_FILE) $(BACKUP_DIR)

verify-backup:
	@if [ -z "$(BACKUP_DIR)" ]; then \
		echo "Usage: make verify-backup BACKUP_DIR=backups/<id>"; \
		exit 2; \
	fi
	./scripts/verify-backup.sh $(BACKUP_DIR)

restore-drill:
	./scripts/backup-restore-drill.py $(if $(RESTORE_DUMP),--source "$(RESTORE_DUMP)",)

restore-drill-status:
	./scripts/backup-restore-drill.py --status

install-backup-restore-drill-timer:
	./scripts/install-backup-restore-drill-timer.sh $(ENV_FILE)

rabbitmq-restore-drill:
	./scripts/rabbitmq-restore-drill.py $(if $(RABBITMQ_BACKUP_SET),--backup-set "$(RABBITMQ_BACKUP_SET)",)

rabbitmq-restore-drill-status:
	./scripts/rabbitmq-restore-drill.py --status

install-rabbitmq-restore-drill-timer:
	./scripts/install-rabbitmq-restore-drill-timer.sh $(ENV_FILE)

slo-status:
	./scripts/operational-slo.py status

slo-verify:
	./scripts/operational-slo.py verify

slo-metrics:
	./scripts/operational-slo.py metrics

capacity-status:
	./scripts/capacity-intelligence.py status

capacity-verify:
	./scripts/capacity-intelligence.py verify

capacity-metrics:
	./scripts/capacity-intelligence.py metrics

desired-state-status:
	./scripts/desired-state.py status

desired-state-verify:
	./scripts/desired-state.py verify

desired-state-metrics:
	./scripts/desired-state.py metrics

start-full-warm-pool:
	./scripts/start-full-warm-pool.sh $(ENV_FILE)

recover-survival:
	./scripts/recover-survival.sh $(ENV_FILE)

recover-map:
	@if [ -z "$(SERVICE)" ] || [ -z "$(PARTITION_ID)" ]; then \
		echo "Usage: make recover-map ENV_FILE=.env SERVICE=heighliner-dungeon PARTITION_ID=18"; \
		exit 2; \
	fi
	./scripts/recover-map.sh $(ENV_FILE) $(SERVICE) $(PARTITION_ID)

watch-maps:
	./scripts/watch-maps.sh $(ENV_FILE)

watch-maps-status:
	./scripts/watch-maps.sh $(ENV_FILE) --status

install-map-watchdog-service:
	./scripts/install-map-watchdog-service.sh $(ENV_FILE)

install-autoheal-services:
	./scripts/install-autoheal-services.sh $(ENV_FILE)

install-swap-warm-service:
	./scripts/install-swap-warm-service.sh $(ENV_FILE)

test-swap-warm:
	bash -n scripts/map-memory-squeeze.sh scripts/swap-warm-daemon.sh scripts/enable-swap-warm-fleet.sh scripts/disable-swap-warm-fleet.sh scripts/install-swap-warm-service.sh

install-player-presence-announcer-service:
	./scripts/install-player-presence-announcer-service.sh $(ENV_FILE)

install-artificial-exchange-service:
	./scripts/install-artificial-exchange-service.sh $(ENV_FILE)

install-artificial-exchange-buyer-service:
	./scripts/install-artificial-exchange-service.sh $(ENV_FILE) /etc/systemd/system/dune-artificial-exchange-bot.service buyer

install-artificial-exchange-populator-service:
	./scripts/install-artificial-exchange-service.sh $(ENV_FILE) /etc/systemd/system/dune-artificial-exchange-populator.service populator

install-artificial-exchange-watchdog-timer:
	./scripts/install-artificial-exchange-watchdog-timer.sh $(ENV_FILE)

install-full-farm-service:
	./scripts/install-full-farm-service.sh $(ENV_FILE)

install-public-ip-monitor:
	./scripts/install-public-ip-monitor.sh $(ENV_FILE)

install-daily-maintenance-timer:
	./scripts/install-daily-maintenance-timer.sh $(ENV_FILE)

full-world-partitions:
	./scripts/full-world-partitions.sh $(ENV_FILE)

compose-config:
	$(COMPOSE) --env-file $(ENV_FILE) config --quiet

check-compose-static-ips:
	./scripts/check-compose-static-ips.py $(ENV_FILE)

secret-scan:
	rg -n --pcre2 "(gho_|FLS_SECRET=eyJ|POSTGRES_[A-Z_]*PASSWORD=(?!(?:change-me|replace-with|<))[A-Za-z0-9_./+=-]{24,}|RMQ_HTTP_TOKEN_AUTH_SECRET=(?!(?:change-me|replace-with|<))[A-Za-z0-9_./+=-]{32,}|DUNE_ADMIN_TOKEN=(?!(?:change-me|replace-with|<))[A-Za-z0-9_./+=-]{24,}|DUNE_ANNOUNCE_[A-Z_]*PASSWORD=(?!(?:change-me|replace-with|<))[A-Za-z0-9_./+=-]{20,}|ServiceAuthToken=[A-Za-z0-9_.-]+|ServerLoginPasswordSecret=\"(?!replace)|UsernameServerLoginSecret=\"(?!replace)|BEGIN .*PRIVATE KEY|PRIVATE KEY)" . --glob '!data/**' --glob '!captures/**' --glob '!backups/**' --glob '!config/tls/**' --glob '!.env' --glob '!Makefile' --glob '!.github/workflows/validate.yml' && exit 1 || true

test-watch-maps:
	./scripts/test-watch-maps.sh

test-map-catalog:
	python3 scripts/test-map-catalog.py
	python3 -m py_compile scripts/map_catalog.py scripts/test-map-catalog.py

test-map-network-autogrow:
	python3 scripts/test-map-worker-discovery.py
	python3 scripts/test-map-network-autogrow.py
	python3 -m py_compile scripts/sync-map-workers.py scripts/ensure-map-network-capacity.py scripts/update-router-map-port-ranges.py
	bash -n scripts/compose-files.sh scripts/start-map-with-post-hooks.sh scripts/recover-map.sh scripts/start-full-warm-pool.sh
	sh -n scripts/restart-target.sh

test-control-plane-health:
	bash -n scripts/control-plane-health.sh scripts/director-watchdog.sh scripts/install-autoheal-services.sh scripts/recover-publication.sh scripts/test-control-plane-health.sh
	./scripts/test-control-plane-health.sh

.PHONY: test-player-identity

test-player-identity:
	python3 scripts/test-player-identity.py

test-admin-panel-safe-surfaces: test-player-identity
	python3 scripts/test-admin-panel-safe-surfaces.py

test-storage-cleanup:
	./scripts/test-storage-cleanup.sh

test-public-ip-monitor:
	./scripts/test-public-ip-monitor.sh

test-update-env-file:
	python3 scripts/test-update-env-file.py

test-update-steam-tool:
	python3 scripts/test-update-steam-tool.py

test-patch-dune-directory-caddy:
	python3 scripts/test-patch-dune-directory-caddy.py

test-sietches:
	./scripts/test-sietches.sh

test-configure-autoscaler-profile:
	./scripts/test-configure-autoscaler-profile.sh

test-inventory-conflicts:
	./scripts/test-inventory-conflicts.sh

inventory-integrity-audit:
	./scripts/inventory-conflicts.sh --env-file $(ENV_FILE) audit

inventory-integrity-repair-preview:
	./scripts/inventory-conflicts.sh --env-file $(ENV_FILE) repair

test-cpu-affinity:
	./scripts/test-cpu-affinity.sh

cpu-affinity-generate:
	./scripts/generate-cpu-affinity.py --env-file $(ENV_FILE)

cpu-affinity-status:
	./scripts/cpu-affinity.sh --env-file $(ENV_FILE) status

cpu-affinity-preview:
	./scripts/cpu-affinity.sh --env-file $(ENV_FILE) apply

test-host-tuning:
	./scripts/test-host-tuning.sh

host-tuning-status:
	./scripts/host-tuning.sh --env-file $(ENV_FILE) status

host-tuning-plan:
	./scripts/host-tuning.sh --env-file $(ENV_FILE) plan --nic

test-admin-access-control:
	python3 scripts/test-admin-access-control.py

admin-access-list:
	./scripts/admin-access.py list

test-character-slot-tool:
	python3 scripts/test-character-slot-tool.py

test-research-catalog:
	python3 scripts/test-research-catalog.py
	python3 scripts/research_catalog.py --validate
	python3 scripts/generate-surface-docs.py --validate

test-discovery-tools:
	python3 scripts/test-discovery-tools.py

test-admin-chat:
	python3 scripts/test-dune-whisper-route.py
	python3 scripts/test-paul-whisper-defaults.py
	python3 scripts/test-admin-chat-commands.py
	python3 scripts/test-player-presence-announcer.py

test-admin-grant-item:
	python3 scripts/test-admin-grant-item.py

.PHONY: test-smugglers-run-mp

test-smugglers-run-mp:
	python3 scripts/test-smugglers-run-mp.py

gm-catalog:
	python3 scripts/gm-command-catalog.py --format markdown

gm-probe-preview:
	python3 scripts/probe-gm-command.py --preview --command "$${GM_COMMAND:-PrintAllowedCommands}" --route "$${GM_ROUTE:-Survival_11}" --target-player "$${GM_TARGET_PLAYER:-SamplePlayer}" --admin-player "$${GM_ADMIN_PLAYER:-$${GM_TARGET_PLAYER:-SamplePlayer}}"

gm-probe-safe:
	python3 scripts/probe-gm-command.py --command "$${GM_COMMAND:-PrintAllowedCommands}" --route "$${GM_ROUTE:-Survival_11}" --target-player "$${GM_TARGET_PLAYER:-SamplePlayer}" --admin-player "$${GM_ADMIN_PLAYER:-$${GM_TARGET_PLAYER:-SamplePlayer}}" --wait-response "$${GM_WAIT_RESPONSE:-3}"

research-catalog:
	python3 scripts/research_catalog.py --validate

research-catalog-markdown:
	python3 scripts/research_catalog.py --format markdown

surface-ledger:
	python3 scripts/generate-surface-docs.py --validate

surface-ledger-markdown:
	python3 scripts/generate-surface-docs.py --format markdown

discovery-queue:
	python3 scripts/generate-discovery-queue.py

binary-candidate-scores:
	python3 scripts/score-binary-candidates.py $(STRINGS_FILE) $(SCORE_FLAGS)

asset-reference-graph:
	python3 scripts/build-asset-reference-graph.py $(GRAPH_PATHS) $(GRAPH_FLAGS)

extract-build-surfaces:
	./scripts/extract-build-surfaces.sh $(ENV_FILE) $(SERVICE) $(OUT_ROOT)

diff-build-surfaces:
	python3 scripts/diff-build-surfaces.py $(OLD_BUILD) $(NEW_BUILD) $(DIFF_FLAGS)

db-function-classifier:
	python3 scripts/classify-db-functions.py $(DB_SURFACE_JSON) $(CLASSIFIER_FLAGS)

fixture-runner:
	python3 scripts/fixture-runner.py $(FIXTURE) --env-file $(ENV_FILE) $(FIXTURE_FLAGS)

.PHONY: smugglers-run-mp-inspect smugglers-run-mp-session smugglers-run-mp-compare smugglers-run-mp-loaner smugglers-run-mp-shared-fixture-before smugglers-run-mp-shared-fixture-after smugglers-run-mp-vehicle-fixture-before smugglers-run-mp-vehicle-fixture-after

smugglers-run-mp-inspect:
	python3 scripts/smugglers-run-mp.py --env-file $(ENV_FILE) inspect $(SMUGGLERS_FLAGS)

smugglers-run-mp-session:
	python3 scripts/smugglers-run-mp.py --env-file $(ENV_FILE) init $(SMUGGLERS_FLAGS)

smugglers-run-mp-compare:
	python3 scripts/smugglers-run-mp.py --env-file $(ENV_FILE) compare $(SMUGGLERS_FLAGS)

smugglers-run-mp-loaner:
	python3 scripts/smugglers-run-mp.py --env-file $(ENV_FILE) loaner $(SMUGGLERS_FLAGS)

smugglers-run-mp-shared-fixture-before:
	python3 scripts/fixture-runner.py fixtures/smugglers-run-mp-shared-map.json --env-file $(ENV_FILE) --phase before

smugglers-run-mp-shared-fixture-after:
	python3 scripts/fixture-runner.py fixtures/smugglers-run-mp-shared-map.json --env-file $(ENV_FILE) --phase after

smugglers-run-mp-vehicle-fixture-before:
	python3 scripts/fixture-runner.py fixtures/smugglers-run-mp-owned-vehicle-safety.json --env-file $(ENV_FILE) --phase before

smugglers-run-mp-vehicle-fixture-after:
	python3 scripts/fixture-runner.py fixtures/smugglers-run-mp-owned-vehicle-safety.json --env-file $(ENV_FILE) --phase after

knob-experiment:
	python3 scripts/knob-experiment.py --catalog $(CATALOG) --env-file $(ENV_FILE) $(EXPERIMENT_FLAGS)

capture-rmq-window:
	./scripts/capture-rmq-window.sh $(ENV_FILE) $(SECONDS) $(TAG)

diff-rmq-captures:
	python3 scripts/diff-rmq-captures.py $(RMQ_BEFORE) $(RMQ_AFTER) $(RMQ_DIFF_FLAGS)

test-operational-borrowing:
	python3 scripts/test-operational-borrowing.py

test-artificial-exchange:
	python3 scripts/test-artificial-exchange.py

test-artificial-exchange-service:
	./scripts/test-artificial-exchange-service.sh

artificial-exchange-smoke:
	./scripts/artificial-exchange-smoke.sh

artificial-exchange-bootstrap-catalog:
	python3 scripts/build-exchange-bootstrap-catalog.py
	python3 scripts/build-exchange-catalog.py

artificial-exchange-research-prices:
	python3 scripts/research-exchange-prices.py --crawl-allpages --no-search-fallback
	python3 scripts/build-exchange-catalog.py

test-vehicle-fidelity-investigation:
	./scripts/investigate-vehicle-fidelity.sh >/dev/null

list-publishable:
	find . -maxdepth 3 \( -path './.git' -o -path './data' -o -path './captures' -o -path './backups' -o -path './config/tls' \) -prune -o -type f -not -name '.env' -print | sort

update-hagga-pois:
	python3 scripts/update-hagga-poi-markers.py

public-site-check:
	bash -n public-site/scripts/*.sh examples/public-site/rclone-sync.sh
	python3 -m py_compile public-site/scripts/render-dune-public-snapshot.py public-site/scripts/build-federated-directory.py public-site/scripts/configure-federated-directory-sources.py public-site/scripts/generate-game-landing.py scripts/update-hagga-poi-markers.py
	node --check public-site/directory/directory.js
	python3 -m unittest public-site/scripts/test_render_dune_static_status.py
	python3 -m unittest public-site/scripts/test_render_dune_public_snapshot.py
	python3 -m unittest public-site/scripts/test_generate_game_landing.py
	./public-site/scripts/validate-dune-public-site.sh public-site/static
	systemd-analyze verify public-site/systemd/render-dune-static-status.service public-site/systemd/render-dune-static-status.timer public-site/systemd/build-dash-federated-directory.service public-site/systemd/build-dash-federated-directory.timer

public-site-package:
	./public-site/scripts/package-dune-public-site.sh

public-site-deploy:
	./public-site/scripts/deploy-dune-public-site.sh $(PUBLIC_SITE_ENV_FILE)

public-site-verify:
	./public-site/scripts/verify-dune-public-site.sh $(PUBLIC_SITE_URL)

admin-panel-deploy:
	./scripts/deploy-admin-panel.sh $(ENV_FILE)

admin-panel-verify:
	./scripts/check-admin-ingress.sh $(ENV_FILE)

verify-local-state-ignored:
	@for path in backups/example captures/example data/example config/tls/example backups-env-example backup-env-example local.env.backup local.env.bak; do \
		git check-ignore -q "$$path" || exit 1; \
	done
