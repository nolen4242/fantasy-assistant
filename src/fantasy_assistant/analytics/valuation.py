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


def scan(counterparties: list[str], top: int = 10) -> list[dict]:
    ev = TradeEvaluator()
    us = ev.race["us"]
    ours = [p for p in roster_players(us) if p["status"] in ("active", "reserve")]
    results = []
    for rival in counterparties:
        theirs = [p for p in roster_players(rival) if p["status"] in ("active", "reserve")]
        for give in ours:
            for get in theirs:
                # only cross-type or same-type swaps that could make sense
                r = ev.evaluate_trade(us, rival, give, get)
                r["with"] = rival
                results.append(r)
    results.sort(key=lambda r: -(r["our_delta"] + min(r["their_delta"], 0)))
    plausible = [r for r in results if r["their_delta"] >= -0.6 and r["our_delta"] > 0]
    return plausible[:top]


if __name__ == "__main__":
    targets = ["Gashouse Gang", "Dawg", "Maga Doge"]
    print(f"TRADE SCAN [{MODEL_VERSION}] vs {', '.join(targets)} — "
          f"1-for-1, both sides valued in projected final points\n")
    for r in scan(targets):
        flag = " [IP-CAP WATCH]" if r["ip_pace_delta"] > 80 else ""
        print(f"  give {r['give']:<24} get {r['get']:<24} ({r['with']:<14}) "
              f"us {r['our_delta']:+.1f}  them {r['their_delta']:+.1f}  "
              f"IP pace {r['ip_pace_delta']:+.0f}{flag}")
