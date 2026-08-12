"""Identity integrity: audit checks that turn today's manual bug hunts into a
nightly report. Findings are printed and counted, never auto-"fixed" — wrong
identities corrupt every layer above, so resolution goes through crosswalk
OVERRIDES or node surgery with human eyes on it.
"""
from __future__ import annotations

from fantasy_assistant.graph.client import session

_CHECKS = {
    "shared_mlbam": """
        MATCH (p:Player) WHERE p.mlbam_id IS NOT NULL
        WITH p.mlbam_id AS k, collect(p.uid) AS us WHERE size(us) > 1
          AND NOT any(u IN us WHERE u CONTAINS 'ohtani')
        RETURN k AS key, us AS detail
    """,
    "shared_cbs_id": """
        MATCH (p:Player) WHERE p.cbs_id IS NOT NULL
        WITH p.cbs_id AS k, collect(p.uid) AS us WHERE size(us) > 1
          AND NOT any(u IN us WHERE u CONTAINS 'ohtani')
        RETURN k AS key, us AS detail
    """,
    # a temporal property stored as STRING poisons every date comparison
    # against it (NULL, silently) — found when 25% of lineup rows appeared
    # to have no covering stint
    "stint_dates_not_date_typed": """
        MATCH (st:RosterStint)
        WHERE (st.from_date IS NOT NULL
               AND apoc.meta.cypher.type(st.from_date) <> 'DATE')
           OR (st.to_date IS NOT NULL
               AND apoc.meta.cypher.type(st.to_date) <> 'DATE')
        RETURN st.uid AS key,
               'from:' + apoc.meta.cypher.type(st.from_date) + ' to:'
               + coalesce(apoc.meta.cypher.type(st.to_date), 'NULL') AS detail
    """,
    # pool entries with DIFFERENT embedded cbs ids on one node = two humans
    # sharing a name within one side (Julio Rodriguez SEA CF vs DET C)
    "multi_cbsid_pool_same_node": """
        MATCH (e:PoolEntry)-[:OF_PLAYER]->(p:Player)
        WHERE NOT p.uid CONTAINS 'ohtani'
        WITH p, e, last(split(e.uid, ':')) AS tail
        WHERE tail =~ '\\\\d{6,}'
        WITH p, collect(DISTINCT tail) AS ids WHERE size(ids) > 1
        RETURN p.uid AS key,
               p.name_full + ' cbs_ids=' + reduce(a='', x IN ids | a + ' ' + x) AS detail
    """,
    # one node in BOTH bat and pit FA pools = two humans sharing a name
    # (the Julio Rodriguez signature; Ohtani is the lone legit two-way)
    "bat_and_pit_pool_same_node": """
        MATCH (e:PoolEntry)-[:OF_PLAYER]->(p:Player)
        WHERE NOT p.uid CONTAINS 'ohtani'
        WITH p, collect(DISTINCT e.side) AS sides WHERE size(sides) > 1
        RETURN p.uid AS key, p.name_full AS detail
    """,
    # a node that is BOTH rostered (open stint) and sitting in today's FA pool
    # is two humans merged: the pool row belongs to a same-named minor leaguer
    # while the stats/stint belong to the rostered player. Caught Cade Smith
    # (NYY pool id 29351817 fused to CLE's closer, rostered by Gashouse Gang)
    # and Jacob Webb. Neither multi_cbsid_pool_same_node nor
    # bat_and_pit_pool_same_node fires here — only ONE pool entry exists on the
    # node, so the collision is invisible to both. Recommending off such a node
    # proposes adding a player somebody already owns.
    "pool_entry_on_rostered_node": """
        MATCH (e:PoolEntry)-[:OF_PLAYER]->(p:Player)
        WHERE NOT p.uid CONTAINS 'ohtani'
          AND e.uid STARTS WITH 'cbs:fapool:' + toString(date())
        MATCH (r:RosterStint)-[:OF_PLAYER]->(p)
        WHERE r.to_date IS NULL
        MATCH (r)-[:ON_TEAM]->(t:FantasyTeam)
        RETURN p.uid AS key,
               p.name_full + ' pool=' + e.avail + ' cbs=' + coalesce(p.cbs_id, '?')
               + ' but rostered by ' + t.cbs_name AS detail
    """,
    "rostered_without_mlbam": """
        MATCH (p:Player)
        WHERE p.mlbam_id IS NULL AND
              ((p)<-[:SELECTED]-(:DraftPick) OR (p)<-[:ADDS]-(:TransactionEvent)
               OR (p)<-[:FILLED_BY]-(:LineupAssignment {section:'active'}))
        RETURN p.uid AS key, p.name_full AS detail
    """,
    "regular_with_no_daylines": """
        MATCH (l:LineupAssignment {section:'active'})-[:FILLED_BY]->(p:Player)
        WITH p, count(DISTINCT l) AS weeks WHERE weeks >= 4
        OPTIONAL MATCH (d:PlayerDayLine)-[:OF_PLAYER]->(p)
        WITH p, weeks, count(d) AS lines WHERE lines = 0
        RETURN p.uid AS key, p.name_full + ' (' + toString(weeks) + ' wks active)' AS detail
    """,
}


def audit(verbose: bool = True) -> dict[str, int]:
    counts = {}
    with session() as s:
        for name, q in _CHECKS.items():
            rows = s.run(q).data()
            counts[name] = len(rows)
            if verbose and rows:
                print(f"[{name}] {len(rows)} findings:")
                for r in rows[:10]:
                    print(f"   {r['key']}: {r['detail']}")
    if verbose:
        clean = all(v == 0 for k, v in counts.items()
                    if k != "regular_with_no_daylines")
        print("identity audit:", counts,
              "" if not clean else "(regular_with_no_daylines includes true "
              "season-long IL/minors starts — review, don't panic)")
    return counts


SPLITS_PATH = None  # resolved lazily to repo data/identity_splits.json


def _splits_path():
    from pathlib import Path
    return Path(__file__).resolve().parents[3] / "data" / "identity_splits.json"


def _aliases_path():
    from pathlib import Path
    return Path(__file__).resolve().parents[3] / "data" / "identity_aliases.json"


def load_aliases() -> dict:
    """alias player uid -> canonical player uid (see merge_cbs_id_duplicates)."""
    import json
    p = _aliases_path()
    return json.loads(p.read_text()) if p.exists() else {}


def merge_cbs_id_duplicates(dry_run: bool = False) -> dict:
    """Merge name-variant duplicates that share one CBS id.

    CBS renders the same human under different names across pages ("George
    Lombard" in the FA pool, "George Lombard Jr." on the roster), which the
    name-keyed uid turns into two nodes. The cbs_id is the strong key: when
    two nodes carry the same one they are the same person, so the pair is
    merged and the losing uid recorded in data/identity_aliases.json, which
    player_uid() consults so future ingests route to the survivor.

    This is the inverse of split_pool_collisions (two humans, one node) and
    is deliberately NOT wired into the nightly run — merges are destructive,
    so a human runs `python -m fantasy_assistant.graph.identity merge`.
    """
    import json
    aliases = load_aliases()
    merged = []
    with session() as s:
        for c in s.run(_CHECKS["shared_cbs_id"]).data():
            uids = c["detail"]
            rows = s.run(
                """
                MATCH (p:Player) WHERE p.uid IN $uids
                OPTIONAL MATCH (p)<-[r]-()
                RETURN p.uid AS uid, p.name_full AS name,
                       p.mlbam_id AS mlbam, count(r) AS rels
                """, uids=uids).data()
            # survivor: a resolved mlbam id wins, then the better-connected
            # node, then the fuller name (CBS's official rendering)
            rows.sort(key=lambda r: (r["mlbam"] is not None, r["rels"],
                                     len(r["name"] or "")), reverse=True)
            canon, losers = rows[0], rows[1:]
            for lose in losers:
                if dry_run:
                    merged.append((lose["uid"], canon["uid"], "dry-run"))
                    continue
                # carry over any property the survivor is missing, then merge
                # relationships onto it and drop the duplicate node
                s.run(
                    """
                    MATCH (c:Player {uid:$canon}), (a:Player {uid:$alias})
                    SET c += apoc.map.removeKeys(
                          apoc.map.fromPairs([k IN keys(a)
                            WHERE c[k] IS NULL | [k, a[k]]]), ['uid'])
                    WITH c, a
                    CALL apoc.refactor.mergeNodes([c, a],
                         {properties:'discard', mergeRels:true}) YIELD node
                    RETURN node.uid AS uid
                    """, canon=canon["uid"], alias=lose["uid"])
                aliases[lose["uid"]] = canon["uid"]
                merged.append((lose["uid"], canon["uid"], "merged"))
    if merged and not dry_run:
        _aliases_path().write_text(json.dumps(aliases, indent=1, sort_keys=True))
    return {"merged": merged, "aliases": len(aliases)}


def split_pool_collisions(capture_dir=None) -> dict:
    """Resolve bat+pit pool name collisions (two humans, one name-keyed node).

    Primary side = the side of the node's day lines (the human our stats
    describe); no day lines -> the side with the better sportsline rank.
    The OTHER side's pool entry moves to '<uid>_<side>' and the routing is
    persisted to data/identity_splits.json, which pool ingest consults so
    future snapshots never re-merge them. Node props restored from the
    primary side's raw pool row.
    """
    import json
    from pathlib import Path
    from fantasy_assistant.capture import parsers
    repo = Path(__file__).resolve().parents[3]
    if capture_dir is None:
        capture_dir = sorted((repo / "data" / "raw").iterdir())[-1]
    # a name can appear MULTIPLE times per side (three Julio Rodriguezes:
    # SEA CF / HOU P / DET C) — keep every row and disambiguate by cbs_id
    pool: dict = {}
    from fantasy_assistant.graph.ingest import _pool_file
    for kind, stem in (("bat", "fa_pool_batters"), ("pit", "fa_pool_pitchers")):
        rows, _ = parsers.parse_pool(_pool_file(capture_dir, stem), kind)
        for r in rows:
            pool.setdefault((parsers.normalize_name(r.player_name), kind), []).append(r)

    def _pick(rows_, cbs_id=None):
        if not rows_:
            return None
        if cbs_id:
            for r in rows_:
                if r.cbs_id == cbs_id:
                    return r
        return rows_[0] if len(rows_) == 1 else None  # ambiguous -> hands off
    splits = {}
    if _splits_path().exists():
        splits = json.loads(_splits_path().read_text())
    fixed = []
    with session() as s:
        coll = s.run(_CHECKS["bat_and_pit_pool_same_node"]).data()
        for c in coll:
            uid = c["key"]
            norm = s.run("MATCH (p:Player {uid:$u}) RETURN p.name_normalized AS n",
                         u=uid).single()["n"]
            dl = {r["side"]: r["n"] for r in s.run(
                "MATCH (d:PlayerDayLine)-[:OF_PLAYER]->(p:Player {uid:$u}) "
                "RETURN d.side AS side, count(*) AS n", u=uid).data()}
            node_cbs = s.run("MATCH (p:Player {uid:$u}) RETURN p.cbs_id AS c",
                             u=uid).single()["c"]
            if dl:
                primary = max(dl, key=dl.get)
            else:
                rb = _pick(pool.get((norm, "bat"), []))
                rp = _pick(pool.get((norm, "pit"), []))
                primary = "bat" if ((rb.rank if rb else None) or 9999) <= \
                                   ((rp.rank if rp else None) or 9999) else "pit"
            other = "pit" if primary == "bat" else "bat"
            new_uid = f"{uid}_{other}"
            row_o = _pick(pool.get((norm, other), []))
            row_p = _pick(pool.get((norm, primary), []), cbs_id=node_cbs)
            s.run("""
                MERGE (np:Player {uid:$nu})
                SET np.name_full=$name, np.name_normalized=$norm,
                    np.cbs_id=$cbs, np.cbs_positions=$pos, np.cbs_mlb_team=$mlb
                WITH np
                MATCH (e:PoolEntry {side:$side})-[r:OF_PLAYER]->(:Player {uid:$u})
                DELETE r
                MERGE (e)-[:OF_PLAYER]->(np)
                """, nu=new_uid, u=uid, side=other,
                  name=(row_o.player_name if row_o else norm.title()), norm=norm,
                  cbs=(row_o.cbs_id if row_o else None),
                  pos=(row_o.positions if row_o else None),
                  mlb=(row_o.mlb_team if row_o else None))
            if row_p:
                s.run("MATCH (p:Player {uid:$u}) SET p.cbs_id=$cbs, "
                      "p.cbs_positions=$pos, p.cbs_mlb_team=$mlb",
                      u=uid, cbs=row_p.cbs_id, pos=row_p.positions,
                      mlb=row_p.mlb_team)
            splits[f"{norm}|{other}"] = new_uid
            fixed.append((uid, primary, new_uid))
        # second pass: same-side collisions (two humans, one name, one side,
        # different cbs_ids). The entry matching the node's cbs_id stays; each
        # foreign-cbs entry moves to '<uid>_cbs<id>' and gets a cbs-keyed
        # route, which pool ingest consults before the name|side route.
        for c in s.run(_CHECKS["multi_cbsid_pool_same_node"]).data():
            uid = c["key"]
            rec = s.run("""
                MATCH (e:PoolEntry)-[:OF_PLAYER]->(p:Player {uid:$u})
                WITH p, e, last(split(e.uid, ':')) AS tail
                WHERE tail =~ '\\\\d{6,}'
                RETURN p.cbs_id AS node_cbs, p.name_full AS name,
                       p.name_normalized AS norm, collect({euid: e.uid, cbs: tail}) AS es
                """, u=uid).single()
            keep = rec["node_cbs"] or sorted(e["cbs"] for e in rec["es"])[0]
            if not rec["node_cbs"]:
                s.run("MATCH (p:Player {uid:$u}) SET p.cbs_id=$c", u=uid, c=keep)
            for e in rec["es"]:
                if e["cbs"] == keep:
                    continue
                new_uid = f"{uid}_cbs{e['cbs']}"
                s.run("""
                    MERGE (np:Player {uid:$nu})
                    SET np.name_full=$name, np.name_normalized=$norm, np.cbs_id=$cbs
                    WITH np
                    MATCH (e:PoolEntry {uid:$euid})-[r:OF_PLAYER]->()
                    DELETE r
                    MERGE (e)-[:OF_PLAYER]->(np)
                    """, nu=new_uid, name=rec["name"], norm=rec["norm"],
                      cbs=e["cbs"], euid=e["euid"])
                splits[f"cbs:{e['cbs']}"] = new_uid
                fixed.append((uid, "cbs:" + keep, new_uid))
    _splits_path().write_text(json.dumps(splits, indent=1, sort_keys=True))
    return {"split": len(fixed), "registry": len(splits)}


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "audit"
    if cmd == "audit":
        audit()
    elif cmd in ("merge", "merge-dry"):
        print(merge_cbs_id_duplicates(dry_run=(cmd == "merge-dry")))
    else:
        sys.exit(f"unknown command: {cmd} (audit|merge|merge-dry)")
