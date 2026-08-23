"""Lineup trust audit — why LineupAssignment.section must not be read as
"this player's stats counted for us".

Background (2026-08-22): a reserve pitcher's start was reported as having
counted because LineupAssignment.section said 'active' for that period. It
had not. The section field comes from /teams/roster-report/<team>/<period>/,
which is a period ROSTER report, not a per-day lineup record:

  * it routinely lists more 'active' players than there are lineup slots
    (11-12 active P against 10 slots; up to 18 active batters against 12),
  * it shows players who spent the period on IL as 'active',
  * and CBS locks lineups PER PLAYER, 5 minutes before that player's first
    game of the period — so a period has no single lineup state for the page
    to report in the first place.

The only trustworthy answer to "did this performance count" is the CREDITED
category total from the official standings, compared against the sum of what
the players actually did. This module reports both signals:

  slot_violations()  — team-periods whose 'active' count exceeds capacity.
                       Non-zero is expected today; it is the evidence that
                       section is unreliable, and a regression guard if the
                       upstream page ever starts reporting real lineups.
  credited_vs_actual() — for our team and a given period, credited category
                       totals vs the sum of our rostered players' real MLB
                       lines. A negative delta means production did not count
                       (bench/IL//slot limits); it is the detector that would
                       have caught the deGrom case.
"""
from __future__ import annotations

from fantasy_assistant.graph.client import read_session

# active lineup capacity, from the period-21/22 roster-report structure
MAX_ACTIVE_P = 10
MAX_ACTIVE_BAT = 12

# credited category -> PlayerDayLine field + side, for counting stats only.
# Rate stats (ERA/WHIP/OBP) are deliberately excluded: they need denominators
# reconstructed from components, and a raw sum would be meaningless.
COUNTING = {
    "K": ("k", "pit"),
    "WQS": ("qs", "pit"),
    "S": ("sv", "pit"),
    "HR": ("hr", "bat"),
    "R": ("r", "bat"),
    "RBI": ("rbi", "bat"),
    "SB": ("sb", "bat"),
}


def slot_violations(limit: int = 10) -> list[dict]:
    """Team-periods where the 'active' section exceeds real lineup capacity."""
    q = """
    MATCH (la:LineupAssignment)-[:IN_PERIOD]->(sp:ScoringPeriod)
    MATCH (la)-[:BY_TEAM]->(t:FantasyTeam)
    WHERE la.section='active'
    WITH t.cbs_name AS team, sp.number AS period,
         sum(CASE WHEN la.slot='P' THEN 1 ELSE 0 END) AS active_p,
         sum(CASE WHEN la.slot<>'P' THEN 1 ELSE 0 END) AS active_bat
    WHERE active_p > $maxp OR active_bat > $maxb
    RETURN period, team, active_p, active_bat
    ORDER BY period DESC, team LIMIT $limit
    """
    with read_session() as s:
        return [r.data() for r in s.run(
            q, maxp=MAX_ACTIVE_P, maxb=MAX_ACTIVE_BAT, limit=limit)]


def credited_vs_actual(period: int) -> list[dict]:
    """Credited period totals vs our rostered players' actual MLB production.

    NOTE: 'our players' is the CURRENT roster (open RosterStint). For a period
    with mid-period adds/drops the comparison is approximate — treat a delta
    as a signal to investigate, not as an exact count of lost production.
    """
    with read_session() as s:
        dates = s.run(
            "MATCH (p:ScoringPeriod {number:$n}) "
            "RETURN toString(p.start_date) AS a, toString(p.end_date) AS b",
            n=period).single()
        if not dates:
            return []
        credited = {r["cat"]: r["value"] for r in s.run(
            """
            MATCH (st:StandingsSnapshot {scope:'period'})-[:FOR_PERIOD]->
                  (p:ScoringPeriod {number:$n}),
                  (st)-[:HAS_LINE]->(l)-[:FOR_TEAM]->(t:FantasyTeam {is_us:true}),
                  (l)-[:IN_CATEGORY]->(c:Category)
            RETURN c.code AS cat, l.value_reported AS value
            """, n=period)}
        rows = []
        for cat, (field, side) in COUNTING.items():
            if cat not in credited:
                continue
            actual = s.run(
                f"""
                MATCH (rs:RosterStint)-[:OF_PLAYER]->(pl:Player),
                      (rs)-[:ON_TEAM]->(t:FantasyTeam {{is_us:true}})
                WHERE rs.to_date IS NULL
                MATCH (dl:PlayerDayLine)-[:OF_PLAYER]->(pl)
                WHERE dl.side=$side AND dl.date >= date($a) AND dl.date <= date($b)
                RETURN sum(coalesce(dl.{field}, 0)) AS total
                """, side=side, a=dates["a"], b=dates["b"]).single()["total"]
            rows.append({"cat": cat, "credited": credited[cat],
                         "roster_actual": actual,
                         "delta": round(credited[cat] - (actual or 0), 3)})
        return rows


def report(period: int | None = None) -> str:
    with read_session() as s:
        if period is None:
            period = s.run(
                "MATCH (st:StandingsSnapshot {scope:'period'})-[:FOR_PERIOD]->(p) "
                "RETURN max(p.number) AS n").single()["n"]
    out = ["LINEUP TRUST AUDIT  [lineup-audit-v1]", ""]
    viol = slot_violations()
    out.append("Slot-capacity violations (active section vs real capacity "
               f"{MAX_ACTIVE_P}P / {MAX_ACTIVE_BAT} bat):")
    if not viol:
        out.append("  none — section may now be a real lineup record; re-verify "
                   "before trusting it.")
    else:
        out.append("  PRESENT — LineupAssignment.section is NOT a lineup record. "
                   "Do not use it to decide whether a")
        out.append("  performance counted. Use credited-vs-actual below.")
        for v in viol[:6]:
            out.append(f"    period {v['period']:>2}  {v['team']:<26} "
                       f"active_P={v['active_p']:<3} active_bat={v['active_bat']}")
    out.append("")
    out.append(f"Credited vs rostered-actual — period {period} "
               "(negative delta = production that did not count):")
    for r in credited_vs_actual(period):
        flag = "  <-- shortfall" if r["delta"] < 0 else ""
        out.append(f"  {r['cat']:<5} credited {r['credited']:>8} "
                   f"roster_actual {str(r['roster_actual']):>8} "
                   f"delta {r['delta']:>8}{flag}")
    return "\n".join(out)


if __name__ == "__main__":
    print(report())
