# Map Worker Catalog

Map-aware operations derive their worker roster from the resolved Docker Compose
model, not a second hard-coded list. The catalog helper identifies each service
whose command has a `-PartitionIndex=N` argument, then reads its cooked map
package, optional dimension and policy labels, Compose profiles, ports, and
container address.

Inspect the effective roster with:

```bash
python3 scripts/map_catalog.py .env --field map-partitions
python3 scripts/map_catalog.py .env --field json
```

The catalog drives world-partition seeding, full warm-pool startup, map watching
and recovery, admin restart/autoscaler routing, safety audits, patch-overlay
reconciliation, and standby promotion. Adding a Compose map worker therefore
does not require separately editing those operational allowlists.

## Automatic discovery

When the installed Funcom `world-template.yaml` is present,
`scripts/compose-files.sh` runs `scripts/sync-map-workers.py` before resolving
Compose. The template supplies enabled map/dimension entries; the
`dune-map-asset-index` binary scans the configured server image's cooked
`.umap` packages to find their exact `/Game/.../Package.Package` paths. Any
template entry with an available cooked asset but no existing worker is emitted
into `backups/admin-panel/compose.auto-maps.yaml` with an unused partition ID,
UDP ports, static address, and isolated saved-data directory. The asset scan is
cached by game-image ID, template, Compose inputs, and indexer checksum; an
image/template update invalidates the cache.

The generated overlay is included by the normal `compose-files.sh` path. The
dynamic map catalog then seeds its database partition row through
`scripts/full-world-partitions.sh` during the normal map-start/restart workflow.
The game's `update_partition_labels(false)` helper assigns special labels such
as `SietchTalab_0`; the existing farm's IDs are not renumbered. Maps listed in a
newer template but absent from the selected image are held pending until that
image contains their cooked map package.

### Automatic UDP range and forwarding growth

Set `DUNE_MAP_NETWORK_AUTOGROW_ENABLED=true` on `kspls0` to let the guarded map
start/restart helpers expand exhausted public UDP ranges before the generated
worker overlay is built. The default growth step is 64 ports; the game range
expands downward no lower than `DUNE_MAP_NETWORK_GAME_PORT_MIN=7000`, and the
IGW range expands upward no higher than `DUNE_MAP_NETWORK_IGW_PORT_MAX=9000`.
Existing worker port assignments are retained, and the planner rejects
overlapping pools or occupied host ports.

When a range must grow, `scripts/update-router-map-port-ranges.py` widens only
the AsusWRT `duneA1` and `duneA2` UDP forwarding ranges, preserves every rule's
destination address, and leaves unrelated router rules untouched. The helper
backs up the prior router rule list and `.env`, updates the host LAN reflection
rules, then proceeds to normal map discovery. It fails closed if the production
hostname, router rules, or port limits do not validate. The feature defaults to
off and runs only through guarded start/restart paths; ordinary
`scripts/compose-files.sh` and Compose inspection do not alter `.env`, router,
or host firewall state.

The first enabled start verifies the router rule shape and synchronizes LAN
reflection. Each later guarded start repeats those idempotent reconciliations so
router or host-firewall drift and interrupted runs self-heal; a private state
marker records the last successfully applied ranges. Existing manual/custom
workers remain the operator's responsibility unless they are in the official
template-and-cooked-asset discovery path.

Set `DUNE_MAP_AUTO_DISCOVERY_ENABLED=false` to disable generation, or set
`DUNE_WORLD_TEMPLATE_PATH` when the installed template is not at the default
server setup path. The production host needs PyYAML and the executable indexer;
rebuild the latter with `scripts/build-map-asset-index.sh` after changing its
Rust source.

Profile-gated workers stay inactive unless their profile is selected. The
legacy `DUNE_WORLD_PARTITION_COUNT` opt-in applies only when a worker explicitly
sets `dune.map.enabled-by-partition-count: "true"`; do not use it as a general
map-discovery switch. `dune.map.remove-when-disabled: "true"` opts a worker into
database-row cleanup when disabled and should be reserved for intentionally
disposable dimensions.

After adding the service, validate the resolved model and network/port plan,
then seed and start it through the normal helpers. For manually configured
workers, keep public UDP ports inside the configured ranges or widen their
forwarding explicitly.

## Manual fallback

For a custom map absent from Funcom's world template, first verify that the
current server build contains its cooked map and establish the exact package
argument. A quest route or map name in logs is not enough to infer the package
path. Define a worker with a unique positive `-PartitionIndex=N`, a unique
database `(map, dimension-index)`, unique host UDP ports, a static container
address if required by the farm, and a distinct saved-data directory.

The database map name defaults to the cooked package object name. Set
`dune.map.database-name` only when the game's database identity differs or the
map is implicit. Set `dune.map.dimension-index` for additional dimensions of
the same database map and `dune.map.partition-label` when a specific display
label is needed.

## Discovery boundary

This provisions maps that Funcom declares as enabled world partitions and that
the selected server image contains as cooked maps; it does not invent workers
for arbitrary `.umap` assets. If the official map name and cooked package name
diverge, or a non-Funcom/custom map is introduced, use the manual fallback and
explicitly set its database identity. Normal transitions still require the
partition rows to be seeded and the generated map services to be started.
