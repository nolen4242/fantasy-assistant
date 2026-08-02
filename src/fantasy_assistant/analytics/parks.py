"""Park factors from our own data: runs per game by venue vs league mean,
season to date (statsapi schedule + linescores; ~100 games/venue by August).
ParkFactor nodes keyed by home-team abbreviation; projections/valuation can
weight hitter/pitcher ROS by opponents' venues later — v1 exposes the data.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date

import httpx

from fantasy_assistant.graph.client import session

STATSAPI = "https://statsapi.mlb.com/api/v1"


def refresh() -> dict:
    r = httpx.get(f"{STATSAPI}/schedule",
                  params={"sportId": 1, "startDate": "2026-03-25",
                          "endDate": date.today().isoformat(),
                          "hydrate": "linescore"}, timeout=120).json()
    runs, games = defaultdict(float), defaultdict(int)
    team_abbr = {t["id"]: t["abbreviation"] for t in
                 httpx.get(f"{STATSAPI}/teams", params={"sportId": 1, "season": 2026},
                           timeout=30).json()["teams"]}
    for d in r.get("dates", []):
        for g in d.get("games", []):
            if g.get("status", {}).get("codedGameState") != "F":
                continue
            ls = g.get("linescore") or {}
            hr_ = (ls.get("teams", {}).get("home", {}) or {}).get("runs")
            ar = (ls.get("teams", {}).get("away", {}) or {}).get("runs")
            if hr_ is None or ar is None:
                continue
            home = team_abbr.get(g["teams"]["home"]["team"]["id"])
            runs[home] += hr_ + ar
            games[home] += 1
    per_game = {t: runs[t] / games[t] for t in games if games[t] >= 20}
    mean = sum(per_game.values()) / len(per_game)
    with session() as s:
        for t, v in per_game.items():
            s.run(
                """
                MERGE (f:ParkFactor {uid:$uid})
                SET f.home_team=$t, f.runs_pg=$v, f.factor=$fac,
                    f.games=$g, f.as_of=date()
                """, uid=f"park:{t}", t=t, v=round(v, 2),
                fac=round(v / mean, 3), g=games[t])
    top = sorted(per_game.items(), key=lambda kv: -kv[1])
    return {"venues": len(per_game),
            "hitter_parks": [(t, round(v / mean, 2)) for t, v in top[:3]],
            "pitcher_parks": [(t, round(v / mean, 2)) for t, v in top[-3:]]}


if __name__ == "__main__":
    print(refresh())
