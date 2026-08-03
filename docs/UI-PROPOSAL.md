# Proposal: Manager view for the dashboard

**Status: proposed** (2026-08-02). The current UI is an *ops* console — health
tiles, bus log, R&D ledger, orphan checks, ask box. That's the right tool for
"is the system healthy," and it stays. But the analysis a manager acts on
(battle plan, recommendations, signals, wire heat, rival activity) is buried
in brief markdown and ad-hoc ask-box queries. This proposes a **Manager view**
as the default tab, with the existing console moving to an **Ops** tab.

## Due diligence: what a roto manager actually needs, by decision cadence

Grounded in PROBLEM.md's scope (category portfolio, roster/asset mgmt, market
ops, opponent modeling, schedule exploitation) and the league's mechanics
(weekly lineups, Monday locks, overnight waivers, $2.50 adds, 1000–1300 IP):

| Cadence | Decision | What you need in one glance | Have the data? |
|---|---|---|---|
| **Weekly (pre-lock)** | Set Monday lineup | Open swap/add recs; IL/minors players occupying roster spots; 2-start streamers | ✅ 33 open `Recommendation` nodes; stint statuses; `ProbableStart` |
| **Weekly (pre-lock)** | Place waiver claims | Claim recs + waiver clear dates; who else claims fast | ✅ recs, `PoolEntry.waiver_clear`, `SNIPED_BY` |
| **Daily** | React to news | Alerts (24h: 48), injury/paternity/demotion on my roster + targets | ✅ `Alert`, `NewsItem` |
| **Daily** | Beat rivals to the wire | Hot FA bats/arms before they're gone; rival add velocity | ✅ `Signal {fa:true}`, txn feed (Dawg made 7 moves in 48h) |
| **Ongoing** | Category strategy | Cheapest next points; where we're about to be passed | ✅ `races` curves (post-fix: OBP 1.0 swaps < WHIP 2.6 < ERA 3.4) |
| **Ongoing** | Stay under IP cap | Pace vs 1300 / floor 1000 | ✅ CBS my-team block (1203 pace, 97 headroom) |
| **Ongoing** | Trades | Top tier of the trade board, or the honest "no positive trades" | ✅ valuation scan |
| **Ongoing** | Season odds | P(win)/P(top5) trend | ⚠️ computed on demand, **not persisted** |
| **Ongoing** | Position flexibility | Eligibility windows opening (10 players at 15+ games) | ✅ `PositionGameCount` |

The one-glance question the page must answer, top to bottom:
**Where do I stand → what must I do before the next lock → what's about to
hurt me → what's worth taking off the wire → what is everyone else doing.**

## Proposed layout

```
┌──────────────────────────────────────────────────────────────────────┐
│ STRIP: standings (pts·rank·Δwk) │ P(win)/P(top5) │ next lock ⏱ │ IP │
├──────────────────────────────────┬───────────────────────────────────┤
│ BEFORE NEXT LOCK (action queue)  │ CATEGORY BATTLE PLAN              │
│ open recs grouped: swaps/adds/   │ per cat: pts now→proj, cheapest   │
│ claims + IL-in-roster hazards    │ next point (units/swaps), sorted; │
│ each: player·why·[adopt]         │ defend-warnings on thin cushions  │
├──────────────────────────────────┼───────────────────────────────────┤
│ ROSTER PULSE                     │ THE WIRE                          │
│ my players w/ fresh signals      │ hot FAs (signal+rank), 2-start    │
│ (hot/cold/velo/sell-high), IL    │ FA pitchers, pending waivers w/   │
│ w/ est return, my starts next 7d │ clear dates + snipe-risk note     │
├──────────────────────────────────┼───────────────────────────────────┤
│ LEAGUE ACTIVITY (48h txns/team,  │ RACE DETAIL (proj final           │
│ trade-board verdict)             │ leaderboard, expandable)          │
└──────────────────────────────────┴───────────────────────────────────┘
Tabs: [Manager] [Ops]   (ops = today's page, unchanged)
```

Principles: every number links to its source (rec → rationale, signal →
why-string, curve → races model version); advisory-only stays true — the page
never has a button that touches CBS; "adopt" just marks the rec (the existing
DecisionRecord matcher confirms against the real transaction).

## Gaps to close (small)

1. **Persist sim odds**: `variance.simulate()` results → a `SimResult` node
   per run (p_win, p_top5, pts p10/50/90, model_version, as_of). One MERGE in
   the routine after races. Gives the strip tile *and* a trend line over time.
2. **YTD rank**: `ingest_standings` writes `OVERALL` without `rank` on the
   ytd snapshot (cumulative has it). One ORDER BY + enumerate.
3. **`/api/manager` endpoint**: one aggregation (10-min TTL cache like the
   ask box) so the page is a single fetch. races.analyze() is already
   memoized per process.
4. **Week-over-week delta**: daily `cbs:standings:<date>` snapshots already
   accumulate — delta = today vs 7 days ago once a week of dailies exists.

## Not in scope (deliberately)

- Live in-period scoring (CBS live page is captured but day-granular; a
  "period so far" panel can come once recompute runs intra-period).
- Any write path to CBS.
- Mobile layout (desktop-first like the ops console).

## Estimate

Backend (SimResult + rank + endpoint): ~150 lines. Frontend: one new PAGE
section reusing the ops console's styles/table helpers. No schema migration;
one new node label. Mockup: `docs/ui-manager-mockup.html` (real numbers from
tonight's graph).
