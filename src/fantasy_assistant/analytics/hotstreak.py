"""Hot/cold streak watcher.

Research grounding (Albert 2008 'streaky' studies; Tango et al., 'The Book';
sample-stabilization literature): short-window RESULTS are mostly noise —
outcome-based streaks have near-zero predictive value once shrunk. What does
carry signal short-term is skill-proxy inputs that stabilize fastest:
  - pitchers: whiff%/CSW% (~50 swings / ~150 pitches) and velocity — we
    already collect these per start. A 'hot arm' is a SKILL claim.
  - hitters: contact quality (EV/barrel ~40 BBE) stabilizes fastest, but we
    only hold per-day RESULT lines; so the hitter heat score is
    results-based wOBA-lite over a 10-game window, shrunk hard toward the
    player's own season baseline (empirical-Bayes weight n/(n+K), K=60 PA)
    and flagged only at |z| >= 1.5. It is labeled results_based=true so
    downstream consumers can discount it appropriately vs skill signals.

Heat score = shrunken window rate minus season rate, in season-sd units.
Signals: hot_bat/cold_bat, hot_arm/cold_arm.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

from fantasy_assistant.graph.client import session

WINDOW_G = 10
K_PA = 60.0
Z_FLAG = 0.9   # post-shrinkage z; even elite 10g tears rarely clear 1.2 — tiers, not certainty
WOBA_W = {"bb": 0.69, "hbp": 0.72, "h1": 0.88, "b2": 1.26, "b3": 1.60, "hr": 2.07}


def _woba_lite(r) -> tuple[float, float]:
    """(numerator, denominator≈PA) from day-line sums; singles = h - xbh."""
    h1 = (r["h"] or 0) - (r["b2"] or 0) - (r["b3"] or 0) - (r["hr"] or 0)
    num = (WOBA_W["bb"] * (r["bb"] or 0) + WOBA_W["hbp"] * (r["hbp"] or 0)
           + WOBA_W["h1"] * h1 + WOBA_W["b2"] * (r["b2"] or 0)
           + WOBA_W["b3"] * (r["b3"] or 0) + WOBA_W["hr"] * (r["hr"] or 0))
    den = (r["ab"] or 0) + (r["bb"] or 0) + (r["hbp"] or 0) + (r["sf"] or 0)
    return num, den


def run() -> dict:
    cutoff = (date.today() - timedelta(days=16)).isoformat()
    out = {"hot_bat": [], "cold_bat": [], "hot_arm": [], "cold_arm": []}
    with session() as s:
        bats = s.run(
            """
            MATCH (p:Player)
            WHERE EXISTS {MATCH (st:RosterStint)-[:OF_PLAYER]->(p) WHERE st.to_date IS NULL}
               OR EXISTS {MATCH (e:PoolEntry)-[:OF_PLAYER]->(p) WHERE e.sportsline_rank <= 400}
            WITH DISTINCT p
            MATCH (d:PlayerDayLine {side:'bat'})-[:OF_PLAYER]->(p)
            WITH p, d ORDER BY d.date DESC
            WITH p, collect(d)[..$w] AS win, collect(d) AS hist
            WHERE size(hist) >= 40 AND size(win) = $w
            RETURN p.uid AS uid, p.name_full AS name,
                   [x IN win | {h:x.h,b2:x.b2,b3:x.b3,hr:x.hr,bb:x.bb,hbp:x.hbp,
                                ab:x.ab,sf:x.sf}] AS w,
                   [x IN hist | {h:x.h,b2:x.b2,b3:x.b3,hr:x.hr,bb:x.bb,hbp:x.hbp,
                                ab:x.ab,sf:x.sf}] AS a
            """, w=WINDOW_G,
        ).data()
        for r in bats:
            wn = wd = an = ad = 0.0
            for x in r["w"]:
                n, d_ = _woba_lite(x)
                wn += n
                wd += d_
            for x in r["a"]:
                n, d_ = _woba_lite(x)
                an += n
                ad += d_
            if wd < 20 or ad < 100:
                continue
            season = an / ad
            window = wn / wd
            shrunk = (wd * window + K_PA * season) / (wd + K_PA)
            sd = math.sqrt(max(season * (1 - min(season, 0.9)), 0.01) / wd)
            z = (shrunk - season) / sd if sd else 0.0
            if abs(z) >= Z_FLAG:
                kind = "hot_bat" if z > 0 else "cold_bat"
                out[kind].append((r["name"], round(z, 2), round(window, 3), round(season, 3)))
                s.run(
                    """
                    MATCH (p:Player {uid:$uid})
                    MERGE (sig:Signal {uid:$suid})
                    SET sig.kind=$kind, sig.strength=$st, sig.as_of=date(),
                        sig.rationale=$why, sig.agent='hotstreak',
                        sig.model_version='heat-v1', sig.results_based=true,
                        sig.fa = NOT EXISTS {MATCH (st:RosterStint)-[:OF_PLAYER]->(p)
                                             WHERE st.to_date IS NULL}
                    MERGE (sig)-[:ABOUT]->(p)
                    """,
                    uid=r["uid"], suid=f"signal:heat:{r['uid']}:{date.today()}",
                    kind=kind, st=min(1.0, abs(z) / 3.0),
                    why=f"10g wOBA~{window:.3f} vs season {season:.3f} (z={z:+.1f}, shrunk)")
        arms = s.run(
            """
            MATCH (p:Player)
            WHERE EXISTS {MATCH (st:RosterStint)-[:OF_PLAYER]->(p) WHERE st.to_date IS NULL}
               OR EXISTS {MATCH (e:PoolEntry)-[:OF_PLAYER]->(p) WHERE e.sportsline_rank <= 400}
            WITH DISTINCT p
            MATCH (v:PitcherGameVelo)-[:OF_PLAYER]->(p)
            WHERE v.csw_pct IS NOT NULL AND v.n_pitches >= 25
            WITH p, v ORDER BY v.date DESC
            WITH p, collect({csw: v.csw_pct, ff: v.ff_avg})[..6] AS g
            WHERE size(g) >= 5
            RETURN p.uid AS uid, p.name_full AS name, g
            """
        ).data()
        for r in arms:
            rec, base = r["g"][:2], r["g"][2:]
            csw_d = sum(x["csw"] for x in rec) / 2 - sum(x["csw"] for x in base) / len(base)
            ffs_r = [x["ff"] for x in rec if x["ff"]]
            ffs_b = [x["ff"] for x in base if x["ff"]]
            velo_d = (sum(ffs_r) / len(ffs_r) - sum(ffs_b) / len(ffs_b)) if ffs_r and ffs_b else 0.0
            heat = csw_d / 0.04 + velo_d / 1.2   # ~1.0 each at meaningful moves
            if abs(heat) >= 1.0:
                kind = "hot_arm" if heat > 0 else "cold_arm"
                out[kind].append((r["name"], round(heat, 2), round(csw_d, 3), round(velo_d, 2)))
                s.run(
                    """
                    MATCH (p:Player {uid:$uid})
                    MERGE (sig:Signal {uid:$suid})
                    SET sig.kind=$kind, sig.strength=$st, sig.as_of=date(),
                        sig.rationale=$why, sig.agent='hotstreak',
                        sig.model_version='heat-v1', sig.results_based=false,
                        sig.fa = NOT EXISTS {MATCH (st:RosterStint)-[:OF_PLAYER]->(p)
                                             WHERE st.to_date IS NULL}
                    MERGE (sig)-[:ABOUT]->(p)
                    """,
                    uid=r["uid"], suid=f"signal:heat:{r['uid']}:{date.today()}",
                    kind=kind, st=min(1.0, abs(heat) / 3.0),
                    why=f"CSW {csw_d:+.1%}, velo {velo_d:+.1f} mph (skill-based)")
    return out


if __name__ == "__main__":
    r = run()
    for k, v in r.items():
        print(f"{k}: {len(v)}")
        for x in sorted(v, key=lambda t: -abs(t[1]))[:5]:
            print("   ", x)
