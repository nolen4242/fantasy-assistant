"""Savant instrument panel: bat speed, sprint speed (YoY), plate discipline.

Season-level skill descriptors -> Player props; sprint YoY decline >= 1.0
ft/s among league-relevant players -> speed_decline Signal (SB forecast).
"""
from __future__ import annotations

import csv
import io
from datetime import date

import httpx

from fantasy_assistant.graph.client import session

B = "https://baseballsavant.mlb.com/leaderboard"


def _rows(url: str) -> list[dict]:
    t = httpx.get(url, timeout=60).text
    return list(csv.DictReader(io.StringIO(t.lstrip("﻿"))))


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def collect() -> dict:
    with session() as s:
        uni = {r["m"]: r["uid"] for r in s.run(
            "MATCH (p:Player) WHERE p.mlbam_id IS NOT NULL "
            "RETURN p.mlbam_id AS m, p.uid AS uid").data()}
        out = {}
        bt = _rows(f"{B}/bat-tracking?csv=true&year=2026&min=50")
        batch = [{"puid": uni[int(r["id"])],
                  "props": {"bat_speed": _f(r.get("avg_bat_speed")),
                            "squared_up": _f(r.get("squared_up_per_swing"))}}
                 for r in bt if r.get("id") and int(r["id"]) in uni]
        s.run("UNWIND $b AS row MATCH (p:Player {uid:row.puid}) SET p += row.props", b=batch)
        out["bat_tracking"] = len(batch)
        ch = _rows(f"{B}/custom?year=2026&type=batter&filter=&min=100"
                   "&selections=oz_swing_percent%2Cwhiff_percent&chart=false&csv=true")
        batch = [{"puid": uni[int(r["player_id"])],
                  "props": {"chase_pct": _f(r.get("oz_swing_percent")),
                            "whiff_pct_bat": _f(r.get("whiff_percent"))}}
                 for r in ch if r.get("player_id") and int(r["player_id"]) in uni]
        s.run("UNWIND $b AS row MATCH (p:Player {uid:row.puid}) SET p += row.props", b=batch)
        out["chase"] = len(batch)
        cur = {int(r["player_id"]): _f(r.get("sprint_speed"))
               for r in _rows(f"{B}/sprint_speed?csv=true&year=2026&min=10")
               if r.get("player_id")}
        prev = {int(r["player_id"]): _f(r.get("sprint_speed"))
                for r in _rows(f"{B}/sprint_speed?csv=true&year=2025&min=10")
                if r.get("player_id")}
        batch, sigs = [], []
        for m, sp in cur.items():
            if m not in uni or sp is None:
                continue
            batch.append({"puid": uni[m], "props": {"sprint_speed": sp}})
            p0 = prev.get(m)
            if p0 and sp - p0 <= -1.0:
                sigs.append({"puid": uni[m], "why": f"sprint {p0} -> {sp} ft/s YoY",
                             "st": min(1.0, (p0 - sp) / 2.5)})
        s.run("UNWIND $b AS row MATCH (p:Player {uid:row.puid}) SET p += row.props", b=batch)
        s.run(
            """
            UNWIND $b AS row
            MATCH (p:Player {uid: row.puid})
            WHERE EXISTS {MATCH (st:RosterStint)-[:OF_PLAYER]->(p) WHERE st.to_date IS NULL}
               OR EXISTS {MATCH (e:PoolEntry)-[:OF_PLAYER]->(p) WHERE e.sportsline_rank <= 250}
            MERGE (sig:Signal {uid: 'signal:speed:' + row.puid + ':' + $d})
            SET sig.kind='speed_decline', sig.strength=row.st, sig.as_of=date($d),
                sig.rationale=row.why, sig.agent='sprint', sig.model_version='sprint-v1'
            MERGE (sig)-[:ABOUT]->(p)
            """, b=sigs, d=date.today().isoformat())
        out["sprint"] = len(batch)
        out["speed_decline_candidates"] = len(sigs)
    return out


if __name__ == "__main__":
    print(collect())
