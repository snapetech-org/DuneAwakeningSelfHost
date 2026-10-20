# Artificial Exchange

This feature adds an operator-controlled artificial Exchange layer for Dune
Awakening self-hosts. It has three separate jobs:

- build a reviewed item price catalog
- buy eligible player listings through the native Exchange fulfill path
- optionally seed tightly gated NPC listings from reviewed, category-mapped
  catalog rows

All write paths are gated. The default service state is enabled in dry-run mode,
with purchases, funding, settlement claim, and populator writes disabled.

Operational model:

- The artificial buyer is demand-side liquidity. It can run continuously after
  dry-run review because it only buys catalog-approved player listings within
  configured caps.
- Seller settlement is a separate claim path for completed player sales. It
  does not buy listings and does not seed stock.
- The populator is supply-side liquidity. It creates DASH/Admin-owned
  `is_npc_order=true` listings and should be treated as an intentional market
  seeding tool, not as a required background daemon.
- Buyer and populator identities should be distinct from normal human player
  characters. Add every populator owner id to
  `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_OWNER_IDS` so the buyer skips seeded
  listings.

First-class operator workflow:

```bash
python3 scripts/import-exchange-category-map.py
python3 scripts/build-exchange-catalog.py
python3 scripts/artificial-exchange-bot.py --check-ready
python3 scripts/artificial-exchange-bot.py --dry-run --report-skips 100
python3 scripts/artificial-exchange-bot.py --settlement-report
make artificial-exchange-smoke
```

Only after that flow is clean should an operator enable live purchase,
auto-claim, funding, or populator apply gates in `.env`.

The seeded-listing populator is first-class but intentionally operator-driven.
It is designed for small or private servers where the Exchange needs a visible
stock baseline. It should be run as a controlled one-shot or explicitly managed
service, not left running accidentally after test seeding.

Confidence levels:

- Catalog building and dry-run scans: high.
- Live native purchase for the tested `PowerPack` path: high.
- Safe seller Solari settlement through the validated direct transaction: high
  for completed seller Solari claim rows matching the tested shape.
- Buyer execution against reviewed rows: high for the tested paths.
- Populator live seeding: moderate. It uses native Exchange order functions and
  has cleanup guards, but it can still affect the visible economy immediately.
- Native `dune_exchange_retrieve_solaris_from_item(...)`: low and not used by
  the bot because this server build showed unsafe behavior.

## Recipes, Schematics, And Patents Do Not Grant On Purchase

Verified on `kspls0` build `dune_sb_1_4_5_0`, 2026-06-17: there is no relational
known-recipes table on this server build. Crafting pattern / recipe unlock state
lives in `encrypted_player_state`. `removed_recipes` is empty and
`building_progression.learned_building_sets` is buildings only. A seeded
schematic listing therefore only delivers an inert item; buying it does not raise
the player's known-pattern count, and seeded schematic items carry an empty
`items.stats` payload. This is the root cause of the player report "bought a
recipe twice, patterns still show 0", and the "armor package that does nothing"
(those armor packages were armor schematics).

Consequence: schematics, blueprints, and building patents are excluded from
seeding via `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_SKIP_BLUEPRINT_CATEGORIES=true`
until a genuine learnable schematic payload is proven through in-client
buy-and-learn testing. Re-enabling recipe seeding requires that proof, not just a
template id. Confidence: high that the current item path cannot grant patterns.

Use `scripts/prune-broken-exchange-listings.py` to remove already-seeded
non-functional listings. It reuses `delete_seeded_orders` (scoped to exchange +
populator owner + `is_npc_order=true`) and targets two classes:

- `--mode schematics`: blueprint-category / schematic / patent listings
- `--mode empty-stateful`: stateful gear with an empty stats payload
- `--mode both` (default)

Dry-run by default; live prune requires `--apply --confirm "PRUNE ARTIFICIAL EXCHANGE"`.

### Making seeded schematics learnable

Genuine player-owned schematics on `dune_sb_1_4_5_0` carry one uniform stats
payload, identical across all 308 samples / 148 templates:

```json
{"FItemStackAndDurabilityStats": [[], {"DecayedMaxDurability": 0.0}]}
```

The empty `{}` payload on old seeds was the only structural difference between a
dud and a real, learnable schematic. The populator now attaches the durability
payload to every blueprint-category staging item (`staging_stats_for_row`), so
seeded schematics are structurally identical to legitimately-acquired ones. The
`template_id` already carries the recipe identity; no server-side grant call
exists (recipe unlock is a client-side learn action against actor/fgl
properties), so the player must still learn the purchased item in-game.

A 100-listing probe is seeded live (priced ~2k-6k). Before enabling schematics
market-wide, verify in-client:

1. On a test character, buy one seeded schematic from the Exchange.
2. Relog so the purchased item materializes, then learn it from inventory.
3. Confirm the known-pattern count increases and the recipe is craftable.

If the learn succeeds, set
`DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_SKIP_BLUEPRINT_CATEGORIES=false` to enable
recipe seeding market-wide. If it fails, run
`prune-broken-exchange-listings.py --mode schematics --apply` to remove the probe
and keep the gate on. Confidence: high that the seeded item is structurally
correct; the in-client learn step is the one unverified link.

### Pricing model note (2026-06-17 review)

The pricing model (dune.exchange market price, geometric-mean outlier damping vs
game-file price, category multipliers, category floors, blueprint floors) is
sound. The highest seeded prices (StaticCompactor_Unique_Compact_06 ~420k,
BuggyMining_Unique_YieldIncrease_06 ~500-667k) are legitimate T6 unique items,
not pricing bugs. Some of those uniques are mis-bucketed (a buggy mining module
seeded under gathering tools, water containers/deployables under consumables);
that is a category-map issue, not a price issue.

Commodity organization (2026-06-17): commodity prices are sourced from
dune.exchange where it has data (run `import-dune-exchange-prices.py`; it covers
high-value refined goods like Spice Melange, Stravidium Fiber, Plastanium,
Plasteel, Diamondine Dust and a few components), and from game-file/wiki base
prices otherwise. dune.exchange's public scanner is sparse for low/mid
commodities, so `PRICE_CATEGORY_FLOORS` now includes resource floors
(`resources/raw` 20, `resources/refined` 100, `resources/fuel` 120,
`resources/components` 250) applied to the pre-multiplier anchor. This keeps
game-file placeholder rows (`baseline_price=1`) from seeding as ~1-Solari junk
while leaving real market-priced commodities untouched. To re-price live
commodities after a refresh, prune the affected listings
(`prune-broken-exchange-listings.py --template-ids ...` or `--category-masks ...`)
and let the populator reseed them.

Buyer demand was retuned 2026-06-17 after finding the buyer's Exchange Solari
balance was effectively empty (`123`): per-tier buy probabilities raised to
`0.0008/0.0015/0.0025` (low/medium/high), daily caps raised to
`150000/60000/80000` (global/seller/template) so valuable goods are buyable, and
the buyer funded to `~1,000,000`. The buyer balance is not auto-refunded; refund
periodically with `--fund-buyer`. Daily Solari injection is bounded by the global
cap (`150000`).

The buyer and populator identities must remain separate. Native Exchange counts
all active orders owned by `in_instigator_id` toward
`DUNE_ARTIFICIAL_EXCHANGE_MAX_ORDERS_PER_PLAYER`; if the buyer reuses the
populator owner, thousands of seeded listings consume that capacity and native
fulfill returns `item_id=0` without buying. The live host uses buyer controller
`7` (a non-player Exchange terminal identity) and keeps populator owner `124`.

## Current Production Snapshot

As of 2026-06-17 on `kspls0` (`dune_sb_1_4_5_0`), the market was re-evaluated
after player feedback. The populator service was never installed, so the market
had drained from the old `5432` snapshot to `~1878` seeded orders dominated by
schematics and other unsold dregs, with the commodity core (refined/fuel/
components resources) at zero. Remediation removed `957` schematic/patent
listings and reseeded `3100` functional orders.

Current verified market state:

- Exchange id: `2`
- Populator/controller owner id: `124`
- Source inventory id: `485`
- Live seeded orders: `4021` across `672` distinct templates
- Schematics/patents seeded: `0` (gated out)
- Commodity core present: MelangeSpice, SpiceResidue, SpicedFuelCell,
  WindTurbineLubricant, T6Watertube at `30` listings each
- Stateful-gear audit: `0` unsafe empty-stats orders, `0` quality mismatches

Replenishment caveat: the buyer service (`dune-artificial-exchange-bot.service`,
`--loop`) runs, but no populator service/watchdog is installed, so seeded supply
does not auto-replenish as players buy it. Install the populator service +
watchdog timer to keep the market topped up, or re-run the reseed periodically.

Production update (2026-07-22): `dune-artificial-exchange-populator.service` is
now enabled and running on `kspls0`. The buyer scans every 60–120 seconds with
all catalog-approved listings eligible within the configured daily and price
caps; the populator scans every 120–240 seconds and only adds stock below the
category targets. The live `.env` backup is retained under
`backups/admin-panel/artificial-exchange/` on the host.

Interim gate note: `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_REQUIRE_SOURCE_CATEGORY`
was set `false` because the generated `source-category-map.json` disagrees with
`exchange_category_map.py` on resource depth (says depth 1; the live-rendering
orders and the static map use depth 2). The catalog's own mask reconcile is the
authority for now. Regenerating a correct source map for build `1.4.5` is the
proper fix. The fuel commodities also still reconcile into `resources/refined`
instead of `resources/fuel`; the catalog reconcile pass overrides the CSV
category and needs a fuel-bucket fix.

The current audit report is
`backups/admin-panel/artificial-exchange/market-category-audit.json`. A clean
audit means the populator does not currently see anything else to add under the
configured category targets and ceiling.

The production seeding shape is:

- Minimum target: `4000` live orders
- Hard/goal ceiling: `20000` live orders
- Category-target planner: enabled
- Scan/populator cadence: every `180` to `420` seconds unless overridden
- Stackable categories: full-stack listings, currently stack size `100`
- Singleton categories: individual listings, currently targeting `125` orders
  per category with a cap of `8` per template/category

Stackable categories currently include consumables, resources, fuel, and ammo:

```env
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_STACKABLE_CATEGORIES=consumables/medical,consumables/spice,resources/components,resources/fuel,resources/raw,resources/refined,vehicles/ammunition,weapons/ammunition
```

Unique Schematics are special. The game UI filters them through the parent
Unique Schematics mask, so seeded schematic listings use parent mask
`0x07000000` with category depth `2`. The catalog still keeps logical schematic
subcategories for balancing and audit reporting. Confidence: high.

Augments are protected. Non-augment rows are not allowed into Augment masks.
The current game-derived sources do not expose trusted standalone augment or
customization item rows, and the previous observed Augment placements were bad
old rows corrected back to weapon categories. Do not seed weapons into
customization just because the weapon has a customization capability tag.

## Audit And Coverage Checklist

Use this checklist after any category-map, pricing, catalog, or `.env` change.
Confidence: high that these checks catch the failure modes seen during the
initial broad seed.

1. Rebuild the game-derived source category map and catalog:

```bash
python3 scripts/import-exchange-category-map.py
python3 scripts/build-exchange-catalog.py
```

2. Run offline tests:

```bash
make test-artificial-exchange
```

3. Dry-run the broad populator against the live market:

```bash
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_ENABLED=true \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_DRY_RUN=true \
DUNE_ARTIFICIAL_EXCHANGE_SCAN_LIMIT=25000 \
  python3 scripts/artificial-exchange-bot.py \
  --populate-all-once \
  --catalog backups/admin-panel/artificial-exchange/catalog.json \
  --populator-owner-id 124 \
  --populator-source-inventory-id 485 \
  --confirm "POPULATE ARTIFICIAL EXCHANGE"
```

4. Inspect the resulting summary. A healthy saturated market should report:

- `missingCatalogRows` or `totalPlanned` near `0`
- no category-gate skips for expected rows
- no category mask mismatch errors
- no source category mismatch errors
- no inventory position exhaustion

5. Inspect live category distribution:

```sql
select o.category_mask, o.category_depth, count(*) as orders
from dune.dune_exchange_orders o
where o.exchange_id = 2
group by o.category_mask, o.category_depth
order by orders desc, o.category_mask, o.category_depth;
```

6. Inspect seeded owner scope:

```sql
select owner_id, is_npc_order, count(*) as orders
from dune.dune_exchange_orders
where exchange_id = 2
group by owner_id, is_npc_order
order by owner_id, is_npc_order;
```

7. Inspect template/category pairs that do not exist in the reviewed catalog:

```sql
select o.template_id, o.category_mask, o.category_depth, count(*) as orders
from dune.dune_exchange_orders o
left join dune.dune_exchange_sell_orders s on s.order_id = o.id
where o.exchange_id = 2
group by o.template_id, o.category_mask, o.category_depth
order by orders desc, o.template_id;
```

Compare that list to `backups/admin-panel/artificial-exchange/catalog.json`.
Rows visible in-game but missing from the catalog are either player listings,
old seeded rows that need purging, or a catalog build regression.

Wrong-bucket triage:

- Items in Augments: first suspect stale rows. Purge the Exchange or the
  DASH/Admin owner, rebuild the catalog, then seed again. If the item returns to
  Augments, inspect its `category`, `category_mask`, `category_depth`, and
  source-map entry.
- Empty category in-game: check that the source map has eligible templates for
  that logical category, then check the dry-run `totalPlanned`. If planned is
  `0`, the market is already saturated under the configured category target.
- Schematics missing in-game: check that schematic rows use parent mask
  `0x07000000` and depth `2`. Child schematic masks can exist logically in the
  catalog, but the client filter expects the parent mask for visibility.
- Fuel/resources swapped: inspect `resources/fuel`, `resources/raw`,
  `resources/refined`, and `resources/components` in the source map. These were
  previously easy to confuse because all live under the Misc top-level bucket.
- Prices obviously wrong: inspect `baseline_price`, `game_file_price` in notes,
  category multiplier, category floor, and
  `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_GAME_FILE_OUTLIER_RATIO`.

## Components

Files:

- `config/artificial-exchange-prices.csv`: reviewed manual catalog rows.
- `scripts/build-exchange-catalog.py`: catalog builder.
- `scripts/import-awakening-wiki-items.py`: imports Community Wiki API item
  metadata and game-file-derived prices into a snapshot CSV.
- `scripts/import-dune-exchange-prices.py`: imports public dune.exchange
  auction-history prices and computes midpoint floors for market-priced rows.
- `scripts/research-exchange-prices.py`: crawls Awakening Wiki page wikitext
  and extracts exact item ids with `Base Vendor Price` fields.
- `scripts/build-exchange-bootstrap-catalog.py`: heuristic broad catalog builder
  from locally observed templates.
- `scripts/artificial-exchange-bot.py`: buyer, settlement, funding, readiness,
  and populator CLI.
- `scripts/artificial-exchange-smoke.sh`: safe end-to-end smoke check.
- `scripts/install-artificial-exchange-service.sh`: systemd unit renderer.
- `scripts/test-artificial-exchange.py`: offline catalog/bot unit tests.
- `scripts/test-artificial-exchange-service.sh`: systemd render tests.
- `config/systemd/dune-artificial-exchange-bot.service`: systemd template.
- `docs/artificial-exchange.md`: this runbook.

Generated local state:

- `backups/admin-panel/artificial-exchange/catalog.json`
- `backups/admin-panel/artificial-exchange/catalog.csv`
- `backups/admin-panel/artificial-exchange/<timestamp>-catalog.json`
- `backups/admin-panel/artificial-exchange/source-category-map.json`
- `backups/admin-panel/artificial-exchange/verified-category-map.json`
- `backups/admin-panel/artificial-exchange/market-category-audit.json`
- `backups/admin-panel/artificial-exchange/bot-audit.jsonl`
- `backups/admin-panel/artificial-exchange/bot-state.json`

The generated state lives under `backups/` and is ignored by git.

## DuneAdmin Panel

The DuneAdmin dashboard exposes Artificial Exchange operations under
`Settings -> Artificial Exchange`.

The panel can:

- show catalog counts, readiness checks, and buyer/populator systemd state
- rebuild `backups/admin-panel/artificial-exchange/catalog.json`
- run `--check-ready`
- run a buyer dry-run scan with expanded skip reporting
- render a settlement report
- run the disposable populator validation path
- install, start, stop, and restart the buyer and populator systemd services
- edit every `DUNE_ARTIFICIAL_EXCHANGE_*` `.env` setting through the safe
  Settings editor

The backend endpoints are:

```text
GET  /api/admin/artificial-exchange
POST /api/admin/artificial-exchange
```

The POST body is:

```json
{"action":"check-ready"}
```

Supported actions:

```text
build-catalog
check-ready
buyer-dry-run
settlement-report
validate-populator
install-buyer-service
install-populator-service
install-watchdog-timer
watchdog-once
start:buyer
stop:buyer
restart:buyer
status:buyer
start:populator
stop:populator
restart:populator
status:populator
start:watchdog
stop:watchdog
restart:watchdog
status:watchdog
```

Service actions require `systemctl` in the runtime where the admin panel is
running. When the panel runs inside the Compose container without host systemd
access, those actions fail cleanly and the response explains that `systemctl`
is unavailable. The setting editor still works in that mode; restart the host
services from the host after saving `.env` changes.

## Database Surfaces

Read surfaces:

- `dune.dune_exchange_orders`
- `dune.dune_exchange_sell_orders`
- `dune.dune_exchange_fulfilled_orders`
- `dune.dune_exchange_users`
- `dune.items`
- `dune.inventories`
- `dune.player_state`
- `dune.player_virtual_currency_balances`, whose `currency_id` is the
  `dune.virtualwallettype` enum (`Solaris`, `HouseCredit`)
- Exchange function signatures from `pg_proc`

Use the typed enum literal `'Solaris'::dune.virtualwallettype` when selecting
or creating the player's Solaris bank-balance row. The old
`dune.get_solaris_id()` helper is absent from current database schemas.

Native write functions used:

- `dune.dune_exchange_fulfill_sell_order(...)` for purchases.
- `dune.dune_exchange_modify_user_solari_balance(...)` for explicit buyer
  funding.
- `dune.dune_exchange_update_recurring_sell_order(...)` for populator listing
  creation.
- `dune.save_item(...)` and `dune.advance_items_id_sequencer(...)` for
  populator staging items.
- `dune.dune_exchange_expire_orders(...)` when seeded-order expiry is requested
  and the native function exists.

Direct SQL writes used:

- settlement creates missing base Solaris rows in
  `player_virtual_currency_balances`
- settlement updates that balance by the exact completed claim value
- settlement deletes the completed seller claim order after validation
- seeded-order cleanup deletes only owner-scoped, Exchange-scoped,
  `is_npc_order=true` rows

Native write function deliberately not used:

- `dune.dune_exchange_retrieve_solaris_from_item(...)`

Reason: live validation showed unsafe behavior on this server build.

## Data Model

Catalog fields:

```csv
template_id,display_name,category,category_mask,category_depth,sellable_status,baseline_price,max_buy_price,price_floor,price_ceiling,liquidity_tier,enabled,source,confidence,notes
```

Meaning:

- `template_id`: item template id used by Dune DB rows.
- `display_name`: human label for operators.
- `category`: operator grouping only.
- `category_mask` and `category_depth`: native Exchange category filter values
  used when seeding or validating category counts.
- `sellable_status`: expected values are `known`, `observed`, or `validated`.
- `baseline_price`: reference price used by the catalog and current populator
  floor. For dune.exchange-backed rows this equals `price_floor`.
- `max_buy_price`: highest price the artificial buyer may pay.
- `price_floor`: minimum price the populator may post.
- `price_ceiling`: maximum price the populator may post.
- `liquidity_tier`: `low`, `medium`, or `high`; controls buy probability.
- `enabled`: only `true` rows are eligible for buying or seeding.
- `source`: `manual`, `snapshot`, `local-db`, `live-test`, or similar.
- `confidence`: `low`, `moderate`, or `high`.
- `notes`: operator comments.

The old one-row `PowerPack` live-test catalog entry is disabled. It remains
only as a historical validation record and must not be used as the production
seed source.

## Source Decisions

Current source policy:

- Game-file/community-wiki rows provide identity, display names, category masks,
  category depths, tiers, and conservative in-game reference prices.
- Public dune.exchange auction-history rows provide live market ceilings.
- Populator rows require both `sellable_status=validated` and a dune.exchange
  market price by default.
- Rows without a market price remain useful for buyer catalog review, but are
  excluded from NPC seeding while
  `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_REQUIRE_MARKET_PRICE=true`.
- Tier 0 and tier 1 rows are excluded from NPC seeding by default. Low-tier
  basics and common schematics produced too many cheap/noisy listings.

Pricing decision:

- `price_ceiling` is the selected dune.exchange market price field, currently
  `averagePrice`.
- `price_floor` is halfway between the in-game/game-file price and
  `price_ceiling`.
- The populator randomly samples the full `price_floor..price_ceiling` range.
- If the dune.exchange price is lower than the in-game/game-file price, the
  ceiling is clamped up to the in-game/game-file price. Confidence: high for the
  implemented rule.

Example: if the game-file price is `10,000` and dune.exchange average is
`50,000`, the floor becomes `30,000`, the ceiling remains `50,000`, and seeded
orders are posted randomly between those values.

Intended uses:

- Keep scarce high-tier resources and reviewed items visible on small/private
  servers.
- Provide controlled liquidity while the player market is thin.
- Exercise Exchange category filters and client rendering with realistic prices.
- Run one-shot validation or short maintenance-window seeding, then stop the
  populator.
- Seed broad category coverage only after price and category gates pass. The
  goal can be thousands of listings, but only from reviewed rows mapped to the
  correct native Exchange bucket.

Non-goals:

- It is not a replacement for player supply.
- It should not seed unknown, excluded, test, cosmetic, or unreconciled rows
  just because they exist in game-file metadata.
- It does not automatically recycle seller proceeds back into buyer funding.

## Catalog Pipeline

Build without DB:

```bash
python3 scripts/build-exchange-catalog.py --no-db
```

Refresh the broad community/game-file snapshot first:

```bash
python3 scripts/import-awakening-wiki-items.py
python3 scripts/build-exchange-catalog.py --no-db
```

Promote imported game-file rows for review or isolated test seeding:

```bash
python3 scripts/import-awakening-wiki-items.py \
  --enabled \
  --sellable-status validated \
  --source awakening-wiki-game-files-full-populate \
  --confidence moderate \
  --output /tmp/awakening-wiki-full-populate.csv
```

Those rows still need market-price evidence before live NPC seeding when
`DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_REQUIRE_MARKET_PRICE=true`. Do not treat a
game-file import alone as approval for production population.
The live populator also requires market-price evidence unless
`DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_ALLOW_UNPRICED_SEEDING=true` is set as an
explicit break-glass override. Confidence: high.

Research exact wiki page vendor prices:

```bash
python3 scripts/research-exchange-prices.py \
  --crawl-allpages \
  --no-search-fallback \
  --output data/exchange-price-snapshots/wiki-base-vendor-prices.csv
python3 scripts/build-exchange-catalog.py --no-db
```

Build with live DB observations:

```bash
python3 scripts/build-exchange-catalog.py
```

Sources, in priority order:

- manual rows from `config/artificial-exchange-prices.csv`
- imported CSV snapshots under `data/exchange-price-snapshots/*.csv`
- local DB observations from `dune_exchange_orders` and
  `dune_exchange_sell_orders`

Manual rows override everything else. Snapshot rows are grouped by template and
use median pricing, with moderate confidence after at least three observations.
DB-observed rows are disabled until reviewed.

The `awakening-wiki-items.csv` snapshot is sourced from
`https://api.awakening.wiki/items`. The API describes itself as Community Wiki
data sourced from game files. Imported rows use `market_price` first and
`base_vendor_price` as a fallback, remain `enabled=false`, and should be treated
as broad reference coverage rather than reviewed live-market prices.

The `wiki-base-vendor-prices.csv` snapshot is sourced from wiki page wikitext at
`https://awakening.wiki`. The extractor only accepts pages that expose an exact
`ITEMID` marker and a numeric `Base Vendor Price` field. Confidence: high for
the extracted price matching the wiki page, moderate for whether that price is
the right player-market baseline without operator review. Example source pages
validated during development: `Plant Fiber`, `Power Pack Mk1`, and
`Scrap Metal Knife`.

Validation behavior:

- duplicate manual/snapshot template ids fail
- malformed prices fail
- unknown confidence values fail
- unknown liquidity tiers fail
- missing `enabled` defaults to false

## Buyer

The buyer scans Exchange sell orders and selects eligible player listings. It is
the safest long-running side of the feature because it starts in dry-run mode,
uses the reviewed catalog, and is bounded by spend caps. Normal scans skip:

- NPC orders
- configured populator owner ids
- templates missing from the catalog
- disabled catalog rows
- blocked sellers
- prices above `max_buy_price` plus `DUNE_ARTIFICIAL_EXCHANGE_MAX_BUY_PRICE_TOLERANCE_PCT`
- orders exceeding global, seller, or template daily caps
- randomized probability skips by liquidity tier
- stale order revisions

After a live artificial purchase succeeds, the buyer sends the seller a private
whisper through the same `chat.whispers` route used by admin replies and
presence automation. The default message tells the seller which listing sold
and that the Solari will appear after their next relog. Offline sellers are not
messaged, but their listings can still be purchased. Notification failure is
audited but does not roll back the completed Exchange purchase.

The buyer uses the native fulfill function:

```sql
dune.dune_exchange_fulfill_sell_order(
  in_exchange_id,
  in_max_orders_per_player,
  in_purchased_completion_type,
  in_sold_completion_type,
  in_instigator_id,
  in_order_id,
  in_order_revision,
  in_dst_inventory_id,
  in_dst_index,
  in_count,
  in_solaris_fee,
  in_purge_time
)
```

The bot passes the actual `dune_exchange_orders.revision`; it does not use
PostgreSQL `xmin`.

For stackable listings, the buyer passes the current listing stack size as
`in_count` and purchases the full remaining quantity. Price limits remain
per-unit catalog checks, while the daily global, seller, and template caps use
`item_price * stack_size`. A purchase is successful only when a fulfilled row
exists and the active listing is gone; a reduced active remainder is rejected.
Seller notifications report the purchased quantity and total Solari, not the
pre-purchase stack size paired with a one-unit price.

Buyer safety boundary:

- live buying requires `DUNE_ARTIFICIAL_EXCHANGE_ENABLED=true`
- live buying requires `DUNE_ARTIFICIAL_EXCHANGE_DRY_RUN=false`
- live buying requires `DUNE_ARTIFICIAL_EXCHANGE_PURCHASES_ENABLED=true`
- apply mode requires a buyer controller id
- manual one-shot apply requires confirmation `RUN ARTIFICIAL EXCHANGE`
- service-loop apply should be enabled only after a reviewed dry-run shows the
  expected selected and skipped orders

The long-running buyer supplies `DUNE_ARTIFICIAL_EXCHANGE_SERVICE_CONFIRM`
automatically when live purchases are enabled. Without that service-only
confirmation, the first eligible listing would stop the unit before calling the
native fulfill function. One-shot commands still require the explicit
`--confirm` argument. The service remains active continuously and starts each
scan after a randomized 60–120 second delay. Each eligible listing then gets an
independent liquidity-tier probability gate; the established production profile
is `0.10` (10%) for low liquidity, `0.25` (25%) for medium liquidity, and `0.50`
(50%) for high liquidity. Daily Solari, seller, template, catalog-price, and
buyer-balance limits remain the economic controls. A probability of `1.0` would
mean every eligible listing is attempted, not that every native purchase will
succeed.

Dry-run scan:

```bash
DUNE_ARTIFICIAL_EXCHANGE_ENABLED=true \
  python3 scripts/artificial-exchange-bot.py --dry-run
```

Apply one scan:

```bash
DUNE_ARTIFICIAL_EXCHANGE_ENABLED=true \
DUNE_ARTIFICIAL_EXCHANGE_DRY_RUN=false \
DUNE_ARTIFICIAL_EXCHANGE_PURCHASES_ENABLED=true \
  python3 scripts/artificial-exchange-bot.py \
  --apply \
  --buyer-controller-id 17 \
  --confirm "RUN ARTIFICIAL EXCHANGE"
```

Loop:

```bash
python3 scripts/artificial-exchange-bot.py --loop
```

## Buyer Funding

Native purchases spend the buyer controller's Exchange Solari balance
(`dune_exchange_users.solari_balance`). This is separate from the base Solaris
row in `player_virtual_currency_balances`.

Check readiness and buyer balance:

```bash
python3 scripts/artificial-exchange-bot.py --check-ready --buyer-controller-id 17
```

Fund the buyer Exchange balance:

```bash
DUNE_ARTIFICIAL_EXCHANGE_FUNDING_ENABLED=true \
  python3 scripts/artificial-exchange-bot.py \
  --buyer-controller-id 17 \
  --fund-buyer 10000 \
  --confirm "FUND ARTIFICIAL EXCHANGE"
```

Funding uses:

```sql
dune.dune_exchange_modify_user_solari_balance(controller_id, delta)
```

The command writes a `buyer-funded` audit event. It is intentionally separate
from buying and settlement.

## Settlement

Completed orders are visible through:

```bash
python3 scripts/artificial-exchange-bot.py --settlement-report
```

Statuses:

- `purchased_item_storage`: completed purchased item stored in Exchange storage.
- `seller_solari_claim_ready`: seller Solari claim with a base Solaris balance
  row already present.
- `unsafe_missing_base_solaris_balance`: seller claim where the base Solaris
  row is absent; the bot can repair this safely during claim.
- `seller_claim_has_item_id`: not a normal seller Solari claim.
- `unknown_completion_type`: unrecognized completion type.

Native retrieve warning:

`dune_exchange_retrieve_solaris_from_item(...)` is not used. On this server build
it attempted to set `player_virtual_currency_balances.balance` to `NULL` and
failed a not-null constraint. In rollback testing it also showed it could delete
the claim row without crediting when the base Solaris row was missing.

The bot uses a direct validated transaction for seller Solari claims. Seller
proceeds are credited to the player's bank (`player_virtual_currency_balances`)
and mirrored to the Exchange user balance; no `SolarisCoin` inventory item or
inventory slot is involved:

- ensure the seller has a base Solaris balance row and lock it
- lock the completed seller claim
- verify owner, completion type, item id, stack size, and expected Solari
- credit exactly `item_price * stack_size` to the bank
- ensure the Exchange user row and verify its Solari mirror
- delete the completed claim order
- verify the claim row is gone
- commit only if every check passes
- rollback on any mismatch

Settlement safety boundary:

- settlement reporting is read-only
- claiming requires `DUNE_ARTIFICIAL_EXCHANGE_AUTO_CLAIM_ENABLED=true`
- manual claim commands require confirmation `CLAIM ARTIFICIAL EXCHANGE`
- auto-claim after scan requires
  `DUNE_ARTIFICIAL_EXCHANGE_AUTO_CLAIM_AFTER_SCAN=true`
- the claim transaction is scoped to completed seller Solari claim rows and
  does not touch active player listings

Claim one order:

```bash
DUNE_ARTIFICIAL_EXCHANGE_AUTO_CLAIM_ENABLED=true \
  python3 scripts/artificial-exchange-bot.py \
  --claim-settlement 7 \
  --confirm "CLAIM ARTIFICIAL EXCHANGE"
```

Claim all eligible seller claims in the report limit:

```bash
DUNE_ARTIFICIAL_EXCHANGE_AUTO_CLAIM_ENABLED=true \
  python3 scripts/artificial-exchange-bot.py \
  --claim-all-settlements \
  --confirm "CLAIM ARTIFICIAL EXCHANGE"
```

Auto-claim after each scan:

```bash
DUNE_ARTIFICIAL_EXCHANGE_AUTO_CLAIM_ENABLED=true \
DUNE_ARTIFICIAL_EXCHANGE_AUTO_CLAIM_AFTER_SCAN=true \
  python3 scripts/artificial-exchange-bot.py --loop
```

Auto-claim after scan does not require the manual confirmation phrase because it
is controlled by explicit environment gates and still uses the same validated
transaction.

## Populator

The populator seeds NPC Exchange listings from enabled catalog rows. It is a
liquidity tool, not a default background requirement. Use it when the server
needs controlled, operator-owned market stock; leave it stopped when you only
want the buyer to purchase player listings.

The populator is the seller-side AI. It creates visible Exchange sell orders
owned by the configured DASH/Admin controller, with `is_npc_order=true`, from
catalog rows that pass the current validation gates. Confidence: moderate for
broad live seeding because it uses native Exchange order functions but still
changes the live economy immediately.

Current default row gates:

- `enabled=true`
- `sellable_status=validated`
- `baseline_price` present and positive
- `source` or `notes` prove a dune.exchange market price
- parsed tier is at least `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MIN_TIER`, default
  `2`
- category mask/depth values are non-negative

Current default pricing:

- `planned_unique_price(...)` anchors on `baseline_price`, applies an
  Exchange category multiplier, then applies the global
  `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_PRICE_MULTIPLIER`, default `1.0`, and
  `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_PRICE_JITTER_PCT`.
- Category multipliers are intentionally not uniform:
  - consumables: lower, so utility items such as Iodine Pills remain obtainable
  - raw/refined/components: moderate, so materials are useful but not instant
  - weapons, armor, vehicles, and high-impact tools: higher, so purchases feel
    rewarding and require real Solari effort
  - schematics/patents: conservative, because many catalog rows have flat or
    placeholder source prices
- If the row has `game_file_price=N` in notes and `baseline_price / N` exceeds
  `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_GAME_FILE_OUTLIER_RATIO`, default `8`,
  the populator uses the geometric mean of `baseline_price` and
  `game_file_price` as the anchor. This dampens public-market spikes without
  collapsing normal items to vendor-trash prices.
- If a row has no `baseline_price` but does have both `price_floor` and
  `price_ceiling`, the populator falls back to their midpoint as the anchor.
- `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MIN_PRICE_SPAN` can widen a too-tight
  floor/ceiling range so multiple active orders for the same template get
  unique prices instead of merging through the native recurring-order path.
- `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_PRICE_JITTER_PCT` is used only for rows
  that do not have explicit floor/ceiling values.
- Blueprint/schematic rows are allowed through the baseline-price gate even
  when the imported source price is a placeholder `1`. The pricing anchor is
  raised to at least `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_BLUEPRINT_PRICE_FLOOR`
  plus `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_BLUEPRINT_PRICE_TIER_STEP` per tier
  above tier 2. This keeps schematics present without making them free.
- Categories with official game-file rows but sparse market prices use explicit
  lower floors so tier 0/1 placeholders do not create 1-Solari weapons, tools,
  vehicles, armor, contracts, or patents.

Current default quantity policy:

- Stackable categories are controlled by
  `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_STACKABLE_CATEGORIES`, defaulting to
  consumables, raw/refined/component/fuel resources, and ammo.
- Stackable rows use `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_FULL_STACK_SIZE`,
  default `100`, for both staged item stack size and max stack size.
- The Exchange order price remains the normal item/listing price for the
  template. It is not multiplied by stack size. The audit event includes
  `totalStackPrice` only as an operator reference.
- `--populate-all-once` targets
  `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_STACKABLE_TARGET_ORDERS`, default `13`,
  active full-stack listings per stackable template.
- Singleton rows use stack size `1` and `--populate-all-once` targets
  `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_SINGLETON_CATEGORY_TARGET_ORDERS`,
  default `125`, active listings per singleton category.
- `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MAX_PER_TEMPLATE_PER_CATEGORY`, default
  `8`, still caps duplicate singleton templates within a category, so the
  category target spreads across multiple weapons, armor pieces, vehicles, and
  schematics instead of repeating one template.

Pricing examples from the current curve:

| Template | Item | Category | Typical seeded price shape |
| --- | --- | --- | --- |
| `AntiRadiationPill` | Iodine Pill | Utility consumable | hundreds, not thousands |
| `SpiceAddictionConsumable_04` | Melange Spiced Wine | Utility consumable | low thousands |
| `LandsraadShipwreckComponent1` | Ship Manifest | Misc components | low hundreds |
| `DuraluminumRod` | Duraluminum Ingot | Misc refined resources | hundreds |
| `T5UniqueComponent` | Spice-infused Duraluminum Dust | Misc components | low thousands |
| `T5RadiatedCoreComponent` | Irradiated Slag | Misc components | low thousands after spike damping |
| `MelangeSpice` | Spice Melange | Misc refined resources | tens of thousands after spike damping |
| `AtreLMG3` | House Vulcan GAU-92 | Weapons ranged | tens of thousands |

Outlier handling:

- Public dune.exchange snapshots are useful, but they can contain scarcity
  spikes. Treat them as a market signal, not an absolute truth.
- Rows imported with `game_file_price=N` get an outlier check. If
  `baseline_price / N > DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_GAME_FILE_OUTLIER_RATIO`,
  the anchor becomes `sqrt(baseline_price * game_file_price)`.
- This caught the Spice Melange failure mode: public baseline `61156`, game-file
  price `6500`, ratio `9.4`. With threshold `8`, the anchor drops to about
  `19938` before the refined-resource multiplier and jitter.
- Normal expensive weapons with smaller public/game-file ratios are not damped.
  Example: `RocketLauncher_Unique_Homing_06` had ratio about `3.0`, so it keeps
  the public-market baseline.

Current default item quality:

- `quality_level` comes from the catalog row when present.
- Otherwise it is inferred from the parsed tier.
- Otherwise it falls back to `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_QUALITY_LEVEL`.
- The minimum allowed quality is
  `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MIN_QUALITY_LEVEL`, current broad profile
  `1`.
- Stateful seeded items, currently categories matching
  `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_STATEFUL_STAT_CATEGORIES`, are skipped by
  default unless stats generation is explicitly solved. The Exchange order can
  display a high `quality_level` while an empty `items.stats` payload causes the
  purchased item to materialize with base/grade-0 behavior in client.
- Valid stateful seeded items use
  `backups/admin-panel/artificial-exchange/stats-library.json`, built from real
  non-empty `dune.items.stats` rows. Build or merge the library with
  `scripts/artificial-exchange-bot.py --build-stats-library --apply
  --stats-source-label <source>`, then check coverage with
  `scripts/artificial-exchange-bot.py --stats-library-report`.
- Run `scripts/artificial-exchange-bot.py --audit-seeded-stats` to inspect
  existing seeded orders for empty stats and order/item quality mismatches
  before re-enabling the populator after a market cleanup.

Stateful stats library workflow:

- Build exact samples from a trusted DB source:
  `scripts/artificial-exchange-bot.py --build-stats-library --apply
  --stats-source-label <source>`.
- Merge exact samples from every available lab/live source before deriving.
  The builder ignores NPC-seeded order items so bad generated stock does not
  become training data.
- Fill remaining gaps only after exact samples are exhausted:
  `scripts/artificial-exchange-bot.py --derive-stats-library --apply
  --stats-source-label derived`.
- Derived rows are marked with `derived`, `inference`,
  `inferredFromTemplate`, `inferredFromCategory`, and `derivedFromItemId`.
  They reuse real same-category or same-family stat structures, strip
  customization values, and normalize durability to full condition.
- Before a live refill, run:
  `scripts/artificial-exchange-bot.py --stats-library-report` and require
  `missingRequiredStatefulTemplates: 0`.
- After a live refill, run:
  `scripts/artificial-exchange-bot.py --audit-seeded-stats --limit 30000` and
  require `unsafeStatefulEmptyStats: 0` and `qualityMismatches: 0`.
- Current remediation result on `kspls0`, verified 2026-06-02: `1136 / 1136`
  required stateful templates covered, `0` missing, `1998` seeded NPC item
  orders checked, `0` unsafe stateful empty-stat orders, and `0` order/item
  quality mismatches. Confidence: high for audit coverage; moderate for
  derived-template purchase behavior until representative in-client buy tests
  are performed.

Known grade boundary:

- The local Exchange and item tables expose numeric `quality_level` values.
- The tooling observed and can create `quality_level=0` and `quality_level=1`.
- The catalog/import metadata exposes tier markers through at least tier `6`.
- Operational answer: there are at least seven numeric quality/tier buckets if
  counting `0` through `6`; the broad profile sets the populator minimum to
  `1`, so only tier 0 is excluded by the quality gate. Confidence: moderate
  because the DB stores numeric levels but does not include a canonical
  grade-name table.

Exchange category model:

- Exchange category masks are not generic item category tags. The native client
  has a separate Exchange category tree under
  `/Game/Dune/GUI/Data/ItemCategories/`.
- `strings` inspection of the local client `GUI.pak` exposed the actual
  Exchange-facing category assets and tag queries, including:
  - `DA_y_Consumables` -> `UI/GameItemCategory_Utility_Consumables`
  - `DA_y_Components` -> `UI/GameItemCategory_Misc_Components`
  - `DA_y_RawResources` -> `UI/GameItemCategory_Misc_RawResources`
  - `DA_y_RefinedResources` -> `UI/GameItemCategory_Misc_RefinedResources`
  - `DA_y_Fuel` -> `UI/GameItemCategory_Misc_Fuel`
  - `DA_y_Ammunition` -> `UI/GameItemCategory_Weapons_Ammunition`
  - `DA_y_HydrationTools`, `DA_y_GatheringTools`, and
    `DA_yrtographyTools`
  - `DA_y_Rifles`, `DA_y_LongBlades`, `DA_y_Sandbike`,
    `DA_y_LightOrnithopter`, `DA_y_MediumOrnithopter`,
    `DA_y_TransportOrnithopter`, `DA_y_Sandcrawler`, and related
    weapon/vehicle/wearable buckets
  - `DA_Exchangey_UniqueSchematics_*` buckets for utility, garments, vehicles,
    weapons, and augments
- Observed client/server category refresh writes confirmed the masks used by
  representative live orders. Examples:
  - Iodine Pill: `50725376/3`
  - Ship Manifest and crafted components: `84017152/2`
  - Duraluminum Ingot and refined resources: `83951616/2`
  - House Vulcan GAU-92: `16844544/3`
  - Artisan Sword: `16777472/3`
  - Sandbike Boost Mk3: `33555712/3`
  - Scout Ornithopter Thruster Mk4: `33687040/3`
- The shared authoritative map lives in
  `scripts/exchange_category_map.py`. Both
  `scripts/import-exchange-category-map.py` and
  `scripts/build-exchange-catalog.py` use it, and the catalog builder performs a
  final category-mask reconcile across all rows so stale CSV/bootstrap masks do
  not leak into seeded listings.
- The generated source category map lives at
  `backups/admin-panel/artificial-exchange/source-category-map.json`. It is
  rebuilt from `api.awakening.wiki` item tags, then reconciled through the local
  Exchange mask table.
- The verified category map lives at
  `backups/admin-panel/artificial-exchange/verified-category-map.json`. It is
  exported by `scripts/observe-exchange-category-updates.py` from observed
  client writes to `update_sell_orders_categories(...)`. Use it for high-risk
  probe work or template-specific verification.
- `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_REQUIRE_SOURCE_CATEGORY=true` is the
  preferred broad live gate. It requires the source category map and rejects
  rows whose source category disagrees with the catalog.
- A narrow fallback admits source-reconciled sparse rows for patents, social
  armor, contracts, hydration/gathering/cartography/mining/deployable tools,
  and schematic subcategories even when public market data is thin. Confidence:
  moderate, because the masks come from local game category assets but those
  sparse buckets still have less public-market data.
- Augment categories exist in the game category tree, but the public item API
  currently exposes no sellable `Items.Augment` or `Items.Schematics.Augments`
  templates. Do not seed guessed `DA_AUGMENT_*` asset names unless a client or
  DB observation proves they are valid Exchange item template ids.
- `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_CATEGORY_SEEDING_VERIFIED=true` switches
  to template-specific verified masks. This is stricter and useful for probes,
  but it only seeds templates present in the verified map.
- `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_SKIP_UNKNOWN_CATEGORY=true` blocks rows
  with `category=unknown` or mask/depth `0/0`.
- `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_PROTECT_AUGMENTS_CATEGORY=true` remains a
  last-resort guard against known bad Augments masks.
- Blueprint-like rows are allowed only in blueprint categories:
  `schematics/weapons`, `schematics/armor`, `schematics/vehicles`, and
  `building/patents`.
- `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MAX_PER_TEMPLATE_PER_CATEGORY=8` is the
  current broad duplicate cap. Population modes must not create more than eight
  active NPC listings for the same template in the same category.
- The optional watchdog treats `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_ENABLED` as
  the source of truth. Keep that gate false for one-shot category seeding.

Single-template safety:

- Listings are NPC orders with `is_npc_order=true`.
- The buyer skips NPC orders and configured populator owners by default.
- The populator avoids duplicate active prices for the same template in one run
  so native recurring-order merging does not silently turn requested listings
  into fewer active orders.
- Apply mode fails if the native recurring sell function reports that no
  inventory was added.

Populator safety boundary:

- service/loop seeding requires `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_ENABLED=true`
- service/loop seeding requires `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_DRY_RUN=false`
- one-shot CLI seeding still requires `--apply` and confirmation
  `POPULATE ARTIFICIAL EXCHANGE`
- broad row gates require validated, reviewed rows with source category
  evidence; market price and tier 2+ gates can be re-enabled for conservative
  validation runs
- broad seeding should use a freshly rebuilt source category map and catalog
- strict probe seeding can enable
  `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_CATEGORY_SEEDING_VERIFIED=true`
- cleanup/purge operations must be scoped by Exchange id or configured
  populator owner; back up rows first

Owner decision:

- Use a dedicated DASH/Admin player controller for
  `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_OWNER_ID`, not a normal human character.
- Add that same id to `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_OWNER_IDS` so the
  buyer never purchases seeded stock.
- On the current host, the DASH/Admin identity is `Paul` controller `124`; this
  is local state, not a portable default.
- Use a separate non-player Exchange identity for
  `DUNE_ARTIFICIAL_EXCHANGE_BUYER_CONTROLLER_ID`; never reuse the populator
  owner. The native order-slot limit defaults to `50` through
  `DUNE_ARTIFICIAL_EXCHANGE_MAX_ORDERS_PER_PLAYER`.

Service decision:

- The populator service uses `Restart=always` when installed.
- Stop and disable it when no seeding should happen:

```bash
sudo systemctl disable --now dune-artificial-exchange-populator.service
```

- The buyer service can remain active while the populator is stopped.

Recommended broad seed workflow:

```bash
python3 scripts/import-exchange-category-map.py
python3 scripts/build-exchange-catalog.py --no-db
make test-artificial-exchange

DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_TARGET_MIN_ORDERS=0 \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_TARGET_MAX_ORDERS=20000 \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_HARD_MAX_ORDERS=20000 \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_USE_CATEGORY_TARGETS=true \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MIN_TIER=0 \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MIN_QUALITY_LEVEL=1 \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MIN_BASELINE_PRICE=1 \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MIN_PRICE_SPAN=200 \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MAX_PER_TEMPLATE_PER_CATEGORY=8 \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_STACKABLE_TARGET_ORDERS=13 \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_SINGLETON_CATEGORY_TARGET_ORDERS=125 \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_FULL_STACK_SIZE=100 \
  python3 scripts/artificial-exchange-bot.py \
  --populate-all-once \
  --catalog backups/admin-panel/artificial-exchange/catalog.json \
  --dry-run \
  --populator-owner-id 124 \
  --populator-source-inventory-id 485 \
  --confirm "POPULATE ARTIFICIAL EXCHANGE"
```

If the dry run looks correct, apply the same command with `--apply`:

```bash
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_TARGET_MIN_ORDERS=0 \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_TARGET_MAX_ORDERS=20000 \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_HARD_MAX_ORDERS=20000 \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_USE_CATEGORY_TARGETS=true \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MIN_TIER=0 \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MIN_QUALITY_LEVEL=1 \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MIN_BASELINE_PRICE=1 \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MIN_PRICE_SPAN=200 \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MAX_PER_TEMPLATE_PER_CATEGORY=8 \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_STACKABLE_TARGET_ORDERS=13 \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_SINGLETON_CATEGORY_TARGET_ORDERS=125 \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_FULL_STACK_SIZE=100 \
  python3 scripts/artificial-exchange-bot.py \
  --populate-all-once \
  --catalog backups/admin-panel/artificial-exchange/catalog.json \
  --apply \
  --populator-owner-id 124 \
  --populator-source-inventory-id 485 \
  --confirm "POPULATE ARTIFICIAL EXCHANGE"
```

Replace `124` and `485` with the local DASH/Admin controller and source
inventory ids. Those values are host-local and not portable.

Dry-run one populate pass:

```bash
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_ENABLED=true \
  python3 scripts/artificial-exchange-bot.py --populate-once
```

Apply one populate pass:

```bash
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_ENABLED=true \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_DRY_RUN=false \
  python3 scripts/artificial-exchange-bot.py \
  --populate-once \
  --apply \
  --populator-owner-id 17 \
  --populator-source-inventory-id 485 \
  --confirm "POPULATE ARTIFICIAL EXCHANGE"
```

Run the populator loop:

```bash
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_ENABLED=true \
  python3 scripts/artificial-exchange-bot.py --populate-loop
```

Populate to a minimum per available catalog subcategory:

```bash
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_ENABLED=true \
  python3 scripts/artificial-exchange-bot.py \
  --populate-categories-once \
  --populator-owner-id 17 \
  --populator-source-inventory-id 485 \
  --populator-target-min-orders 20
```

Apply mode:

```bash
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_ENABLED=true \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_DRY_RUN=false \
  python3 scripts/artificial-exchange-bot.py \
  --populate-categories-once \
  --apply \
  --populator-owner-id 17 \
  --populator-source-inventory-id 485 \
  --populator-target-min-orders 20 \
  --confirm "POPULATE ARTIFICIAL EXCHANGE"
```

Populate one listing for every eligible, enabled catalog template that is not
already seeded:

```bash
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_ENABLED=true \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_DRY_RUN=true \
  python3 scripts/artificial-exchange-bot.py \
  --populate-all-once \
  --populator-owner-id 17 \
  --populator-source-inventory-id 485
```

Apply mode:

```bash
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_ENABLED=true \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_DRY_RUN=false \
  python3 scripts/artificial-exchange-bot.py \
  --populate-all-once \
  --apply \
  --populator-owner-id 17 \
  --populator-source-inventory-id 485 \
  --confirm "POPULATE ARTIFICIAL EXCHANGE"
```

Populate up to a fixed active order target for every eligible template:

```bash
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_ENABLED=true \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_TEMPLATE_TARGET_ORDERS=20 \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_REQUIRE_MARKET_PRICE=true \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MIN_TIER=3 \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MAX_PER_TEMPLATE_PER_CATEGORY=2 \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MIN_PRICE_SPAN=20 \
  python3 scripts/artificial-exchange-bot.py \
  --populate-templates-once \
  --populator-owner-id 17 \
  --populator-source-inventory-id 485 \
  --limit 50000
```

Apply mode:

```bash
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_ENABLED=true \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_DRY_RUN=false \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_TEMPLATE_TARGET_ORDERS=20 \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_REQUIRE_MARKET_PRICE=true \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MIN_TIER=3 \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MAX_PER_TEMPLATE_PER_CATEGORY=2 \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MIN_PRICE_SPAN=20 \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_TARGET_MAX_ORDERS=50000 \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_HARD_MAX_ORDERS=50000 \
  python3 scripts/artificial-exchange-bot.py \
  --populate-templates-once \
  --apply \
  --populator-owner-id 17 \
  --populator-source-inventory-id 485 \
  --limit 50000 \
  --confirm "POPULATE ARTIFICIAL EXCHANGE"
```

This mode is intentionally one-shot. It does not change the installed
`--populate-loop` service behavior. Leave the service stopped unless you
explicitly want the older global target loop to run.

Observe client-verified Exchange category masks:

```bash
python3 scripts/observe-exchange-category-updates.py --install --force-hash-mismatch -1 --export
```

Then seed a small set of market-priced probe orders, open or refresh the
Exchange in a game client, and export the observed map:

```bash
python3 scripts/observe-exchange-category-updates.py --export
```

The export writes
`backups/admin-panel/artificial-exchange/verified-category-map.json`. If the
export reports `templates: 0`, no client has supplied category updates yet.
Use this path for strict template-level category proof. Broad seeding can use
the source category map plus `scripts/exchange_category_map.py` after catalog
reconciliation. Confidence: high for observed templates; moderate-high for
broad categories backed by GUI assets and source tags.

Run expiry and over-cap cleanup for seeded NPC listings:

```bash
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_ENABLED=true \
  python3 scripts/artificial-exchange-bot.py --expire-seeded
```

Cleanup is scoped to:

- target exchange id
- configured populator owner id
- `is_npc_order=true`

It does not select human orders.

Expire all current seeded NPC listings for the configured owner through the bot:

```bash
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_TARGET_MIN_ORDERS=0 \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_TARGET_MAX_ORDERS=0 \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_HARD_MAX_ORDERS=5000 \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_EXPIRE_PROBABILITY=1 \
  python3 scripts/artificial-exchange-bot.py \
  --expire-seeded \
  --limit 10000 \
  --apply \
  --confirm "POPULATE ARTIFICIAL EXCHANGE"
```

Verify the purge:

```sql
select owner_id, is_npc_order, count(*)
from dune.dune_exchange_orders
where exchange_id = 2
group by owner_id, is_npc_order
order by owner_id, is_npc_order;
```

Emergency hard purge for one Exchange:

Use this when the visible market must be forced to zero before validation. It
backs up the targeted rows, deletes matching sell-order children, deletes the
orders, and verifies the Exchange is empty. This bypasses expiry behavior and is
intentionally scoped to one `exchange_id`.

```bash
ts=$(date -u +%Y%m%dT%H%M%SZ)
out="backups/admin-panel/artificial-exchange/live-runs/${ts}-before-exchange-2-purge.json"

docker compose --env-file .env -f compose.yaml exec -T postgres \
  psql -U dune -d dune_sb_1_4_0_0 -Atc \
  "copy (select row_to_json(o) from dune.dune_exchange_orders o where exchange_id=2 order by id) to stdout" \
  > "$out"

docker compose --env-file .env -f compose.yaml exec -T postgres \
  psql -U dune -d dune_sb_1_4_0_0 -v ON_ERROR_STOP=1 -Atc \
  "delete from dune.dune_exchange_sell_orders s using dune.dune_exchange_orders o where s.order_id=o.id and o.exchange_id=2;
   delete from dune.dune_exchange_orders where exchange_id=2;
   select 'exchange2_orders', count(*) from dune.dune_exchange_orders where exchange_id=2;"
```

Emergency hard purge for only DASH/Admin seeded orders:

```bash
ts=$(date -u +%Y%m%dT%H%M%SZ)
out="backups/admin-panel/artificial-exchange/live-runs/${ts}-before-owner-124-purge.json"

docker compose --env-file .env -f compose.yaml exec -T postgres \
  psql -U dune -d dune_sb_1_4_0_0 -Atc \
  "copy (select row_to_json(o) from dune.dune_exchange_orders o where owner_id=124 order by id) to stdout" \
  > "$out"

docker compose --env-file .env -f compose.yaml exec -T postgres \
  psql -U dune -d dune_sb_1_4_0_0 -v ON_ERROR_STOP=1 -Atc \
  "delete from dune.dune_exchange_sell_orders s using dune.dune_exchange_orders o where s.order_id=o.id and o.owner_id=124;
   delete from dune.dune_exchange_orders where owner_id=124;
   select 'owner124_orders', count(*) from dune.dune_exchange_orders where owner_id=124;"
```

Run a guarded one-listing validation:

```bash
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_ENABLED=true \
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_LIVE_VALIDATION_ENABLED=true \
  python3 scripts/artificial-exchange-bot.py \
  --validate-populator-once \
  --populator-owner-id 17 \
  --populator-source-inventory-id 485
```

With default dry-run behavior this reports the planned listing. With `--apply`
and `--confirm "POPULATE ARTIFICIAL EXCHANGE"`, it creates exactly one seeded
NPC listing at a validation-only price, runs a buyer dry-run scan to confirm the
new order is skipped, then deletes only the newly-created seeded order id.

The buyer skips NPC orders and populator-owned orders by default, so it does not
buy its own seeded liquidity. Use `--include-npc-test-orders` only for controlled
validation.

## Environment Reference

Global buyer:

```env
DUNE_ARTIFICIAL_EXCHANGE_ENABLED=true
DUNE_ARTIFICIAL_EXCHANGE_DRY_RUN=true
DUNE_ARTIFICIAL_EXCHANGE_PURCHASES_ENABLED=false
DUNE_ARTIFICIAL_EXCHANGE_ID=2
DUNE_ARTIFICIAL_EXCHANGE_ACCESS_POINT_ID=1
DUNE_ARTIFICIAL_EXCHANGE_BUYER_CONTROLLER_ID=0
DUNE_ARTIFICIAL_EXCHANGE_MAX_ORDERS_PER_PLAYER=50
DUNE_ARTIFICIAL_EXCHANGE_SCAN_LIMIT=25000
DUNE_ARTIFICIAL_EXCHANGE_SCAN_INTERVAL_MIN_SECONDS=60
DUNE_ARTIFICIAL_EXCHANGE_SCAN_INTERVAL_MAX_SECONDS=120
DUNE_ARTIFICIAL_EXCHANGE_SERVICE_CONFIRM=RUN ARTIFICIAL EXCHANGE
```

Budgets and probabilities:

```env
DUNE_ARTIFICIAL_EXCHANGE_DAILY_SOLARI_CAP=50000
DUNE_ARTIFICIAL_EXCHANGE_DAILY_SELLER_CAP=10000
DUNE_ARTIFICIAL_EXCHANGE_DAILY_TEMPLATE_CAP=15000
DUNE_ARTIFICIAL_EXCHANGE_MAX_BUY_PRICE_TOLERANCE_PCT=10
DUNE_ARTIFICIAL_EXCHANGE_LOW_BUY_PROBABILITY=0.10
DUNE_ARTIFICIAL_EXCHANGE_MEDIUM_BUY_PROBABILITY=0.25
DUNE_ARTIFICIAL_EXCHANGE_HIGH_BUY_PROBABILITY=0.50
DUNE_ARTIFICIAL_EXCHANGE_PURCHASE_NOTIFY_ENABLED=true
DUNE_ARTIFICIAL_EXCHANGE_PURCHASE_NOTIFY_TEMPLATE=Your Exchange listing was purchased: {count}x {template_id} for {price} Solari. The Solari will be in your bank after your next relog.
DUNE_ARTIFICIAL_EXCHANGE_PURCHASE_NOTIFY_EXCHANGE=chat.whispers
DUNE_ARTIFICIAL_EXCHANGE_PURCHASE_NOTIFY_CHANNEL=Whispers
DUNE_ARTIFICIAL_EXCHANGE_PURCHASE_NOTIFY_ROUTING_KEY=
DUNE_ARTIFICIAL_EXCHANGE_BLOCKED_SELLERS=
```

Settlement and funding:

```env
DUNE_ARTIFICIAL_EXCHANGE_AUTO_CLAIM_ENABLED=false
DUNE_ARTIFICIAL_EXCHANGE_AUTO_CLAIM_AFTER_SCAN=false
DUNE_ARTIFICIAL_EXCHANGE_FUNDING_ENABLED=false
```

Meaning:

- `DUNE_ARTIFICIAL_EXCHANGE_AUTO_CLAIM_ENABLED` gates any settlement write.
- `DUNE_ARTIFICIAL_EXCHANGE_AUTO_CLAIM_AFTER_SCAN` runs the same settlement
  claim logic after buyer scans.
- `DUNE_ARTIFICIAL_EXCHANGE_FUNDING_ENABLED` gates explicit buyer Exchange
  balance funding through the native balance function.

Populator:

```env
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_ENABLED=false
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_DRY_RUN=true
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_LIVE_VALIDATION_ENABLED=false
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_OWNER_ID=0
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_OWNER_IDS=
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_SOURCE_INVENTORY_ID=0
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_SOURCE_POSITION_START=0
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_SOURCE_POSITION_MAX=100000
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_TARGET_MIN_ORDERS=4000
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_TARGET_MAX_ORDERS=20000
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_HARD_MAX_ORDERS=20000
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_USE_CATEGORY_TARGETS=true
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_PRICE_JITTER_PCT=20
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_PRICE_MULTIPLIER=1.0
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_GAME_FILE_OUTLIER_RATIO=8
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MAX_PER_TEMPLATE_PER_CATEGORY=8
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_STACKABLE_TARGET_ORDERS=13
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_SINGLETON_CATEGORY_TARGET_ORDERS=125
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_FULL_STACK_SIZE=100
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_STACKABLE_CATEGORIES=consumables/medical,consumables/spice,resources/components,resources/fuel,resources/raw,resources/refined,vehicles/ammunition,weapons/ammunition
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MIN_PRICE_SPAN=200
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_BLUEPRINT_PRICE_FLOOR=2500
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_BLUEPRINT_PRICE_TIER_STEP=1500
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_EXPIRY_MIN_SECONDS=3600
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_EXPIRY_MAX_SECONDS=86400
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_EXPIRE_PROBABILITY=0.10
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_FORCE_COUNT=
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_VALIDATION_PRICE=
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_REQUIRE_VALIDATED=true
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_REQUIRE_MARKET_PRICE=false
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_ALLOW_UNPRICED_SEEDING=true
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MIN_TIER=0
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_STACK_SIZE=1
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MAX_STACK_SIZE=1
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_CATEGORY_MASK=0
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_CATEGORY_DEPTH=0
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_DURABILITY_CUR=1
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_DURABILITY_MAX=1
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MIN_QUALITY_LEVEL=1
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_QUALITY_LEVEL=2
```

This block documents the broad private-server profile. For a conservative
first validation run, keep `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_DRY_RUN=true`,
set `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_TARGET_MIN_ORDERS=20`, set
`DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_TARGET_MAX_ORDERS=80`, and keep
`DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_REQUIRE_MARKET_PRICE=true`. For broad
population, `REQUIRE_MARKET_PRICE=false` plus `ALLOW_UNPRICED_SEEDING=true`
allows reviewed game-file rows without public market observations. Confidence:
high.

`DUNE_ARTIFICIAL_EXCHANGE_SCAN_LIMIT=25000` matters for broad seeded markets.
If this is too low, the bot may not see enough existing seeded rows to make
correct duplicate, cap, and under-target decisions.

`DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_USE_CATEGORY_TARGETS=true` changes the
planner from a simple global fill to category coverage. The populator first
counts eligible catalog rows by reconciled category, then fills empty and
under-target categories before adding broad stock. This is the mode that
prevents categories such as ammunition, tools, deployables, schematics, fuel,
armor, vehicles, and resources from being skipped while a few high-volume
categories consume the order budget.

Pricing knobs:

- `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_PRICE_MULTIPLIER` is a global scalar on
  top of the built-in category multipliers. Keep it at `1.0` for the documented
  curve. Raise or lower it only when the whole seeded economy is too expensive
  or too cheap.
- `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_GAME_FILE_OUTLIER_RATIO` controls when a
  public-market baseline is treated as a spike relative to `game_file_price`.
  Lower values damp more rows; higher values trust dune.exchange more.
- `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_PRICE_JITTER_PCT` spreads listings around
  the computed anchor so the market does not look static.
- `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MIN_PRICE_SPAN` prevents duplicate prices
  from merging into a single native recurring order.

## Bootstrap Catalog

Generate a broad heuristic catalog from locally observed templates:

```bash
python3 scripts/build-exchange-bootstrap-catalog.py --limit-per-category 20
python3 scripts/build-exchange-catalog.py
```

The bootstrap builder reads templates from:

- `dune.items`
- `dune.vehicle_modules`
- `dune.landsraad_house_rewards`
- `dune.landsraad_task_rewards`

It skips obvious non-market entries:

- emotes
- contract items
- SolarisCoin
- swatches and dye packs
- building blueprints
- known internal/test-looking ids

It classifies by template-id heuristics into these subcategories:

- `resources/raw`
- `resources/refined`
- `resources/components`
- `consumables/medical`
- `tools/mining`
- `tools/utility`
- `weapons/melee`
- `weapons/ranged`
- `armor/combat`
- `armor/stillsuit`
- `armor/social`
- `vehicles/sandbike`
- `vehicles/ornithopter`
- `vehicles/parts`
- `schematics/weapons`
- `schematics/armor`
- `schematics/vehicles`

The generated rows are marked:

- `sellable_status=observed`
- `enabled=false`
- `source=local-bootstrap`
- `confidence=low`

That means they are discovery placeholders only. Prices are heuristic category
defaults with simple tier multipliers and are not production prices. Confidence:
low. Do not use them for live market seeding.

There is an explicit unsafe override:

```bash
python3 scripts/build-exchange-bootstrap-catalog.py --enable-heuristic-prices
```

That marks heuristic rows as enabled and validated. Use it only for isolated
test servers, never for the real market population.

## Services

Render/install buyer service:

```bash
make install-artificial-exchange-buyer-service ENV_FILE=.env
```

Render/install populator service:

```bash
make install-artificial-exchange-populator-service ENV_FILE=.env
```

Render/install the watchdog timer:

```bash
make install-artificial-exchange-watchdog-timer ENV_FILE=.env
```

Direct installer syntax:

```bash
scripts/install-artificial-exchange-service.sh .env /etc/systemd/system/dune-artificial-exchange-bot.service buyer
scripts/install-artificial-exchange-service.sh .env /etc/systemd/system/dune-artificial-exchange-populator.service populator
scripts/install-artificial-exchange-watchdog-timer.sh .env
```

The service unit:

- reads the selected `EnvironmentFile`
- runs `build-exchange-catalog.py` as `ExecStartPre`
- runs either buyer loop or populator loop
- does not hardcode artificial Exchange gates
- enables at boot and starts immediately when installed under
  `/etc/systemd/system`
- uses `Restart=always` so it comes back after process crashes

The watchdog timer runs once per minute. If `.env` says the buyer or populator
gate is enabled, the corresponding systemd unit is enabled, and that unit is
inactive, the watchdog starts it. This covers accidental/manual clean stops,
which `Restart=always` intentionally does not recover.

Prefer buyer and populator as separate services for clearer operations. The
installer also supports `both` for a combined process when explicitly selected.

## Command Reference

Catalog:

```bash
python3 scripts/build-exchange-catalog.py
python3 scripts/build-exchange-catalog.py --no-db
python3 scripts/build-exchange-catalog.py --manual config/artificial-exchange-prices.csv --snapshot-dir data/exchange-price-snapshots
```

Inspection:

```bash
python3 scripts/artificial-exchange-bot.py --check-ready
python3 scripts/artificial-exchange-bot.py --settlement-report
```

Buyer:

```bash
python3 scripts/artificial-exchange-bot.py --dry-run
python3 scripts/artificial-exchange-bot.py --loop
python3 scripts/artificial-exchange-bot.py --apply --buyer-controller-id <id> --confirm "RUN ARTIFICIAL EXCHANGE"
```

Settlement:

```bash
python3 scripts/artificial-exchange-bot.py --claim-settlement <order_id> --confirm "CLAIM ARTIFICIAL EXCHANGE"
python3 scripts/artificial-exchange-bot.py --claim-all-settlements --confirm "CLAIM ARTIFICIAL EXCHANGE"
```

Funding:

```bash
python3 scripts/artificial-exchange-bot.py --buyer-controller-id <id> --fund-buyer <amount> --confirm "FUND ARTIFICIAL EXCHANGE"
```

Populator:

```bash
python3 scripts/artificial-exchange-bot.py --populate-once
python3 scripts/artificial-exchange-bot.py --populate-loop
python3 scripts/artificial-exchange-bot.py --populate-categories-once
python3 scripts/artificial-exchange-bot.py --populate-all-once
python3 scripts/artificial-exchange-bot.py --expire-seeded
```

Test-order escape hatch:

```bash
python3 scripts/artificial-exchange-bot.py --include-npc-test-orders --dry-run
```

Use the escape hatch only for controlled validation. It bypasses the normal NPC
and populator-owner buyer skips.

## Smoke Checks

Safe full smoke check:

```bash
make artificial-exchange-smoke
```

It runs:

- catalog rebuild
- readiness check
- settlement report
- buyer dry-run scan
- populator dry-run plan if `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_OWNER_ID` is set
- service render validation

Standard repo validation:

```bash
make validate
```

Artificial Exchange-specific tests:

```bash
python3 scripts/test-artificial-exchange.py
./scripts/test-artificial-exchange-service.sh
python3 -m py_compile scripts/artificial-exchange-bot.py scripts/build-exchange-catalog.py
```

## Logging

The bot writes structured JSON events to stdout/stderr for `journalctl` and
important state transitions to:

```bash
backups/admin-panel/artificial-exchange/bot-audit.jsonl
```

Systemd sets `PYTHONUNBUFFERED=1`, so service logs are emitted immediately.

Follow the buyer service:

```bash
journalctl -u dune-artificial-exchange-bot.service -f
```

Inspect recent failures and restarts:

```bash
systemctl status dune-artificial-exchange-bot.service --no-pager --lines=80
journalctl -u dune-artificial-exchange-bot.service -n 200 --no-pager
```

Useful event names:

- `bot-start`: process start and CLI mode.
- `loop-iteration-start`: each buyer/populator loop pass.
- `scan-start` and `scan-complete`: buyer scan boundaries and counts.
- `catalog-loaded`: catalog path and enabled item count.
- `db-connect-attempt` and `db-connect-ok`: database connection target/result.
- `purchase-attempt` and `purchase-result`: live purchase execution.
- `settlement-claim-start` and `settlement-claim-complete`: manual claim path.
- `settlement-auto-claim-start` and `settlement-auto-claim-complete`: auto-claim
  pass.
- `populate-start` and `populate-complete`: populator pass.
- `loop-iteration-failed`: exception inside a service loop iteration.
- `bot-fatal`: top-level crash; includes exception type and traceback.

The audit JSONL file records purchase selections, settlement observations,
claim results, funding events, populator plans, and failure payloads. Use it
when journald has rotated:

```bash
tail -n 100 backups/admin-panel/artificial-exchange/bot-audit.jsonl
```

## Live Validation Record

Current populator policy as of the broad category-targeted seeding update:

- Populator service can run deliberately when broad seeded stock is desired.
- Current broad eligible set includes reviewed game-file rows with source
  category evidence, even when public market pricing is sparse.
- The latest audit observed `1758` eligible catalog rows, `5432` live seeded
  orders, `1176` live seeded templates, `50` live categories, and `0` planned
  additions.
- Existing bad rows from older mappings were purged before the current seed.
  A normal Exchange query should show only rows matching the current catalog
  masks and owner scope.
- Older broad-population runs below are retained only as historical validation
  of mechanics and cleanup behavior. They are not the current production
  seeding recommendation.

Validated on May 20, 2026:

- Exchange id: `2`
- Access point id: `1`
- Test order: `5`
- Template: `PowerPack`
- Price: `123`
- Owner/controller: `17`

Purchase result:

- dry-run selected order `5`
- apply mode bought order `5`
- native fulfill returned `item_id=33256527`
- original sell order `5` was removed
- purchased item storage order `6` was created
- seller Solari claim order `7` was created
- buyer Exchange balance returned to `0`

Settlement result:

- native retrieve was rejected as unsafe after failed validation
- direct validated settlement credited controller `17` exactly `123` base Solaris
- claim order `7` was deleted
- purchased item storage order `6` remains

Historical known live state after first service validation:

- active seeded NPC orders: `20`
- active player orders selected by buyer: `0`
- completed purchased-item storage order `6`
- no pending seller claim rows
- no unsafe settlement rows
- controller `17` base Solaris balance `0` after buyer funding
- buyer Exchange balance `123`
- active seeded item rows are in Exchange inventory `485`
- stale orphaned Exchange inventory items from earlier bad staging tests were
  purged

Historical category populate run on May 21, 2026:

- backup before category populate:
  `backups/admin-panel/artificial-exchange/live-runs/20260521T022133Z-before-category-populate.sql`
- generated bootstrap catalog rows: `151`
- active seeded NPC orders after apply: `340`
- seeded categories: `17`
- target per category: `20`
- minimum quality: `1`
- rows below minimum quality: `0`
- buyer dry-run selected: `0`
- buyer dry-run skipped seeded orders: `340`, all via `npc order skipped`

Historical service validation on May 20, 2026 local time:

- `config/artificial-exchange-prices.csv` contains a broad disabled catalog and
  a reviewed enabled seed subset of `20` rows.
- `build-exchange-catalog.py` rebuilt a catalog with `2278` known rows and `20`
  enabled rows.
- `dune-artificial-exchange-bot.service` and
  `dune-artificial-exchange-populator.service` were both enabled and active.
- Both units use `Restart=always` and `RestartSec=15s`.
- Both units were killed with `SIGKILL`; systemd restarted both automatically.
- After restart, `NRestarts=1` for both units, `ActiveState=active`, and
  `UnitFileState=enabled`.
- The buyer loop skipped all `20` seeded NPC orders and selected `0`.
- The populator preflight passed, saw `20` active seeded orders, planned `0`,
  and deleted `0`.
- Cleanup was fixed to preserve
  `DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_TARGET_MIN_ORDERS`.
- Cleanup now deletes the selected order rows first, then deletes only unreferenced
  staged items from the native Exchange inventory.
- Earlier orphaned staging items were purged after verifying no active order
  referenced them.

Price research run after rollback:

- `scripts/research-exchange-prices.py --crawl-allpages --no-search-fallback`
  crawled Awakening Wiki mainspace pages in revision batches.
- Extracted exact wiki price records: `1259`.
- Matched locally observed seedable templates: `121`.
- Missed locally observed seedable templates: `30`.
- Output:
  `data/exchange-price-snapshots/wiki-base-vendor-prices.csv`.

Verified category counts:

| Category | Orders | Native mask filter count |
| --- | ---: | ---: |
| `armor/combat` | 20 | 20 |
| `armor/social` | 20 | 20 |
| `armor/stillsuit` | 20 | 20 |
| `consumables/medical` | 20 | 20 |
| `resources/components` | 20 | 20 |
| `resources/raw` | 20 | 20 |
| `resources/refined` | 20 | 20 |
| `schematics/armor` | 20 | 20 |
| `schematics/vehicles` | 20 | 20 |
| `schematics/weapons` | 20 | 20 |
| `tools/mining` | 20 | 20 |
| `tools/utility` | 20 | 20 |
| `vehicles/ornithopter` | 20 | 20 |
| `vehicles/parts` | 20 | 20 |
| `vehicles/sandbike` | 20 | 20 |
| `weapons/melee` | 20 | 20 |
| `weapons/ranged` | 20 | 20 |

Rejected full catalog population run on May 21, 2026:

- backup before populate-all:
  `backups/admin-panel/artificial-exchange/live-runs/20260521T023525Z-before-populate-all.sql`
- enabled catalog rows: `2278`
- eligible validated rows: `2278`
- dry-run planned rows: `2278`
- apply planned rows: `2278`
- active seeded NPC orders after apply: `2278`
- distinct seeded templates after apply: `2278`
- seeded price range after apply: `1` to `568078`
- source inventory id: `485`

Decision after review: this was too broad and included too many 1-Solari rows
and stale category assignments. Do not repeat this run on the live market. The
current policy uses category-aware floors, source-category reconciliation,
outlier damping, a 4k minimum target, and a 20k hard ceiling.

## Recovery And Rollback

Before live mutation tests, take a narrow dump:

```bash
docker compose --env-file .env exec -T postgres \
  pg_dump -U dune -d dune_sb_1_4_0_0 \
  -n dune \
  -t dune.dune_exchange_orders \
  -t dune.dune_exchange_sell_orders \
  -t dune.dune_exchange_fulfilled_orders \
  -t dune.dune_exchange_users \
  -t dune.player_virtual_currency_balances \
  --data-only --inserts \
  > backups/dune-exchange-before-$(date -u +%Y%m%dT%H%M%SZ).sql
```

Audit trail:

```bash
tail -n 100 backups/admin-panel/artificial-exchange/bot-audit.jsonl
cat backups/admin-panel/artificial-exchange/bot-state.json
```

Disable all live behavior:

```env
DUNE_ARTIFICIAL_EXCHANGE_ENABLED=false
DUNE_ARTIFICIAL_EXCHANGE_PURCHASES_ENABLED=false
DUNE_ARTIFICIAL_EXCHANGE_AUTO_CLAIM_ENABLED=false
DUNE_ARTIFICIAL_EXCHANGE_AUTO_CLAIM_AFTER_SCAN=false
DUNE_ARTIFICIAL_EXCHANGE_FUNDING_ENABLED=false
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_ENABLED=false
```

Stop services:

```bash
sudo systemctl disable --now dune-artificial-exchange-bot.service
sudo systemctl disable --now dune-artificial-exchange-populator.service
```

## Operational Sequence

Recommended rollout:

1. Edit `config/artificial-exchange-prices.csv`.
2. Run `python3 scripts/build-exchange-catalog.py`.
3. Run `make artificial-exchange-smoke`.
4. Run `python3 scripts/artificial-exchange-bot.py --check-ready --buyer-controller-id <id>`.
5. Fund the buyer Exchange balance if purchases will be enabled.
6. Start buyer service in dry-run.
7. Inspect `bot-audit.jsonl` and settlement reports.
8. Enable purchases only after dry-runs select expected orders.
9. Enable auto-claim after settlement reports are clean.
10. Start populator separately only if seeded liquidity is desired.
