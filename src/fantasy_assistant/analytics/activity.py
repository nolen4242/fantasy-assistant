"""Player activity gate — is this player actually playing right now?

Season aggregates and 30-day windows both read as healthy for a player who
stopped appearing weeks ago. Shohei Ohtani carries 85 IP, 12 QS and a 1.79 ERA
on the season and last pitched 2026-07-03; a team-page read said "best arm on
the roster", the game log said "hasn't thrown in 40 days". Nothing in the
advisory path consulted the game log, so recommendations were generated for
inactive players and for role-mismatched ones (a setup man read as a closer,
a part-time pinch-runner read as a starter).

Everything here is derived from PlayerDayLine, which we already capture. The
gate is deliberately blunt: no appearance in APPEARANCE_WINDOW days means the
player cannot be recommended, regardless of how good the season line looks.

Roles are derived from usage, never from position labels:
  - a pitcher's saves-vs-holds split identifies the closer, not the depth chart
  - a batter's PA/game identifies a regular, not the fact that he has an OF tag
"""
from __future__ import annotations

from datetime import date, timedelta

from fantasy_assistant.graph.client import session

APPEARANCE_WINDOW = 14   # no appearance in this many days -> hard block
POOL_RANK_MAX = 600      # gamelog capture covers pool players ranked better
                         # than this; past it, activity reads as unverified
STALE_DAYS = 7           # appeared, but long enough ago to flag
PARTTIME_PA_PER_G = 2.5  # batter PA/game below this = platoon/bench usage
REGULAR_PA_PER_G = 3.4   # at or above this = everyday player
CLOSER_SV_SHARE = 0.6    # sv / (sv + hld) over the window = closer, not setup
STARTER_GS_SHARE = 0.5   # gs / apps over the window = starter


def load(as_of: date | None = None) -> dict:
    """-> {(player_uid, side): row}. Rows carry both the raw counts and the
    derived role so callers never have to re-derive (and re-derive wrongly)."""
    as_of = as_of or date.today()
    w14 = (as_of - timedelta(days=APPEARANCE_WINDOW)).isoformat()
    w30 = (as_of - timedelta(days=30)).isoformat()
    with session() as s:
        rows = s.run(
            """
            MATCH (d:PlayerDayLine)-[:OF_PLAYER]->(p:Player)
            WHERE d.date <= date($as_of)
            WITH p, d.side AS side,
                 max(d.date) AS last_game,
                 sum(CASE WHEN d.date > date($w14) THEN 1 ELSE 0 END) AS apps14,
                 sum(CASE WHEN d.date > date($w30) THEN 1 ELSE 0 END) AS apps30,
                 sum(CASE WHEN d.date > date($w30) THEN coalesce(d.gs, 0) ELSE 0 END) AS gs30,
                 sum(CASE WHEN d.date > date($w30) THEN coalesce(d.sv, 0) ELSE 0 END) AS sv30,
                 sum(CASE WHEN d.date > date($w30) THEN coalesce(d.hld, 0) ELSE 0 END) AS hld30,
                 sum(CASE WHEN d.date > date($w14) THEN coalesce(d.pa, 0) ELSE 0 END) AS pa14,
                 sum(CASE WHEN d.date > date($w30) THEN coalesce(d.outs, 0) ELSE 0 END) AS outs30
            RETURN p.uid AS uid, p.name_full AS name, p.cbs_id AS cbs_id, side,
                   toString(last_game) AS last_game, apps14, apps30, gs30,
                   sv30, hld30, pa14, outs30
            """,
            as_of=as_of.isoformat(), w14=w14, w30=w30,
        ).data()
    out = {}
    for r in rows:
        last = date.fromisoformat(r["last_game"])
        r["days_since"] = (as_of - last).days
        r["role"] = _role(r)
        r["ok"], r["reason"] = _verdict(r)
        out[(r["uid"], r["side"])] = r
    return out


def _role(r: dict) -> str:
    if r["apps14"] == 0:
        return "inactive"
    if r["side"] == "pit":
        if r["apps30"] and r["gs30"] / r["apps30"] >= STARTER_GS_SHARE:
            return "starter"
        decisions = r["sv30"] + r["hld30"]
        if decisions == 0:
            return "reliever"
        # the saves-vs-holds split is the only reliable closer signal we have
        return "closer" if r["sv30"] / decisions >= CLOSER_SV_SHARE else "setup"
    games = r["apps14"] or 1
    per_g = r["pa14"] / games
    if per_g >= REGULAR_PA_PER_G:
        return "regular"
    return "part_time" if per_g >= PARTTIME_PA_PER_G else "bench"


def _verdict(r: dict) -> tuple[bool, str]:
    """Hard block only on genuine absence. Role weakness is a caller's problem
    to weigh, not a reason to hide the player."""
    if r["apps14"] == 0:
        return False, (f"no appearance in {APPEARANCE_WINDOW}d "
                       f"(last {r['last_game']}, {r['days_since']}d ago)")
    if r["days_since"] > STALE_DAYS:
        return True, f"stale: last played {r['days_since']}d ago"
    return True, ""


def check(act: dict, uid: str, side: str) -> tuple[str, str]:
    """-> (status, reason), status in {'ok', 'blocked', 'unverified'}.

    'blocked' means we HAVE game logs and they show no recent appearance —
    proven inactive, the Ohtani case. 'unverified' means the player sits
    outside the gamelog universe (capture.mlb_gamelogs fetches the rostered
    universe plus pool players above POOL_RANK_MAX): that is absence of
    evidence, not evidence of absence, so it is surfaced rather than silently
    allowed or silently cut. Failing closed on unverified would have blocked
    three quarters of the real FA candidates.
    """
    row = act.get((uid, side))
    if row is None:
        return "unverified", "outside gamelog universe — activity unchecked"
    return ("ok" if row["ok"] else "blocked"), row["reason"]


def gate(act: dict, entries: list[dict], uid_key: str = "uid",
         side_key: str = "side") -> dict[str, list[dict]]:
    """Split candidates into {'ok', 'unverified', 'blocked'}. Nothing is
    dropped silently — every bucket is returned for the caller to report."""
    out: dict[str, list[dict]] = {"ok": [], "unverified": [], "blocked": []}
    for e in entries:
        uid = e.get(uid_key)
        side = e.get(side_key)
        status, why = check(act, uid, side) if uid else ("blocked", "unresolved player")
        row = act.get((uid, side), {})
        out[status].append(dict(e, role=row.get("role"), activity_note=why,
                                days_since=row.get("days_since"),
                                activity_status=status))
    return out


def our_roster_activity(as_of: date | None = None) -> list[dict]:
    act = load(as_of)
    with session() as s:
        ours = s.run(
            """
            MATCH (r:RosterStint)-[:OF_PLAYER]->(p:Player),
                  (r)-[:ON_TEAM]->(t:FantasyTeam {is_us:true})
            WHERE r.to_date IS NULL
            RETURN p.uid AS uid, p.name_full AS name, r.status AS status
            """
        ).data()
    out = []
    for o in ours:
        rows = [act[k] for k in act if k[0] == o["uid"]]
        best = max(rows, key=lambda r: r["apps14"], default=None)
        out.append({**o,
                    "side": best["side"] if best else None,
                    "last_game": best["last_game"] if best else None,
                    "days_since": best["days_since"] if best else None,
                    "apps14": best["apps14"] if best else 0,
                    "role": best["role"] if best else "no day lines"})
    return sorted(out, key=lambda r: (-(r["days_since"] or 9999)))


def report(as_of: date | None = None) -> str:
    rows = our_roster_activity(as_of)
    lines = [f"ROSTER ACTIVITY — as of {as_of or date.today()} "
             f"(block = no appearance in {APPEARANCE_WINDOW}d)", "",
             f"  {'player':<26}{'status':<9}{'role':<11}{'last game':<12}{'d ago':>6}{'app14':>7}"]
    for r in rows:
        flag = "  <-- BLOCKED" if r["apps14"] == 0 else (
            "  <-- stale" if (r["days_since"] or 0) > STALE_DAYS else "")
        lines.append(f"  {r['name']:<26}{str(r['status']):<9}{str(r['role']):<11}"
                     f"{str(r['last_game'] or '-'):<12}{str(r['days_since'] or '-'):>6}"
                     f"{r['apps14']:>7}{flag}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(report())
