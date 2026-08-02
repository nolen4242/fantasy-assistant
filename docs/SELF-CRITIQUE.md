# Self-critique & roadmap (2026-08-02, day one retrospective)

An honest inventory of weaknesses and the concrete upgrades they imply.
Ordered by expected impact on win probability per unit effort.

## Scouting metrics (velo was one instrument; the panel is bigger)

Implemented today alongside velocity:
- **Skill-vs-results gaps** (Savant expected stats): wOBA−xwOBA hitters,
  ERA−xERA pitchers → buy_low/sell_high Signals, scoped to league-relevant
  players. The single best "market inefficiency" detector available free.
- **Whiff% / CSW% per start** (from the same pitch feed as velo): stuff
  trends that velo alone misses (command-driven decline, new-grip gains).
- **Pitch-mix change** (usage shift ≥10pp): the classic breakout tell.

Next instruments, in order:
1. **Rolling barrel%/hard-hit% (hitters, ~50-PA window)** — Savant statcast
   leaderboard CSV; earliest power breakout/decline indicator.
2. **Chase%/BB% trend** — plate-discipline shifts precede OBP moves; we are
   an OBP-fragile team, this is self-defense.
3. **Sprint speed year-over-year** — SB forecast decay (Savant sprint CSV).
4. **Bat speed / squared-up%** (Savant, new since '24) — skill-change
   detection with tiny samples.
5. **FanGraphs Stuff+/Location+** — the best public pitch-quality models;
   scrape-with-membership question, revisit if free channels prove thin.

## Math the models still lack

- **Schedule awareness (biggest known bias):** ROS projections assume equal
  weeks, but MLB teams play 5-7 games/week and we HAVE the schedule. Weight
  player ROS by actual remaining games (and 2-start weeks for SP).
- **Park/opponent adjustment** on weekly projections (Savant park factors
  are already a property on MLBTeam nodes' blob, unused).
- **Hierarchical shrinkage** (player -> role -> league instead of flat
  group means) — the fix for the player-model's K failure.
- **Uncertainty on player projections** (posterior intervals feeding the
  MC sim instead of team-level historical sd).
- **Rate-category simulation via components** (simulate num/denom, not the
  rate directly) — removes the RATE_ROS_WEIGHT hack.

## Graph algorithms worth adding (Neo4j GDS plugin, one compose line)

- **Node similarity / kNN on player stat vectors** — "who is the closest
  free-agent replacement to X" and trade-equivalence classes.
- **Bipartite projection + community detection** on (manager)-(player)
  transaction history — revealed preferences; who values what archetypes.
- **Temporal motif: waiver-sniping** — who claims within a day of whose
  drops (predator/prey pairs) -> blocking-claim priors.
- **Degree/PageRank over the news-signal layer** — attention concentration
  as a leading indicator of FAAB-style rushes (waiver-rank spending).
- Provenance paths (already native): every advice number traceable to
  capture runs — keep it a hard invariant.

## Telemetry not yet captured

- **Lineup-lock timestamps per player** (who rivals start before locks —
  currently invisible until the roster-report next day).
- **Rival activity-time patterns** from posted_at histograms (when is each
  manager awake — claim-timing edge is real when order is contested).
- **Injury est-return dates** (MLB feed has some; CBS news has more —
  parse into InjuryStint.est_return).
- **Weather/park-day** for streaming decisions (marginal; last).

## Decision-quality mechanics

- Pre-registration exists (Recommendation nodes). Add a **shadow
  portfolio**: simulate the roster that took ALL advice vs the real one —
  the cleanest measure of advice value (start of season 2027).
- **Sensitivity tags on trade proposals**: how much does the verdict change
  if the player's ROS rate is +/-1 sd? Fragile wins should say so.
- **Threshold fitting**: alert/signal thresholds (0.8 mph, 5pp CSW, 0.040
  wOBA) are hand-set; fit them to realized hit-rates once OutcomeReviews
  accumulate (the R&D ledger is the substrate).

## Known structural debts

- Name-keyed Player uids (ADR-0003): full cbs_id migration in offseason.
- UI ask-box requires the claude CLI (`npm install -g @anthropic-ai/claude-code`).
- vis-network from CDN — vendor the JS locally for offline robustness.
- Trade valuation ignores 2-for-1s and in-kind roster-slot constraints.
