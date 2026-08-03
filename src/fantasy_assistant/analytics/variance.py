"""Monte Carlo standings simulation (v2): P(win), P(top-5), rank distributions.

v2 adds within-team category correlation: a hot offense lifts HR/R/RBI/OBP
together, pitching volume links K/WQS, etc. The 10x10 weekly correlation
matrix is estimated from the by-period history (per-team demeaned weekly
values, pooled across teams) and applied via Cholesky to each team's draws.
Teams remain independent of each other (they play different opponents), and
roster-change dynamics are still not modeled.
"""
from __future__ import annotations

import math
import random
from collections import defaultdict

from fantasy_assistant.analytics import races
from fantasy_assistant.analytics.recompute import recompute_v2
from fantasy_assistant.graph.client import session

RATE_COMP = {"OBP": ("ob", "pa", 1.0), "ERA": ("er", "outs", 27.0),
             "WHIP": ("wh", "outs", 3.0)}

MODEL_VERSION = "variance-v3"
N_SIMS = 5000
# v3: rate categories simulated via components (num/denom weekly draws),
# replacing the v2 RATE_ROS_WEIGHT dampening hack


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


def weekly_matrix(cats: list[str], teams: list[str]) -> list[list[float]]:
    """Pooled within-team weekly correlation across categories, via per
    (team, cat) z-scored weekly values."""
    with session() as s:
        rows = s.run(
            """
            MATCH (st:StandingsSnapshot {scope:'period'})-[:FOR_PERIOD]->(p),
                  (st)-[:HAS_LINE]->(l)-[:FOR_TEAM]->(t:FantasyTeam),
                  (l)-[:IN_CATEGORY]->(c:Category)
            RETURN t.cbs_name AS team, c.code AS cat, p.number AS period,
                   l.value_reported AS v
            """
        ).data()
    series: dict = {}
    for r in rows:
        series.setdefault((r["team"], r["cat"]), {})[r["period"]] = r["v"]
    # z-score per (team, cat), then pool observations as vectors per (team, period)
    zs: dict = {}
    for (team, cat), by_p in series.items():
        vals = list(by_p.values())
        m = sum(vals) / len(vals)
        sd = _sd(vals) or 1.0
        for period, v in by_p.items():
            zs.setdefault((team, period), {})[cat] = (v - m) / sd
    obs = [[vec.get(c, 0.0) for c in cats]
           for vec in zs.values() if len(vec) == len(cats)]
    n = len(obs)
    k = len(cats)
    corr = [[0.0] * k for _ in range(k)]
    for i in range(k):
        for j in range(k):
            corr[i][j] = sum(o[i] * o[j] for o in obs) / max(n - 1, 1)
    for i in range(k):
        corr[i][i] = 1.0 + 1e-6  # jitter for Cholesky stability
    return corr


def cholesky(a: list[list[float]]) -> list[list[float]]:
    k = len(a)
    L = [[0.0] * k for _ in range(k)]
    for i in range(k):
        for j in range(i + 1):
            s_ = sum(L[i][m] * L[j][m] for m in range(j))
            if i == j:
                L[i][j] = math.sqrt(max(a[i][i] - s_, 1e-9))
            else:
                L[i][j] = (a[i][j] - s_) / L[j][j]
    return L


def simulate(n_sims: int = N_SIMS, seed: int = 2026) -> dict:
    rng = random.Random(seed)
    race = races.analyze()
    R = race["remaining_periods"]
    hist = weekly_history()
    us = race["us"]
    teams = [t for t, _ in race["projected_final"]]

    # counting cats: (mean_final, sd_final); rate cats: component params
    comps = recompute_v2()["components"]
    comp_hist: dict = {}
    for (team, period, side), c in comps.items():
        for k, v in c.items():
            comp_hist.setdefault((team, k), []).append(v)
    params = {}
    rate_params = {}
    for cat, d in race["categories"].items():
        direction = d["direction"]
        is_rate = d["kind"] == "rate"
        for team, proj_v, _ in d["proj_leaderboard"]:
            if is_rate:
                nk, dk, scale = RATE_COMP[cat]
                nh, dh = comp_hist.get((team, nk), [0]), comp_hist.get((team, dk), [1])
                n0, d0 = sum(nh), sum(dh)
                rate_params[(cat, team)] = {
                    "n0": n0, "d0": d0, "scale": scale,
                    "n_mu": sum(nh[-4:]) / min(len(nh), 4), "n_sd": _sd(nh),
                    "d_mu": sum(dh[-4:]) / min(len(dh), 4), "d_sd": _sd(dh)}
                params[(cat, team)] = (proj_v, 0.0, direction, True)
            else:
                wk_sd = _sd(hist.get((cat, team), []))
                params[(cat, team)] = (proj_v, wk_sd * math.sqrt(R), direction, False)

    cats = list(race["categories"].keys())
    L = cholesky(weekly_matrix(cats, teams))
    k = len(cats)

    win = defaultdict(int)
    top5 = defaultdict(int)
    rank_sum = defaultdict(float)
    our_pts_samples = []
    for _ in range(n_sims):
        # correlated shocks per team across categories
        draws_by_cat: dict = {c: {} for c in cats}
        for team in teams:
            z = [rng.gauss(0.0, 1.0) for _ in range(k)]
            corr_z = [sum(L[i][m] * z[m] for m in range(i + 1)) for i in range(k)]
            for i, cat in enumerate(cats):
                mean, sd, direction, is_rate = params[(cat, team)]
                if is_rate:
                    rp = rate_params[(cat, team)]
                    # correlated shock drives the numerator (runs/baserunners
                    # co-move with team form); denominator gets its own noise
                    n_ros = max(0.0, rp["n_mu"] * R + rp["n_sd"] * math.sqrt(R) * corr_z[i])
                    d_ros = max(1.0, rp["d_mu"] * R + rp["d_sd"] * math.sqrt(R) * rng.gauss(0, 1))
                    tot_d = rp["d0"] + d_ros
                    draws_by_cat[cat][team] = ((rp["n0"] * (mean * rp["d0"] / rp["scale"] / max(rp["n0"], 1e-9))
                                                + n_ros) * rp["scale"] / tot_d) if tot_d else mean
                else:
                    draws_by_cat[cat][team] = mean + sd * corr_z[i]
        totals = defaultdict(float)
        for cat, d in race["categories"].items():
            pts = races._points_for(draws_by_cat[cat], d["direction"])
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


def simulate_and_store(n_sims: int = N_SIMS) -> dict:
    """Run the sim and persist a SimResult node — the manager view's odds
    tile and, over time, the odds trend line."""
    from datetime import date as _date
    r = simulate(n_sims)
    us = r["us"]
    p10, p50, p90 = r["our_pts_p10_p50_p90"]
    with session() as s:
        s.run(
            """
            MERGE (sr:SimResult {uid:$uid})
            SET sr.as_of=date($d), sr.as_of_period=$per, sr.model=$model,
                sr.n_sims=$n, sr.p_win=$pw, sr.p_top5=$pt, sr.mean_rank=$mr,
                sr.pts_p10=$p10, sr.pts_p50=$p50, sr.pts_p90=$p90
            """,
            uid=f"sim:{_date.today()}", d=_date.today().isoformat(),
            per=r["as_of_period"], model=r["model"], n=r["n_sims"],
            pw=round(r["p_win"][us], 4), pt=round(r["p_top5"][us], 4),
            mr=round(r["mean_rank"][us], 2), p10=p10, p50=p50, p90=p90,
        )
    return r
