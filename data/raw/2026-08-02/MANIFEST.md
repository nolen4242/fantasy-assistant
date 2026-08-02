# Raw capture manifest — 2026-08-02

First telemetry snapshot, captured ~11:30–12:00 ET via authenticated browser
session, during period-19→20 boundary (period 19 ended today; period-20
add/drops locked, effective 8/3).

| file | source | contents | caveats |
|---|---|---|---|
| `transactions_all_raw.txt` | `/transactions?print_rows=9999` | Full 2026 season transaction log: 518 transactions, 3/27/26 → 8/2/26, with timestamps, teams, players, effective dates, fees | Page text incl. site boilerplate; parse rows matching `^M/D/26` |
| `fa_pool_batters_period20.psv` | `/stats/stats-main?print_rows=9999` (FA batters, projections, period 20) | 4,337 data rows (incl. dupe header): `cbs_id\|add_pos\|avail\|player pos • team\|AB\|R\|H\|1B\|2B\|3B\|HR\|RBI\|BB\|K\|SB\|CS\|AVG\|OBP\|SLG\|Rank` (SportsLine weekly projections) | `•` mangled to `�` (clipboard encoding; fixed for pitchers file). Avail: `FA` or `W (M/D)` = waivers until date. First 2 lines are headers |
| `fa_pool_pitchers_period20.psv` | `/stats/stats-main/fa:P/period-20:p/standard/projections?print_rows=9999` | 3,721 data rows: `cbs_id\|P\|avail\|player P • team\|INNs\|APP\|GS\|QS\|CG\|W\|L\|S\|BS\|HD\|K\|BB\|H\|ERA\|WHIP\|Rank` | UTF-8 clean. First 2 lines are headers |
| `standings_overall.txt` | `/standings/overall` | Overall + all 10 category breakdowns, through period 19 | Transcribed table text (values verbatim) |
| `draft_results.txt` | `/draft/results` | Full 2026 draft: 299 picks (23 rounds × 13), with elapsed time, CBS rank, auto/queued tags, plus the draft-room chat log | Rounds 1–3 straight, snake from round 4. Chat log confirms 2026 rule changes (plain Saves, 2 reserve slots) |
| `roster_grid.txt` | `/teams/roster-grid` | All 13 rosters by lineup position with (R)/(I)/(M) tags | Abbreviated first names as CBS renders them |

Notes:
- CBS ids present in FA files (from `default_add=POS:ID` in action buttons) —
  seed for the identity crosswalk.
- Waiver order at capture: Runtime Terror 4 of 13 (from page footer).
- Rules/constitution and scoring settings captured in conversation and encoded
  in PROBLEM.md; live page at `/rules` is stable.
