"""Two-sided trade evaluation + Pareto counter search (v2).

v1 (2026-08-22) fixed the structural failure — counters proposed without
pricing the counterparty — but carried four caveats doing load-bearing work.
v2 retires three of them:

  * displacement & replacement — team value now comes from the slot-aware
    lineup model (analytics.lineup): a gained player only adds his margin
    over whoever he displaces, a lost player's slot refills from the bench,
    and freed roster spots refill from the FA pool priced at SportsLine
    weekly projections. Deltas are whole-TEAM lineup diffs.
  * rate categories — OBP/ERA/WHIP scored from component sums (ob/PA-den,
    ER/outs, walks+hits/outs) over the assigned lineup, layered on each
    team's live YTD rate. YTD denominators are uniform league-typical
    (only ours are directly observable); this scales rate deltas by up to
    ~15 percent for extreme-volume teams but does not reorder bundles.
  * acceptance realism — a counterparty's willingness is scored, not just
    their point delta: revealed preferences from their last-30d adds and
    any live inbound offer (pending_trades capture), plus a star-gap
    plausibility filter (nobody trades a top-tier player for role players,
    whatever the curve math says).

Remaining v2 limits: IL return dates aren't modeled (pending returners are
priced at zero); acceptance weights are heuristics, not fitted; the greedy
lineup assignment is approximate. All three are visible in the report.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from itertools import combinations
from pathlib import Path

from fantasy_assistant.analytics import lineup, races
from fantasy_assistant.graph.client import read_session

MODEL_VERSION = "trades-v2"
COUNTING = lineup.COUNTING
RATES = {"OBP": ("ob", "paden", True), "ERA": ("er", "outs", False),
         "WHIP": ("wh", "outs", False)}
# uniform YTD denominators (league-typical; ours observed 8/22: 5868 PA-den,
# 971 IP -> 2914 outs). Per-team real values are a v3 scrape.
YTD_PADEN = 5900.0
YTD_OUTS = 2915.0
MIN_THEIR_GAIN = 0.5
MAX_BUNDLE = 2
TOP_ASSETS = 12
FA_TOP = 15
STAR_RANK = 15          # top-N overall = "star"
STAR_GAP_RANK = 40      # star-for-(nothing-better-than-rank-40) = implausible
ALIGN_WEIGHT = 0.75     # acceptance-score weight on revealed-preference fit
RAW_ROOT = Path(__file__).resolve().parents[3] / "data" / "raw"


def _points_for_values(values: list[float], higher_better: bool = True) -> list[float]:
    """CBS roto points for a list of team values (ties share points)."""
    n = len(values)
    order = sorted(range(n), key=lambda i: values[i], reverse=higher_better)
    pts = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        share = sum(n - r for r in range(i, j + 1)) / (j - i + 1)
        for r in range(i, j + 1):
            pts[order[r]] = share
        i = j + 1
    return pts


def _era(er: float, outs: float) -> float:
    return er * 27.0 / outs if outs else 0.0


def _rate_value(cat: str, ytd_value: float, comps: dict) -> float:
    """Projected final rate = (ytd_num + ros_num) / (ytd_den + ros_den)."""
    num_k, den_k, _ = RATES[cat]
    if cat == "OBP":
        ytd_num, ytd_den = ytd_value * YTD_PADEN, YTD_PADEN
    elif cat == "ERA":
        ytd_num, ytd_den = ytd_value * YTD_OUTS / 27.0, YTD_OUTS
    else:  # WHIP: value = wh / IP = wh / (outs/3)
        ytd_num, ytd_den = ytd_value * YTD_OUTS / 3.0, YTD_OUTS
    ros_num, ros_den = comps[num_k], comps[den_k]
    num, den = ytd_num + ros_num, ytd_den + ros_den
    if cat == "ERA":
        return _era(num, den) if den else 0.0
    if cat == "WHIP":
        return num / (den / 3.0) if den else 0.0
    return num / den if den else 0.0


class Market:
    """Cached league state for a run: rosters, projections, curves, prefs."""

    def __init__(self):
        self.race = races.analyze()
        self.us = self.race["us"]
        self.remaining = self.race["remaining_periods"]
        self.scales = lineup.league_scales(self.remaining)
        self.fa = sorted(lineup.fa_pool(self.remaining),
                         key=lambda p: -lineup.scalar(p, self.scales))[:FA_TOP]
        self._rosters: dict[str, list[dict]] = {}
        self._base: dict[str, dict] = {}
        self._pursuit: dict[str, dict[str, float]] = {}
        self._zstats = None
        self._star_rank: dict[str, int] | None = None

    def roster(self, team: str) -> list[dict]:
        if team not in self._rosters:
            self._rosters[team] = lineup.roster_players(team, self.remaining)
        return self._rosters[team]

    def base(self, team: str) -> dict:
        if team not in self._base:
            self._base[team] = lineup.team_totals(self.roster(team), self.scales)
        return self._base[team]

    # ---- category points ----------------------------------------------
    def cat_delta_pts(self, team: str, deltas: dict[str, float]) -> list[dict]:
        """Point changes from shifting this team's projected values."""
        out = []
        for cat in COUNTING + list(RATES):
            d = deltas.get(cat, 0.0)
            if abs(d) < 1e-9:
                continue
            board = self.race["categories"][cat]["proj_leaderboard"]
            teams = [t for t, _, _ in board]
            values = [v for _, v, _ in board]
            idx = teams.index(team)
            hb = RATES[cat][2] if cat in RATES else True
            base = _points_for_values(values, hb)[idx]
            shifted = values.copy()
            shifted[idx] += d
            pts = _points_for_values(shifted, hb)[idx] - base
            if abs(pts) > 1e-9:
                out.append({"cat": cat, "delta_units": round(d, 3),
                            "pts": round(pts, 2)})
        return out

    def eval_trade(self, team_a: str, gives_a: list[str], team_b: str,
                   gives_b: list[str]) -> dict:
        """Both-side point deltas; gives_* are player uids on each roster."""
        out = {"model": MODEL_VERSION, "teams": {}, "detail": []}
        for team, lose, gain_from in ((team_a, gives_a, team_b),
                                      (team_b, gives_b, team_a)):
            lose_set = set(lose)
            gained = [p for p in self.roster(gain_from)
                      if p["uid"] in (gives_b if team == team_a else gives_a)]
            roster2 = [p for p in self.roster(team)
                       if p["uid"] not in lose_set] + gained
            quota = max(0, len(lose) - len(gained))
            after = lineup.team_totals(roster2, self.scales,
                                       fa_quota=quota, fa_candidates=self.fa)
            base = self.base(team)
            deltas = {c: after["totals"][c] - base["totals"][c]
                      for c in COUNTING}
            for cat in RATES:
                ytd = self._ytd_rate(team, cat)
                deltas[cat] = (_rate_value(cat, ytd, after["totals"])
                               - _rate_value(cat, ytd, base["totals"]))
            detail = self.cat_delta_pts(team, deltas)
            for d in detail:
                d["team"] = team
            out["detail"] += detail
            out["teams"][team] = round(sum(d["pts"] for d in detail), 2)
        return out

    def _ytd_rate(self, team: str, cat: str) -> float:
        for t, v, _ in self.race["categories"][cat]["proj_leaderboard"]:
            if t == team:
                return v
        return 0.0

    # ---- acceptance ----------------------------------------------------
    def _z(self):
        """League per-cat mean/std of rostered players' season units."""
        if self._zstats is None:
            import statistics as st
            vals = {c: [] for c in COUNTING}
            for team in {t for t, _, _ in
                         self.race["categories"]["HR"]["proj_leaderboard"]}:
                for p in self.roster(team):
                    for c in COUNTING:
                        vals[c].append(p["season"].get(c, 0) or 0)
            self._zstats = {c: (st.mean(v), st.pstdev(v) or 1.0)
                            for c, v in vals.items()}
        return self._zstats

    def zvec(self, player: dict) -> dict[str, float]:
        return {c: ((player["season"].get(c, 0) or 0) - m) / s
                for c, (m, s) in self._z().items()}

    def pursuit(self, team: str) -> dict[str, float]:
        """Revealed preferences: category z-profile of their recent adds,
        plus categories of any player they asked us for in a live offer."""
        if team in self._pursuit:
            return self._pursuit[team]
        cutoff = (date.today() - timedelta(days=30)).isoformat() + "T00:00:00Z"
        with read_session() as s:
            names = [r["nm"] for r in s.run(
                """
                MATCH (e:TransactionEvent)-[:BY_TEAM]->
                      (t:FantasyTeam {cbs_name:$team}),
                      (e)-[:ADDS]->(p:Player)
                WHERE e.posted_at >= datetime($cutoff)
                RETURN p.name_full AS nm
                """, team=team, cutoff=cutoff)]
        vec = {c: 0.0 for c in COUNTING}
        # adds may no longer be rostered anywhere; look them up directly
        if names:
            with read_session() as s:
                rows = s.run(
                    """
                    UNWIND $names AS nm MATCH (p:Player {name_full:nm})
                    OPTIONAL MATCH (d:PlayerDayLine)-[:OF_PLAYER]->(p)
                    RETURN nm,
                      sum(CASE WHEN d.side='bat' THEN coalesce(d.hr,0) ELSE 0 END) AS HR,
                      sum(CASE WHEN d.side='bat' THEN coalesce(d.r,0) ELSE 0 END) AS R,
                      sum(CASE WHEN d.side='bat' THEN coalesce(d.rbi,0) ELSE 0 END) AS RBI,
                      sum(CASE WHEN d.side='bat' THEN coalesce(d.sb,0) ELSE 0 END) AS SB,
                      sum(CASE WHEN d.side='pit' THEN coalesce(d.k,0) ELSE 0 END) AS K,
                      sum(CASE WHEN d.side='pit' THEN coalesce(d.sv,0) ELSE 0 END) AS S,
                      sum(CASE WHEN d.side='pit' THEN coalesce(d.w,0)+coalesce(d.qs,0) ELSE 0 END) AS WQS
                    """, names=names).data()
            for r in rows:
                z = self.zvec({"season": r})
                for c in COUNTING:
                    vec[c] += max(z[c], 0.0)
        # waiver adds show what a team fills for $2.50; a live trade OFFER
        # shows what it will pay real assets for. Weight offers to dominate:
        # unit-normalize the adds signal, then stack offer-wants on top.
        norm = sum(v * v for v in vec.values()) ** 0.5 or 1.0
        vec = {c: 0.5 * v / norm for c, v in vec.items()}
        for c in self._pending_wants(team):
            vec[c] += 1.0
        norm = sum(v * v for v in vec.values()) ** 0.5 or 1.0
        self._pursuit[team] = {c: v / norm for c, v in vec.items()}
        return self._pursuit[team]

    def _pending_wants(self, team: str) -> list[str]:
        """Categories of our players a team asked for in a live offer."""
        f = None
        for d in sorted(RAW_ROOT.iterdir(), reverse=True):
            cand = d / "pending_trades_raw.txt"
            if cand.exists():
                f = cand
                break
        if not f:
            return []
        txt = f.read_text()
        # scope to the offers section — the page body also carries a stats
        # widget naming dozens of unrelated players
        m = re.search(r"PENDING TRADES(.*?)(?:\nMore\n|\Z)", txt, re.S)
        if not m or team not in m.group(1):
            return []
        section = m.group(1)
        wanted = []
        for p in self.roster(self.us):
            if p["name"] in section:
                z = self.zvec(p)
                top = sorted(z, key=lambda c: -z[c])[:2]
                wanted += [c for c in top if z[c] > 0.5]
        return wanted

    def star_rank(self, uid: str) -> int:
        if self._star_rank is None:
            allp = []
            for team in {t for t, _, _ in
                         self.race["categories"]["HR"]["proj_leaderboard"]}:
                allp += self.roster(team)
            allp.sort(key=lambda p: -lineup.scalar(p, self.scales))
            self._star_rank = {p["uid"]: i + 1 for i, p in enumerate(allp)}
        return self._star_rank.get(uid, 999)

    def acceptance(self, them: str, they_give: list[str],
                   they_get: list[dict], their_delta: float) -> dict:
        pv = self.pursuit(them)
        align = 0.0
        for p in they_get:
            z = self.zvec(p)
            align += sum(pv[c] * max(z[c], 0.0) for c in COUNTING)
        align /= max(len(they_get), 1)
        best_given = min((self.star_rank(u) for u in they_give), default=999)
        best_gotten = min((self.star_rank(p["uid"]) for p in they_get),
                          default=999)
        implausible = best_given <= STAR_RANK and best_gotten > STAR_GAP_RANK
        score = their_delta + ALIGN_WEIGHT * align - (3.0 if implausible else 0)
        return {"align": round(align, 2), "implausible": implausible,
                "score": round(score, 2)}


def counter_search(them: str, market: Market | None = None) -> dict:
    mkt = market or Market()
    us = mkt.us
    ours, theirs = mkt.roster(us), mkt.roster(them)
    names = {p["uid"]: p["name"] for p in ours + theirs}
    by_uid = {p["uid"]: p for p in ours + theirs}

    def top_assets(players):
        act = [p for p in players if p["status"] not in ("minors",)]
        act.sort(key=lambda p: -lineup.scalar(p, mkt.scales))
        return [p["uid"] for p in act[:TOP_ASSETS]]

    def bundles(pool):
        out = [[c] for c in pool]
        if MAX_BUNDLE >= 2:
            out += [list(pair) for pair in combinations(pool, 2)]
        return out

    viable, searched = [], 0
    for give in bundles(top_assets(ours)):
        for get in bundles(top_assets(theirs)):
            searched += 1
            ev = mkt.eval_trade(us, give, them, get)
            d_us, d_them = ev["teams"][us], ev["teams"][them]
            if d_us <= 0 or d_them < MIN_THEIR_GAIN:
                continue
            acc = mkt.acceptance(them, get, [by_uid[u] for u in give], d_them)
            viable.append({"we_give": [names[u] for u in give],
                           "we_get": [names[u] for u in get],
                           "our_delta": d_us, "their_delta": d_them,
                           **acc, "detail": ev["detail"]})
    viable.sort(key=lambda v: (v["implausible"], -v["our_delta"], -v["score"]))
    return {"model": MODEL_VERSION, "us": us, "them": them,
            "as_of_period": mkt.race["as_of_period"],
            "pursuit": mkt.pursuit(them), "viable": viable,
            "searched": searched}


def report(result: dict, top: int = 12) -> str:
    pv = result["pursuit"]
    hot = ", ".join(f"{c} {v:.2f}" for c, v in
                    sorted(pv.items(), key=lambda kv: -kv[1]) if v > 0.15)
    plaus = [v for v in result["viable"] if not v["implausible"]]
    lines = [
        f"PARETO COUNTER SEARCH — {result['us']} <-> {result['them']} "
        f"(through period {result['as_of_period']}) [{result['model']}]",
        f"{result['searched']} bundles searched; {len(result['viable'])} "
        f"Pareto-viable, {len(plaus)} plausible after star-gap filter",
        f"their revealed pursuit (recent adds + live offers): {hot or 'none'}",
        ""]
    for v in plaus[:top]:
        lines.append(
            f"  us {v['our_delta']:+5.2f} / them {v['their_delta']:+5.2f} "
            f"accept {v['score']:>5.2f}  "
            f"give: {' + '.join(v['we_give']):<38} "
            f"get: {' + '.join(v['we_get'])}")
    if not plaus:
        lines.append("  none plausible — no bundle helps both sides without "
                     "an implausible star gap.")
    lines += ["", "v2 scores all 10 categories with displacement + FA "
              "replacement; remaining limits: IL returners priced at zero,",
              "uniform YTD rate denominators, heuristic acceptance weights, "
              "greedy (approximate) lineup assignment."]
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    them = sys.argv[1] if len(sys.argv) > 1 else "Like a Nightmare"
    print(report(counter_search(them)))
