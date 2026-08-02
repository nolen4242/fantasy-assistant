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


if __name__ == "__main__":
    audit()
