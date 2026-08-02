"""PositionGameCount from MLB fielding game logs -> eligibility windows.

League rule: a player gains a position at 20 games played there (this year
or last). Counting games by position for the rostered universe surfaces
windows BEFORE they open (15-19 games = 'opening soon') — an edge rivals
who only look at CBS's current eligibility list will miss.
"""
from __future__ import annotations

import asyncio

import httpx

from fantasy_assistant.graph.client import session

STATSAPI = "https://statsapi.mlb.com/api/v1"
SEM = 8


async def collect(season: int = 2026) -> dict:
    with session() as s:
        bats = s.run(
            """
            MATCH (p:Player) WHERE p.mlbam_id IS NOT NULL
              AND p.primary_position <> 'P'
              AND ((p)<-[:SELECTED]-(:DraftPick) OR (p)<-[:ADDS]-(:TransactionEvent))
            RETURN p.uid AS uid, p.mlbam_id AS m
            """
        ).data()
    sem = asyncio.Semaphore(SEM)
    rows = []

    async def one(client, p):
        async with sem:
            r = await client.get(f"{STATSAPI}/people/{p['m']}/stats",
                                 params={"stats": "gameLog", "group": "fielding",
                                         "season": season})
        if r.status_code != 200:
            return
        stats = r.json().get("stats") or []
        counts: dict[str, int] = {}
        for sp in (stats[0].get("splits", []) if stats else []):
            pos = (sp.get("position") or {}).get("abbreviation")
            if pos:
                counts[pos] = counts.get(pos, 0) + 1
        # map MLB fielding positions onto CBS slot positions (LF/CF/RF -> OF;
        # DH games grant nothing beyond U, which everyone has)
        mapped: dict[str, int] = {}
        for pos, n in counts.items():
            cbs = "OF" if pos in ("LF", "CF", "RF", "OF") else pos
            if cbs == "DH":
                continue
            mapped[cbs] = mapped.get(cbs, 0) + n
        for pos, n in mapped.items():
            rows.append({"uid": f"pgc:{p['m']}:{pos}:{season}", "puid": p["uid"],
                         "pos": pos, "games": n, "season": season})

    async with httpx.AsyncClient(timeout=30) as client:
        await asyncio.gather(*(one(client, p) for p in bats))
    with session() as s:
        for i in range(0, len(rows), 1000):
            s.run(
                """
                UNWIND $batch AS row
                MATCH (p:Player {uid: row.puid})
                MERGE (g:PositionGameCount {uid: row.uid})
                SET g.position=row.pos, g.games=row.games, g.season=row.season,
                    g.as_of=date()
                MERGE (g)-[:OF_PLAYER]->(p)
                """,
                batch=rows[i:i + 1000],
            )
        windows = s.run(
            """
            MATCH (g:PositionGameCount {season:$season})-[:OF_PLAYER]->(p:Player)
            WHERE g.games >= 15 AND g.games < 20
              AND NOT (p.cbs_positions CONTAINS g.position)
            RETURN p.name_full AS n, g.position AS pos, g.games AS games
            ORDER BY g.games DESC
            """, season=season,
        ).data()
    return {"counts": len(rows), "windows_opening": windows}


if __name__ == "__main__":
    r = asyncio.run(collect())
    print(f"{r['counts']} position-game counts")
    print("eligibility windows opening (15-19 games at a NEW position):")
    for w in r["windows_opening"][:12]:
        print(f"  {w['n']:<26} {w['pos']} ({w['games']} games)")
