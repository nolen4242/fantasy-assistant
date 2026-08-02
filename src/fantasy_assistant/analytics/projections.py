"""Player-level rest-of-season projections (the successor to team-week pace).

Team ROS production = sum over the CURRENT roster of shrunken player weekly
rates. This sees roster changes the team-level blend is blind to (a closer
acquired last week projects at his own rate, not the team's season average).

Shrinkage is empirical-Bayes-lite at the player level: weekly rate pulled
toward the positional-group mean with volume-based weight
    alpha = vol / (vol + V0)      (V0 = 150 PA for bats, 120 outs for arms)

Availability weights: players in the most recent active lineup count 1.0;
other non-IL/minors roster members (bench, new adds without a lineup yet)
count BENCH_W — the active-21 constraint means bench production mostly
doesn't score.

Historical mode (`as_of_period=T`) uses only day lines and rosters known at
T, for backtesting.
"""
from __future__ import annotations

from collections import defaultdict

from fantasy_assistant.graph.client import session
from fantasy_assistant.graph.refdata import period_dates

STATS = ["hr", "r", "rbi", "sb", "ob", "pa", "k", "sv", "wqs", "er", "outs", "wh"]
V0 = {"bat": 150.0, "pit": 120.0}
BENCH_W = 0.35


def _player_weeks(as_of_period: int) -> list[dict]:
    """Per-player summed production and side, using day lines <= period T."""
    end = period_dates(as_of_period)[1].isoformat()
    with session() as s:
        return s.run(
            """
            MATCH (d:PlayerDayLine)-[:OF_PLAYER]->(p:Player)
            WHERE d.date <= date($end)
            WITH p.uid AS uid, d.side AS side,
                 sum(coalesce(d.hr,0)) AS hr, sum(coalesce(d.r,0)) AS r,
                 sum(coalesce(d.rbi,0)) AS rbi, sum(coalesce(d.sb,0)) AS sb,
                 sum(coalesce(d.h,0)+coalesce(d.bb,0)+coalesce(d.hbp,0)) AS ob,
                 sum(coalesce(d.ab,0)+coalesce(d.bb,0)+coalesce(d.hbp,0)+coalesce(d.sf,0)) AS pa,
                 sum(coalesce(d.k,0)) AS k, sum(coalesce(d.sv,0)) AS sv,
                 sum(coalesce(d.w,0)+coalesce(d.qs,0)) AS wqs,
                 sum(coalesce(d.er,0)) AS er, sum(coalesce(d.outs,0)) AS outs,
                 sum(coalesce(d.ha,0)+coalesce(d.bbi,0)) AS wh
            RETURN uid, side, hr, r, rbi, sb, ob, pa, k, sv, wqs, er, outs, wh
            """,
            end=end,
        ).data()


def _rosters(as_of_period: int) -> dict[str, list[str]]:
    """team -> player uids on roster (non-IL/minors membership) at end of T."""
    end = period_dates(as_of_period)[1].isoformat()
    with session() as s:
        rows = s.run(
            """
            MATCH (st:RosterStint)-[:ON_TEAM]->(t:FantasyTeam),
                  (st)-[:OF_PLAYER]->(p:Player)
            WHERE st.from_date <= date($end)
              AND (st.to_date IS NULL OR st.to_date > date($end))
            RETURN t.cbs_name AS team, p.uid AS uid, st.status AS status
            """,
            end=end,
        ).data()
    out = defaultdict(list)
    for r in rows:
        if r["status"] in ("active",):  # replay status: excludes il/minors
            out[r["team"]].append(r["uid"])
    return out


def _active_lineup(as_of_period: int) -> dict[str, set]:
    with session() as s:
        rows = s.run(
            """
            MATCH (l:LineupAssignment {section:'active'})-[:IN_PERIOD]->(per:ScoringPeriod {number:$n}),
                  (l)-[:BY_TEAM]->(t:FantasyTeam), (l)-[:FILLED_BY]->(p:Player)
            RETURN t.cbs_name AS team, collect(p.uid) AS uids
            """,
            n=as_of_period,
        ).data()
    return {r["team"]: set(r["uids"]) for r in rows}


def team_weekly_ros(as_of_period: int) -> dict[str, dict[str, float]]:
    """-> {team: {stat: expected weekly production of current roster}}."""
    weeks = as_of_period
    players = _player_weeks(as_of_period)
    by_uid: dict[str, dict] = defaultdict(dict)
    for row in players:
        by_uid[row["uid"]][row["side"]] = row

    # positional-group weekly means (per side), volume-weighted
    grp_tot = {"bat": defaultdict(float), "pit": defaultdict(float)}
    grp_n = {"bat": 0, "pit": 0}
    for row in players:
        side = row["side"]
        grp_n[side] += 1
        for st in STATS:
            grp_tot[side][st] += (row.get(st) or 0)
    grp_mu = {side: {st: (grp_tot[side][st] / grp_n[side] / weeks if grp_n[side] else 0.0)
                     for st in STATS} for side in ("bat", "pit")}

    rosters = _rosters(as_of_period)
    lineup = _active_lineup(as_of_period)
    out: dict[str, dict[str, float]] = {}
    for team, uids in rosters.items():
        tot = defaultdict(float)
        act = lineup.get(team, set())
        for uid in uids:
            w_avail = 1.0 if uid in act else BENCH_W
            for side, row in by_uid.get(uid, {}).items():
                vol = (row.get("pa") or 0) if side == "bat" else (row.get("outs") or 0)
                alpha = vol / (vol + V0[side])
                for st in STATS:
                    player_wk = (row.get(st) or 0) / weeks
                    shrunk = alpha * player_wk + (1 - alpha) * grp_mu[side][st] * 0.5
                    tot[st] += w_avail * shrunk
        out[team] = dict(tot)
    return out


if __name__ == "__main__":
    ros = team_weekly_ros(19)
    us = "Runtime Terror"
    print(f"{us} projected weekly ROS: " +
          ", ".join(f"{k}={v:.1f}" for k, v in sorted(ros[us].items())))
