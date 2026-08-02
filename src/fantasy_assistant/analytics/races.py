"""Race analysis v1: pace-based projections + marginal standings curves.

For each category, projects end-of-season values (YTD + recent-form pace for
counting stats; YTD held for rates), re-ranks into projected final standings
points, and computes our marginal curves — how many category units buy each
additional standings point. This is the valuation substrate PROBLEM.md asks
for: value denominated in expected standings movement, not raw stats.

v1 simplifications (each recorded on the AnalyticsRun):
- rate categories (OBP/ERA/WHIP) projected flat at current YTD value
- pace = mean of the last FORM_PERIODS by-period values
- no variance model yet — curves are deterministic distances
"""
from __future__ import annotations

import json
from datetime import datetime

from fantasy_assistant.graph.client import session
from fantasy_assistant.graph.refdata import CATEGORIES
from fantasy_assistant.analytics.recompute import team_rate_inputs

# one-roster-spot swap assumptions for rate-category curves (explicit v2 model)
SWAP = {"OBP": {"vol": 240.0, "edge": 0.040},   # ROS plate appearances, OBP edge
        "ERA": {"vol": 60.0, "edge": 1.00},     # ROS innings, ERA edge
        "WHIP": {"vol": 60.0, "edge": 0.12}}

MODEL_VERSION = "races-v3"
# Backtest-fitted (2026 periods 8/11/14 -> 19): pure recent-form pace LOSES to
# season-average baseline (17.95 vs 16.87 pts-MAE); a 0.35 recent-weight blend
# beats both (16.46). Team weekly production is mostly noise around season
# averages — shrink hard toward the mean.
FORM_WEIGHT = 0.35
FORM_PERIODS = 4
FINAL_PERIOD = 27


def _points_for(values: dict[str, float], direction: str) -> dict[str, float]:
    """Roto points with ties sharing the average of tied ranks."""
    reverse = direction == "higher"
    ordered = sorted(values.items(), key=lambda kv: kv[1], reverse=reverse)
    pts: dict[str, float] = {}
    i = 0
    n = len(ordered)
    while i < n:
        j = i
        while j + 1 < n and ordered[j + 1][1] == ordered[i][1]:
            j += 1
        # ranks i..j (0-based) share points; rank r earns n - r
        share = sum(n - r for r in range(i, j + 1)) / (j - i + 1)
        for k in range(i, j + 1):
            pts[ordered[k][0]] = share
        i = j + 1
    return pts


def load_inputs():
    with session() as s:
        ytd = s.run(
            """
            MATCH (st:StandingsSnapshot {scope:'ytd'})-[:HAS_LINE]->(l)
                  -[:FOR_TEAM]->(t:FantasyTeam), (l)-[:IN_CATEGORY]->(c:Category)
            RETURN c.code AS cat, t.cbs_name AS team, l.value_reported AS value,
                   l.points AS points
            """
        ).data()
        latest_period = s.run(
            "MATCH (st:StandingsSnapshot {scope:'period'})-[:FOR_PERIOD]->(p) "
            "RETURN max(p.number) AS n"
        ).single()["n"]
        recent = s.run(
            """
            MATCH (st:StandingsSnapshot {scope:'period'})-[:FOR_PERIOD]->(p:ScoringPeriod),
                  (st)-[:HAS_LINE]->(l)-[:FOR_TEAM]->(t:FantasyTeam),
                  (l)-[:IN_CATEGORY]->(c:Category)
            WHERE p.number > $cut
            RETURN c.code AS cat, t.cbs_name AS team, avg(l.value_reported) AS mean
            """,
            cut=latest_period - FORM_PERIODS,
        ).data()
        us = s.run(
            "MATCH (t:FantasyTeam {is_us:true}) RETURN t.cbs_name AS n"
        ).single()["n"]
    return ytd, recent, latest_period, us


_CACHE: dict = {}


def analyze() -> dict:
    if "result" in _CACHE:
        return _CACHE["result"]
    ytd, recent, latest_period, us = load_inputs()
    remaining = FINAL_PERIOD - latest_period
    meta = {c[0]: {"kind": c[1], "direction": c[2]} for c in CATEGORIES}

    ytd_by_cat: dict[str, dict[str, float]] = {}
    for row in ytd:
        ytd_by_cat.setdefault(row["cat"], {})[row["team"]] = row["value"]
    pace: dict[tuple[str, str], float] = {
        (r["cat"], r["team"]): r["mean"] for r in recent
    }

    result: dict = {"model": MODEL_VERSION, "as_of_period": latest_period,
                    "remaining_periods": remaining, "categories": {}, "us": us}
    projected_totals: dict[str, float] = {}
    rate_in = team_rate_inputs()

    def project_rate(cat: str, team: str, ytd_value: float) -> tuple[float, float]:
        """Official-anchored: official YTD rate over derived YTD volume, plus
        derived recent-form components for the remaining weeks. Returns
        (projected_rate, projected_final_denominator)."""
        ri = rate_in.get(team)
        if not ri:
            return ytd_value, 0.0
        y, rec = ri["ytd"], ri["recent"]
        if cat == "OBP":
            denom0, num_wk, den_wk = y.get("pa", 0), rec.get("ob", 0), rec.get("pa", 0)
        elif cat == "ERA":
            denom0, num_wk, den_wk = y.get("outs", 0), rec.get("er", 0), rec.get("outs", 0)
        else:  # WHIP
            denom0, num_wk, den_wk = y.get("outs", 0), rec.get("wh", 0), rec.get("outs", 0)
        scale = 27.0 if cat == "ERA" else (1.0 if cat == "OBP" else 3.0)
        num0 = ytd_value * denom0 / scale
        num_wk = FORM_WEIGHT * num_wk + (1 - FORM_WEIGHT) * num0 / latest_period
        den_wk = FORM_WEIGHT * den_wk + (1 - FORM_WEIGHT) * denom0 / latest_period
        denom_f = denom0 + den_wk * remaining
        num_f = num0 + num_wk * remaining
        rate = num_f * scale / denom_f if denom_f else ytd_value
        return round(rate, 4), denom_f

    for cat, values in ytd_by_cat.items():
        kind, direction = meta[cat]["kind"], meta[cat]["direction"]
        rate_denoms: dict[str, float] = {}
        if kind == "counting":
            proj = {}
            for t, v in values.items():
                blended = (FORM_WEIGHT * pace.get((cat, t), 0.0)
                           + (1 - FORM_WEIGHT) * v / latest_period)
                proj[t] = v + blended * remaining
        else:
            proj = {}
            for t, v in values.items():
                proj[t], rate_denoms[t] = project_rate(cat, t, v)
        pts_now = _points_for(values, direction)
        pts_proj = _points_for(proj, direction)
        for t, p in pts_proj.items():
            projected_totals[t] = projected_totals.get(t, 0.0) + p

        ordered = sorted(proj.items(), key=lambda kv: kv[1],
                         reverse=(direction == "higher"))
        our_idx = next(i for i, (t, _) in enumerate(ordered) if t == us)
        curve = []
        if kind == "counting":
            # units of season-total production needed to pass each team above
            for i in range(our_idx - 1, -1, -1):
                gap = abs(ordered[i][1] - proj[us])
                curve.append({"pass": ordered[i][0], "units_needed": round(gap, 1),
                              "pts_gain": round(pts_proj[ordered[i][0]] - pts_proj[us], 1)})
            cushion = (abs(proj[us] - ordered[our_idx + 1][1])
                       if our_idx + 1 < len(ordered) else None)
        else:
            # rate curves in one-roster-spot swap equivalents
            sw = SWAP[cat]
            denom_f = rate_denoms.get(us) or 1.0
            scale = 27.0 if cat == "ERA" else (1.0 if cat == "OBP" else 3.0)
            impact = sw["vol"] * (3.0 if cat != "OBP" else 1.0) * sw["edge"] * scale / (
                denom_f * (9.0 if cat == "ERA" else (3.0 if cat == "WHIP" else 1.0)))
            # impact = |rate change| from converting one roster spot's ROS
            # volume to a player better by `edge`
            for i in range(our_idx - 1, -1, -1):
                gap = abs(ordered[i][1] - proj[us])
                curve.append({"pass": ordered[i][0],
                              "units_needed": round(gap, 4),
                              "swaps": round(gap / impact, 1) if impact else None,
                              "pts_gain": round(pts_proj[ordered[i][0]] - pts_proj[us], 1)})
            cushion = (abs(proj[us] - ordered[our_idx + 1][1])
                       if our_idx + 1 < len(ordered) else None)
            if cushion is not None and impact:
                cushion = round(cushion / impact, 2)  # in swap equivalents

        result["categories"][cat] = {
            "kind": kind, "direction": direction,
            "ytd_value": values[us], "ytd_pts": pts_now[us],
            "proj_value": round(proj[us], 4), "proj_pts": pts_proj[us],
            "our_pace_per_period": round(pace.get((cat, us), 0.0), 2),
            "curve_up": curve[:4], "cushion_down": cushion,
            "cushion_unit": "swaps" if kind == "rate" else "units",
            "swap_impact": (impact if kind == "rate" else None),
            "proj_leaderboard": [(t, round(v, 3), pts_proj[t]) for t, v in ordered],
        }

    result["projected_final"] = sorted(projected_totals.items(),
                                       key=lambda kv: -kv[1])
    _CACHE["result"] = result
    return result


def write_graph(result: dict) -> None:
    ts = datetime.now().isoformat(timespec="seconds")
    run_uid = f"analytics:races:{ts}"
    with session() as s:
        s.run(
            """
            MERGE (a:AnalyticsRun {uid:$uid})
            SET a.agent='races', a.model_version=$mv, a.finished_at=datetime($ts),
                a.status='complete', a.as_of_period=$p
            """,
            uid=run_uid, mv=MODEL_VERSION, ts=ts, p=result["as_of_period"],
        )
        for cat, data in result["categories"].items():
            s.run(
                """
                MATCH (a:AnalyticsRun {uid:$run}), (c:Category {uid:$cuid})
                MERGE (r:RaceAnalysis {uid:$uid})
                SET r.as_of_period=$p, r.model_version=$mv, r.payload=$payload,
                    r.computed_at=datetime($ts)
                MERGE (r)-[:IN_CATEGORY]->(c)
                MERGE (r)-[:PRODUCED_BY]->(a)
                """,
                run=run_uid, cuid=f"category:{cat}",
                uid=f"race:{cat}:p{result['as_of_period']}:{MODEL_VERSION}",
                p=result["as_of_period"], mv=MODEL_VERSION,
                payload=json.dumps(data), ts=ts,
            )


def report(result: dict) -> str:
    us = result["us"]
    lines = [f"RACE ANALYSIS — through period {result['as_of_period']}, "
             f"{result['remaining_periods']} periods left  [{result['model']}]", ""]
    lines.append("Projected final standings:")
    for i, (t, pts) in enumerate(result["projected_final"], 1):
        marker = " <== us" if t == us else ""
        lines.append(f"  {i:>2}. {t:<26} {pts:5.1f}{marker}")
    lines.append("")
    lines.append(f"{us} categories (sorted by opportunity):")

    def opportunity(item):
        cat, d = item
        first = d["curve_up"][0] if d["curve_up"] else None
        return first["units_needed"] if first else 1e9

    for cat, d in sorted(result["categories"].items(), key=opportunity):
        lines.append(f"  {cat:<5} ytd {d['ytd_value']:<8g} pts {d['ytd_pts']:>4} -> "
                     f"proj pts {d['proj_pts']:>4}  pace/wk {d['our_pace_per_period']}")
        for step in d["curve_up"]:
            lines.append(f"        +{step['units_needed']:<7} season units passes "
                         f"{step['pass']:<26} (+{step['pts_gain']} pts)")
        if d["cushion_down"] is not None:
            lines.append(f"        cushion below: {round(d['cushion_down'], 1)} units")
    return "\n".join(lines)


if __name__ == "__main__":
    result = analyze()
    write_graph(result)
    print(report(result))
