"""Two-sided trade evaluation + Pareto counter search (v1).

Why this exists (2026-08-22): a counter-offer (a mid-rotation starter for a
good bat) was proposed to Like a Nightmare using curve math for OUR side and
nothing for theirs. Their rival profile — chasing R/RBI/HR, surplus WHIP,
44 of 68 transactions churning pitchers, Bieber added off waivers the same
night — made the counter obviously dead: they price starting pitching at
$2.50, and the offer asked them to pay a real bat for it. The failure mode
is structural: every analysis pipeline in this repo prices Runtime Terror's
side only. A trade is two optimization problems joined by a constraint —
the counterparty accepts only if THEY gain — so any counter proposed
without pricing their side is a guess.

This module prices both sides in each team's own marginal points:

  eval_trade(a, gives_a, b, gives_b) -> both teams' projected point deltas,
      by category, using races.analyze()'s projected leaderboards re-ranked
      after shifting each team's totals by the players' ROS contributions.
  counter_search(them) -> enumerate 1-1 / 2-1 / 1-2 / 2-2 bundles between
      our roster and theirs, keep the Pareto-viable set (both sides gain,
      counterparty gain >= MIN_THEIR_GAIN so they'd plausibly act), rank by
      our gain. This is the "weigh the alternatives computationally" loop:
      ~6k bundles scored per counterparty instead of one hand-picked guess.

Honest limits of v1 (all deliberately visible in the report footer):
  * Counting categories only (HR R RBI SB K S WQS). Rate cats (OBP ERA WHIP)
    need per-team ROS denominators and replacement-level modeling to score
    honestly; v1 flags rate-relevant players qualitatively instead of
    pretending precision. This UNDERVALUES elite-ratio arms and OBP bats.
  * Replacement level = 0: a traded-away player's ROS production is counted
    as fully lost, though the freed slot gets a waiver body. Overstates the
    cost of giving depth; scarce stats (S, SB) are least affected, which is
    the right direction of error for this league.
  * ROS pace = 50/50 blend of last-30d and season weekly rates. Players with
    zero recent appearances (IL) project ~half their season rate — crude but
    it keeps injured stars from being priced at full health.
  * Acceptance is modeled as point-gain only. Managers also weigh positional
    fit, name value, and standings pressure; treat the viable set as a
    shortlist, not a prediction.
"""
from __future__ import annotations

from datetime import date, timedelta
from itertools import combinations

from fantasy_assistant.analytics import races
from fantasy_assistant.graph.client import read_session

MODEL_VERSION = "trades-v1"
COUNTING = ["HR", "R", "RBI", "SB", "K", "S", "WQS"]
_FIELDS = {  # cat -> (side, cypher expr over day line d)
    "HR": ("bat", "coalesce(d.hr,0)"), "R": ("bat", "coalesce(d.r,0)"),
    "RBI": ("bat", "coalesce(d.rbi,0)"), "SB": ("bat", "coalesce(d.sb,0)"),
    "K": ("pit", "coalesce(d.k,0)"), "S": ("pit", "coalesce(d.sv,0)"),
    "WQS": ("pit", "coalesce(d.w,0)+coalesce(d.qs,0)"),
}
SEASON_WEEKS_ELAPSED = 21.0
RECENT_DAYS = 30
MIN_THEIR_GAIN = 0.5   # counterparty must clear this to plausibly accept
MAX_BUNDLE = 2
TOP_ASSETS = 12        # per side, ranked by total ROS counting value


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


def roster(team: str) -> list[dict]:
    with read_session() as s:
        return s.run(
            """
            MATCH (st:RosterStint)-[:ON_TEAM]->(t:FantasyTeam {cbs_name:$team}),
                  (st)-[:OF_PLAYER]->(p:Player)
            WHERE st.to_date IS NULL
            RETURN DISTINCT p.name_full AS name, p.uid AS uid,
                   p.cbs_positions AS pos, st.status AS status
            """, team=team).data()


def ros_values(uids: list[str], remaining: int) -> dict[str, dict[str, float]]:
    """player uid -> {cat: projected rest-of-season units} via pace blend."""
    cutoff = (date.today() - timedelta(days=RECENT_DAYS)).isoformat()
    projs: dict[str, dict[str, float]] = {c: {} for c in uids}
    with read_session() as s:
        for cat in COUNTING:
            side, expr = _FIELDS[cat]
            rows = s.run(
                f"""
                UNWIND $ids AS uid
                MATCH (p:Player {{uid:uid}})
                OPTIONAL MATCH (d:PlayerDayLine)-[:OF_PLAYER]->(p)
                WHERE d.side=$side
                WITH uid, sum({expr}) AS season,
                     sum(CASE WHEN d.date >= date($cutoff)
                         THEN {expr} ELSE 0 END) AS recent
                RETURN uid, season, recent
                """, ids=uids, side=side, cutoff=cutoff).data()
            for r in rows:
                wk_season = (r["season"] or 0) / SEASON_WEEKS_ELAPSED
                wk_recent = (r["recent"] or 0) / (RECENT_DAYS / 7.0)
                projs[r["uid"]][cat] = round(
                    remaining * (0.5 * wk_season + 0.5 * wk_recent), 2)
    return projs


def eval_trade(team_a: str, gives_a: list[str], team_b: str,
               gives_b: list[str], race: dict | None = None,
               projs: dict | None = None) -> dict:
    """Both-side point deltas for team_a sending gives_a for team_b's gives_b.

    gives_* are player uids. Counting categories only — see module docstring.
    """
    race = race or races.analyze()
    remaining = race["remaining_periods"]
    projs = projs or ros_values(gives_a + gives_b, remaining)

    out = {"model": MODEL_VERSION, "teams": {team_a: {}, team_b: {}},
           "detail": []}
    for team, gained, lost in ((team_a, gives_b, gives_a),
                               (team_b, gives_a, gives_b)):
        total = 0.0
        for cat in COUNTING:
            board = race["categories"][cat]["proj_leaderboard"]
            teams = [t for t, _, _ in board]
            values = [v for _, v, _ in board]
            idx = teams.index(team)
            base_pts = _points_for_values(values)[idx]
            delta_units = (sum(projs[c].get(cat, 0) for c in gained)
                           - sum(projs[c].get(cat, 0) for c in lost))
            if abs(delta_units) < 1e-9:
                continue
            shifted = values.copy()
            shifted[idx] += delta_units
            new_pts = _points_for_values(shifted)[idx]
            if abs(new_pts - base_pts) > 1e-9:
                out["detail"].append(
                    {"team": team, "cat": cat, "delta_units": round(delta_units, 1),
                     "pts": round(new_pts - base_pts, 2)})
            total += new_pts - base_pts
        out["teams"][team] = round(total, 2)
    return out


def counter_search(them: str, us: str | None = None) -> dict:
    """Pareto-viable bundles vs one counterparty, ranked by our gain."""
    race = races.analyze()
    us = us or race["us"]
    remaining = race["remaining_periods"]

    ours, theirs = roster(us), roster(them)
    ids = [p["uid"] for p in ours + theirs if p["uid"]]
    projs = ros_values(ids, remaining)
    names = {p["uid"]: p["name"] for p in ours + theirs}

    def top_assets(players):
        scored = [(sum(projs.get(p["uid"], {}).values()), p["uid"])
                  for p in players if p["uid"]]
        scored.sort(reverse=True)
        return [cid for _, cid in scored[:TOP_ASSETS]]

    our_ids, their_ids = top_assets(ours), top_assets(theirs)

    def bundles(pool):
        out = [[c] for c in pool]
        if MAX_BUNDLE >= 2:
            out += [list(pair) for pair in combinations(pool, 2)]
        return out

    viable = []
    for give in bundles(our_ids):
        for get in bundles(their_ids):
            ev = eval_trade(us, give, them, get, race=race, projs=projs)
            d_us, d_them = ev["teams"][us], ev["teams"][them]
            if d_us > 0 and d_them >= MIN_THEIR_GAIN:
                viable.append({
                    "we_give": [names[c] for c in give],
                    "we_get": [names[c] for c in get],
                    "our_delta": d_us, "their_delta": d_them,
                    "detail": ev["detail"]})
    viable.sort(key=lambda v: (-v["our_delta"], -v["their_delta"]))
    return {"model": MODEL_VERSION, "us": us, "them": them,
            "as_of_period": race["as_of_period"], "viable": viable,
            "searched": len(bundles(our_ids)) * len(bundles(their_ids))}


def report(result: dict, top: int = 12) -> str:
    lines = [f"PARETO COUNTER SEARCH — {result['us']} <-> {result['them']} "
             f"(through period {result['as_of_period']}) [{result['model']}]",
             f"{result['searched']} bundles searched; "
             f"{len(result['viable'])} viable (both sides gain, "
             f"theirs >= {MIN_THEIR_GAIN})", ""]
    for v in result["viable"][:top]:
        gives = " + ".join(v["we_give"])
        gets = " + ".join(v["we_get"])
        lines.append(f"  us {v['our_delta']:+5.2f} / them {v['their_delta']:+5.2f}"
                     f"   give: {gives:<38} get: {gets}")
    if not result["viable"]:
        lines.append("  none — no bundle in the searched space helps both "
                     "sides on counting stats.")
    lines += ["", "caveats: counting cats only (OBP/ERA/WHIP unscored — "
              "elite-ratio arms and OBP bats are undervalued);",
              "replacement level = 0 (giving depth is over-penalized); "
              "acceptance modeled as point-gain only;",
              "displacement ignored (a gained player is scored as pure "
              "addition even when the lineup slot he'd take is occupied —",
              "gains are overstated for full lineups; net against the "
              "displaced player's ROS before offering)."]
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    them = sys.argv[1] if len(sys.argv) > 1 else "Like a Nightmare"
    print(report(counter_search(them)))
