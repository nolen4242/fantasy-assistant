"""Slot-aware lineup model: displacement and replacement for trade math.

The v1 trade evaluator scored a gained player as pure addition and a lost
player as pure loss. Both are wrong in a slotted roto lineup:

  * displacement — a gained bat only adds his margin over whoever he pushes
    to the bench (active slots: C 1B 2B 3B SS MI CI OF x4 U, plus 10 P);
  * replacement — a lost player's slot refills from the bench, and a freed
    roster spot refills from the FA pool (SportsLine weekly projections in
    the captured fa_pool psvs price exactly that body).

So team value must be computed the way the lineup actually works: assign the
best eligible players to slots, sum the starters, and diff whole TEAM totals
across a roster change. Both effects then emerge from the assignment instead
of being bolted-on corrections.

Approximations (documented, deliberate):
  * Greedy assignment in scarcity order (C SS 2B 3B 1B MI CI OF U), best
    scalar first — not optimal matching, but slot-eligibility overlap in
    this league is small enough that greedy is rarely off by a real margin.
  * Scalar for "who plays" = sum of counting-cat ROS units, each normalized
    by league-typical team weekly production (so 1 save-week ~ 1 HR-week).
    Rate quality doesn't influence who starts, only team rate components.
  * IL/minors players never start (their return date isn't modeled); they
    contribute zero — pessimistic for pending returns.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

from fantasy_assistant.graph.client import read_session

BAT_SLOTS = ["C", "SS", "2B", "3B", "1B", "MI", "CI", "OF", "OF", "OF", "OF", "U"]
N_PIT = 10
_MI = {"2B", "SS"}
_CI = {"1B", "3B"}
COUNTING = ["HR", "R", "RBI", "SB", "K", "S", "WQS"]
RATE_COMPS = ["ob", "paden", "er", "outs", "wh"]   # OBP / ERA / WHIP components
SEASON_WEEKS_ELAPSED = 21.0
RECENT_DAYS = 30
RAW_ROOT = Path(__file__).resolve().parents[3] / "data" / "raw"


def eligible(pos: set[str], slot: str) -> bool:
    if slot == "U":
        return bool(pos - {"P"})
    if slot == "MI":
        return bool(pos & _MI)
    if slot == "CI":
        return bool(pos & _CI)
    return slot in pos


def league_scales(remaining: int) -> dict[str, float]:
    """cat -> typical one-team ROS production (normalizer for the scalar)."""
    with read_session() as s:
        rows = s.run(
            """
            MATCH (st:StandingsSnapshot {scope:'period'})-[:FOR_PERIOD]->(p),
                  (st)-[:HAS_LINE]->(l)-[:FOR_TEAM]->(t:FantasyTeam),
                  (l)-[:IN_CATEGORY]->(c:Category)
            WHERE c.code IN $cats
            RETURN c.code AS cat, avg(l.value_reported) AS wk
            """, cats=COUNTING).data()
    return {r["cat"]: max((r["wk"] or 1.0) * remaining, 1e-6) for r in rows}


def roster_players(team: str, remaining: int) -> list[dict]:
    """Current roster with ROS counting vectors + rate components (pace blend)."""
    cutoff = (date.today() - timedelta(days=RECENT_DAYS)).isoformat()
    with read_session() as s:
        rows = s.run(
            """
            MATCH (st:RosterStint)-[:ON_TEAM]->(t:FantasyTeam {cbs_name:$team}),
                  (st)-[:OF_PLAYER]->(p:Player)
            WHERE st.to_date IS NULL
            WITH DISTINCT p, st.status AS status
            OPTIONAL MATCH (d:PlayerDayLine)-[:OF_PLAYER]->(p)
            WITH p, status, d,
                 (CASE WHEN d IS NOT NULL AND d.date >= date($cutoff)
                       THEN 1 ELSE 0 END) AS recent
            WITH p, status,
              [x IN collect({d:d, r:recent}) WHERE x.d IS NOT NULL | x] AS ds
            RETURN p.uid AS uid, p.name_full AS name,
                   coalesce(p.cbs_positions, '') AS pos, status,
              [w IN [0,1] |
                [reduce(a=0, x IN ds | a + CASE WHEN x.d.side='bat' AND (w=0 OR x.r=1) THEN coalesce(x.d.hr,0)  ELSE 0 END),
                 reduce(a=0, x IN ds | a + CASE WHEN x.d.side='bat' AND (w=0 OR x.r=1) THEN coalesce(x.d.r,0)   ELSE 0 END),
                 reduce(a=0, x IN ds | a + CASE WHEN x.d.side='bat' AND (w=0 OR x.r=1) THEN coalesce(x.d.rbi,0) ELSE 0 END),
                 reduce(a=0, x IN ds | a + CASE WHEN x.d.side='bat' AND (w=0 OR x.r=1) THEN coalesce(x.d.sb,0)  ELSE 0 END),
                 reduce(a=0, x IN ds | a + CASE WHEN x.d.side='pit' AND (w=0 OR x.r=1) THEN coalesce(x.d.k,0)   ELSE 0 END),
                 reduce(a=0, x IN ds | a + CASE WHEN x.d.side='pit' AND (w=0 OR x.r=1) THEN coalesce(x.d.sv,0)  ELSE 0 END),
                 reduce(a=0, x IN ds | a + CASE WHEN x.d.side='pit' AND (w=0 OR x.r=1) THEN coalesce(x.d.w,0)+coalesce(x.d.qs,0) ELSE 0 END),
                 reduce(a=0, x IN ds | a + CASE WHEN x.d.side='bat' AND (w=0 OR x.r=1) THEN coalesce(x.d.h,0)+coalesce(x.d.bb,0)+coalesce(x.d.hbp,0) ELSE 0 END),
                 reduce(a=0, x IN ds | a + CASE WHEN x.d.side='bat' AND (w=0 OR x.r=1) THEN coalesce(x.d.ab,0)+coalesce(x.d.bb,0)+coalesce(x.d.hbp,0)+coalesce(x.d.sf,0) ELSE 0 END),
                 reduce(a=0, x IN ds | a + CASE WHEN x.d.side='pit' AND (w=0 OR x.r=1) THEN coalesce(x.d.er,0)  ELSE 0 END),
                 reduce(a=0, x IN ds | a + CASE WHEN x.d.side='pit' AND (w=0 OR x.r=1) THEN coalesce(x.d.outs,0) ELSE 0 END),
                 reduce(a=0, x IN ds | a + CASE WHEN x.d.side='pit' AND (w=0 OR x.r=1) THEN coalesce(x.d.ha,0)+coalesce(x.d.bbi,0) ELSE 0 END)
                ]] AS sums
            """, team=team, cutoff=cutoff).data()
    keys = COUNTING + RATE_COMPS
    out = []
    for r in rows:
        season, recent = r["sums"]
        proj = {}
        for i, k in enumerate(keys):
            wk = 0.5 * (season[i] / SEASON_WEEKS_ELAPSED) \
               + 0.5 * (recent[i] / (RECENT_DAYS / 7.0))
            proj[k] = round(wk * remaining, 2)
        pos = set(p for p in r["pos"].split(",") if p)
        out.append({"uid": r["uid"], "name": r["name"], "pos": pos,
                    "status": r["status"], "proj": proj, "fa": False,
                    "season": dict(zip(keys, season))})
    return out


_NAME_POS = re.compile(r"^(.*?)\s+([A-Z0-9,]+)\s+•")


def _latest_pool_file(name: str) -> Path | None:
    for d in sorted(RAW_ROOT.iterdir(), reverse=True):
        f = d / name
        if f.exists() and f.stat().st_size > 10000:
            return f
    return None


def fa_pool(remaining: int, top: int = 15) -> list[dict]:
    """Best free agents as replacement bodies, priced from SportsLine weekly
    projections in the captured pool psvs (weekly x remaining periods)."""
    out = []
    bat = _latest_pool_file("fa_pool_batters.psv")
    if bat:
        for line in bat.read_text().splitlines():
            p = line.split("|")
            if len(p) != 20 or not p[0].isdigit():
                continue
            m = _NAME_POS.match(p[3])
            if not m:
                continue
            try:
                ab, r, hr, rbi, bb, sb, obp = (float(p[4]), float(p[5]),
                    float(p[10]), float(p[11]), float(p[12]), float(p[14]),
                    float(p[17]))
            except ValueError:
                continue
            paden = ab + bb
            proj = {c: 0.0 for c in COUNTING + RATE_COMPS}
            proj.update(HR=hr * remaining, R=r * remaining, RBI=rbi * remaining,
                        SB=sb * remaining, ob=obp * paden * remaining,
                        paden=paden * remaining)
            out.append({"uid": f"fa:{p[0]}", "name": m.group(1),
                        "pos": set(m.group(2).split(",")), "status": "fa",
                        "proj": proj, "fa": True, "season": {}})
    pit = _latest_pool_file("fa_pool_pitchers.psv")
    if pit:
        for line in pit.read_text().splitlines():
            p = line.split("|")
            if len(p) != 20 or not p[0].isdigit():
                continue
            m = _NAME_POS.match(p[3])
            if not m:
                continue
            try:
                inn, qs, w, sv, k, era, whip = (float(p[4]), float(p[7]),
                    float(p[9]), float(p[11]), float(p[14]), float(p[17]),
                    float(p[18]))
            except ValueError:
                continue
            proj = {c: 0.0 for c in COUNTING + RATE_COMPS}
            proj.update(K=k * remaining, S=sv * remaining,
                        WQS=(w + qs) * remaining, outs=inn * 3 * remaining,
                        er=era * inn / 9 * remaining, wh=whip * inn * remaining)
            out.append({"uid": f"fa:{p[0]}", "name": m.group(1),
                        "pos": {"P"}, "status": "fa", "proj": proj,
                        "fa": True, "season": {}})
    return out


def scalar(player: dict, scales: dict[str, float]) -> float:
    return sum(player["proj"].get(c, 0) / scales[c] for c in COUNTING)


def team_totals(players: list[dict], scales: dict[str, float],
                fa_quota: int = 0, fa_candidates: list[dict] | None = None,
                ) -> dict:
    """Assign the active lineup and sum starter ROS values + rate components.

    fa_quota: how many waiver bodies may join (freed roster spots after a
    trade). Candidates compete for slots on merit; the quota caps how many
    can win one.
    """
    pool = [p for p in players if p["status"] not in ("il", "minors")]
    if fa_quota > 0 and fa_candidates:
        pool = pool + fa_candidates
    used: set[str] = set()
    fa_used = 0

    def take(p):
        nonlocal fa_used
        used.add(p["uid"])
        if p["fa"]:
            fa_used += 1

    def candidates():
        return [p for p in pool if p["uid"] not in used
                and (not p["fa"] or fa_used < fa_quota)]

    starters = []
    for slot in BAT_SLOTS:
        elig = [p for p in candidates() if eligible(p["pos"], slot)]
        if elig:
            best = max(elig, key=lambda p: scalar(p, scales))
            take(best)
            starters.append((slot, best))
    pits = [p for p in candidates() if "P" in p["pos"]]
    pits.sort(key=lambda p: -scalar(p, scales))
    filled = 0
    for p in pits:
        if filled >= N_PIT:
            break
        if p["fa"] and fa_used >= fa_quota:
            continue   # quota may have been consumed by an earlier pick
        take(p)
        starters.append(("P", p))
        filled += 1

    totals = {c: 0.0 for c in COUNTING + RATE_COMPS}
    for _, p in starters:
        for c in totals:
            totals[c] += p["proj"].get(c, 0)
    return {"totals": totals, "starters": starters, "fa_used": fa_used}
