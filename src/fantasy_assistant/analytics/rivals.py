"""Rival needs assessment + trade-market complementarity (v1).

For every team: which categories they can cheaply gain (chasing) and where
they have expendable cushion (surplus), using the same projected-final race
math as analytics.races, normalized by league-typical remaining production.
Then scores each rival's trade complementarity with us — mutual gain exists
when their surplus feeds our chase and vice versa — and names candidate
players on both sides from actual season production.

Trade context: commissioner-approved trades only; deadline read from the
Season node (site countdown is authoritative — the constitution date was stale).
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime

from fantasy_assistant.analytics import races
from fantasy_assistant.graph.client import session

MODEL_VERSION = "rivals-v2"
COUNTING = ["HR", "R", "RBI", "SB", "K", "S", "WQS"]
CAT_STATS = {  # category -> day-line expression pieces used for player value
    "HR": ("bat", "hr"), "R": ("bat", "r"), "RBI": ("bat", "rbi"),
    "SB": ("bat", "sb"), "K": ("pit", "k"), "S": ("pit", "sv"),
}


def _league_weekly(cat_values_by_period: dict) -> float:
    vals = [v for period in cat_values_by_period.values() for v in period.values()]
    return sum(vals) / len(vals) if vals else 1.0


def analyze() -> dict:
    race = races.analyze()
    remaining = race["remaining_periods"]
    us = race["us"]

    with session() as s:
        weekly = s.run(
            """
            MATCH (st:StandingsSnapshot {scope:'period'})-[:FOR_PERIOD]->(p),
                  (st)-[:HAS_LINE]->(l)-[:FOR_TEAM]->(t:FantasyTeam),
                  (l)-[:IN_CATEGORY]->(c:Category)
            WHERE c.code IN $cats
            RETURN c.code AS cat, avg(l.value_reported) AS wk_mean
            """,
            cats=COUNTING,
        ).data()
        trade_counts = s.run(
            """
            MATCH (e:TransactionEvent)-[:BY_TEAM]->(t:FantasyTeam)
            WHERE 'trade' IN e.kinds
            RETURN t.cbs_name AS team, count(e) AS trades
            """
        ).data()
    wk_mean = {r["cat"]: r["wk_mean"] or 1.0 for r in weekly}
    trades_by_team = {r["team"]: r["trades"] for r in trade_counts}

    # per-team needs/surplus from projected leaderboards. Counting cats are
    # normalized by league-typical remaining production; rate cats by the
    # one-roster-spot swap impact from races (same scale for all teams, v2
    # approximation since denominators are league-similar).
    profiles: dict[str, dict] = defaultdict(lambda: {"chasing": [], "surplus": []})
    for cat, d in race["categories"].items():
        board = d["proj_leaderboard"]  # [(team, value, pts)] best->worst
        is_rate = d["kind"] == "rate"
        if is_rate:
            norm = d.get("swap_impact") or 1.0
            chase_max, surplus_min = 1.2, 2.0     # in swaps
        else:
            norm = wk_mean[cat] * remaining        # typical remaining production
            chase_max, surplus_min = 0.25, 0.22
        for i, (team, value, pts) in enumerate(board):
            if i > 0:
                gap_up = abs(board[i - 1][1] - value)
                pts_up = board[i - 1][2] - pts
                if pts_up > 0 and gap_up / norm < chase_max:
                    profiles[team]["chasing"].append(
                        {"cat": cat, "gap": round(gap_up, 4), "pts": pts_up,
                         "effort": round(gap_up / norm, 3)})
            if i + 1 < len(board):
                cushion = abs(value - board[i + 1][1])
                if cushion / norm > surplus_min:
                    profiles[team]["surplus"].append(
                        {"cat": cat, "cushion": round(cushion, 4),
                         "slack": round(cushion / norm, 3)})

    for team, p in profiles.items():
        p["chasing"].sort(key=lambda x: x["effort"])
        p["surplus"].sort(key=lambda x: -x["slack"])
        p["trades_made"] = trades_by_team.get(team, 0)

    # complementarity vs us
    ours = profiles[us]
    our_chase = {c["cat"]: c for c in ours["chasing"]}
    our_surplus = {c["cat"]: c for c in ours["surplus"]}
    matches = []
    for team, p in profiles.items():
        if team == us:
            continue
        gives_us = [c for c in p["surplus"] if c["cat"] in our_chase]
        wants_ours = [c for c in p["chasing"] if c["cat"] in our_surplus]
        score = (sum(our_chase[c["cat"]]["pts"] for c in gives_us)
                 + sum(c["pts"] for c in wants_ours))
        if gives_us or wants_ours:
            matches.append({"team": team, "score": round(score, 1),
                            "they_can_give": gives_us, "they_want": wants_ours,
                            "trades_made": p["trades_made"]})
    matches.sort(key=lambda m: -m["score"])
    with session() as s:
        deadline = s.run(
            "MATCH (se:Season) RETURN toString(se.trade_deadline) AS d"
        ).single()["d"]
    return {"model": MODEL_VERSION, "as_of_period": race["as_of_period"],
            "us": us, "profiles": dict(profiles), "matches": matches,
            "deadline": deadline}


def player_candidates(team: str, cats: list[str], top: int = 4) -> list[dict]:
    """A team's current players ranked by season production in given cats."""
    parts = []
    for cat in cats:
        side_stat = CAT_STATS.get(cat)
        if side_stat:
            parts.append(side_stat)
    if not parts:
        return []
    with session() as s:
        return s.run(
            """
            MATCH (st:RosterStint)-[:ON_TEAM]->(t:FantasyTeam {cbs_name:$team}),
                  (st)-[:OF_PLAYER]->(p:Player)
            WHERE st.to_date IS NULL
            MATCH (d:PlayerDayLine)-[:OF_PLAYER]->(p)
            WITH p.name_full AS name, p.cbs_positions AS pos,
                 sum(CASE WHEN d.side='bat' THEN coalesce(d.hr,0) ELSE 0 END) AS hr,
                 sum(CASE WHEN d.side='bat' THEN coalesce(d.r,0) ELSE 0 END) AS r,
                 sum(CASE WHEN d.side='bat' THEN coalesce(d.rbi,0) ELSE 0 END) AS rbi,
                 sum(CASE WHEN d.side='bat' THEN coalesce(d.sb,0) ELSE 0 END) AS sb,
                 sum(CASE WHEN d.side='pit' THEN coalesce(d.k,0) ELSE 0 END) AS k,
                 sum(CASE WHEN d.side='pit' THEN coalesce(d.sv,0) ELSE 0 END) AS sv,
                 sum(CASE WHEN d.side='pit' THEN coalesce(d.w,0)+coalesce(d.qs,0) ELSE 0 END) AS wqs,
                 sum(CASE WHEN d.side='bat' THEN coalesce(d.h,0)+coalesce(d.bb,0)+coalesce(d.hbp,0) ELSE 0 END) AS ob,
                 sum(CASE WHEN d.side='bat' THEN coalesce(d.ab,0)+coalesce(d.bb,0)+coalesce(d.hbp,0)+coalesce(d.sf,0) ELSE 0 END) AS pa,
                 sum(CASE WHEN d.side='pit' THEN coalesce(d.er,0) ELSE 0 END) AS er,
                 sum(CASE WHEN d.side='pit' THEN coalesce(d.outs,0) ELSE 0 END) AS outs,
                 sum(CASE WHEN d.side='pit' THEN coalesce(d.ha,0)+coalesce(d.bbi,0) ELSE 0 END) AS wh
            RETURN name, pos, hr, r, rbi, sb, k, sv, wqs, ob, pa, er, outs, wh
            """,
            team=team,
        ).data()


def report(result: dict) -> str:
    us = result["us"]
    lines = [f"RIVAL NEEDS & TRADE MARKET — through period {result['as_of_period']} "
             f"[{result['model']}] · trade deadline {result.get('deadline', '?')} (commish approval required)", ""]
    ours = result["profiles"][us]
    lines.append(f"{us}: chasing " +
                 ", ".join(f"{c['cat']} (+{c['pts']} pts for {c['gap']})" for c in ours["chasing"]) )
    lines.append(f"{'':<15}surplus " +
                 ", ".join(f"{c['cat']} (cushion {c['cushion']})" for c in ours["surplus"]))
    lines.append("")
    lines.append("Best trade counterparties (mutual-gain score):")
    cat_key = {"HR": "hr", "R": "r", "RBI": "rbi", "SB": "sb", "K": "k", "S": "sv", "WQS": "wqs"}
    RATES = {"OBP", "ERA", "WHIP"}

    def best_for(cands, cat, n=3):
        if cat == "OBP":
            pool = [c for c in cands if (c["pa"] or 0) >= 150]
            pool.sort(key=lambda c: -(c["ob"] / c["pa"]))
            return ", ".join(f"{c['name']} ({c['ob']/c['pa']:.3f} OBP, {c['pa']:g} PA)" for c in pool[:n])
        if cat in ("ERA", "WHIP"):
            pool = [c for c in cands if (c["outs"] or 0) >= 120]
            if cat == "ERA":
                pool.sort(key=lambda c: c["er"] * 27 / c["outs"])
                return ", ".join(f"{c['name']} ({c['er']*27/c['outs']:.2f} ERA, {c['outs']/3:.0f} IP)" for c in pool[:n])
            pool.sort(key=lambda c: c["wh"] / (c["outs"] / 3))
            return ", ".join(f"{c['name']} ({c['wh']/(c['outs']/3):.2f} WHIP, {c['outs']/3:.0f} IP)" for c in pool[:n])
        key = cat_key.get(cat, "r")
        pool = sorted(cands, key=lambda p: -(p.get(key) or 0))
        return ", ".join(f"{p['name']} ({p[key]:g} {cat})" for p in pool[:n])
    for m in result["matches"][:4]:
        lines.append(f"  {m['team']} — score {m['score']} ({m['trades_made']} trades this season)")
        if m["they_can_give"]:
            gives = ", ".join(f"{c['cat']} (cushion {c['cushion']})" for c in m["they_can_give"])
            lines.append(f"    they can spare: {gives}")
            want_cats = [c["cat"] for c in m["they_can_give"]]
            cands = player_candidates(m["team"], want_cats)
            lines.append(f"    targets: {best_for(cands, want_cats[0])}")
        if m["they_want"]:
            wants = ", ".join(f"{c['cat']} (+{c['pts']} pts for {c['gap']})" for c in m["they_want"])
            lines.append(f"    they're chasing: {wants}")
            want_cats = [c["cat"] for c in m["they_want"]]
            cands = player_candidates(us, want_cats)
            lines.append(f"    our chips: {best_for(cands, want_cats[0])}")
        lines.append("")
    return "\n".join(lines)


def write_graph(result: dict) -> None:
    ts = datetime.now().isoformat(timespec="seconds")
    with session() as s:
        for team, p in result["profiles"].items():
            s.run(
                """
                MATCH (t:FantasyTeam {cbs_name:$team})
                MERGE (r:RivalNeedsAssessment {uid:$uid})
                SET r.as_of_period=$p, r.model_version=$mv,
                    r.payload=$payload, r.computed_at=datetime($ts)
                MERGE (r)-[:FOR_TEAM]->(t)
                """,
                team=team, uid=f"needs:{team}:p{result['as_of_period']}:{MODEL_VERSION}",
                p=result["as_of_period"], mv=MODEL_VERSION,
                payload=json.dumps(p), ts=ts,
            )


if __name__ == "__main__":
    result = analyze()
    write_graph(result)
    print(report(result))
