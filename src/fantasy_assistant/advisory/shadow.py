"""Shadow portfolio: the roster that takes ALL the advice.

Initialized at period 20 from the real roster. Each week: apply every
add/claim Recommendation from that period's brief (dropping the
lowest-quality same-side shadow player), then score shadow vs actual
production for the closed period. The cleanest measure of advice value —
if shadow doesn't beat actual over a season, the advice layer isn't
earning its keep. Full campaign run planned for 2027; the 2026 remainder
is its shakedown.
"""
from __future__ import annotations

import json
from datetime import date

from fantasy_assistant.analytics.valuation import quality, roster_players
from fantasy_assistant.graph.client import session
from fantasy_assistant.graph.refdata import period_dates, period_for_date

INIT_PERIOD = 20


def init() -> dict:
    with session() as s:
        us = s.run("MATCH (t:FantasyTeam {is_us:true}) RETURN t.cbs_name AS n").single()["n"]
        existing = s.run("MATCH (sr:ShadowRoster {period:$p}) RETURN sr.uid AS u",
                         p=INIT_PERIOD).data()
        if existing:
            return {"already_initialized": True}
        roster = [p["name"] for p in roster_players(us)
                  if p["status"] in ("active", "reserve", "il")]
        s.run(
            """
            MERGE (sr:ShadowRoster {uid:$uid})
            SET sr.period=$p, sr.players=$players, sr.created=datetime(),
                sr.note='initialized from real roster pre-period-20'
            """, uid=f"shadow:2026:{INIT_PERIOD}", p=INIT_PERIOD, players=roster)
    return {"initialized": len(roster)}


def advance() -> dict:
    """Roll the shadow roster forward one period, applying that brief's
    add/claim recommendations."""
    with session() as s:
        cur = s.run("MATCH (sr:ShadowRoster) RETURN sr.period AS p, sr.players AS pl "
                    "ORDER BY sr.period DESC LIMIT 1").single()
        if not cur:
            return {"error": "not initialized"}
        p, players = cur["p"], list(cur["pl"])
        now_p = period_for_date(date.today())
        if p >= now_p:
            return {"up_to_date": p}
        recs = s.run(
            """
            MATCH (b:Brief)-[:FOR_PERIOD]->(:ScoringPeriod {number:$p}),
                  (b)-[:CONTAINS]->(r:Recommendation)
            WHERE r.kind IN ['add', 'claim']
            RETURN r.action_blob AS blob
            """, p=p + 1).data()
        added = []
        for r_ in recs:
            player = json.loads(r_["blob"]).get("player")
            if player and player not in players:
                us_q = sorted(
                    (x for x in roster_players("Runtime Terror") if x["name"] in players),
                    key=quality)
                if us_q:
                    players.remove(us_q[0]["name"])
                players.append(player)
                added.append(player)
        s.run(
            """
            MERGE (sr:ShadowRoster {uid:$uid})
            SET sr.period=$p, sr.players=$players, sr.created=datetime(),
                sr.added=$added
            """, uid=f"shadow:2026:{p + 1}", p=p + 1, players=players, added=added)
    return {"advanced_to": p + 1, "applied": added}


def score(period: int) -> dict | None:
    start, end = period_dates(period)
    if end >= date.today():
        return None
    with session() as s:
        sr = s.run("MATCH (sr:ShadowRoster {period:$p}) RETURN sr.players AS pl",
                   p=period).single()
        if not sr:
            return None
        rows = s.run(
            """
            UNWIND $names AS nm
            MATCH (p:Player {name_full: nm})
            OPTIONAL MATCH (d:PlayerDayLine)-[:OF_PLAYER]->(p)
            WHERE d.date >= date($a) AND d.date <= date($b)
            RETURN sum(coalesce(d.k,0)) AS k, sum(coalesce(d.hr,0)) AS hr,
                   sum(coalesce(d.sv,0)) AS sv,
                   sum(coalesce(d.w,0)+coalesce(d.qs,0)) AS wqs
            """, names=sr["pl"], a=start.isoformat(), b=end.isoformat()).single()
        s.run("MATCH (sr:ShadowRoster {period:$p}) SET sr.scored=$sc",
              p=period, sc=json.dumps(dict(rows)))
        return dict(rows)


if __name__ == "__main__":
    print("init:", init())
    print("advance:", advance())
