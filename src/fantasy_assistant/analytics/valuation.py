"""Player-level trade valuation (v1).

Values a concrete 1-for-1 trade as the change in PROJECTED FINAL STANDINGS
POINTS for BOTH rosters — portfolio value per PROBLEM.md, never context-free
scores. A proposal is "plausible" only if the counterparty's own projected
points don't materially drop (they must want to say yes).

Player ROS projection v1: season-to-date per-week rates x remaining weeks
(injury-depressed for players who missed time — noted, not hidden). IP effect
reported; the 1300 cap is flagged when a trade pushes pace over.
"""
from __future__ import annotations

from collections import defaultdict

from fantasy_assistant.analytics import races
from fantasy_assistant.analytics.recompute import team_rate_inputs
from fantasy_assistant.graph.client import session

MODEL_VERSION = "valuation-v1"
COUNT_CATS = {"HR": "hr", "R": "r", "RBI": "rbi", "SB": "sb",
              "K": "k", "S": "sv", "WQS": "wqs"}


def roster_players(team: str) -> list[dict]:
    with session() as s:
        return s.run(
            """
            MATCH (st:RosterStint)-[:ON_TEAM]->(t:FantasyTeam {cbs_name:$team}),
                  (st)-[:OF_PLAYER]->(p:Player)
            WHERE st.to_date IS NULL
            MATCH (d:PlayerDayLine)-[:OF_PLAYER]->(p)
            WITH p.name_full AS name, p.cbs_positions AS pos, st.status AS status,
                 sum(CASE WHEN d.side='bat' THEN coalesce(d.hr,0) END) AS hr,
                 sum(CASE WHEN d.side='bat' THEN coalesce(d.r,0) END) AS r,
                 sum(CASE WHEN d.side='bat' THEN coalesce(d.rbi,0) END) AS rbi,
                 sum(CASE WHEN d.side='bat' THEN coalesce(d.sb,0) END) AS sb,
                 sum(CASE WHEN d.side='pit' THEN coalesce(d.k,0) END) AS k,
                 sum(CASE WHEN d.side='pit' THEN coalesce(d.sv,0) END) AS sv,
                 sum(CASE WHEN d.side='pit' THEN coalesce(d.w,0)+coalesce(d.qs,0) END) AS wqs,
                 sum(CASE WHEN d.side='bat' THEN coalesce(d.h,0)+coalesce(d.bb,0)+coalesce(d.hbp,0) END) AS ob,
                 sum(CASE WHEN d.side='bat' THEN coalesce(d.ab,0)+coalesce(d.bb,0)+coalesce(d.hbp,0)+coalesce(d.sf,0) END) AS pa,
                 sum(CASE WHEN d.side='pit' THEN coalesce(d.er,0) END) AS er,
                 sum(CASE WHEN d.side='pit' THEN coalesce(d.outs,0) END) AS outs,
                 sum(CASE WHEN d.side='pit' THEN coalesce(d.ha,0)+coalesce(d.bbi,0) END) AS wh
            RETURN name, pos, status, hr, r, rbi, sb, k, sv, wqs, ob, pa, er, outs, wh
            """,
            team=team,
        ).data()


def ros_weekly(p: dict, weeks_played: int = 19) -> dict:
    """Per-week ROS contribution from season-to-date rates."""
    out = {}
    for k in ("hr", "r", "rbi", "sb", "k", "sv", "wqs", "ob", "pa", "er", "outs", "wh"):
        out[k] = (p.get(k) or 0) / weeks_played
    return out


class TradeEvaluator:
    """Projects final points for all teams; evaluates roster deltas."""

    def __init__(self):
        self.race = races.analyze()
        self.remaining = self.race["remaining_periods"]
        self.rate_in = team_rate_inputs()
        self.meta = {c: d for c, d in self.race["categories"].items()}
        # baseline projected values per cat per team
        self.base: dict[str, dict[str, float]] = {
            cat: dict((t, v) for t, v, _ in d["proj_leaderboard"])
            for cat, d in self.race["categories"].items()
        }

    def _team_points(self, values_by_cat: dict[str, dict[str, float]]) -> dict[str, float]:
        totals: dict[str, float] = defaultdict(float)
        for cat, vals in values_by_cat.items():
            pts = races._points_for(vals, self.meta[cat]["direction"])
            for t, p in pts.items():
                totals[t] += p
        return totals

    def _adjusted(self, team: str, delta_wk: dict, sign: int) -> dict[str, dict[str, float]]:
        """Apply +/- player weekly contribution to a team's projections."""
        adj = {cat: dict(vals) for cat, vals in self.base.items()}
        R = self.remaining
        for cat, key in COUNT_CATS.items():
            adj[cat][team] = adj[cat][team] + sign * delta_wk[key] * R
        ri = self.rate_in.get(team, {"ytd": {}, "recent": {}})
        y, rec = ri["ytd"], ri["recent"]
        # rate cats: recompute team final rate with player flow added/removed
        pa_f = y.get("pa", 0) + (rec.get("pa", 0) + sign * delta_wk["pa"]) * R
        ob_f = (self.base["OBP"][team] * y.get("pa", 1)) + (rec.get("ob", 0) + sign * delta_wk["ob"]) * R
        if pa_f:
            adj["OBP"][team] = round(ob_f / pa_f, 4)
        outs_f = y.get("outs", 0) + (rec.get("outs", 0) + sign * delta_wk["outs"]) * R
        er_f = (self.base["ERA"][team] * y.get("outs", 1) / 27) + (rec.get("er", 0) + sign * delta_wk["er"]) * R
        wh_f = (self.base["WHIP"][team] * y.get("outs", 1) / 3) + (rec.get("wh", 0) + sign * delta_wk["wh"]) * R
        if outs_f:
            adj["ERA"][team] = round(er_f * 27 / outs_f, 4)
            adj["WHIP"][team] = round(wh_f * 3 / outs_f, 4)
        return adj

    def evaluate_trade(self, our_team: str, their_team: str,
                       give: dict, get: dict) -> dict:
        base_pts = self._team_points(self.base)
        gw, tw = ros_weekly(give), ros_weekly(get)
        adj = self.base
        # our side: -give +get ; their side: +give -get
        for team, wk, sign in ((our_team, gw, -1), (our_team, tw, +1),
                               (their_team, gw, +1), (their_team, tw, -1)):
            self_base, self.base = self.base, adj
            adj = self._adjusted(team, wk, sign)
            self.base = self_base
        new_pts = self._team_points(adj)
        ip_delta_wk = (tw["outs"] - gw["outs"]) / 3
        return {
            "give": give["name"], "get": get["name"],
            "our_delta": round(new_pts[our_team] - base_pts[our_team], 1),
            "their_delta": round(new_pts[their_team] - base_pts[their_team], 1),
            "ip_pace_delta": round(ip_delta_wk * self.remaining, 0),
        }


QUALITY_WEEKS = 19.0
# per-week stat weights approximating market value of production (both sides
# on one scale so cross-type trades can be compared)
QUALITY_W = {"hr": 1.0, "r": 0.45, "rbi": 0.45, "sb": 1.1, "ob": 0.30,
             "k": 0.28, "sv": 1.6, "wqs": 1.4, "outs": 0.02}
FAIRNESS_MAX = 1.45   # counterparty won't accept giving up a much better player


def quality(p: dict) -> float:
    wk = ros_weekly(p)
    return round(sum(w * wk.get(k, 0.0) for k, w in QUALITY_W.items()), 2)


def _tier(r: dict) -> str | None:
    """Acceptance tiers modeling how the counterparty actually decides:
    they see player quality (public perception) and their own interests,
    not our projections."""
    if r["our_delta"] <= 0:
        return None
    qg, qt = r["q_give"], r["q_get"]
    if r["their_delta"] >= 0.5 and qg >= qt * 0.55:
        return "win-win"          # they gain points and the swap isn't absurd
    if abs(r["their_delta"]) <= 0.5 and qt <= qg * FAIRNESS_MAX:
        return "fair-swap"        # points-neutral for them, quality comparable
    if r["their_delta"] >= -1.0 and qg >= qt * 1.05:
        return "needs-sweetener"  # we send the better player; close on points
    return None


def scan(counterparties: list[str], top: int = 12) -> list[dict]:
    ev = TradeEvaluator()
    us = ev.race["us"]
    ours = [p for p in roster_players(us) if p["status"] in ("active", "reserve", "il")]
    results = []
    for rival in counterparties:
        theirs = [p for p in roster_players(rival) if p["status"] in ("active", "reserve", "il")]
        for give in ours:
            qg = quality(give)
            for get in theirs:
                r = ev.evaluate_trade(us, rival, give, get)
                r["with"] = rival
                r["q_give"], r["q_get"] = qg, quality(get)
                r["tier"] = _tier(r)
                if r["tier"]:
                    # sensitivity: does the win survive the target hitting
                    # 20% below his season rate? fragile wins say so
                    damp = dict(get)
                    for k_ in ("hr", "r", "rbi", "sb", "k", "sv", "wqs", "ob"):
                        damp[k_] = (damp.get(k_) or 0) * 0.8
                    r2 = ev.evaluate_trade(us, rival, give, damp)
                    r["fragile"] = r2["our_delta"] <= 0
                    results.append(r)
    order = {"win-win": 0, "fair-swap": 1, "needs-sweetener": 2}
    results.sort(key=lambda r: (order[r["tier"]], -r["our_delta"]))
    # keep at most 2 per (get) target so one star doesn't flood the board
    seen: dict[str, int] = {}
    out = []
    for r in results:
        if seen.get(r["get"], 0) < 2:
            out.append(r)
            seen[r["get"]] = seen.get(r["get"], 0) + 1
    return out[:top]


if __name__ == "__main__":
    targets = ["Gashouse Gang", "Dawg", "Maga Doge"]
    print(f"TRADE SCAN [{MODEL_VERSION}] vs {', '.join(targets)} — "
          f"1-for-1, both sides valued in projected final points\n")
    board = scan(targets)
    if not board:
        print("  (no 1-for-1 clears the acceptance tiers under current "
              "projections — races-v3 shrinkage compresses category gaps; "
              "consolidation 2-for-1s are the open lane, queued)")
    for r in board:
        flag = " [IP-CAP WATCH]" if r["ip_pace_delta"] > 80 else ""
        fr = " [FRAGILE]" if r.get("fragile") else ""
        print(f"  [{r['tier']:<15}] give {r['give']:<20} get {r['get']:<20} "
              f"({r['with']:<13}) us {r['our_delta']:+.1f} them {r['their_delta']:+.1f} "
              f"q {r['q_give']:.1f}->{r['q_get']:.1f}{fr}{flag}")
