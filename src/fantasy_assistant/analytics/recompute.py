"""Standings recomputation: PlayerDayLine x LineupAssignment -> category
values per (team, period), verified against CBS's own by-period standings.

This is the deepest reconciliation in the system: if our derived stats match
CBS's scoreboard, then every layer under it (identity crosswalk, day lines,
lineup capture, period calendar) is simultaneously validated. Known
approximation: when a slot changed hands mid-period (players lock
individually), we attribute the full period to every listed occupant, so
small overcounts are expected exactly where in-period swaps occurred —
mismatches are reported, never hidden.
"""
from __future__ import annotations

from collections import defaultdict

from fantasy_assistant.graph.client import session


def recompute() -> dict:
    with session() as s:
        rows = s.run(
            """
            MATCH (l:LineupAssignment {section:'active'})-[:IN_PERIOD]->(per:ScoringPeriod),
                  (l)-[:BY_TEAM]->(t:FantasyTeam), (l)-[:FILLED_BY]->(p:Player)
            MATCH (d:PlayerDayLine)-[:OF_PLAYER]->(p)
            WHERE d.date >= per.start_date AND d.date <= per.end_date
              AND ((l.slot = 'P') = (d.side = 'pit'))
            WITH t.cbs_name AS team, per.number AS period, d.side AS side,
                 sum(d.hr) AS hr, sum(d.r) AS r, sum(d.rbi) AS rbi,
                 sum(d.sb) AS sb, sum(d.h) AS h, sum(d.bb) AS bb,
                 sum(d.hbp) AS hbp, sum(d.ab) AS ab, sum(d.sf) AS sf,
                 sum(d.k) AS k, sum(d.sv) AS sv, sum(d.w) AS w,
                 sum(d.qs) AS qs, sum(d.er) AS er, sum(d.outs) AS outs,
                 sum(d.ha) AS ha, sum(d.bbi) AS bbi
            RETURN team, period, side, hr, r, rbi, sb, h, bb, hbp, ab, sf,
                   k, sv, w, qs, er, outs, ha, bbi
            """
        ).data()
        official = s.run(
            """
            MATCH (st:StandingsSnapshot {scope:'period'})-[:FOR_PERIOD]->(per),
                  (st)-[:HAS_LINE]->(l)-[:FOR_TEAM]->(t:FantasyTeam),
                  (l)-[:IN_CATEGORY]->(c:Category)
            RETURN per.number AS period, t.cbs_name AS team, c.code AS cat,
                   l.value_reported AS value
            """
        ).data()

    derived: dict[tuple[str, int], dict[str, float]] = defaultdict(dict)
    for row in rows:
        key = (row["team"], row["period"])
        d = derived[key]
        if row["side"] == "bat":
            d["HR"] = row["hr"] or 0
            d["R"] = row["r"] or 0
            d["RBI"] = row["rbi"] or 0
            d["SB"] = row["sb"] or 0
            ob = (row["h"] or 0) + (row["bb"] or 0) + (row["hbp"] or 0)
            pa = (row["ab"] or 0) + (row["bb"] or 0) + (row["hbp"] or 0) + (row["sf"] or 0)
            d["OBP"] = round(ob / pa, 4) if pa else 0.0
        else:
            d["K"] = row["k"] or 0
            d["S"] = row["sv"] or 0
            d["WQS"] = (row["w"] or 0) + (row["qs"] or 0)
            outs = row["outs"] or 0
            d["ERA"] = round((row["er"] or 0) * 27 / outs, 3) if outs else 0.0
            d["WHIP"] = round(((row["ha"] or 0) + (row["bbi"] or 0)) / (outs / 3), 4) if outs else 0.0

    tol = {"OBP": 0.0011, "ERA": 0.011, "WHIP": 0.011}
    stats = defaultdict(lambda: {"match": 0, "close": 0, "miss": 0})
    misses = []
    for row in official:
        key = (row["team"], row["period"])
        cat = row["cat"]
        ours = derived.get(key, {}).get(cat)
        if ours is None:
            stats[cat]["miss"] += 1
            continue
        diff = abs(ours - row["value"])
        if diff < 1e-9:
            stats[cat]["match"] += 1
        elif diff <= tol.get(cat, 0.5):
            stats[cat]["close"] += 1
        else:
            stats[cat]["miss"] += 1
            misses.append((key[0], key[1], cat, row["value"], ours))
    return {"stats": dict(stats), "misses": misses,
            "team_periods": len(derived)}


if __name__ == "__main__":
    r = recompute()
    total = {"match": 0, "close": 0, "miss": 0}
    print(f"recomputed {r['team_periods']} team-periods")
    print(f"{'cat':<6}{'exact':>7}{'close':>7}{'miss':>6}")
    for cat, st in sorted(r["stats"].items()):
        for k in total:
            total[k] += st[k]
        print(f"{cat:<6}{st['match']:>7}{st['close']:>7}{st['miss']:>6}")
    n = sum(total.values())
    print(f"total  exact {total['match']}/{n} ({100*total['match']/n:.1f}%), "
          f"within-tolerance {100*(total['match']+total['close'])/n:.1f}%")
    print("\nworst misses (team, period, cat, official, ours):")
    for m in sorted(r["misses"], key=lambda x: -abs(x[3] - x[4]))[:12]:
        print("  ", m)


# ---------------------------------------------------------------------------
# v2: infer hidden non-participants at over-occupied slots
#
# CBS locks each player individually at his first game of the period, so a
# slot's occupant either contributes his whole period or nothing. When the
# roster-report lists more occupants than a slot's capacity, exactly
# (occupants - capacity) of them contributed nothing — and which ones is
# recoverable by solving the exclusion subset against CBS's official
# category totals. (Validated: Runtime Terror p14, Sonny Gray K=20 = delta.)
# ---------------------------------------------------------------------------

from itertools import combinations, product

SLOT_CAP = {"C": 1, "1B": 1, "2B": 1, "3B": 1, "SS": 1, "MI": 1, "CI": 1,
            "OF": 4, "U": 1, "P": 9}

BAT_COUNT = ["HR", "R", "RBI", "SB"]
PIT_COUNT = ["K", "S", "WQS"]


def _per_player(s, side: str):
    return s.run(
        """
        MATCH (l:LineupAssignment {section:'active'})-[:IN_PERIOD]->(per:ScoringPeriod),
              (l)-[:BY_TEAM]->(t:FantasyTeam), (l)-[:FILLED_BY]->(p:Player)
        WHERE (l.slot = 'P') = ($side = 'pit')
        OPTIONAL MATCH (d:PlayerDayLine {side:$side})-[:OF_PLAYER]->(p)
        WHERE d.date >= per.start_date AND d.date <= per.end_date
        WITH t.cbs_name AS team, per.number AS period, l.slot AS slot,
             p.uid AS puid, p.name_full AS name,
             sum(d.hr) AS hr, sum(d.r) AS r, sum(d.rbi) AS rbi, sum(d.sb) AS sb,
             sum(d.h) AS h, sum(d.bb) AS bb, sum(d.hbp) AS hbp, sum(d.ab) AS ab,
             sum(d.sf) AS sf, sum(d.k) AS k, sum(d.sv) AS sv,
             sum(d.w) AS w, sum(d.qs) AS qs, sum(d.er) AS er,
             sum(d.outs) AS outs, sum(d.ha) AS ha, sum(d.bbi) AS bbi
        RETURN team, period, slot, puid, name, hr, r, rbi, sb, h, bb, hbp,
               ab, sf, k, sv, w, qs, er, outs, ha, bbi
        """,
        side=side,
    ).data()


def recompute_v2() -> dict:
    with session() as s:
        bat = _per_player(s, "bat")
        pit = _per_player(s, "pit")
        official_rows = s.run(
            """
            MATCH (st:StandingsSnapshot {scope:'period'})-[:FOR_PERIOD]->(per),
                  (st)-[:HAS_LINE]->(l)-[:FOR_TEAM]->(t:FantasyTeam),
                  (l)-[:IN_CATEGORY]->(c:Category)
            RETURN per.number AS period, t.cbs_name AS team, c.code AS cat,
                   l.value_reported AS value
            """
        ).data()

    official: dict[tuple[str, int], dict[str, float]] = defaultdict(dict)
    for row in official_rows:
        official[(row["team"], row["period"])][row["cat"]] = row["value"]

    def cat_totals(players, side):
        z = lambda k: sum((p[k] or 0) for p in players)
        if side == "bat":
            ob, pa = z("h") + z("bb") + z("hbp"), z("ab") + z("bb") + z("hbp") + z("sf")
            return {"HR": z("hr"), "R": z("r"), "RBI": z("rbi"), "SB": z("sb"),
                    "OBP": round(ob / pa, 4) if pa else 0.0}
        outs = z("outs")
        return {"K": z("k"), "S": z("sv"), "WQS": z("w") + z("qs"),
                "ERA": round(z("er") * 27 / outs, 3) if outs else 0.0,
                "WHIP": round((z("ha") + z("bbi")) / (outs / 3), 4) if outs else 0.0}

    CAPACITY = {"bat": 11, "pit": 9}
    RATE_TOL = {"OBP": 0.0011, "ERA": 0.011, "WHIP": 0.011}

    def _accept(totals, off, counts, rates):
        if not all(abs(totals[c] - off.get(c, -1)) < 1e-9 for c in counts):
            return False
        # rate check breaks degeneracy: a 0-K blowup reliever's exclusion is
        # invisible to counting cats but not to ERA/WHIP
        return all(abs(totals[c] - off[c]) <= RATE_TOL[c]
                   for c in rates if c in off)

    def solve(players, side, off):
        counts = BAT_COUNT if side == "bat" else PIT_COUNT
        rates = ["OBP"] if side == "bat" else ["ERA", "WHIP"]
        by_slot: dict[str, list] = defaultdict(list)
        for p in players:
            by_slot[p["slot"]].append(p)
        options = []
        for slot, occ in by_slot.items():
            cap = SLOT_CAP.get(slot, 1)
            if len(occ) > cap:
                options.append(list(combinations(occ, len(occ) - cap)))
        if not options and len(players) <= CAPACITY[side]:
            return players, cat_totals(players, side), "no-excess"

        def search(strict: bool):
            for combo in product(*options):
                excluded = {p["puid"] for group in combo for p in group}
                kept = [p for p in players if p["puid"] not in excluded]
                totals = cat_totals(kept, side)
                ok = (_accept(totals, off, counts, rates) if strict else
                      all(abs(totals[c] - off.get(c, -1)) < 1e-9 for c in counts))
                if ok:
                    return kept, totals, "solved"
            k = len(players) - CAPACITY[side]
            if 0 < k <= 4:
                for excl in combinations(players, k):
                    excluded = {p["puid"] for p in excl}
                    kept = [p for p in players if p["puid"] not in excluded]
                    totals = cat_totals(kept, side)
                    ok = (_accept(totals, off, counts, rates) if strict else
                          all(abs(totals[c] - off.get(c, -1)) < 1e-9 for c in counts))
                    if ok:
                        return kept, totals, "solved-crossslot"
            return None

        hit = search(strict=True) or search(strict=False)
        if hit:
            return hit
        return players, cat_totals(players, side), "unsolved"

    groups: dict[tuple[str, int, str], list] = defaultdict(list)
    for row in bat:
        groups[(row["team"], row["period"], "bat")].append(row)
    for row in pit:
        groups[(row["team"], row["period"], "pit")].append(row)

    tol = {"OBP": 0.0011, "ERA": 0.011, "WHIP": 0.011}
    stats = defaultdict(lambda: {"match": 0, "close": 0, "miss": 0})
    outcomes = defaultdict(int)
    misses = []
    inferred = []
    components: dict = {}
    last_complete = max(p for (_, p, _) in groups) - 1
    for (team, period, side), players in groups.items():
        if period > last_complete:
            continue  # in-progress period: CBS live-updates, our logs lag
        off = official.get((team, period), {})
        kept, totals, outcome = solve(players, side, off)
        outcomes[outcome] += 1
        zc = lambda k: sum((p[k] or 0) for p in kept)
        if side == "bat":
            components[(team, period, "bat")] = {
                "ob": zc("h") + zc("bb") + zc("hbp"),
                "pa": zc("ab") + zc("bb") + zc("hbp") + zc("sf")}
        else:
            components[(team, period, "pit")] = {
                "er": zc("er"), "outs": zc("outs"),
                "wh": zc("ha") + zc("bbi")}
        if outcome == "solved":
            dropped = {p["name"] for p in players} - {p["name"] for p in kept}
            inferred.append((team, period, side, sorted(dropped)))
        for cat, ours in totals.items():
            if cat not in off:
                continue
            diff = abs(ours - off[cat])
            if diff < 1e-9:
                stats[cat]["match"] += 1
            elif diff <= tol.get(cat, 0.5):
                stats[cat]["close"] += 1
            else:
                stats[cat]["miss"] += 1
                misses.append((team, period, cat, off[cat], ours))
    return {"stats": dict(stats), "misses": misses, "outcomes": dict(outcomes),
            "inferred_nonparticipants": inferred, "components": components}


import functools


@functools.cache
def team_rate_inputs(form_periods: int = 4) -> dict:
    """Per-team rate components: YTD totals + recent-form weekly means, from
    the lock-solved attribution. Feeds component-based rate projection."""
    comps = recompute_v2()["components"]
    latest = max(p for (_, p, _) in comps)
    out: dict = {}
    for (team, period, side), c in comps.items():
        t = out.setdefault(team, {"ytd": defaultdict(float), "recent": defaultdict(float)})
        for k, v in c.items():
            t["ytd"][k] += v
            if period > latest - form_periods:
                t["recent"][k] += v / form_periods
    for t in out.values():
        t["ytd"] = dict(t["ytd"])
        t["recent"] = dict(t["recent"])
    return out
