"""Batter contact quality from the same MLB game feeds (hitData per BBE).

Skill-based hitter heat — the fix for hotstreak's results_based weakness:
exit velocity stabilizes around ~40 BBE, far faster than outcomes. Official
barrel definition approximated: EV>=98 with the qualifying LA window
widening ~2-3 degrees per mph up to EV 116 (8-50).

BatterGameEV per (batter, game): bbe, ev_mean, hardhit (EV>=95), barrels.
Signals: contact_hot / contact_cold — last ~25 BBE hard-hit% vs season,
binomial z, flagged at |z| >= 1.5 (skill-based, results_based=false).
"""
from __future__ import annotations

import asyncio
import math
from datetime import date, timedelta

import httpx

from fantasy_assistant.graph.client import session
from fantasy_assistant.capture.velocity import FEED, _game_pks, CONCURRENCY

WINDOW_BBE = 25
Z_FLAG = 1.5


def is_barrel(ev: float, la: float) -> bool:
    if ev < 98:
        return False
    lo = max(8.0, 26.0 - (ev - 98) * 1.2)
    hi = min(50.0, 30.0 + (ev - 98) * 1.5)
    return lo <= la <= hi


async def _game_ev(client, pk, sem):
    async with sem:
        r = await client.get(FEED.format(pk=pk))
    if r.status_code != 200:
        return []
    data = r.json()
    game_date = data.get("gameData", {}).get("datetime", {}).get("officialDate")
    agg: dict[int, list] = {}
    for play in data.get("liveData", {}).get("plays", {}).get("allPlays", []):
        bid = (play.get("matchup", {}).get("batter") or {}).get("id")
        if not bid:
            continue
        for ev_ in play.get("playEvents", []):
            hd = ev_.get("hitData") or {}
            ls, la = hd.get("launchSpeed"), hd.get("launchAngle")
            if ls is None:
                continue
            agg.setdefault(bid, []).append((ls, la if la is not None else 0.0))
    return [{"uid": f"bev:{bid}:{pk}", "mlbam": bid, "game_pk": pk,
             "date": game_date, "bbe": len(xs),
             "ev_mean": round(sum(x[0] for x in xs) / len(xs), 1),
             "hardhit": sum(1 for x in xs if x[0] >= 95),
             "barrels": sum(1 for x in xs if is_barrel(*x))}
            for bid, xs in agg.items()]


async def collect(days_back: int = 3) -> dict:
    start = (date.today() - timedelta(days=days_back)).isoformat()
    end = date.today().isoformat()
    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(timeout=60) as client:
        pks = await _game_pks(client, start, end)
        res = await asyncio.gather(*(_game_ev(client, pk, sem) for pk in pks))
    rows = [r for g in res for r in g]
    with session() as s:
        uni = {r["m"]: r["uid"] for r in s.run(
            "MATCH (p:Player) WHERE p.mlbam_id IS NOT NULL "
            "RETURN p.mlbam_id AS m, p.uid AS uid").data()}
        rows = [r | {"puid": uni[r["mlbam"]]} for r in rows if r["mlbam"] in uni]
        s.run(
            """
            UNWIND $b AS row
            MATCH (p:Player {uid: row.puid})
            MERGE (v:BatterGameEV {uid: row.uid})
            SET v.date=date(row.date), v.game_pk=row.game_pk, v.bbe=row.bbe,
                v.ev_mean=row.ev_mean, v.hardhit=row.hardhit,
                v.barrels=row.barrels, v.source='mlb_feed'
            MERGE (v)-[:OF_PLAYER]->(p)
            """, b=rows)
    return {"games": len(pks), "batter_games": len(rows)}


def contact_signals() -> list[dict]:
    out = []
    with session() as s:
        rows = s.run(
            """
            MATCH (p:Player)<-[:OF_PLAYER]-(st:RosterStint) WHERE st.to_date IS NULL
            WITH DISTINCT p
            MATCH (v:BatterGameEV)-[:OF_PLAYER]->(p)
            WITH p, v ORDER BY v.date DESC
            WITH p, collect({bbe: v.bbe, hh: v.hardhit, b: v.barrels}) AS g
            WHERE size(g) >= 15
            RETURN p.uid AS uid, p.name_full AS name, g
            """
        ).data()
        for r in rows:
            wb = wh = 0
            for x in r["g"]:
                if wb >= WINDOW_BBE:
                    break
                wb += x["bbe"]
                wh += x["hh"]
            sb_ = sum(x["bbe"] for x in r["g"])
            sh = sum(x["hh"] for x in r["g"])
            if wb < 15 or sb_ < 80:
                continue
            p0 = sh / sb_
            z = (wh / wb - p0) / math.sqrt(max(p0 * (1 - p0), 0.05) / wb)
            if abs(z) >= Z_FLAG:
                kind = "contact_hot" if z > 0 else "contact_cold"
                out.append({"name": r["name"], "z": round(z, 2),
                            "win": round(wh / wb, 3), "season": round(p0, 3)})
                s.run(
                    """
                    MATCH (p:Player {uid:$uid})
                    MERGE (sig:Signal {uid:$suid})
                    SET sig.kind=$kind, sig.strength=$st, sig.as_of=date(),
                        sig.rationale=$why, sig.agent='contact',
                        sig.model_version='ev-v1', sig.results_based=false
                    MERGE (sig)-[:ABOUT]->(p)
                    """,
                    uid=r["uid"], suid=f"signal:ev:{kind}:{r['uid']}:{date.today()}",
                    kind=kind, st=min(1.0, abs(z) / 3.0),
                    why=f"hard-hit% last {wb} BBE {wh/wb:.0%} vs season {p0:.0%} (z={z:+.1f})")
    return out


if __name__ == "__main__":
    import sys
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    print(asyncio.run(collect(days)))
    sigs = contact_signals()
    print(f"{len(sigs)} contact signals:")
    for x in sorted(sigs, key=lambda t: -abs(t["z"]))[:10]:
        print(f"  {x['name']:<24} z={x['z']:+.2f} win {x['win']:.0%} vs {x['season']:.0%}")
