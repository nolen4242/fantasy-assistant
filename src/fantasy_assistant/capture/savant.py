"""Baseball Savant expected-statistics: skill-vs-results gaps.

xwOBA-wOBA (hitters) and xERA-ERA (pitchers) measure luck: results ahead of
skill = sell-high; behind = buy-low. Weekly capture -> Player props + Signal
nodes for material gaps on league-relevant players (rostered or pool-known).
"""
from __future__ import annotations

import csv
import io
from datetime import date

import httpx

from fantasy_assistant.graph.client import session

URL = ("https://baseballsavant.mlb.com/leaderboard/expected_statistics"
       "?type={kind}&year={year}&position=&team=&min={min}&csv=true")
BAT_GAP, PIT_GAP = 0.040, 1.00


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def collect(year: int = 2026) -> dict:
    out = {"batters": 0, "pitchers": 0, "signals": []}
    with session() as s:
        uni = {r["m"]: r["uid"] for r in s.run(
            "MATCH (p:Player) WHERE p.mlbam_id IS NOT NULL "
            "RETURN p.mlbam_id AS m, p.uid AS uid").data()}
        relevant = {r["uid"] for r in s.run(
            """
            MATCH (p:Player) WHERE p.mlbam_id IS NOT NULL
              AND ((p)<-[:SELECTED]-(:DraftPick) OR (p)<-[:ADDS]-(:TransactionEvent)
                   OR EXISTS {MATCH (p)<-[:OF_PLAYER]-(e:PoolEntry)
                              WHERE e.sportsline_rank <= 250})
            RETURN p.uid AS uid
            """).data()}
        for kind, min_, gap_thresh in (("batter", 150, BAT_GAP), ("pitcher", 50, PIT_GAP)):
            text = httpx.get(URL.format(kind=kind, year=year, min=min_),
                             timeout=60).text
            rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
            batch, signals = [], []
            for r in rows:
                mlbam = int(r["player_id"])
                if mlbam not in uni:
                    continue
                if kind == "batter":
                    woba, xwoba = _f(r["woba"]), _f(r["est_woba"])
                    if woba is None or xwoba is None:
                        continue
                    gap = woba - xwoba
                    props = {"woba": woba, "xwoba": xwoba,
                             "xba": _f(r["est_ba"]), "xslg": _f(r["est_slg"]),
                             "luck_gap": round(gap, 4)}
                    props = {k: v for k, v in props.items() if v is not None}
                    breach = abs(gap) >= gap_thresh
                else:
                    era, xera = _f(r.get("era")), _f(r.get("xera"))
                    if era is None or xera is None:
                        continue
                    gap = era - xera
                    props = {"era_sv": era, "xera": xera,
                             "pit_luck_gap": round(gap, 3)}
                    breach = abs(gap) >= gap_thresh
                batch.append({"puid": uni[mlbam], "props": props})
                if breach and uni[mlbam] in relevant:
                    # hitters: results > skill (gap>0) = sell-high
                    # pitchers: ERA > xERA (gap>0) = unlucky = buy-low
                    if kind == "batter":
                        sk = "sell_high" if gap > 0 else "buy_low"
                    else:
                        sk = "buy_low" if gap > 0 else "sell_high"
                    signals.append({"puid": uni[mlbam], "kind": sk,
                                    "why": (f"wOBA {props.get('woba')} vs xwOBA {props.get('xwoba')}"
                                            if kind == "batter" else
                                            f"ERA {props.get('era_sv')} vs xERA {props.get('xera')}"),
                                    "strength": min(1.0, abs(gap) / (gap_thresh * 3))})
            s.run(
                "UNWIND $b AS row MATCH (p:Player {uid: row.puid}) SET p += row.props",
                b=batch)
            s.run(
                """
                UNWIND $b AS row
                MATCH (p:Player {uid: row.puid})
                MERGE (sig:Signal {uid: 'signal:luck:' + row.puid + ':' + $d})
                SET sig.kind=row.kind, sig.strength=row.strength, sig.as_of=date($d),
                    sig.rationale=row.why, sig.agent='savant_xstats',
                    sig.model_version='xstats-v1'
                MERGE (sig)-[:ABOUT]->(p)
                """,
                b=signals, d=date.today().isoformat())
            out["batters" if kind == "batter" else "pitchers"] = len(batch)
            out["signals"] += [s_["kind"] for s_ in signals]
    return out


if __name__ == "__main__":
    r = collect()
    from collections import Counter
    print(f"batters {r['batters']}, pitchers {r['pitchers']}, "
          f"signals {dict(Counter(r['signals']))}")
