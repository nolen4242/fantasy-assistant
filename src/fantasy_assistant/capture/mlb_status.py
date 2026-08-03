"""MLB transactions/status feed + probable starters (official statsapi).

- fetch_status_events: IL placements, activations, options/recalls, DFAs,
  trades, paternity/bereavement — structured, minutes-fresh, league-wide.
  Filtered to our player universe (anyone with an mlbam_id in the graph).
  Alert-worthy types are returned for the bus to notify on.
- fetch_probables: actual announced probable starters for a date window ->
  ProbableStart nodes (first-hand two-start detection).

Both idempotent; used by the daily routine (backfill) and the bus (poll).
"""
from __future__ import annotations

from datetime import date, timedelta

import httpx

from fantasy_assistant.graph.client import session

STATSAPI = "https://statsapi.mlb.com/api/v1"

ALERTABLE = {"Placed on Injured List", "Placed on 10-Day Injured List",
             "Placed on 15-Day Injured List", "Placed on 60-Day Injured List",
             "Placed on Paternity List", "Placed on Bereavement List",
             "Designated for Assignment", "Optioned", "Recalled", "Selected",
             "Claimed Off Waivers", "Trade", "Released", "Activated",
             "Status Change"}


def _universe(s) -> dict[int, str]:
    return {r["m"]: r["uid"] for r in s.run(
        "MATCH (p:Player) WHERE p.mlbam_id IS NOT NULL "
        "RETURN p.mlbam_id AS m, p.uid AS uid").data()}


def fetch_status_events(start: str, end: str) -> dict:
    from fantasy_assistant.capture.http import get_json
    with httpx.Client(timeout=30) as c:
        txns = get_json(f"{STATSAPI}/transactions",
                        params={"startDate": start, "endDate": end},
                        client=c).get("transactions", [])
    with session() as s:
        uni = _universe(s)
        rows, alerts = [], []
        for t in txns:
            pid = (t.get("person") or {}).get("id")
            if pid not in uni:
                continue
            desc = t.get("typeDesc", "")
            row = {
                "uid": f"mlbtxn:{t['id']}",
                "puid": uni[pid],
                "type_desc": desc,
                "date": t.get("date"),
                "effective": t.get("effectiveDate") or t.get("date"),
                "description": t.get("description", ""),
                "to_team": (t.get("toTeam") or {}).get("name"),
                "from_team": (t.get("fromTeam") or {}).get("name"),
            }
            rows.append(row)
            if any(k.lower() in desc.lower() for k in
                   ("injured", "paternity", "bereavement", "designated",
                    "optioned", "recalled", "trade", "claimed", "released")):
                alerts.append(row)
        for i in range(0, len(rows), 500):
            s.run(
                """
                UNWIND $batch AS row
                MATCH (p:Player {uid: row.puid})
                MERGE (e:MlbStatusEvent {uid: row.uid})
                ON CREATE SET e.first_seen = datetime()
                SET e.type_desc=row.type_desc, e.date=date(row.date),
                    e.effective=row.effective, e.description=row.description,
                    e.to_team=row.to_team, e.from_team=row.from_team,
                    e.source='mlb_statsapi'
                MERGE (e)-[:OF_PLAYER]->(p)
                """,
                batch=rows[i:i + 500],
            )
    return {"ingested": len(rows), "alertable": alerts}


def fetch_probables(days_ahead: int = 8) -> dict:
    start = date.today().isoformat()
    end = (date.today() + timedelta(days=days_ahead)).isoformat()
    from fantasy_assistant.capture.http import get_json
    with httpx.Client(timeout=30) as c:
        sched = get_json(f"{STATSAPI}/schedule",
                         params={"sportId": 1, "startDate": start, "endDate": end,
                                 "hydrate": "probablePitcher"}, client=c)
    rows = []
    for d in sched.get("dates", []):
        for g in d.get("games", []):
            for side in ("home", "away"):
                pp = (g["teams"][side].get("probablePitcher") or {})
                if pp.get("id"):
                    rows.append({
                        "uid": f"probable:{g['gamePk']}:{pp['id']}",
                        "mlbam": pp["id"], "date": d["date"],
                        "game_pk": g["gamePk"],
                    })
    with session() as s:
        uni = _universe(s)
        rows = [r | {"puid": uni[r["mlbam"]]} for r in rows if r["mlbam"] in uni]
        s.run(
            """
            UNWIND $batch AS row
            MATCH (p:Player {uid: row.puid})
            MERGE (e:ProbableStart {uid: row.uid})
            SET e.date=date(row.date), e.game_pk=row.game_pk,
                e.source='mlb_statsapi', e.confirmed=true
            MERGE (e)-[:OF_PLAYER]->(p)
            """,
            batch=rows,
        )
        # convenience: two-start pitchers next period
        two = s.run(
            """
            MATCH (e:ProbableStart)-[:OF_PLAYER]->(p:Player)
            WHERE e.date >= date($a) AND e.date <= date($b)
            WITH p, count(e) AS starts WHERE starts >= 2
            RETURN p.name_full AS n, starts ORDER BY n
            """,
            a=(date.today() + timedelta(days=1)).isoformat(),
            b=(date.today() + timedelta(days=7)).isoformat(),
        ).data()
    return {"probables": len(rows), "two_start_next7": [t["n"] for t in two]}


if __name__ == "__main__":
    import sys
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    start = (date.today() - timedelta(days=days)).isoformat()
    r = fetch_status_events(start, date.today().isoformat())
    print(f"status events: {r['ingested']} ingested, {len(r['alertable'])} alertable")
    for a in r["alertable"][:8]:
        print(f"  {a['date']} {a['type_desc']}: {a['description'][:90]}")
    print(fetch_probables())
