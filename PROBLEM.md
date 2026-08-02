# Problem Statement

## Mission

Win the Bob Uecker Imaginary Baseball League.

The assistant exists to maximize the probability that Runtime Terror finishes 1st
in the campaign it is advising. Standings points, money finishes, category ranks —
all instrumental signals, never the goal.

## Horizon

- **2027 is the target campaign** — full lifecycle: offseason analysis, draft, and
  all 27 scoring periods.
- **The remainder of 2026 is the testbed.** The system runs live against the real
  league (periods 20–27), issues real recommendations, and logs everything — but it
  is judged on the quality and calibration of its advice, not on rescuing a
  9th-place season. Build order follows: telemetry capture and the graph first,
  advisory polish second.

## Scope: a strategic intelligence system, not a transaction recommender

The league gives a manager only a few actuators — set a lineup, claim off waivers,
make a trade, stash or activate. A system that just ranks those transactions is
thinking too small. Winning is decided by the intelligence *behind* the actuators,
and that is the assistant's real scope:

- **Category portfolio strategy.** Which of the ten races to contend, hedge, or
  concede — and when that posture should change. Variance posture conditional on
  standings position (grind when ahead, seek variance when behind). Every player
  valuation is downstream of this portfolio view.
- **Roster & asset management.** The active 21, the two reserve spots, IL and
  minors slots as free warehousing, position-eligibility windows (20-game rule)
  that open and close, service/role timelines for stashes.
- **Market operations.** The waiver wire and trade market as markets: what a
  player is worth *to us*, *to each rival*, and *to the market consensus* — and
  where those diverge (buy-low / sell-high windows, news-latency edges, blocking
  claims). Waiver position and transaction fees are capital to be spent, not
  formalities.
- **Opponent intelligence.** Twelve rival rosters modeled with the same rigor as
  our own: their category positions, their IP pace, their roster holes, their
  observed tendencies (who churns waivers, who hoards, who trades). Trades are
  negotiations with people; blocking requires predicting them.
- **Schedule & matchup exploitation.** Two-start pitcher weeks, matchup and park
  quality, off-day patterns — within the reality of per-player Monday locks.
- **Campaign lifecycle.** The offseason and the 23-round draft are in scope for
  2027; in-season learning (player valuations, rival models, what worked) feeds
  draft prep. No keepers — every March is a full reset informed by everything the
  graph accumulated.

## Valuation principles

Lessons written in blood from the draft assistant:

1. **Portfolio value, not player value.** A player's worth is the marginal change
   in expected category standings of the roster that holds him — never a
   context-free score. The draft assistant once valued relievers over starters
   because rate stats (ERA/WHIP) flatter low-inning arms when nothing accounts
   for the innings a roster must accumulate. Resource floors and caps — the
   1000–1300 IP band above all — are woven into every valuation, not tracked as
   a separate metric or bolted on as a penalty term.
2. **Marginal standings math.** Gaining 20 strikeouts matters if it flips a rank
   and is noise if it doesn't. All value is denominated in expected standings
   movement given the actual race landscape, ultimately in P(win).
3. **Time-awareness.** Value depends on what remains: periods left, IP headroom
   left, waiver position held, trade window open or closed.

## Graph & data flow

The graph is the single source of truth and is maintained two ways:

- **Real-time, event-driven.** Scouting agents write events as they happen:
  stat lines, transactions (ours and rivals'), injuries, role changes, news,
  lineup locks. Consumers react to events — a star hitting waivers should raise
  an alert within the claim window, not at the weekly brief.
- **Weekly reconciliation.** At each period boundary a reconciliation pass
  re-derives state from authoritative sources (CBS standings, rosters,
  transaction log) and diffs it against the graph. Discrepancies are logged,
  corrected, and treated as capture bugs to fix — the guarantee that missed
  events can't silently corrupt state.

Every recommendation, the decision actually taken, and the eventual outcome are
recorded in the same graph — the R&D loop's training data.

## Cadence

- **Weekly brief** — before Monday lineup locks: the full strategic picture
  (portfolio posture, lineup, market opportunities, race analysis, resource
  status).
- **Daily alerts** — event-driven, only when actionable: an injury or role change
  touching our roster or targets, a valuable player on waivers, a rival move that
  shifts a race.

## Hard constraints (2026 league reality)

- 13-team roto, 5×5: HR / OBP / R / RBI / SB — ERA / K / **S (plain saves)** /
  WHIP / WQS (wins + quality starts). *(No holds — draft-repo docs saying SHOLDS
  are stale.)*
- Rosters: 21 active (C, 1B, 2B, 3B, SS, MI, CI, 4×OF, U, 9×P), ≤2 reserve,
  unlimited IL/minors (only players actually on IL / in minors).
- IP: min 1000, max **1300** (CBS-enforced; the constitution's 1400 is stale).
  At the cap, no pitcher transactions for the rest of the season.
- Waivers nightly; order never resets; a successful claim sends you to 13th;
  dropped players clear after ≥1 day. Adds/trades $2.50 each.
- Trades need commissioner approval; deadline last Sunday in August.
- Eligibility: primary position + 20 games last year or this year.
- Lineups lock per-player 5 minutes before their first game of the period.

## The agent architecture (30,000 ft)

- **Scouting agents** — ingest the outside world (league site, MLB stats,
  Statcast, news, roles) and write timestamped events into the graph.
- **Data analytics agents** — derive state: rest-of-season projections, portfolio
  and race analysis, standings simulation, P(win), opponent models.
- **R&D agents** — the meta-loop: score past recommendations against outcomes,
  backtest heuristics, tune valuations, improve the models the other agents use.
- **Advisory layer** — composes the weekly brief and daily alerts.

## Success criteria for the 2026 testbed

1. Gap-free league telemetry from go-live, with weekly reconciliation showing
   shrinking (ideally zero) diffs.
2. A brief every period and alerts on actionable events, each with logged
   reasoning.
3. A scored track record — advised vs. done vs. outcome — sufficient to measure
   calibration and improve for 2027.
4. Demonstrated standings-point pickup where realistically available (the K / S /
   WQS races are razor-thin) — evidence the advice has teeth, without pretending
   2026 is winnable.

## Non-goals

- Autonomous execution of roster moves (advisory only; a human clicks).
- Handling CBS credentials (the human authenticates; the system uses the session).
- General-purpose fantasy tooling for other leagues/platforms — one league, done
  deeply.
