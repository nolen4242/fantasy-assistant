"""Per-start pitch velocity from MLB game feeds (official, free, pitch-level).

For each MLB game in a date window, pull the live-feed JSON and aggregate
fastball velocity per pitcher-game -> PitcherGameVelo nodes. Then derive
velocity-trend Signals: mean of last 2 appearances vs prior 5, flagged when
the delta exceeds THRESH mph. This is the leading-indicator channel — velo
moves before results do.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import date, timedelta

import httpx

from fantasy_assistant.graph.client import session

STATSAPI = "https://statsapi.mlb.com/api/v1"
FEED = "https://statsapi.mlb.com/api/v1.1/game/{pk}/feed/live"
FASTBALLS = {"FF", "SI", "FT"}
THRESH = 0.8
CONCURRENCY = 6


async def _game_pks(client: httpx.AsyncClient, start: str, end: str) -> list[int]:
    r = await client.get(f"{STATSAPI}/schedule",
                         params={"sportId": 1, "startDate": start, "endDate": end})
    return [g["gamePk"] for d in r.json().get("dates", [])
            for g in d.get("games", [])
            if g.get("status", {}).get("codedGameState") == "F"]


async def _game_velo(client: httpx.AsyncClient, pk: int, sem) -> list[dict]:
    async with sem:
        r = await client.get(FEED.format(pk=pk))
    if r.status_code != 200:
        return []
    data = r.json()
    game_date = data.get("gameData", {}).get("datetime", {}).get("officialDate")
    agg: dict[int, dict] = defaultdict(lambda: {"ff": [], "all": []})
    for play in data.get("liveData", {}).get("plays", {}).get("allPlays", []):
        pid = (play.get("matchup", {}).get("pitcher") or {}).get("id")
        if not pid:
            continue
        for ev in play.get("playEvents", []):
            if not ev.get("isPitch"):
                continue
            speed = (ev.get("pitchData") or {}).get("startSpeed")
            if speed is None:
                continue
            code = ((ev.get("details") or {}).get("type") or {}).get("code")
            agg[pid]["all"].append(speed)
            if code in FASTBALLS:
                agg[pid]["ff"].append(speed)
    return [{"uid": f"velo:{pid}:{pk}", "mlbam": pid, "game_pk": pk,
             "date": game_date,
             "ff_avg": round(sum(a["ff"]) / len(a["ff"]), 2) if a["ff"] else None,
             "n_ff": len(a["ff"]), "n_pitches": len(a["all"])}
            for pid, a in agg.items()]


async def collect(days_back: int = 3) -> dict:
    start = (date.today() - timedelta(days=days_back)).isoformat()
    end = date.today().isoformat()
    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(timeout=60) as client:
        pks = await _game_pks(client, start, end)
        results = await asyncio.gather(*(_game_velo(client, pk, sem) for pk in pks))
    rows = [r for game in results for r in game]
    with session() as s:
        uni = {r["m"]: r["uid"] for r in s.run(
            "MATCH (p:Player) WHERE p.mlbam_id IS NOT NULL "
            "RETURN p.mlbam_id AS m, p.uid AS uid").data()}
        rows = [r | {"puid": uni[r["mlbam"]]} for r in rows if r["mlbam"] in uni]
        s.run(
            """
            UNWIND $batch AS row
            MATCH (p:Player {uid: row.puid})
            MERGE (v:PitcherGameVelo {uid: row.uid})
            SET v.date=date(row.date), v.game_pk=row.game_pk,
                v.ff_avg=row.ff_avg, v.n_ff=row.n_ff,
                v.n_pitches=row.n_pitches, v.source='mlb_feed'
            MERGE (v)-[:OF_PLAYER]->(p)
            """,
            batch=rows,
        )
    return {"games": len(pks), "pitcher_games": len(rows)}


def velocity_signals() -> list[dict]:
    """Recent-2 vs prior-5 fastball velo per pitcher; write Signals on breach."""
    from datetime import datetime
    with session() as s:
        rows = s.run(
            """
            MATCH (v:PitcherGameVelo)-[:OF_PLAYER]->(p:Player)
            WHERE v.ff_avg IS NOT NULL AND v.n_ff >= 8
            WITH p, v ORDER BY v.date DESC
            WITH p, collect({d: v.date, ff: v.ff_avg})[..7] AS games
            WHERE size(games) >= 4
            RETURN p.uid AS uid, p.name_full AS name, games
            """
        ).data()
        out = []
        ts = datetime.now().isoformat(timespec="seconds")
        for r in rows:
            recent = [g["ff"] for g in r["games"][:2]]
            prior = [g["ff"] for g in r["games"][2:]]
            delta = round(sum(recent) / len(recent) - sum(prior) / len(prior), 2)
            if abs(delta) >= THRESH:
                kind = "velocity_up" if delta > 0 else "velocity_down"
                out.append({"name": r["name"], "delta": delta, "kind": kind})
                s.run(
                    """
                    MATCH (p:Player {uid:$uid})
                    MERGE (sig:Signal {uid:$suid})
                    SET sig.kind=$kind, sig.strength=$strength, sig.as_of=date(),
                        sig.rationale=$why, sig.agent='velocity', sig.model_version='velo-v1'
                    MERGE (sig)-[:ABOUT]->(p)
                    """,
                    uid=r["uid"], suid=f"signal:velo:{r['uid']}:{r['games'][0]['d']}",
                    kind=kind, strength=min(1.0, abs(delta) / 2.5),
                    why=f"FF velo last2 vs prior5: {delta:+.2f} mph",
                )
        return sorted(out, key=lambda x: -abs(x["delta"]))


if __name__ == "__main__":
    import sys
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    print(asyncio.run(collect(days)))
    sigs = velocity_signals()
    print(f"{len(sigs)} velocity signals:")
    for x in sigs[:10]:
        print(f"  {x['kind']:<14} {x['name']:<24} {x['delta']:+.2f} mph")
