"""Roster replay + reconciliation.

Derives every team's roster membership by replaying the draft and the season
transaction log, writes RosterStint interval nodes, then diffs the derived
current rosters against the roster-grid snapshot. Mismatches become
Discrepancy nodes under a ReconciliationRun — per PROBLEM.md, discrepancies
are capture bugs to chase, never silently absorbed.

Scope note: replay covers membership (who is on which team), not active/
reserve slotting — reserve moves are lineup-type transactions CBS excludes
from the default log. IL/minors stint status is carried on the stint.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from fantasy_assistant.capture import parsers
from fantasy_assistant.graph.client import session
from fantasy_assistant.graph.ingest import ACTION_KIND, player_uid
from fantasy_assistant.graph.refdata import TEAMS, team_uid

DRAFT_DATE = "2026-03-18"


def _draft_file(capture_dir: Path) -> Path:
    """Draft results for a capture, falling back to the newest prior capture.

    The draft is a one-time pre-season event, so the file is static and the
    runner does not re-fetch it daily; captures that lack it borrow the most
    recent one rather than failing reconciliation outright."""
    own = capture_dir / "draft_results.txt"
    if own.exists():
        return own
    for prior in sorted(capture_dir.parent.iterdir(), reverse=True):
        if prior.name >= capture_dir.name or not prior.is_dir():
            continue
        cand = prior / "draft_results.txt"
        if cand.exists():
            return cand
    return own  # let the parser raise on the canonical name


def replay(capture_dir: Path, posted_cutoff: str | None = None) -> dict:
    """Replay to derive roster membership. posted_cutoff (ISO timestamp)
    limits replay to transactions *posted* on or before that instant. CBS's
    roster grid reflects executed transactions immediately — even those with
    a future effective date — so snapshot diffs must cut by posted time, not
    effective date."""
    picks, _ = parsers.parse_draft(_draft_file(capture_dir))
    txns, _ = parsers.sniff_and_parse_transactions(capture_dir / "transactions_all_raw.txt")
    # CBS lists newest-first; waiver-run batches share one timestamp, so a
    # plain stable sort would keep newest-first order *within* each batch.
    # Reverse first: ties then preserve true chronological (bottom-up) order.
    txns.reverse()
    txns.sort(key=lambda t: t.posted_at)

    # open stints: (team, player_uid) -> stint dict; closed accumulate in list
    open_stints: dict[tuple[str, str], dict] = {}
    closed: list[dict] = []
    anomalies: list[str] = []

    def open_stint(team: str, puid: str, name: str, date: str, via: str):
        key = (team, puid)
        if key in open_stints:
            anomalies.append(f"double-open {name} on {team} at {date} via {via}")
            return
        open_stints[key] = {"team": team, "puid": puid, "name": name,
                            "from_date": date, "via": via, "status": "active"}

    def close_stint(team: str, puid: str, name: str, date: str, how: str):
        key = (team, puid)
        st = open_stints.pop(key, None)
        if st is None:
            anomalies.append(f"close-without-open {name} on {team} at {date} via {how}")
            return
        st["to_date"] = date
        st["ended_by"] = how
        closed.append(st)

    for p in picks:
        open_stint(p.team, player_uid(p.player_name), p.player_name, DRAFT_DATE, "draft")

    # manual corrections: facts CBS's log is missing (e.g. commissioner
    # actions, one-sided trade rows). Applied in date order with the rest.
    corrections = []
    corr_path = capture_dir / "corrections.jsonl"
    if corr_path.exists():
        import json
        corrections = [json.loads(line) for line in corr_path.read_text().splitlines() if line.strip()]

    events: list[tuple[str, str, object]] = [(t.effective_date or t.posted_at[:10], t.posted_at, t) for t in txns]
    for c in corrections:
        events.append((c["effective_date"], c["effective_date"], c))
    events.sort(key=lambda e: (e[0], e[1]))

    for eff, posted, item in events:
        if posted_cutoff and posted > posted_cutoff:
            continue
        if isinstance(item, dict):  # correction
            if item["type"] == "trade_unlogged":
                puid = player_uid(item["player"])
                close_stint(item["from_team"], puid, item["player"], eff, "trade(corrected)")
                open_stint(item["to_team"], puid, item["player"], eff, "trade(corrected)")
            continue
        t = item
        for a in t.actions:
            puid = player_uid(a.player_name)
            kind = ACTION_KIND.get(a.action, a.action)
            if kind in ("add", "claim_add"):
                open_stint(t.team, puid, a.player_name, eff, kind)
            elif kind == "drop":
                close_stint(t.team, puid, a.player_name, eff, "drop")
            elif kind == "trade":
                if a.trade_counterparty:
                    close_stint(a.trade_counterparty, puid, a.player_name, eff, "trade")
                open_stint(t.team, puid, a.player_name, eff, "trade")
            elif kind in ("move_to_il", "move_from_il", "move_to_minors"):
                st = open_stints.get((t.team, puid))
                if st is None:
                    anomalies.append(f"status-move-without-stint {a.player_name} on {t.team}")
                else:
                    st["status"] = {"move_to_il": "il", "move_from_il": "active",
                                    "move_to_minors": "minors"}[kind]

    return {"open": open_stints, "closed": closed, "anomalies": anomalies}


# ---------------------------------------------------------------------------
# Grid matching
# ---------------------------------------------------------------------------

def grid_captured_at(capture_dir: Path) -> str:
    """Read the capture timestamp from the grid snapshot header, e.g.
    'captured: 2026-08-02 ~11:35 ET' -> '2026-08-02T11:35:00'."""
    for line in (capture_dir / "roster_grid.txt").read_text().splitlines():
        m = re.match(r"captured: (\d{4}-\d{2}-\d{2})[T ~]+(\d{1,2}):(\d{2})", line)
        if m:
            d, hh, mm = m.groups()
            # capture stamps are local (CT); CBS posted_at timestamps are ET —
            # shift +1h so the cutoff compares in league time
            from datetime import datetime as _dt, timedelta as _td
            t = _dt.fromisoformat(f"{d}T{int(hh):02d}:{mm}:00") + _td(hours=1)
            return t.isoformat()
    return capture_dir.name + "T23:59:59"


_PAREN_RE = re.compile(r"\s*\((Batter|Pitcher)\)\s*$", re.I)


def _label_key(label: str) -> tuple[str, str, str]:
    """'D Rushing' -> ('d','rushing',''); 'S Ohtani (Pitcher)' -> ('s','ohtani','pitcher')"""
    qual = ""
    m = _PAREN_RE.search(label)
    if m:
        qual = m.group(1).lower()
        label = label[: m.start()]
    norm = parsers.normalize_name(label)
    parts = norm.split()
    return (parts[0][:1] if parts else "", parts[-1] if parts else "", qual)


def _name_key(name: str) -> tuple[str, str, str]:
    return _label_key(name)  # same rules apply to full names


def reconcile(capture_dir: Path, write_graph: bool = True) -> dict:
    # cut replay at the moment the grid was captured (transactions posted
    # later — even minutes later — are not reflected in the grid)
    state = replay(capture_dir, posted_cutoff=grid_captured_at(capture_dir))
    grid = parsers.sniff_and_parse_roster_grid(capture_dir / "roster_grid.txt")

    derived: dict[str, dict[tuple, dict]] = {t: {} for t in TEAMS}
    for st in state["open"].values():
        derived.setdefault(st["team"], {})[_name_key(st["name"])] = st

    diffs: list[dict] = []
    matched = 0
    grid_by_team: dict[str, list] = {}
    for e in grid:
        grid_by_team.setdefault(e.team, []).append(e)

    GRID_STATUS = {"active": "active", "reserve": "active",
                   "il": "il", "minors": "minors"}
    status_fixes: list[dict] = []
    for team, entries in grid_by_team.items():
        pool = dict(derived.get(team, {}))
        for e in entries:
            key = _label_key(e.label)
            st = pool.pop(key, None)
            if st is None:
                diffs.append({"kind": "grid_only", "team": team, "who": e.label,
                              "detail": f"in grid ({e.status}) but not in replayed roster"})
            else:
                matched += 1
                # grid is authoritative for CURRENT status — replay can go
                # stale when an activation never hits the transaction log
                want = GRID_STATUS[e.status]
                if st["status"] != want:
                    status_fixes.append({"team": st["team"], "puid": st["puid"],
                                         "from": st["status"], "to": want})
                    st["status"] = want
        for key, st in pool.items():
            diffs.append({"kind": "replay_only", "team": team, "who": st["name"],
                          "detail": f"replay has open stint (via {st['via']} from {st['from_date']}) but not in grid"})

    result = {
        "status_fixes": status_fixes,
        "teams": len(grid_by_team),
        "grid_entries": len(grid),
        "matched": matched,
        "open_stints": len(state["open"]),
        "closed_stints": len(state["closed"]),
        "replay_anomalies": state["anomalies"],
        "diffs": diffs,
    }

    if write_graph:
        _write(state, result, capture_dir)
    return result


def _write(state: dict, result: dict, capture_dir: Path) -> None:
    ts = datetime.now().isoformat(timespec="seconds")
    all_stints = list(state["open"].values()) + state["closed"]
    with session() as s:
        s.run(
            """
            UNWIND $batch AS row
            MATCH (t:FantasyTeam {uid: row.tuid})
            MERGE (p:Player {uid: row.puid})
            ON CREATE SET p.name_full = row.name,
                          p.name_normalized = row.norm
            MERGE (st:RosterStint {uid: row.uid})
            SET st.from_date = date(row.from_date), st.to_date = date(row.to_date),
                st.status = row.status, st.acquired_via = row.via,
                st.ended_by = row.ended_by, st.derived = true
            MERGE (st)-[:ON_TEAM]->(t)
            MERGE (st)-[:OF_PLAYER]->(p)
            """,
            batch=[{
                "uid": f"stint:{team_uid(st['team'])}:{st['puid']}:{st['from_date']}",
                "tuid": team_uid(st["team"]), "puid": st["puid"],
                "name": st["name"], "norm": parsers.normalize_name(st["name"]),
                "from_date": st["from_date"], "to_date": st.get("to_date"),
                "status": st["status"], "via": st["via"],
                "ended_by": st.get("ended_by"),
            } for st in all_stints],
        )
        # garbage-collect stints this replay no longer derives — stale stints
        # from older (buggier) parses otherwise live forever (found: ghost
        # players like 'Nightmare Shohei Ohtani' from a pre-v2 trade parse)
        derived_uids = [f"stint:{team_uid(st['team'])}:{st['puid']}:{st['from_date']}"
                        for st in all_stints]
        gone = s.run(
            """
            MATCH (st:RosterStint) WHERE NOT st.uid IN $uids
            DETACH DELETE st RETURN count(*) AS n
            """, uids=derived_uids).single()["n"]
        if gone:
            print(f"  replay GC: removed {gone} stale stints")
        orphans = s.run(
            """
            MATCH (p:Player) WHERE NOT (p)--() DETACH DELETE p RETURN count(*) AS n
            """).single()["n"]
        if orphans:
            print(f"  replay GC: removed {orphans} orphaned player nodes")
        run_uid = f"recon:{capture_dir.name}:roster"
        s.run("MATCH (:ReconciliationRun {uid:$u})-[:FOUND]->(d:Discrepancy) "
              "DETACH DELETE d", u=run_uid)
        s.run(
            """
            MERGE (r:ReconciliationRun {uid:$uid})
            SET r.ran_at=datetime($ts), r.kind='roster_replay_vs_grid',
                r.discrepancy_count=$n, r.anomaly_count=$a, r.diffs_json=$dj
            """,
            uid=run_uid, ts=ts, n=len(result["diffs"]),
            a=len(result["replay_anomalies"]),
            dj=__import__('json').dumps(result['diffs'][:10]),
        )
        s.run(
            """
            MATCH (r:ReconciliationRun {uid:$run})
            UNWIND $batch AS row
            MERGE (d:Discrepancy {uid: row.uid})
            SET d.kind=row.kind, d.team=row.team, d.who=row.who,
                d.detail=row.detail, d.status='open'
            MERGE (r)-[:FOUND]->(d)
            """,
            run=run_uid,
            batch=[{
                "uid": f"{run_uid}:{d['kind']}:{team_uid(d['team'])}:{parsers.normalize_name(d['who'])}",
                **d,
            } for d in result["diffs"]],
        )


if __name__ == "__main__":
    import json
    import sys
    r = reconcile(Path(sys.argv[1] if len(sys.argv) > 1 else "data/raw/2026-08-02"))
    print(json.dumps({k: v for k, v in r.items() if k != "diffs"}, indent=1))
    for d in r["diffs"]:
        print(f"  DIFF [{d['kind']}] {d['team']}: {d['who']} — {d['detail']}")
