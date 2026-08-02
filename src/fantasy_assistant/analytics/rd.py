"""R&D ledger: the meta-loop made durable.

- record_backtest(): run the projection backtest for every registered model
  and persist ModelEval nodes — the experiment ledger. Re-run as history
  grows; accuracy trends become queryable instead of anecdotal.
- score_sportsline(period): once a period closes, score SportsLine's weekly
  player projections (captured pre-lock in the FA pool) against realized
  day lines -> SourceScore node per period. The projection scoreboard.
- calibration(): recommendation funnel + outcome scores to date.
- report(): one screen of R&D truth.
"""
from __future__ import annotations

import json
from datetime import datetime

from fantasy_assistant.analytics import backtest as bt
from fantasy_assistant.analytics.recompute import recompute_v2
from fantasy_assistant.graph.client import session
from fantasy_assistant.graph.refdata import period_dates


def record_backtest(stands=(8, 11, 14)) -> list[dict]:
    period_vals = bt.load_period_values()
    actual_vals, actual_pts = bt.load_actual_ytd()
    comps = recompute_v2()["components"]
    with session() as s:
        us = s.run("MATCH (t:FantasyTeam {is_us:true}) RETURN t.cbs_name AS n").single()["n"]
    ts = datetime.now().isoformat(timespec="seconds")
    out = []
    for model in ("baseline", "form", "blend35"):
        for T in stands:
            if model == "blend35":
                base = bt.project(T, "baseline", period_vals, comps)
                form = bt.project(T, "form", period_vals, comps)
                proj = {c: {t: 0.65 * base[c][t] + 0.35 * form[c][t] for t in base[c]}
                        for c in base}
            else:
                proj = bt.project(T, model, period_vals, comps)
            sc = bt.score(proj, actual_vals, actual_pts, us)
            out.append({"model": model, "stand_at": T, **sc})
    with session() as s:
        for r in out:
            s.run(
                """
                MERGE (m:ModelEval {uid: $uid})
                SET m.model=$model, m.stand_at=$T, m.eval_at=$eval_at,
                    m.pts_mae=$mae, m.rank_disp=$rd, m.our_err=$oe,
                    m.recorded=datetime($ts), m.detail=$detail
                """,
                uid=f"eval:{r['model']}:s{r['stand_at']}:e{bt.EVAL_AT}",
                model=r["model"], T=r["stand_at"], eval_at=bt.EVAL_AT,
                mae=r["pts_mae_total"], rd=r["mean_rank_displacement"],
                oe=r["our_total_pts_err"], ts=ts,
                detail=json.dumps(r["pts_mae_by_cat"]),
            )
    return out


def score_sportsline(period: int) -> dict | None:
    """Score SportsLine pre-lock weekly projections vs realized period stats
    for pool pitchers (K as the anchor stat; extend as needed)."""
    from datetime import date as _date
    start, end = period_dates(period)
    if end >= _date.today():
        return None  # period not closed — scoring would compare vs zeros
    with session() as s:
        rows = s.run(
            """
            MATCH (f:FreeAgentPoolSnapshot {period_projected:$p})-[:HAS]->
                  (e:PoolEntry {side:'pit'})-[:OF_PLAYER]->(pl:Player)
            WHERE pl.mlbam_id IS NOT NULL
            WITH DISTINCT pl, e
            OPTIONAL MATCH (d:PlayerDayLine {side:'pit'})-[:OF_PLAYER]->(pl)
            WHERE d.date >= date($a) AND d.date <= date($b)
            RETURN pl.name_full AS name, e.sportsline_week AS blob,
                   sum(coalesce(d.k,0)) AS k_actual
            """,
            p=period, a=start.isoformat(), b=end.isoformat(),
        ).data()
    if not rows:
        return None
    errs, n = [], 0
    for r in rows:
        proj = {kv.split("=")[0]: float(kv.split("=")[1]) for kv in (r["blob"] or [])
                if "=" in kv}
        if "k" in proj:
            errs.append(abs(proj["k"] - r["k_actual"]))
            n += 1
    if not n:
        return None
    mae = sum(errs) / n
    with session() as s:
        s.run(
            """
            MERGE (sc:SourceScore {uid:$uid})
            SET sc.source='sportsline', sc.period=$p, sc.stat='K',
                sc.mae=$mae, sc.n=$n, sc.recorded=datetime()
            """,
            uid=f"srcscore:sportsline:K:p{period}", p=period, mae=round(mae, 2), n=n,
        )
    return {"period": period, "stat": "K", "mae": round(mae, 2), "n": n}


def calibration() -> dict:
    with session() as s:
        recs = s.run("MATCH (r:Recommendation) RETURN r.status AS s, count(*) AS c").data()
        outs = s.run(
            "MATCH (o:OutcomeReview) RETURN count(o) AS n, avg(o.score) AS mean"
        ).single()
        alerts = s.run("MATCH (a:Alert) RETURN count(a) AS n").single()["n"]
    return {"recommendations": {r["s"]: r["c"] for r in recs},
            "outcome_reviews": outs["n"], "mean_outcome_score": outs["mean"],
            "alerts_raised": alerts}


def report() -> str:
    lines = ["R&D LEDGER", ""]
    with session() as s:
        evals = s.run(
            "MATCH (m:ModelEval) RETURN m.model AS model, m.stand_at AS T, "
            "m.pts_mae AS mae ORDER BY m.model, m.stand_at"
        ).data()
        scores = s.run(
            "MATCH (sc:SourceScore) RETURN sc.source AS src, sc.stat AS stat, "
            "sc.period AS p, sc.mae AS mae, sc.n AS n ORDER BY p"
        ).data()
    lines.append("model evals (projection backtest, pts-MAE by stand-at):")
    by_model: dict = {}
    for e in evals:
        by_model.setdefault(e["model"], []).append((e["T"], e["mae"]))
    for m, xs in by_model.items():
        lines.append(f"  {m:<10} " + "  ".join(f"p{t}:{v}" for t, v in xs))
    lines.append("")
    lines.append("source scoreboard:")
    for sc in scores or [{"src": "(none yet — first lands when a projected "
                          "period closes)", "stat": "", "p": "", "mae": "", "n": ""}]:
        lines.append(f"  {sc['src']} {sc['stat']} p{sc['p']}: MAE {sc['mae']} (n={sc['n']})")
    lines.append("")
    lines.append(f"calibration: {json.dumps(calibration())}")
    return "\n".join(lines)


if __name__ == "__main__":
    record_backtest()
    for p in (19, 20):
        r = score_sportsline(p)
        if r:
            print("scored sportsline:", r)
    print(report())
