"""Ingest a raw capture directory (data/raw/YYYY-MM-DD) into the graph.

Idempotent: every node is MERGEd on a deterministic uid, so re-running over the
same capture is a no-op. Players are keyed by normalized name until the MLBAM
crosswalk lands (cbs_id attached when the FA pool knows it).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from fantasy_assistant.capture import parsers
from fantasy_assistant.graph.client import session
from fantasy_assistant.graph.refdata import SEASON_2026, team_uid

ACTION_KIND = {
    "Added": "add",
    "Added off Waivers": "claim_add",
    "Dropped": "drop",
    "Moved to IR": "move_to_il",
    "Moved to Minors": "move_to_minors",
    "Activated": "move_from_il",
    "Traded": "trade",
}


def player_uid(name: str) -> str:
    return "player:name:" + parsers.normalize_name(name).replace(" ", "_")


def _capture_run(s, capture_dir: Path, agent: str) -> str:
    uid = f"capture:{capture_dir.name}:{agent}"
    s.run(
        """
        MERGE (c:CaptureRun {uid:$uid})
        SET c.agent=$agent, c.source='cbs', c.capture_date=date($d), c.status='running'
        """,
        uid=uid, agent=agent, d=capture_dir.name,
    )
    return uid


def _capture_done(s, run_uid: str) -> None:
    """Provenance must not lie: 'complete' only after the writes finished."""
    s.run("MATCH (c:CaptureRun {uid:$uid}) SET c.status='complete', "
          "c.completed_at=datetime()", uid=run_uid)


def ingest_transactions(capture_dir: Path) -> dict:
    txns, rejects = parsers.sniff_and_parse_transactions(capture_dir / "transactions_all_raw.txt")
    with session() as s:
        run_uid = _capture_run(s, capture_dir, "transactions")
        for t in txns:
            s.run(
                """
                MATCH (team:FantasyTeam {uid:$team_uid}), (c:CaptureRun {uid:$run})
                MERGE (e:TransactionEvent {uid:$uid})
                SET e.posted_at=datetime($posted), e.effective_date=$eff,
                    e.fee=$cost, e.raw=$raw, e.kinds=$kinds, e.source='cbs'
                MERGE (e)-[:BY_TEAM]->(team)
                MERGE (e)-[:OBSERVED_IN]->(c)
                """,
                team_uid=team_uid(t.team), run=run_uid, uid=t.uid,
                posted=t.posted_at, eff=t.effective_date, cost=t.cost, raw=t.raw,
                kinds=sorted({ACTION_KIND.get(a.action, a.action) for a in t.actions}),
            )
            for a in t.actions:
                rel = {
                    "add": "ADDS", "claim_add": "ADDS", "drop": "DROPS",
                    "trade": "ADDS",  # trade rows are written from acquiring team's view
                }.get(ACTION_KIND.get(a.action, ""), "MOVES")
                s.run(
                    f"""
                    MATCH (e:TransactionEvent {{uid:$euid}})
                    MERGE (p:Player {{uid:$puid}})
                    ON CREATE SET p.name_full=$name, p.name_normalized=$norm
                    SET p.cbs_positions=$pos, p.cbs_mlb_team=$mlb
                    MERGE (e)-[r:{rel}]->(p)
                    SET r.action=$action, r.via_waivers=$waiv, r.trade_from=$tcp
                    """,
                    euid=t.uid, puid=player_uid(a.player_name),
                    name=a.player_name, norm=parsers.normalize_name(a.player_name),
                    pos=a.positions, mlb=a.mlb_team, action=ACTION_KIND.get(a.action, a.action),
                    waiv=a.action == "Added off Waivers",
                    tcp=a.trade_counterparty,
                )
        _capture_done(s, run_uid)
    return {"transactions": len(txns), "rejects": len(rejects)}


def _pool_target_period(capture_dir: Path) -> int:
    from datetime import date as _date
    from fantasy_assistant.graph.refdata import period_for_date
    d = _date.fromisoformat(capture_dir.name)
    return period_for_date(d) + (1 if d.weekday() == 6 else 0)


def _pool_file(capture_dir: Path, stem: str) -> Path:
    """Current runner writes '<stem>.psv'; older captures used a period-
    suffixed name. Prefer current, fall back to any suffixed legacy file."""
    plain = capture_dir / f"{stem}.psv"
    if plain.exists():
        return plain
    legacy = sorted(capture_dir.glob(f"{stem}_period*.psv"))
    if legacy:
        return legacy[-1]
    return plain  # let the parser raise on the canonical name


def standings_period(capture_dir: Path) -> int:
    """The period a YTD standings capture belongs to: games through 'now'
    include the capture date's own (possibly in-progress) period."""
    from datetime import date as _date
    from fantasy_assistant.graph.refdata import period_for_date
    return period_for_date(_date.fromisoformat(capture_dir.name))


def ingest_pool(capture_dir: Path, as_of: str) -> dict:
    counts = {}
    with session() as s:
        run_uid = _capture_run(s, capture_dir, "fa_pool")
        snap_uid = f"cbs:fapool:{capture_dir.name}"
        s.run(
            """
            MATCH (c:CaptureRun {uid:$run})
            MERGE (f:FreeAgentPoolSnapshot {uid:$uid})
            SET f.as_of=datetime($as_of), f.period_projected=$pp
            MERGE (f)-[:OBSERVED_IN]->(c)
            """,
            run=run_uid, uid=snap_uid, as_of=as_of, pp=_pool_target_period(capture_dir),
        )
        from fantasy_assistant.graph import identity as _identity
        _splits = {}
        if _identity._splits_path().exists():
            _splits = json.loads(_identity._splits_path().read_text())
        for kind, stem in (("bat", "fa_pool_batters"), ("pit", "fa_pool_pitchers")):
            rows, rejects = parsers.parse_pool(_pool_file(capture_dir, stem), kind)
            batch = [
                {
                    "entry_uid": f"{snap_uid}:{kind}:{r.cbs_id or parsers.normalize_name(r.player_name)}",
                    "puid": _splits.get(
                        f"cbs:{r.cbs_id}",
                        _splits.get(f"{parsers.normalize_name(r.player_name)}|{kind}",
                                    player_uid(r.player_name))),
                    "name": r.player_name,
                    "norm": parsers.normalize_name(r.player_name),
                    "cbs_id": r.cbs_id, "pos": r.positions, "mlb": r.mlb_team,
                    "avail": r.avail, "clear": r.waiver_clear, "rank": r.rank,
                    "side": kind, "stats": [f"{k}={v}" for k, v in r.stats.items() if v is not None],
                }
                for r in rows
            ]
            s.run(
                """
                MATCH (f:FreeAgentPoolSnapshot {uid:$snap})
                UNWIND $batch AS row
                MERGE (p:Player {uid:row.puid})
                ON CREATE SET p.name_full=row.name, p.name_normalized=row.norm
                // a DIFFERENT cbs_id on the same name-keyed node means two
                // humans share the name — never clobber; identity.audit()
                // surfaces these for a manual split (see Muncy/Julio Rodriguez)
                SET p.cbs_id = coalesce(p.cbs_id, row.cbs_id),
                    p.cbs_positions = CASE WHEN p.cbs_id IS NULL OR p.cbs_id = row.cbs_id
                                           THEN row.pos ELSE p.cbs_positions END,
                    p.cbs_mlb_team = CASE WHEN p.cbs_id IS NULL OR p.cbs_id = row.cbs_id
                                          THEN row.mlb ELSE p.cbs_mlb_team END
                MERGE (e:PoolEntry {uid:row.entry_uid})
                SET e.avail=row.avail, e.waiver_clear=row.clear,
                    e.sportsline_rank=row.rank, e.side=row.side,
                    e.sportsline_week=row.stats
                MERGE (f)-[:HAS]->(e)
                MERGE (e)-[:OF_PLAYER]->(p)
                """,
                snap=snap_uid, batch=batch,
            )
            counts[kind] = {"rows": len(rows), "rejects": len(rejects)}
        _capture_done(s, run_uid)
    return counts


def ingest_standings(capture_dir: Path, as_of: str, period: int) -> dict:
    data = parsers.parse_standings(capture_dir / "standings_overall.txt")
    snap_uid = f"cbs:standings:{capture_dir.name}"
    with session() as s:
        run_uid = _capture_run(s, capture_dir, "standings")
        s.run(
            """
            MATCH (c:CaptureRun {uid:$run}), (p:ScoringPeriod {uid:$puid})
            MERGE (st:StandingsSnapshot {uid:$uid})
            SET st.as_of=datetime($as_of), st.scope='ytd'
            MERGE (st)-[:THROUGH_PERIOD]->(p)
            MERGE (st)-[:OBSERVED_IN]->(c)
            """,
            run=run_uid, uid=snap_uid, as_of=as_of, puid=f"period:2026:{period}",
        )
        for code, lines in data["categories"].items():
            for rank, row in enumerate(lines, start=1):
                s.run(
                    """
                    MATCH (st:StandingsSnapshot {uid:$snap}),
                          (t:FantasyTeam {uid:$tuid}), (cat:Category {uid:$cuid})
                    MERGE (l:CategoryStandingLine {uid:$uid})
                    SET l.value_reported=$value, l.points=$points, l.rank=$rank
                    MERGE (st)-[:HAS_LINE]->(l)
                    MERGE (l)-[:FOR_TEAM]->(t)
                    MERGE (l)-[:IN_CATEGORY]->(cat)
                    """,
                    snap=snap_uid, tuid=team_uid(row["team"]),
                    cuid=f"category:{code}",
                    uid=f"{snap_uid}:{code}:{team_uid(row['team'])}",
                    value=row["value"], points=row["points"], rank=rank,
                )
        for rank, row in enumerate(
                sorted(data["overall"], key=lambda r: -r["total"]), start=1):
            s.run(
                """
                MATCH (st:StandingsSnapshot {uid:$snap}), (t:FantasyTeam {uid:$tuid})
                MERGE (st)-[r:OVERALL]->(t)
                SET r.batting=$b, r.pitching=$p, r.total=$tot, r.rank=$rank
                """,
                snap=snap_uid, tuid=team_uid(row["team"]), b=row["batting"],
                p=row["pitching"], tot=row["total"], rank=rank,
            )
        _capture_done(s, run_uid)
    n = sum(len(v) for v in data["categories"].values())
    return {"overall_rows": len(data["overall"]), "category_lines": n}


def ingest_roster_grid(capture_dir: Path, as_of: str) -> dict:
    entries = parsers.sniff_and_parse_roster_grid(capture_dir / "roster_grid.txt")
    snap_uid = f"cbs:rostergrid:{capture_dir.name}"
    with session() as s:
        run_uid = _capture_run(s, capture_dir, "roster_grid")
        s.run(
            """
            MATCH (c:CaptureRun {uid:$run})
            MERGE (g:RosterGridSnapshot {uid:$uid})
            SET g.as_of=datetime($as_of)
            MERGE (g)-[:OBSERVED_IN]->(c)
            """,
            run=run_uid, uid=snap_uid, as_of=as_of, pp=_pool_target_period(capture_dir),
        )
        batch = [
            {
                "uid": f"{snap_uid}:{team_uid(e.team)}:{e.slot_group}:{parsers.normalize_name(e.label)}",
                "tuid": team_uid(e.team), "slot": e.slot_group,
                "label": e.label, "status": e.status,
            }
            for e in entries
        ]
        s.run(
            """
            MATCH (g:RosterGridSnapshot {uid:$snap})
            UNWIND $batch AS row
            MATCH (t:FantasyTeam {uid:row.tuid})
            MERGE (e:RosterGridEntry {uid:row.uid})
            SET e.slot_group=row.slot, e.label=row.label, e.status=row.status
            MERGE (g)-[:HAS]->(e)
            MERGE (e)-[:ON_TEAM]->(t)
            """,
            snap=snap_uid, batch=batch,
        )
        _capture_done(s, run_uid)
    return {"entries": len(entries)}


def ingest_capture(capture_dir: Path) -> None:
    as_of = datetime.fromisoformat(capture_dir.name + "T12:00:00").isoformat()
    print("transactions:", ingest_transactions(capture_dir))
    print("fa pool:", ingest_pool(capture_dir, as_of))
    print("standings:", ingest_standings(capture_dir, as_of,
                                          period=standings_period(capture_dir)))
    print("roster grid:", ingest_roster_grid(capture_dir, as_of))


def ingest_draft(capture_dir: Path) -> dict:
    picks, rejects = parsers.parse_draft(capture_dir / "draft_results.txt")
    with session() as s:
        run_uid = _capture_run(s, capture_dir, "draft")
        batch = [
            {
                "uid": f"draft:2026:{p.overall}",
                "round": p.round, "pick": p.pick_in_round, "overall": p.overall,
                "auto": p.auto, "queued": p.queued,
                "tuid": team_uid(p.team),
                "puid": player_uid(p.player_name),
                "name": p.player_name,
                "norm": parsers.normalize_name(p.player_name),
                "pos": p.positions, "mlb": p.mlb_team,
            }
            for p in picks
        ]
        s.run(
            """
            MATCH (se:Season {uid:$suid}), (c:CaptureRun {uid:$run})
            UNWIND $batch AS row
            MATCH (t:FantasyTeam {uid:row.tuid})
            MERGE (p:Player {uid:row.puid})
            ON CREATE SET p.name_full=row.name, p.name_normalized=row.norm,
                          p.cbs_positions=row.pos, p.cbs_mlb_team=row.mlb
            MERGE (d:DraftPick {uid:row.uid})
            SET d.round=row.round, d.pick_in_round=row.pick, d.overall=row.overall,
                d.auto=row.auto, d.queued=row.queued
            MERGE (d)-[:IN_SEASON]->(se)
            MERGE (d)-[:BY_TEAM]->(t)
            MERGE (d)-[:SELECTED]->(p)
            MERGE (d)-[:OBSERVED_IN]->(c)
            """,
            suid=SEASON_2026, run=run_uid, batch=batch,
        )
        _capture_done(s, run_uid)
    return {"picks": len(picks), "rejects": len(rejects)}


def ingest_byperiod(capture_dir: Path) -> dict:
    data = parsers.parse_byperiod(capture_dir / "standings_byperiod_all.txt")
    n_lines = 0
    with session() as s:
        run_uid = _capture_run(s, capture_dir, "standings_byperiod")
        for period, pdata in data.items():
            snap_uid = f"cbs:standings:period:{period}"
            s.run(
                """
                MATCH (c:CaptureRun {uid:$run}), (p:ScoringPeriod {uid:$puid})
                MERGE (st:StandingsSnapshot {uid:$uid})
                SET st.as_of=datetime($as_of), st.scope='period'
                MERGE (st)-[:FOR_PERIOD]->(p)
                MERGE (st)-[:OBSERVED_IN]->(c)
                """,
                run=run_uid, uid=snap_uid, puid=f"period:2026:{period}",
                as_of=capture_dir.name + "T12:00:00",
            )
            batch = []
            for code, rows in pdata["categories"].items():
                for rank, row in enumerate(rows, start=1):
                    batch.append({
                        "uid": f"{snap_uid}:{code}:{team_uid(row['team'])}",
                        "tuid": team_uid(row["team"]), "cuid": f"category:{code}",
                        "value": row["value"], "points": row["points"],
                        "dif": row["dif"], "rank": rank,
                    })
            s.run(
                """
                MATCH (st:StandingsSnapshot {uid:$snap})
                UNWIND $batch AS row
                MATCH (t:FantasyTeam {uid:row.tuid}), (cat:Category {uid:row.cuid})
                MERGE (l:CategoryStandingLine {uid:row.uid})
                SET l.value_reported=row.value, l.points=row.points,
                    l.rank=row.rank, l.dif=row.dif
                MERGE (st)-[:HAS_LINE]->(l)
                MERGE (l)-[:FOR_TEAM]->(t)
                MERGE (l)-[:IN_CATEGORY]->(cat)
                """,
                snap=snap_uid, batch=batch,
            )
            n_lines += len(batch)
            for row in pdata["overall"]:
                s.run(
                    """
                    MATCH (st:StandingsSnapshot {uid:$snap}), (t:FantasyTeam {uid:$tuid})
                    MERGE (st)-[r:OVERALL]->(t)
                    SET r.rank=$rank, r.batting=$b, r.pitching=$p, r.total=$tot,
                        r.dif=$dif, r.behind=$behind
                    """,
                    snap=snap_uid, tuid=team_uid(row["team"]), rank=row["rank"],
                    b=row["batting"], p=row["pitching"], tot=row["total"],
                    dif=row["dif"], behind=row["behind"],
                )
        _capture_done(s, run_uid)
    return {"periods": len(data), "category_lines": n_lines}


def ingest_lineups(capture_dir: Path) -> dict:
    rows, rejects = parsers.parse_lineups(capture_dir / "lineups_all.psv")
    with session() as s:
        run_uid = _capture_run(s, capture_dir, "lineups")
        batch = [
            {
                "uid": (f"lineup:2026:{r.period}:{team_uid(r.team)}:{r.section}:"
                        f"{r.slot}:{parsers.normalize_name(r.player_name).replace(' ', '_')}"),
                "tuid": team_uid(r.team), "puid": player_uid(r.player_name),
                "period_uid": f"period:2026:{r.period}",
                "name": r.player_name, "norm": parsers.normalize_name(r.player_name),
                "slot": r.slot, "section": r.section, "cbs_id": r.cbs_id,
            }
            for r in rows
        ]
        for i in range(0, len(batch), 2000):
            s.run(
                """
                MATCH (c:CaptureRun {uid:$run})
                UNWIND $batch AS row
                MATCH (t:FantasyTeam {uid:row.tuid}), (per:ScoringPeriod {uid:row.period_uid})
                MERGE (p:Player {uid:row.puid})
                ON CREATE SET p.name_full=row.name, p.name_normalized=row.norm
                SET p.cbs_id = coalesce(row.cbs_id, p.cbs_id)
                MERGE (l:LineupAssignment {uid:row.uid})
                SET l.slot=row.slot, l.section=row.section
                MERGE (l)-[:BY_TEAM]->(t)
                MERGE (l)-[:IN_PERIOD]->(per)
                MERGE (l)-[:FILLED_BY]->(p)
                MERGE (l)-[:OBSERVED_IN]->(c)
                """,
                run=run_uid, batch=batch[i:i + 2000],
            )
        _capture_done(s, run_uid)
    return {"lineup_rows": len(rows), "rejects": len(rejects)}


def _est_return_weeks(text: str) -> float | None:
    import re as _re
    m = _re.search(r"(\d+)(?:[- ]to[- ](\d+))?[- ]weeks?", text or "", _re.I)
    if not m:
        m2 = _re.search(r"(\d+)(?:[- ]to[- ](\d+))?[- ]months?", text or "", _re.I)
        if not m2:
            return None
        lo = float(m2.group(1)) * 4
        hi = float(m2.group(2)) * 4 if m2.group(2) else lo
        return (lo + hi) / 2
    lo = float(m.group(1))
    hi = float(m.group(2)) if m.group(2) else lo
    return (lo + hi) / 2


def ingest_news(capture_dir: Path) -> dict:
    path = capture_dir / "player_news_raw.txt"
    if not path.exists():
        return {"news": 0, "skipped": "no capture"}
    rows, rejects = parsers.parse_player_news(path)
    from datetime import datetime as _dt
    ts = _dt.now().isoformat(timespec="seconds")
    our_new = []
    with session() as s:
        batch = [{
            "uid": "cbsnews:" + parsers.uid_hash(r.player_name, r.headline),
            "puid": player_uid(r.player_name),
            "name": r.player_name,
            "norm": parsers.normalize_name(r.player_name),
            "headline": r.headline, "body": r.body, "age": r.age_text,
            "est_weeks": _est_return_weeks(r.body or r.headline),
        } for r in rows]
        created = s.run(
            """
            UNWIND $batch AS row
            MERGE (p:Player {uid: row.puid})
            ON CREATE SET p.name_full = row.name, p.name_normalized = row.norm
            MERGE (nw:NewsItem {uid: row.uid})
            ON CREATE SET nw.first_seen = datetime($ts), nw.is_new = true
            ON MATCH SET nw.is_new = false
            SET nw.headline = row.headline, nw.body = row.body,
                nw.age_at_capture = row.age, nw.source = 'cbs_rotowire'
            MERGE (nw)-[:ABOUT]->(p)
            WITH row, nw, p
            FOREACH (_ IN CASE WHEN row.est_weeks IS NULL THEN [] ELSE [1] END |
                SET p.est_return_weeks = row.est_weeks,
                    p.est_return_src = row.headline)
            RETURN row.name AS name, row.headline AS h, nw.is_new AS fresh
            """,
            batch=batch, ts=ts,
        ).data()
        our_new = s.run(
            """
            MATCH (nw:NewsItem {is_new: true, source:'cbs_rotowire'})-[:ABOUT]->(p:Player),
                  (st:RosterStint)-[:OF_PLAYER]->(p),
                  (st)-[:ON_TEAM]->(:FantasyTeam {is_us:true})
            WHERE st.to_date IS NULL
            RETURN p.name_full AS n, nw.headline AS h
            """
        ).data()
    return {"news": len(rows), "rejects": len(rejects),
            "fresh": sum(1 for c in created if c["fresh"]),
            "our_roster_fresh": [(x["n"], x["h"]) for x in our_new]}


if __name__ == "__main__":
    import sys
    ingest_capture(Path(sys.argv[1] if len(sys.argv) > 1 else "data/raw/2026-08-02"))
