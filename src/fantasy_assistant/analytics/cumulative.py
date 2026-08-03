"""Materialize cumulative standings THROUGH each period.

'What were the standings after period N' is a natural question the graph
couldn't answer: only per-period slices and one current-YTD snapshot
existed. This derives, for every closed period T: cumulative category
values (counting summed; rates rebuilt from lock-solved components),
re-ranked roto points, and overall totals — written as
StandingsSnapshot{scope:'cumulative'}-[:THROUGH_PERIOD]->ScoringPeriod
with the same HAS_LINE shape queries already expect.
"""
from __future__ import annotations

from collections import defaultdict

from fantasy_assistant.analytics import races
from fantasy_assistant.analytics.recompute import recompute_v2
from fantasy_assistant.graph.client import session
from fantasy_assistant.graph.refdata import CATEGORIES, team_uid

RATE_COMP = {"OBP": ("ob", "pa", 1.0), "ERA": ("er", "outs", 27.0),
             "WHIP": ("wh", "outs", 3.0)}


def materialize() -> dict:
    with session() as s:
        rows = s.run(
            """
            MATCH (st:StandingsSnapshot {scope:'period'})-[:FOR_PERIOD]->(p),
                  (st)-[:HAS_LINE]->(l)-[:FOR_TEAM]->(t:FantasyTeam),
                  (l)-[:IN_CATEGORY]->(c:Category)
            RETURN p.number AS period, c.code AS cat, t.cbs_name AS team,
                   l.value_reported AS v
            """
        ).data()
    per = defaultdict(dict)
    for r in rows:
        per[(r["cat"], r["team"])][r["period"]] = r["v"]
    comps = recompute_v2()["components"]
    periods = sorted({r["period"] for r in rows})
    meta = {c[0]: (c[1], c[2]) for c in CATEGORIES}
    teams = sorted({r["team"] for r in rows})
    n_lines = 0
    with session() as s:
        for T in periods:
            values = {}
            for cat, (kind, _) in meta.items():
                vals = {}
                for team in teams:
                    if kind == "counting":
                        vals[team] = sum(per[(cat, team)].get(p, 0.0)
                                         for p in range(1, T + 1))
                    else:
                        nk, dk, scale = RATE_COMP[cat]
                        side = "bat" if cat == "OBP" else "pit"
                        num = sum(comps.get((team, p, side), {}).get(nk, 0)
                                  for p in range(1, T + 1))
                        den = sum(comps.get((team, p, side), {}).get(dk, 0)
                                  for p in range(1, T + 1))
                        vals[team] = round(num * scale / den, 4) if den else 0.0
                values[cat] = vals
            totals = defaultdict(float)
            snap = f"cbs:standings:cum:{T}"
            s.run(
                """
                MATCH (p:ScoringPeriod {uid:$puid})
                MERGE (st:StandingsSnapshot {uid:$uid})
                SET st.scope='cumulative', st.as_of=date()
                MERGE (st)-[:THROUGH_PERIOD]->(p)
                """, uid=snap, puid=f"period:2026:{T}")
            for cat, vals in values.items():
                pts = races._points_for(vals, meta[cat][1])
                batch = []
                ordered = sorted(vals, key=lambda t: -pts[t])
                for rank, team in enumerate(ordered, 1):
                    totals[team] += pts[team]
                    batch.append({"uid": f"{snap}:{cat}:{team_uid(team)}",
                                  "tuid": team_uid(team), "cuid": f"category:{cat}",
                                  "v": round(vals[team], 4), "pts": pts[team],
                                  "rank": rank})
                s.run(
                    """
                    MATCH (st:StandingsSnapshot {uid:$snap})
                    UNWIND $b AS row
                    MATCH (t:FantasyTeam {uid:row.tuid}), (c:Category {uid:row.cuid})
                    MERGE (l:CategoryStandingLine {uid:row.uid})
                    SET l.value_reported=row.v, l.points=row.pts, l.rank=row.rank
                    MERGE (st)-[:HAS_LINE]->(l)
                    MERGE (l)-[:FOR_TEAM]->(t)
                    MERGE (l)-[:IN_CATEGORY]->(c)
                    """, snap=snap, b=batch)
                n_lines += len(batch)
            standing = sorted(totals, key=lambda t: -totals[t])
            for rank, team in enumerate(standing, 1):
                s.run(
                    """
                    MATCH (st:StandingsSnapshot {uid:$snap}), (t:FantasyTeam {uid:$tuid})
                    MERGE (st)-[r:OVERALL]->(t) SET r.total=$tot, r.rank=$rank
                    """, snap=snap, tuid=team_uid(team),
                    tot=round(totals[team], 1), rank=rank)
    return {"periods": len(periods), "lines": n_lines}


if __name__ == "__main__":
    print(materialize())
