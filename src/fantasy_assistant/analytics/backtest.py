"""Backtest the projection models against held-out season history.

Stand at period T using only data available then; project every team's
category values at period 19; score against what actually happened. Two
models compete:
  - form:     races-v2 logic (recent-FORM_PERIODS pace; component-blend rates)
  - baseline: season-average pace (value/T per week; rates held at cumulative)

Scored on (a) category-value MAE, (b) standings-points MAE after re-ranking,
(c) our own team's total-points error. The R&D loop's first real exercise:
if `form` can't beat `baseline` here, the trade board doesn't get to use it.
"""
from __future__ import annotations

from collections import defaultdict

from fantasy_assistant.analytics import races
from fantasy_assistant.analytics.recompute import recompute_v2
from fantasy_assistant.graph.client import session
from fantasy_assistant.graph.refdata import CATEGORIES

FORM_PERIODS = 4
EVAL_AT = 19


def load_period_values() -> dict:
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
    out: dict = defaultdict(dict)
    for r in rows:
        out[(r["cat"], r["team"])][r["period"]] = r["v"]
    return out


def load_actual_ytd() -> dict:
    with session() as s:
        rows = s.run(
            """
            MATCH (st:StandingsSnapshot {scope:'ytd'})-[:HAS_LINE]->(l)
                  -[:FOR_TEAM]->(t:FantasyTeam), (l)-[:IN_CATEGORY]->(c:Category)
            RETURN c.code AS cat, t.cbs_name AS team, l.value_reported AS v,
                   l.points AS pts
            """
        ).data()
    vals, pts = defaultdict(dict), defaultdict(dict)
    for r in rows:
        vals[r["cat"]][r["team"]] = r["v"]
        pts[r["cat"]][r["team"]] = r["pts"]
    return vals, pts


def project(T: int, model: str, period_vals: dict, comps: dict) -> dict:
    """-> {cat: {team: projected_value_at_19}} using only data <= T."""
    meta = {c[0]: (c[1], c[2]) for c in CATEGORIES}
    teams = sorted({team for (_, team) in period_vals})
    R = EVAL_AT - T
    out: dict = defaultdict(dict)
    for cat, (kind, _) in meta.items():
        for team in teams:
            hist = period_vals.get((cat, team), {})
            missing = [p for p in range(1, T + 1) if p not in hist]
            if missing:
                raise ValueError(
                    f"backtest inputs incomplete: {cat}/{team} missing periods "
                    f"{missing} — refusing to score against fabricated zeros")
            upto = [hist[p] for p in range(1, T + 1)]
            if kind == "counting":
                cum = sum(upto)
                pace = (sum(upto[-FORM_PERIODS:]) / FORM_PERIODS if model == "form"
                        else cum / T)
                out[cat][team] = cum + pace * R
            else:
                key_n, key_d = {"OBP": ("ob", "pa"), "ERA": ("er", "outs"),
                                "WHIP": ("wh", "outs")}[cat]
                side = "bat" if cat == "OBP" else "pit"
                n0 = sum(comps.get((team, p, side), {}).get(key_n, 0) for p in range(1, T + 1))
                d0 = sum(comps.get((team, p, side), {}).get(key_d, 0) for p in range(1, T + 1))
                if model == "form":
                    nw = sum(comps.get((team, p, side), {}).get(key_n, 0)
                             for p in range(T - FORM_PERIODS + 1, T + 1)) / FORM_PERIODS
                    dw = sum(comps.get((team, p, side), {}).get(key_d, 0)
                             for p in range(T - FORM_PERIODS + 1, T + 1)) / FORM_PERIODS
                else:
                    nw, dw = n0 / T, d0 / T
                nf, df = n0 + nw * R, d0 + dw * R
                scale = {"OBP": 1.0, "ERA": 27.0, "WHIP": 3.0}[cat]
                out[cat][team] = (nf * scale / df) if df else 0.0
    return out


def score(proj: dict, actual_vals: dict, actual_pts: dict, us: str) -> dict:
    meta = {c[0]: c[2] for c in CATEGORIES}
    val_mae, pts_mae = {}, {}
    our_total_err = 0.0
    total_pred, total_act = defaultdict(float), defaultdict(float)
    for cat, pred in proj.items():
        acts = actual_vals[cat]
        errs = [abs(pred[t] - acts[t]) for t in pred]
        val_mae[cat] = sum(errs) / len(errs)
        ppts = races._points_for(pred, meta[cat])
        perrs = [abs(ppts[t] - actual_pts[cat][t]) for t in pred]
        pts_mae[cat] = sum(perrs) / len(perrs)
        for t in pred:
            total_pred[t] += ppts[t]
            total_act[t] += actual_pts[cat][t]
    our_total_err = total_pred[us] - total_act[us]
    rank_pred = sorted(total_pred, key=lambda t: -total_pred[t])
    rank_act = sorted(total_act, key=lambda t: -total_act[t])
    # Spearman-ish: mean abs rank displacement
    disp = sum(abs(rank_pred.index(t) - rank_act.index(t)) for t in total_pred) / len(total_pred)
    return {"pts_mae_total": round(sum(pts_mae.values()), 2),
            "pts_mae_by_cat": {c: round(v, 2) for c, v in pts_mae.items()},
            "our_total_pts_err": round(our_total_err, 1),
            "mean_rank_displacement": round(disp, 2)}


def run(stand_at: list[int] = (8, 11, 14)) -> None:
    period_vals = load_period_values()
    actual_vals, actual_pts = load_actual_ytd()
    comps = recompute_v2()["components"]
    with session() as s:
        us = s.run("MATCH (t:FantasyTeam {is_us:true}) RETURN t.cbs_name AS n").single()["n"]

    print(f"BACKTEST — project to period {EVAL_AT}, scored vs actual "
          f"(points MAE across 13 teams x 10 cats; lower is better)\n")
    agg = defaultdict(list)
    for T in stand_at:
        print(f"stand at period {T} ({EVAL_AT - T} periods ahead):")
        for model in ("baseline", "form"):
            proj = project(T, model, period_vals, comps)
            sc = score(proj, actual_vals, actual_pts, us)
            agg[model].append(sc["pts_mae_total"])
            print(f"  {model:<9} pts-MAE {sc['pts_mae_total']:>6}  "
                  f"rank-displacement {sc['mean_rank_displacement']:>5}  "
                  f"our-total-err {sc['our_total_pts_err']:+.1f}")
        print()
    for model, v in agg.items():
        print(f"mean pts-MAE {model}: {sum(v)/len(v):.2f}")


if __name__ == "__main__":
    run()
