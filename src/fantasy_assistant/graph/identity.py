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
    # one node in BOTH bat and pit FA pools = two humans sharing a name
    # (the Julio Rodriguez signature; Ohtani is the lone legit two-way)
    "bat_and_pit_pool_same_node": """
        MATCH (e:PoolEntry)-[:OF_PLAYER]->(p:Player)
        WHERE NOT p.uid CONTAINS 'ohtani'
        WITH p, collect(DISTINCT e.side) AS sides WHERE size(sides) > 1
        RETURN p.uid AS key, p.name_full AS detail
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
    pool = {}
    from fantasy_assistant.graph.ingest import _pool_file
    for kind, stem in (("bat", "fa_pool_batters"), ("pit", "fa_pool_pitchers")):
        rows, _ = parsers.parse_pool(_pool_file(capture_dir, stem), kind)
        for r in rows:
            pool[(parsers.normalize_name(r.player_name), kind)] = r
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
            if dl:
                primary = max(dl, key=dl.get)
            else:
                rb = pool.get((norm, "bat")); rp = pool.get((norm, "pit"))
                primary = "bat" if (rb.rank or 9999) <= (rp.rank or 9999) else "pit"
            other = "pit" if primary == "bat" else "bat"
            new_uid = f"{uid}_{other}"
            row_o = pool.get((norm, other))
            row_p = pool.get((norm, primary))
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
    _splits_path().write_text(json.dumps(splits, indent=1, sort_keys=True))
    return {"split": len(fixed), "registry": len(splits)}


if __name__ == "__main__":
    audit()
