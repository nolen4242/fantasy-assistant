"""PlayerDayLine capture from the MLB Stats API (public, no auth).

Fetches per-game logs for every player in the rostered universe (anyone who
was ever drafted or added — the population whose production decides the
standings) and writes PlayerDayLine nodes with raw category components.
Innings are stored as integer outs per SCHEMA; rates are never stored.
"""
from __future__ import annotations

import asyncio

import httpx

from fantasy_assistant.graph.client import session

STATSAPI = "https://statsapi.mlb.com/api/v1"
CONCURRENCY = 8

HIT_FIELDS = {
    "plateAppearances": "pa", "atBats": "ab", "hits": "h", "doubles": "b2",
    "triples": "b3", "homeRuns": "hr", "runs": "r", "rbi": "rbi",
    "baseOnBalls": "bb", "intentionalWalks": "ibb", "hitByPitch": "hbp",
    "sacFlies": "sf", "stolenBases": "sb", "caughtStealing": "cs",
    "strikeOuts": "so",
}
PIT_FIELDS = {
    "gamesStarted": "gs", "wins": "w", "losses": "l", "saves": "sv",
    "blownSaves": "bs", "holds": "hld", "earnedRuns": "er", "hits": "ha",
    "baseOnBalls": "bbi", "strikeOuts": "k", "battersFaced": "batters_faced",
    "numberOfPitches": "pitches",
}


def ip_to_outs(ip: str | None) -> int | None:
    if not ip:
        return None
    whole, _, frac = str(ip).partition(".")
    return int(whole) * 3 + (int(frac) if frac else 0)


def quality_start(outs: int | None, er: int | None, gs: int | None) -> int:
    return 1 if (gs and outs is not None and er is not None and outs >= 18 and er <= 3) else 0


async def fetch_logs(client: httpx.AsyncClient, mlbam_id: int, group: str, season: int) -> list[dict]:
    r = await client.get(
        f"{STATSAPI}/people/{mlbam_id}/stats",
        params={"stats": "gameLog", "season": season, "group": group},
    )
    if r.status_code != 200:
        return []
    stats = r.json().get("stats") or []
    return stats[0].get("splits", []) if stats else []


def to_line(mlbam_id: int, side: str, split: dict) -> dict:
    st = split.get("stat", {})
    fields = HIT_FIELDS if side == "bat" else PIT_FIELDS
    row = {out: st.get(src) for src, out in fields.items()}
    game_pk = (split.get("game") or {}).get("gamePk")
    if side == "pit":
        row["outs"] = ip_to_outs(st.get("inningsPitched"))
        row["qs"] = quality_start(row["outs"], row.get("er"), row.get("gs"))
    row.update({
        "uid": f"dayline:{mlbam_id}:{split['date']}:{side}:{game_pk}",
        "mlbam_id": mlbam_id, "date": split["date"], "side": side,
        "game_pk": game_pk,
    })
    return row


async def collect(season: int = 2026) -> dict:
    with session() as s:
        players = s.run(
            """
            MATCH (p:Player) WHERE p.mlbam_id IS NOT NULL
              AND ((p)<-[:SELECTED]-(:DraftPick) OR (p)<-[:ADDS]-(:TransactionEvent))
            RETURN p.uid AS uid, p.mlbam_id AS mlbam, p.primary_position AS pos,
                   p.name_normalized AS norm
            """
        ).data()

    sem = asyncio.Semaphore(CONCURRENCY)
    lines: list[tuple[str, dict]] = []  # (player_uid, line)

    async def work(client: httpx.AsyncClient, p: dict):
        # CBS splits Ohtani into two entities sharing one mlbam_id; each
        # entity gets only its own side's lines. Others: side by position.
        if "(batter)" in p["norm"]:
            groups = [("hitting", "bat")]
        elif "(pitcher)" in p["norm"]:
            groups = [("pitching", "pit")]
        elif p["pos"] == "P":
            groups = [("pitching", "pit")]
        elif p["pos"] == "TWP":
            groups = [("hitting", "bat"), ("pitching", "pit")]
        else:
            groups = [("hitting", "bat")]
        for group, side in groups:
            async with sem:
                splits = await fetch_logs(client, p["mlbam"], group, season)
            for sp in splits:
                lines.append((p["uid"], to_line(p["mlbam"], side, sp)))

    async with httpx.AsyncClient(timeout=30) as client:
        await asyncio.gather(*(work(client, p) for p in players))

    with session() as s:
        for i in range(0, len(lines), 2000):
            chunk = lines[i:i + 2000]
            s.run(
                """
                UNWIND $batch AS row
                MATCH (p:Player {uid: row.puid})
                MERGE (d:PlayerDayLine {uid: row.line.uid})
                SET d += row.line, d.date = date(row.line.date)
                MERGE (d)-[:OF_PLAYER]->(p)
                """,
                batch=[{"puid": uid, "line": line} for uid, line in chunk],
            )
    return {"players": len(players), "day_lines": len(lines)}


if __name__ == "__main__":
    print(asyncio.run(collect()))
