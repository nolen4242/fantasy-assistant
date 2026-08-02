# Graph Schema

Property-graph schema (Neo4j-flavored labels/edges; portable to Memgraph/FalkorDB —
engine choice is deliberately deferred). Everything the system knows lives here:
raw events, authoritative snapshots, and derived beliefs, all with provenance.

## Design principles

1. **Three strata, never mixed.**
   - **Events** — immutable facts with timestamps (a stat line, a transaction, an
     injury). Append-only; corrections append, never mutate.
   - **Snapshots** — authoritative state captured from a source at an instant
     (standings page, roster grid). Used by reconciliation as ground truth.
   - **Derived state** — anything we computed (projections, race analyses, rival
     models, valuations). Always versioned, always traceable to the model version
     and inputs that produced it. Beliefs are first-class nodes, not properties.
2. **Bitemporal-lite.** Every event/snapshot carries `occurred_at` (when true in
   the world; for transactions also `effective_date` — CBS posts on 8/2 effective
   8/3) and `observed_at` (when we captured it). The gap between them is the
   news-latency edge we're trying to win.
3. **Intervals as stint nodes.** Anything with duration (roster membership, IL
   stay, closer role) is a node with `from_*`/`to_*` (null = open), opened and
   closed by events. No mutating edge properties to "end" something.
4. **Deterministic IDs → idempotent ingestion.** Every event's `uid` is a hash of
   source + natural key (e.g. `cbs:txn:2026-08-02T11:33:dawg:manaea:add`).
   Re-scraping a page upserts, never duplicates. Reconciliation depends on this.
5. **Traverse-or-blob rule.** If we filter/traverse on it → property or edge. If
   we only ever read it whole (rationale text, full stat payload beyond category
   components) → JSON blob property. Keeps the graph lean without losing data.
6. **Provenance on everything.** Every event/snapshot node points at the
   `CaptureRun` that produced it. No orphan facts.

## Conventions

- Node labels `PascalCase`, edges `SCREAMING_SNAKE`, properties `snake_case`.
- `uid` unique per node; natural keys documented per label.
- All timestamps UTC ISO-8601; `*_date` = league-local calendar date.
- Innings stored as **integer outs** (`outs=584` not `ip=194.2`). Rates (ERA,
  WHIP, OBP) never stored on aggregates we compute — always recomputed from
  components; source-reported rates kept only on snapshots for reconciliation.

---

## 1. Reference & identity spine

### `League`
`{uid, name, cbs_slug: "buecker", platform: "cbs"}`

### `Season`
`{uid, year, start_date, end_date, draft_date, draft_rounds, trade_deadline,
ip_min: 1000, ip_max: 1300, roster_rules: JSON, fee_add: 2.50, fee_trade: 2.50}`
— rules pinned per season, because they drift (SHOLDS→S taught us that).
- `(Season)-[:OF_LEAGUE]->(League)`

### `ScoringPeriod`
`{uid, number, start_date, end_date, is_final}` — natural key (season, number).
- `(ScoringPeriod)-[:IN_SEASON]->(Season)`, `-[:FOLLOWS]->(ScoringPeriod)`

### `Category`
Ten nodes: HR, OBP, R, RBI, SB, ERA, K, S, WHIP, WQS.
`{uid, code, kind: counting|rate, direction: higher|lower,
components: ["h","bb","hbp","ab","sf"], side: bat|pit}`

### `FantasyTeam`
`{uid, cbs_name, abbrev, is_us: bool}` — persists across seasons.
- `(FantasyTeam)-[:COMPETES_IN {final_rank?, final_points?}]->(Season)`

### `Manager`
`{uid, name, contact?: JSON}` — the humans; opponent intelligence hangs off this.
- `(Manager)-[:MANAGES {from_season, to_season?}]->(FantasyTeam)`

### `Player`
`{uid, name_full, name_normalized, mlbam_id, cbs_id?, fangraphs_id?, bbref_id?,
bats, throws, birthdate, primary_position}`
— **mlbam_id is the identity anchor** (Savant and MLB Stats API share it); other
ids are crosswalk. Unresolved mappings become `Discrepancy` nodes, never guesses.
Ohtani note: CBS splits him into two roster entities ("(Batter)"/"(Pitcher)") —
both map to one `Player`; the roster stint records `cbs_entity: batter|pitcher`.

### `MLBTeam`
`{uid, abbrev, league: AL|NL, division, park_id, park_factors: JSON}`
- `(Player)-[:PLAYS_FOR {from_date, to_date?}]->(MLBTeam)` — stint-style edge,
  closed by trade/release events.

### `MLBGame`
`{uid, game_pk, date, start_ts, status, home_score?, away_score?, is_makeup}`
- `(MLBGame)-[:HOME]->(MLBTeam)`, `-[:AWAY]->(MLBTeam)`
- `(MLBGame)-[:IN_PERIOD]->(ScoringPeriod)`

---

## 2. Baseball performance (events)

### `PlayerDayLine`
One node per (player, date) — finest grain we act on with weekly locks;
per-game split kept in the blob when doubleheaders matter.
`{uid, date, source, blob: JSON}` plus flattened category components:
- batting: `pa, ab, h, b1, b2, b3, hr, r, rbi, bb, ibb, hbp, sf, sb, cs, so`
- pitching: `outs, gs, qs, w, l, sv, bs, hld, er, ha, bbi, k, pitches, batters_faced`
- `(PlayerDayLine)-[:OF_PLAYER]->(Player)`, `-[:ON_DATE_IN]->(ScoringPeriod)`,
  `-[:IN_GAME]->(MLBGame)` (0..2)

### `PlayerPeriodLine` *(derived cache)*
Same components aggregated per (player, period). Rebuilt from day lines; carries
`derived_from_run` → `AnalyticsRun`. Exists because every valuation touches it.

### `StatcastProfile`
Versioned per (player, season, window): `{uid, season, window: ytd|l30|l14,
as_of, xwoba, xba, xslg, ev_avg, ev_max, barrel_pct, hardhit_pct, chase_pct,
whiff_pct, sprint_speed, arm_value?, blob: JSON}` — pitchers get their own fields
in blob (stuff metrics, velo by pitch). `-[:SUPERSEDES]->` prior window node.
- `(StatcastProfile)-[:OF_PLAYER]->(Player)`

### `ProbableStart`
`{uid, date, announced_at, confirmed: bool, source}` — fuel for two-start
planning and WQS/K streaming.
- `-[:OF_PLAYER]->(Player)`, `-[:FOR_GAME]->(MLBGame)`

---

## 3. Player status (events + stints)

### `InjuryEvent` / `InjuryStint`
Event: `{uid, occurred_at, observed_at, kind: injured|il_placed|il_transferred|
activated|setback|rehab_start, il_type?: 10|15|60|7_concussion, body_part?,
severity_text, est_return?: date, source}`
Stint: `{uid, from_date, to_date?, il_type, resolved_how?}`
- events `-[:OPENED|CLOSED|UPDATED]->` stint; both `-[:OF_PLAYER]->(Player)`

### `RoleState` *(versioned belief about the real world)*
`{uid, role: closer|high_leverage|setup|rotation|spot_starter|long_relief|
platoon_strong|platoon_weak|everyday|bench|leadoff|cleanup|top6, as_of,
confidence: 0-1, source, evidence_blob}`
- `-[:OF_PLAYER]->(Player)`, `-[:SUPERSEDES]->(RoleState)`
- Saves and WQS hunting live and die on closer/rotation `RoleState` freshness.

### `MinorsStint`
`{uid, from_date, to_date?, level: AAA|AA|..., option_status?}`
- `-[:OF_PLAYER]->(Player)` — gates CBS minors-slot legality.

### `PositionGameCount`
`{uid, season, position, games, as_of}` — the 20-game eligibility engine.
Updated by scouting; **eligibility windows are queryable**: games ≥ 15 and < 20
at a non-eligible position = window opening soon.
- `-[:OF_PLAYER]->(Player)`

### `EligibilityState` *(derived, versioned)*
`{uid, as_of, positions: [C,1B,...], source: cbs|computed}` — CBS's word is
authoritative (their eligibility page); computed version flags divergence early.
- `-[:OF_PLAYER]->(Player)`, `-[:SUPERSEDES]->`

### `NewsItem`
`{uid, published_at, observed_at, source, headline, url, body_ref?, blob}`
- `-[:ABOUT]->(Player|MLBTeam)` (1..n)
- Scouting agents attach interpretation, not just text:

### `Signal` *(derived from news/stats — the scouting agent's judgment)*
`{uid, kind: injury_risk|role_change|velocity_drop|hot_streak|playing_time_up|
playing_time_down|call_up_imminent|trade_rumor, direction: +|-,
strength: 0-1, as_of, agent, model_version, rationale}`
- `-[:ABOUT]->(Player)`, `-[:DERIVED_FROM]->(NewsItem|StatcastProfile|PlayerDayLine)`

---

## 4. League state (events + stints + snapshots)

### `TransactionEvent`
`{uid, kind: add|drop|claim_add|move_to_il|move_from_il|move_to_minors|
move_from_minors|to_reserve|to_active|commissioner_action, posted_at,
effective_date, fee, via_waivers: bool, by_commissioner: bool, source}`
- `-[:BY_TEAM]->(FantasyTeam)`, `-[:ADDS]->(Player)`, `-[:DROPS]->(Player)`,
  `-[:MOVES]->(Player)`
- `(TransactionEvent)-[:OPENED|CLOSED]->(RosterStint)`

### `TradeEvent`
`{uid, posted_at, effective_date, approved_at?, status: proposed|accepted|
approved|vetoed|completed, fee_per_team, source}`
- `-[:PARTY]->(FantasyTeam)` (2), `-[:TRANSFERS {from_team_uid, to_team_uid}]->(Player)` (1..n)
- Proposed-but-dead trades are kept — they're opponent intelligence.

### `RosterStint`
The load-bearing node. One per continuous (player, team, status) run.
`{uid, status: active|reserve|il|minors, cbs_entity?: batter|pitcher,
from_date, to_date?, acquired_via: draft|waiver|fa|trade, acquisition_cost?}`
- `-[:OF_PLAYER]->(Player)`, `-[:ON_TEAM]->(FantasyTeam)`
- Status changes close one stint and open the next (linked `-[:CONTINUES]->`),
  so "roster on date X" and "how long was Judge stashed on IL" are single hops.

### `LineupAssignment`
Per (team, period, slot): `{uid, slot: C|1B|2B|3B|SS|MI|CI|OF1..OF4|U|P1..P9,
locked_at?, was_locked_default: bool}`
- `-[:IN_PERIOD]->(ScoringPeriod)`, `-[:BY_TEAM]->(FantasyTeam)`, `-[:FILLED_BY]->(Player)`
- Rivals' lineup choices are capturable (roster grid) — feed for tendency models.

### `DraftPick`
`{uid, season, round, overall, keeper: false}` — history feeds 2027 prep.
- `-[:BY_TEAM]->`, `-[:SELECTED]->(Player)`, `-[:OPENED]->(RosterStint)`

### `WaiverOrderSnapshot`
`{uid, as_of, order: [team_uid,...]}` + `WaiverMove` events
`{uid, occurred_at, team_uid, from_rank, to_rank, cause: claim|passive}`.
Claims burn capital — the graph must know the order at any past instant.

### `StandingsSnapshot` / `CategoryStandingLine`
Snapshot: `{uid, as_of, period_number, scope: ytd|period}`
Line (one per team×category): `{uid, value_reported, points, rank, dif}`
- `(StandingsSnapshot)-[:HAS_LINE]->(CategoryStandingLine)-[:FOR_TEAM]->(FantasyTeam)`,
  `(CategoryStandingLine)-[:IN_CATEGORY]->(Category)`
- Cadence: daily capture + period-boundary capture (reconciliation anchor).

### `FreeAgentPoolSnapshot` / `PoolEntry`
Snapshot: `{uid, as_of}`; Entry: `{uid, avail: fa|waivers, waiver_clear_date?,
sportsline_week_blob: JSON}` — CBS gives SportsLine weekly projections on this
page; capture them, they're a free rival-visible consensus.
- `(PoolEntry)-[:OF_PLAYER]->(Player)`, `(FreeAgentPoolSnapshot)-[:HAS]->(PoolEntry)`
- Cheap alternative view: `avail` is derivable from roster stints (anyone not
  rostered is FA) — the snapshot exists to catch waiver-clear dates and to
  reconcile the derived view.

---

## 5. Derived analytics (versioned, model-attributed)

Everything here carries `{model_version, computed_at}` and an edge
`-[:PRODUCED_BY]->(AnalyticsRun)`. Nothing here is ever authoritative.

### `AnalyticsRun`
`{uid, agent, model_version, started_at, finished_at, inputs_blob, status}`

### `ProjectionSet` / `PlayerProjection`
Set: `{uid, source: sportsline|steamer_ros|atc_ros|zips_ros|internal_blend,
horizon: ros|next_period|season, as_of}`
Projection: `{uid, pt_basis: pa|outs, ...same stat components as day lines...,
playing_time, role_assumed}`
- `(ProjectionSet)-[:CONTAINS]->(PlayerProjection)-[:OF_PLAYER]->(Player)`
- Sets are immutable; a new scrape/blend = new set `-[:SUPERSEDES]->` prior.
  Projection *history* is deliberately kept — R&D scores every source's accuracy.

### `TeamStateAssessment`
Per (team, as_of): `{uid, as_of, ip_outs_used, ip_pace_outs, ip_headroom_outs,
transactions_spent, fees_spent, waiver_rank, reserve_slots_free, il_slots_used,
category_positions_blob}` — the resource ledger. IP lives here as one resource
among several, an input to valuation — **not a standalone advisory metric**.
- `-[:FOR_TEAM]->(FantasyTeam)`

### `RaceAnalysis`
Per (category, as_of): `{uid, as_of, standings_now_blob, projected_final_blob,
volatility, our_marginal_curve_blob}` — the marginal curve is the heart of
portfolio valuation: expected points gained per unit of category production.
- `-[:IN_CATEGORY]->(Category)`

### `PortfolioPosture` *(a decision-relevant belief about ourselves)*
`{uid, as_of, contend: [cats], hedge: [cats], concede: [cats], variance_stance:
grind|neutral|seek, rationale}` — versioned; the weekly brief must state it and
diffs against last week are themselves reportable.

### `PlayerValuation`
Per (player, as_of, context): `{uid, as_of, context: our_roster|team_uid|market,
value_pts_ros, value_pwin_delta, components_blob (per-category marginal
contributions, ip_cost, replacement_baseline), rank_overall, rank_at_position}`
- `-[:OF_PLAYER]->(Player)`, `-[:PRODUCED_BY]->(AnalyticsRun)`
- **Context is mandatory** — value-to-us ≠ value-to-Rieken-Havoc ≠ market. The
  three-way divergence *is* the trade/waiver opportunity finder.

### `StandingsSimulation`
`{uid, as_of, n_paths, horizon: end_of_season, p_win_by_team_blob,
p_top5_by_team_blob, category_final_distributions_blob}`

### `TendencyBelief` *(opponent intelligence)*
Per (manager, dimension): `{uid, as_of, dimension: waiver_aggression|churn_rate|
trade_appetite|position_hoarding|reacts_to_news_speed|lineup_diligence,
value, confidence, evidence_count}`
- `-[:ABOUT_MANAGER]->(Manager)`, `-[:EVIDENCED_BY]->(TransactionEvent|LineupAssignment|TradeEvent)`,
  `-[:SUPERSEDES]->`

### `RivalNeedsAssessment`
Per (team, as_of): `{uid, as_of, needs_blob (per-category urgency), surplus_blob,
likely_targets_blob}` — powers blocking-claim and trade-counterparty logic.

---

## 6. Advisory & telemetry (the R&D loop's food)

### `Brief`
`{uid, period_number, published_at, posture_uid, content_ref, summary}`
- `-[:FOR_PERIOD]->(ScoringPeriod)`, `-[:CONTAINS]->(Recommendation)`

### `Alert`
`{uid, raised_at, urgency: fyi|act_today|act_before_waivers, window_closes_at?}`
- `-[:TRIGGERED_BY]->(Signal|TransactionEvent|InjuryEvent|PoolEntry)`,
  `-[:CONTAINS]->(Recommendation)`

### `Recommendation`
`{uid, created_at, kind: lineup_set|claim|drop|trade_proposal|stash|activate|
hold (explicit do-nothing), action_blob (machine-readable proposed action),
expected_delta_pts, expected_delta_pwin, confidence, rationale,
expires_at, status: open|expired|adopted|partial|declined}`
- `-[:TARGETS]->(Player)` (0..n), `-[:ALTERNATIVE_TO]->(Recommendation)`,
  `-[:BASED_ON]->(PlayerValuation|RaceAnalysis|StandingsSimulation|Signal)`
- The `BASED_ON` edges make every recommendation auditable back to inputs.

### `DecisionRecord`
`{uid, decided_at, action_taken_blob, matches_recommendation: full|partial|none|
unprompted}` — what the human actually did, including moves we never suggested.
- `-[:RESPONDS_TO]->(Recommendation)` (0..1), `-[:EXECUTED_AS]->(TransactionEvent|LineupAssignment)` (0..n)

### `OutcomeReview`
`{uid, reviewed_at, horizon: 1p|4p|eos, realized_delta_pts, counterfactual_blob,
score: -1..1, calibration_note}` — written by R&D agents, per recommendation,
at fixed horizons after the fact.
- `-[:EVALUATES]->(Recommendation)`

### `CaptureRun` *(provenance)*
`{uid, agent, source: cbs|mlb_statsapi|savant|fangraphs|news, urls_blob,
started_at, finished_at, status, items_written, error?}`
- every event/snapshot `-[:OBSERVED_IN]->(CaptureRun)`

### `ReconciliationRun` / `Discrepancy`
Run: `{uid, period_number, ran_at, sources_blob, discrepancy_count}`
Discrepancy: `{uid, entity_kind, entity_uid?, expected_blob, observed_blob,
severity: cosmetic|stat|state|identity, status: open|fixed|explained,
resolution?}`
- `(ReconciliationRun)-[:FOUND]->(Discrepancy)`, `(Discrepancy)-[:ABOUT]->(any)`
- The success metric: discrepancy counts trend to zero, and every one becomes a
  capture-bug fix.

---

## 7. Acid-test queries (the schema must make these easy)

1. **Streaming finder.** FA/waiver SPs, next-period two-start flags, projected
   K/WQS per out consumed, ranked by marginal standings points given current
   `RaceAnalysis` curves and our `TeamStateAssessment.ip_headroom_outs`.
2. **Blocking-claim analysis.** For closer X on waivers: each rival's
   `RivalNeedsAssessment` S-urgency × `TendencyBelief` waiver aggression ×
   current waiver rank → P(claimed by whom), vs our cost (rank burn + $2.50).
3. **Eligibility windows.** Our players + FA pool where `PositionGameCount`
   at a new position ∈ [15,20) — value unlockable in ≤2 weeks.
4. **Roster time-travel.** Any team's exact 21+reserve+IL on any past date
   (stint interval query), diffable against any `FreeAgentPoolSnapshot`.
5. **Recommendation audit.** Period 22's brief → every recommendation → its
   BASED_ON inputs → the decision taken → outcome reviews at 1p/4p horizons.
6. **Latency ledger.** For every injury/role event: `observed_at - occurred_at`,
   and whether a rival transacted on it before our alert fired.
7. **Projection scoreboard.** Per source (SportsLine, Steamer-ROS, internal
   blend): realized vs projected error by stat, rolling — which source earns
   weight in the blend.
8. **Trade counterparty scan.** Teams whose `RivalNeedsAssessment` surplus
   intersects our need categories and vice versa, filtered by their manager's
   `trade_appetite`, with candidate packages valued three-way (us/them/market).

## 8. Deliberately unresolved

- **Engine**: Neo4j (default), Memgraph (streaming-friendly), FalkorDB (light).
  Schema is engine-portable; decide at implementation kickoff.
- **Blob discipline**: `*_blob` fields are the pressure valve; promote a field to
  a real property the first time a query needs to filter on it. Review quarterly.
- **2026 backfill**: transactions (full log exists), standings-by-period, and
  draft results are backfillable; FA-pool and Statcast history are
  capture-forward only. Backfill scope = first implementation decision.
- **News sourcing**: which feeds, and their terms — before scouting agents ship.
