"""Monte Carlo standings simulation (v1): P(win), P(top-5), rank distributions.

Simulates the remaining weeks per (team, category): counting production drawn
weekly ~ Normal(recent-form mean, season weekly sd); rate categories drawn as
final-rate around the component projection, scaled by the remaining-volume
share. Categories are simulated independently (no cross-cat correlation, no
roster-change dynamics — both noted on the run).
"""
from __future__ import annotations

import math
import random
from collections import defaultdict

from fantasy_assistant.analytics import races
from fantasy_assistant.graph.client import session

MODEL_VERSION = "variance-v1"
N_SIMS = 5000
RATE_ROS_WEIGHT = 0.30  # remaining-volume share dampening for rate noise


def weekly_history() -> dict:
    with session() as s:
        rows = s.run(
            """
            MATCH (st:StandingsSnapshot {scope:'period'})-[:FOR_PERIOD]->(p),
                  (st)-[:HAS_LINE]->(l)-[:FOR_TEAM]->(t:FantasyTeam),
                  (l)-[:IN_CATEGORY]->(c:Category)
            RETURN c.code AS cat, t.cbs_name AS team,
                   collect(l.value_reported) AS vals
            """
        ).data()
    return {(r["cat"], r["team"]): r["vals"] for r in rows}


def _sd(vals: list[float]) -> float:
    if len(vals) < 2:
        return 0.0
    m = sum(vals) / len(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))


def simulate(n_sims: int = N_SIMS, seed: int = 2026) -> dict:
    rng = random.Random(seed)
    race = races.analyze()
    R = race["remaining_periods"]
    hist = weekly_history()
    us = race["us"]
    teams = [t for t, _ in race["projected_final"]]

    # per (cat, team): (mean_final, sd_final, direction, is_rate, ytd_value)
    params = {}
    for cat, d in race["categories"].items():
        direction = d["direction"]
        is_rate = d["kind"] == "rate"
        for team, proj_v, _ in d["proj_leaderboard"]:
            wk_sd = _sd(hist.get((cat, team), []))
            if is_rate:
                sd_final = wk_sd * RATE_ROS_WEIGHT / math.sqrt(R)
            else:
                sd_final = wk_sd * math.sqrt(R)
            params[(cat, team)] = (proj_v, sd_final, direction, is_rate)

    win = defaultdict(int)
    top5 = defaultdict(int)
    rank_sum = defaultdict(float)
    our_pts_samples = []
    for _ in range(n_sims):
        totals = defaultdict(float)
        for cat, d in race["categories"].items():
            draws = {}
            for team in teams:
                mean, sd, direction, is_rate = params[(cat, team)]
                draws[team] = rng.gauss(mean, sd) if sd else mean
            pts = races._points_for(draws, d["direction"])
            for t, p in pts.items():
                totals[t] += p
        order = sorted(teams, key=lambda t: -totals[t])
        win[order[0]] += 1
        for t in order[:5]:
            top5[t] += 1
        for i, t in enumerate(order, 1):
            rank_sum[t] += i
        our_pts_samples.append(totals[us])

    our_pts_samples.sort()
    q = lambda p: our_pts_samples[int(p * n_sims)]
    return {
        "model": MODEL_VERSION, "n_sims": n_sims, "us": us,
        "as_of_period": race["as_of_period"],
        "p_win": {t: win[t] / n_sims for t in teams},
        "p_top5": {t: top5[t] / n_sims for t in teams},
        "mean_rank": {t: rank_sum[t] / n_sims for t in teams},
        "our_pts_p10_p50_p90": (round(q(0.10), 1), round(q(0.50), 1), round(q(0.90), 1)),
    }


def report(r: dict) -> str:
    us = r["us"]
    lines = [f"STANDINGS SIMULATION — {r['n_sims']} seasons from period "
             f"{r['as_of_period']} [{r['model']}]", "",
             f"{'team':<26}{'P(win)':>8}{'P(top5)':>9}{'E[rank]':>9}"]
    for t in sorted(r["p_win"], key=lambda t: r["mean_rank"][t]):
        mark = " <== us" if t == us else ""
        lines.append(f"{t:<26}{r['p_win'][t]:>8.1%}{r['p_top5'][t]:>9.1%}"
                     f"{r['mean_rank'][t]:>9.1f}{mark}")
    p10, p50, p90 = r["our_pts_p10_p50_p90"]
    lines.append("")
    lines.append(f"{us} final-points distribution: p10 {p10} / median {p50} / p90 {p90}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(report(simulate()))
