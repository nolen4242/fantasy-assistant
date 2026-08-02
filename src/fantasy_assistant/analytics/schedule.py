"""Schedule awareness: MLB teams play 5-7 games a week — projections that
ignore this carry a known bias (SELF-CRITIQUE #1 math gap).

games_next_period(): per MLB team, games in the upcoming scoring period.
ros_factor(): per MLB team, remaining-games vs league mean — a multiplier
for player ROS volume. Cached in-graph as ScheduleFactor nodes.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

import httpx

from fantasy_assistant.graph.client import session
from fantasy_assistant.graph.refdata import period_dates, period_for_date

STATSAPI = "https://statsapi.mlb.com/api/v1"
SEASON_END = date(2026, 9, 27)


def _schedule(start: date, end: date) -> dict[str, int]:
    r = httpx.get(f"{STATSAPI}/schedule",
                  params={"sportId": 1, "startDate": start.isoformat(),
                          "endDate": end.isoformat()}, timeout=60).json()
    games: dict[str, int] = defaultdict(int)
    for d in r.get("dates", []):
        for g in d.get("games", []):
            if g.get("status", {}).get("codedGameState") in ("S", "P", "F", "I"):
                for side in ("home", "away"):
                    games[g["teams"][side]["team"]["name"]] += 1
    return dict(games)


def refresh() -> dict:
    nxt = period_for_date(date.today()) + (1 if date.today().weekday() == 6 else 0)
    p_start, p_end = period_dates(nxt)
    week = _schedule(max(p_start, date.today() + timedelta(days=1)), p_end)
    ros = _schedule(date.today() + timedelta(days=1), SEASON_END)
    mean_ros = sum(ros.values()) / max(len(ros), 1)
    with session() as s:
        # map MLB full names -> our cbs team abbrevs via players' cbs_mlb_team?
        # store by MLB team name; consumers join through player.cbs_mlb_team
        # using the statsapi teams endpoint abbreviation map
        teams = httpx.get(f"{STATSAPI}/teams", params={"sportId": 1, "season": 2026},
                          timeout=30).json()["teams"]
        for t in teams:
            name, abbr = t["name"], t["abbreviation"]
            s.run(
                """
                MERGE (f:ScheduleFactor {uid:$uid})
                SET f.mlb_team=$abbr, f.games_next_period=$g,
                    f.games_ros=$ros, f.ros_factor=$fac,
                    f.next_period=$np, f.as_of=date()
                """,
                uid=f"sched:{abbr}", abbr=abbr,
                g=week.get(name, 0), ros=ros.get(name, 0),
                fac=round(ros.get(name, 0) / mean_ros, 3) if mean_ros else 1.0,
                np=nxt,
            )
    return {"teams": len(teams), "next_period": nxt,
            "spread_next_week": (min(week.values()), max(week.values())) if week else None}


if __name__ == "__main__":
    print(refresh())
