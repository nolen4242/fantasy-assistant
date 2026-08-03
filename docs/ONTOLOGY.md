# Deployed ontology (generated — do not edit)
Regenerate: `.venv/bin/python -m fantasy_assistant.graph.ontology`

## Node labels
- **Alert** (48) — raised_at:DATE_TIME, source:STRING, text:STRING, uid:STRING, urgency:STRING
- **AnalyticsRun** (5) — agent:STRING, as_of_period:INTEGER, finished_at:DATE_TIME, model_version:STRING, status:STRING, uid:STRING
- **BatterGameEV** (31,717) — barrels:INTEGER, bbe:INTEGER, date:DATE, ev_mean:FLOAT, game_pk:INTEGER, hardhit:INTEGER, source:STRING, uid:STRING
- **Brief** (1) — model_version:STRING, published_at:DATE_TIME, uid:STRING
- **CaptureRun** (7) — agent:STRING, capture_date:DATE, source:STRING, status:STRING, uid:STRING
- **Category** (10) — code:STRING, components:LIST, direction:STRING, kind:STRING, side:STRING, uid:STRING
- **CategoryStandingLine** (5,070) — dif:FLOAT, points:FLOAT, rank:INTEGER, uid:STRING, value_reported:FLOAT
- **DecisionRecord** (2) — decided_at:DATE_TIME, matches_recommendation:STRING, recorded_at:DATE_TIME, uid:STRING
- **DraftPick** (299) — auto:BOOLEAN, overall:INTEGER, pick_in_round:INTEGER, queued:BOOLEAN, round:INTEGER, uid:STRING
- **FantasyTeam** (13) — abbrev:STRING, cbs_name:STRING, is_us:BOOLEAN, uid:STRING
- **FreeAgentPoolSnapshot** (1) — as_of:DATE_TIME, period_projected:INTEGER, uid:STRING
- **League** (1) — cbs_slug:STRING, name:STRING, platform:STRING, uid:STRING
- **LineupAssignment** (6,753) — section:STRING, slot:STRING, uid:STRING
- **Manager** (11) — name:STRING, uid:STRING
- **MlbStatusEvent** (164) — date:DATE, description:STRING, effective:STRING, first_seen:DATE_TIME, from_team:STRING, source:STRING, to_team:STRING, type_desc:STRING, uid:STRING
- **ModelEval** (9) — detail:STRING, eval_at:INTEGER, model:STRING, our_err:FLOAT, pts_mae:FLOAT, rank_disp:FLOAT, recorded:DATE_TIME, stand_at:INTEGER, uid:STRING
- **NewsItem** (90) — age_at_capture:STRING, body:STRING, first_seen:DATE_TIME, headline:STRING, is_new:BOOLEAN, source:STRING, uid:STRING
- **ParkFactor** (30) — as_of:DATE, factor:FLOAT, games:INTEGER, home_team:STRING, runs_pg:FLOAT, uid:STRING
- **PitcherGameVelo** (13,440) — csw_pct:FLOAT, date:DATE, ff_avg:FLOAT, game_pk:INTEGER, mix:STRING, n_ff:INTEGER, n_pitches:INTEGER, source:STRING, uid:STRING, whiff_pct:FLOAT
- **Player** (8,377) — bat_speed:FLOAT, bats:STRING, birthdate:STRING, cbs_id:STRING, cbs_mlb_team:STRING, cbs_positions:STRING, chase_pct:FLOAT, era_sv:FLOAT, feat:LIST, feat_side:STRING, luck_gap:FLOAT, mlb_team_current:STRING, mlbam_id:INTEGER, name_full:STRING, name_normalized:STRING, pit_luck_gap:FLOAT, primary_position:STRING, sprint_speed:FLOAT, squared_up:FLOAT, throws:STRING, uid:STRING, whiff_pct_bat:FLOAT, woba:FLOAT, xba:FLOAT, xera:FLOAT, xslg:FLOAT, xwoba:FLOAT
- **PlayerDayLine** (28,846) — ab:INTEGER, b2:INTEGER, b3:INTEGER, batters_faced:INTEGER, bb:INTEGER, bbi:INTEGER, bs:INTEGER, cs:INTEGER, date:DATE, er:INTEGER, game_pk:INTEGER, gs:INTEGER, h:INTEGER, ha:INTEGER, hbp:INTEGER, hld:INTEGER, hr:INTEGER, ibb:INTEGER, k:INTEGER, l:INTEGER, mlbam_id:INTEGER, outs:INTEGER, pa:INTEGER, pitches:INTEGER, qs:INTEGER, r:INTEGER, rbi:INTEGER, sb:INTEGER, sf:INTEGER, side:STRING, so:INTEGER, sv:INTEGER, uid:STRING, w:INTEGER
- **PoolEntry** (8,060) — avail:STRING, side:STRING, sportsline_rank:INTEGER, sportsline_week:LIST, uid:STRING, waiver_clear:STRING
- **PositionGameCount** (397) — as_of:DATE, games:INTEGER, position:STRING, season:INTEGER, uid:STRING
- **ProbableStart** (92) — confirmed:BOOLEAN, date:DATE, game_pk:INTEGER, source:STRING, uid:STRING
- **RaceAnalysis** (30) — as_of_period:INTEGER, computed_at:DATE_TIME, model_version:STRING, payload:STRING, uid:STRING
- **Recommendation** (32) — action_blob:STRING, created_at:DATE_TIME, kind:STRING, rationale:STRING, status:STRING, uid:STRING
- **ReconciliationRun** (1) — anomaly_count:INTEGER, diffs_json:STRING, discrepancy_count:INTEGER, kind:STRING, ran_at:DATE_TIME, uid:STRING
- **RivalNeedsAssessment** (26) — as_of_period:INTEGER, computed_at:DATE_TIME, model_version:STRING, payload:STRING, uid:STRING
- **RosterGridEntry** (369) — label:STRING, slot_group:STRING, status:STRING, uid:STRING
- **RosterGridSnapshot** (1) — as_of:DATE_TIME, uid:STRING
- **RosterStint** (720) — acquired_via:STRING, derived:BOOLEAN, ended_by:STRING, from_date:DATE, status:STRING, to_date:STRING, uid:STRING
- **ScheduleFactor** (30) — as_of:DATE, games_next_period:INTEGER, games_ros:INTEGER, mlb_team:STRING, next_period:INTEGER, ros_factor:FLOAT, uid:STRING
- **ScoringPeriod** (27) — end_date:DATE, is_final:BOOLEAN, number:INTEGER, start_date:DATE, uid:STRING
- **Season** (1) — draft_date:DATE, draft_rounds:INTEGER, fee_add:FLOAT, fee_trade:FLOAT, ip_max:INTEGER, ip_min:INTEGER, start_date:DATE, trade_deadline:DATE, uid:STRING, year:INTEGER
- **ShadowRoster** (1) — created:DATE_TIME, note:STRING, period:INTEGER, players:LIST, uid:STRING
- **Signal** (777) — agent:STRING, as_of:DATE, fa:BOOLEAN, kind:STRING, model_version:STRING, rationale:STRING, results_based:BOOLEAN, strength:FLOAT, uid:STRING
- **SignalEval** (4) — baseline_move:FLOAT, edge:FLOAT, flagged_move:FLOAT, n:INTEGER, recorded:DATE_TIME, rule:STRING, threshold:FLOAT, uid:STRING
- **StandingsSnapshot** (39) — as_of:DATE_TIME, scope:STRING, uid:STRING
- **TransactionEvent** (521) — effective_date:STRING, fee:FLOAT, kinds:LIST, posted_at:DATE_TIME, raw:STRING, source:STRING, uid:STRING

## Relationships (src -> dst)
- (BatterGameEV)-[:OF_PLAYER]->(Player)
- (Brief)-[:CONTAINS]->(Recommendation)
- (Brief)-[:FOR_PERIOD]->(ScoringPeriod)
- (CategoryStandingLine)-[:FOR_TEAM]->(FantasyTeam)
- (CategoryStandingLine)-[:IN_CATEGORY]->(Category)
- (DecisionRecord)-[:EXECUTED_AS]->(TransactionEvent)
- (DecisionRecord)-[:RESPONDS_TO]->(Recommendation)
- (DraftPick)-[:BY_TEAM]->(FantasyTeam)
- (DraftPick)-[:IN_SEASON]->(Season)
- (DraftPick)-[:OBSERVED_IN]->(CaptureRun)
- (DraftPick)-[:SELECTED]->(Player)
- (FantasyTeam)-[:COMPETES_IN]->(Season)
- (FantasyTeam)-[:SNIPED_BY]->(FantasyTeam) {n:INTEGER}
- (FreeAgentPoolSnapshot)-[:HAS]->(PoolEntry)
- (FreeAgentPoolSnapshot)-[:HAS]->(RosterGridEntry)
- (FreeAgentPoolSnapshot)-[:OBSERVED_IN]->(CaptureRun)
- (LineupAssignment)-[:BY_TEAM]->(FantasyTeam)
- (LineupAssignment)-[:FILLED_BY]->(Player)
- (LineupAssignment)-[:IN_PERIOD]->(ScoringPeriod)
- (LineupAssignment)-[:OBSERVED_IN]->(CaptureRun)
- (Manager)-[:MANAGES]->(FantasyTeam)
- (MlbStatusEvent)-[:OF_PLAYER]->(Player)
- (NewsItem)-[:ABOUT]->(Player)
- (PitcherGameVelo)-[:OF_PLAYER]->(Player)
- (Player)-[:SIMILAR_TO]->(Player) {score:FLOAT}
- (PlayerDayLine)-[:OF_PLAYER]->(Player)
- (PoolEntry)-[:OF_PLAYER]->(Player)
- (PositionGameCount)-[:OF_PLAYER]->(Player)
- (ProbableStart)-[:OF_PLAYER]->(Player)
- (RaceAnalysis)-[:IN_CATEGORY]->(Category)
- (RaceAnalysis)-[:PRODUCED_BY]->(AnalyticsRun)
- (RivalNeedsAssessment)-[:FOR_TEAM]->(FantasyTeam)
- (RosterGridEntry)-[:ON_TEAM]->(FantasyTeam)
- (RosterGridSnapshot)-[:HAS]->(PoolEntry)
- (RosterGridSnapshot)-[:HAS]->(RosterGridEntry)
- (RosterGridSnapshot)-[:OBSERVED_IN]->(CaptureRun)
- (RosterStint)-[:OF_PLAYER]->(Player)
- (RosterStint)-[:ON_TEAM]->(FantasyTeam)
- (ScoringPeriod)-[:IN_SEASON]->(Season)
- (Season)-[:OF_LEAGUE]->(League)
- (Signal)-[:ABOUT]->(Player)
- (StandingsSnapshot)-[:FOR_PERIOD]->(ScoringPeriod)
- (StandingsSnapshot)-[:HAS_LINE]->(CategoryStandingLine)
- (StandingsSnapshot)-[:OBSERVED_IN]->(CaptureRun)
- (StandingsSnapshot)-[:OVERALL]->(FantasyTeam) {batting:FLOAT, behind:FLOAT, dif:FLOAT, pitching:FLOAT, rank:INTEGER, total:FLOAT}
- (StandingsSnapshot)-[:THROUGH_PERIOD]->(ScoringPeriod)
- (TransactionEvent)-[:ADDS]->(Player) {action:STRING, trade_from:STRING, via_waivers:BOOLEAN}
- (TransactionEvent)-[:BY_TEAM]->(FantasyTeam)
- (TransactionEvent)-[:DROPS]->(Player) {action:STRING, via_waivers:BOOLEAN}
- (TransactionEvent)-[:MOVES]->(Player) {action:STRING, via_waivers:BOOLEAN}
- (TransactionEvent)-[:OBSERVED_IN]->(CaptureRun)

## Semantics
SEMANTICS (curated, verified):
- ALL data edges point INTO Player: (x)-[:OF_PLAYER|ABOUT|SELECTED|ADDS|DROPS|MOVES]->(p:Player).
- rostered = open RosterStint (st.to_date IS NULL) with ANY status (active/il/minors).
  Free agent = NO open stint. Every Signal carries sig.fa (true = free agent).
- StandingsSnapshot scopes: 'period' (one period, FOR_PERIOD), 'cumulative'
  (season THROUGH a period, THROUGH_PERIOD), 'ytd' (current, latest only).
  Points live ON the OVERALL relationship: o.total, o.batting, o.pitching, o.rank.
- Formulas from PlayerDayLine sums: ERA=er*27/outs, WHIP=(ha+bbi)*3/outs, IP=outs/3,
  OBP=(h+bb+hbp)/(ab+bb+hbp+sf). Pitchers: PlayerDayLine.side='pit'.
- e.kinds on TransactionEvent is a LIST -> use 'trade' IN e.kinds.
- Trades are logged ONE-SIDED per team: each side is its own event with ONLY
  [:ADDS] edges (r.action='trade', r.trade_from = the counterparty team name).
  There is NO DROPS edge on a trade. List trades:
  MATCH (e:TransactionEvent)-[:BY_TEAM]->(t), (e)-[r:ADDS]->(p)
  WHERE 'trade' IN e.kinds RETURN e.posted_at, t.cbs_name, p.name_full, r.trade_from.
- NewsItem time = n.first_seen (datetime). No published_at.
- Eligibility windows: PositionGameCount g.games >= 15 AND g.position NOT IN
  split(p.cbs_positions, ',').
- Signal kinds: hot_bat cold_bat hot_arm cold_arm contact_hot contact_cold
  velocity_up velocity_down csw_up csw_down mix_change buy_low sell_high
  speed_decline. sig.strength, sig.as_of, sig.rationale, sig.results_based.
- Rel props: OVERALL{total,batting,pitching,rank} SIMILAR_TO{score} SNIPED_BY{n}
  ADDS/DROPS/MOVES{action,via_waivers,trade_from}.
- Teams: Runtime Terror (is_us:true), Rieken Havoc, Young Guns, Big Sticks,
  Maga Doge, Like a Nightmare, Dawg, Guillotine, Long Balls, Gashouse Gang,
  Magnum GI, Simba's Dublin Green Sox, Trex.
