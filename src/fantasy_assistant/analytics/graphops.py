"""Graph algorithms (Neo4j GDS): similarity, sniping motifs, communities.

- write_features + knn(): per-week production vectors on rostered+relevant
  players -> gds.knn -> SIMILAR_TO edges. Powers "closest FA replacement"
  and trade-equivalence classes.
- waiver_sniping(): temporal motif — team B claims a player <=2 days after
  team A drops him; pair counts become blocking-claim priors.
- manager_communities(): louvain over the manager–player transaction
  bipartite (projected) -> archetype clusters.
"""
from __future__ import annotations

from collections import defaultdict

from fantasy_assistant.graph.client import session

FEATS_BAT = ["hr", "r", "rbi", "sb", "ob", "pa"]
FEATS_PIT = ["k", "sv", "wqs", "er", "outs", "wh"]


def write_features() -> dict:
    from fantasy_assistant.graph.refdata import period_for_date
    from datetime import date as _date
    weeks = float(max(1, period_for_date(_date.today()) - 1))
    with session() as s:
        n = s.run(
            """
            MATCH (p:Player) WHERE p.mlbam_id IS NOT NULL
            MATCH (d:PlayerDayLine)-[:OF_PLAYER]->(p)
            WITH p, d.side AS side,
                 sum(coalesce(d.hr,0))/$wk AS hr, sum(coalesce(d.r,0))/$wk AS r,
                 sum(coalesce(d.rbi,0))/$wk AS rbi, sum(coalesce(d.sb,0))/$wk AS sb,
                 sum(coalesce(d.h,0)+coalesce(d.bb,0)+coalesce(d.hbp,0))/$wk AS ob,
                 sum(coalesce(d.ab,0)+coalesce(d.bb,0)+coalesce(d.hbp,0)+coalesce(d.sf,0))/$wk AS pa,
                 sum(coalesce(d.k,0))/$wk AS k, sum(coalesce(d.sv,0))/$wk AS sv,
                 sum(coalesce(d.w,0)+coalesce(d.qs,0))/$wk AS wqs,
                 sum(coalesce(d.er,0))/$wk AS er, sum(coalesce(d.outs,0))/$wk AS outs,
                 sum(coalesce(d.ha,0)+coalesce(d.bbi,0))/$wk AS wh
            WITH p, side, [hr, r, rbi, sb, ob/10.0, pa/10.0] AS bat,
                 [k, sv, wqs, er, outs/10.0, wh/5.0] AS pit
            SET p.feat = CASE WHEN side='bat' THEN bat ELSE pit END,
                p.feat_side = side
            RETURN count(p) AS c
            """, wk=weeks,
        ).single()["c"]
    return {"features_written": n}


def knn(top_k: int = 6) -> dict:
    with session() as s:
        s.run("CALL gds.graph.drop('players', false)")
        s.run(
            """
            CALL gds.graph.project('players',
              {Player: {label: 'Player', properties: ['feat'],
                        nodeFilter: 'n.feat IS NOT NULL'}},
              '*')
            """
        ) if False else None
        # cypher projection (feat present only)
        s.run(
            """
            CALL gds.graph.project.cypher('players',
              'MATCH (p:Player) WHERE p.feat IS NOT NULL
               RETURN id(p) AS id, p.feat AS feat',
              'MATCH (a:Player)-[:OF_PLAYER]-(b) WHERE false RETURN 0 AS source, 0 AS target')
            """
        )
        s.run("MATCH ()-[r:SIMILAR_TO]->() DELETE r")
        res = s.run(
            """
            CALL gds.knn.write('players', {
              nodeProperties: ['feat'], topK: $k, similarityCutoff: 0.92,
              writeRelationshipType: 'SIMILAR_TO', writeProperty: 'score'})
            YIELD relationshipsWritten RETURN relationshipsWritten AS n
            """, k=top_k,
        ).single()["n"]
        s.run("CALL gds.graph.drop('players', false)")
    return {"similar_edges": res}


def similar_free_agents(player_name: str, limit: int = 6) -> list[dict]:
    with session() as s:
        return s.run(
            """
            MATCH (p:Player {name_normalized: $n})-[r:SIMILAR_TO]-(q:Player)
            WHERE NOT EXISTS {MATCH (st:RosterStint)-[:OF_PLAYER]->(q)
                              WHERE st.to_date IS NULL}
            RETURN q.name_full AS name, round(r.score, 3) AS score,
                   q.cbs_positions AS pos
            ORDER BY r.score DESC LIMIT $l
            """, n=player_name.lower(), l=limit,
        ).data()


def waiver_sniping() -> list[dict]:
    with session() as s:
        rows = s.run(
            """
            MATCH (d:TransactionEvent)-[:BY_TEAM]->(ta:FantasyTeam),
                  (d)-[:DROPS]->(p:Player)<-[ad:ADDS]-(a:TransactionEvent),
                  (a)-[:BY_TEAM]->(tb:FantasyTeam)
            WHERE ta <> tb AND a.posted_at > d.posted_at
              AND a.posted_at < d.posted_at + duration('P3D')
            RETURN ta.cbs_name AS victim, tb.cbs_name AS sniper, count(*) AS n
            ORDER BY n DESC
            """
        ).data()
        for r in rows:
            s.run(
                """
                MATCH (a:FantasyTeam {cbs_name:$v}), (b:FantasyTeam {cbs_name:$s})
                MERGE (a)-[r:SNIPED_BY]->(b) SET r.n = $n
                """, v=r["victim"], s=r["sniper"], n=r["n"])
    return rows


def manager_communities() -> list[dict]:
    with session() as s:
        s.run("CALL gds.graph.drop('txn', false)")
        # team-team projection: an edge when two teams transacted the SAME
        # player (bipartite louvain was degenerate — teams rarely overlap
        # except through churn chains, which is exactly the signal)
        s.run(
            """
            CALL gds.graph.project.cypher('txn',
              'MATCH (t:FantasyTeam) RETURN id(t) AS id',
              'MATCH (ta:FantasyTeam)<-[:BY_TEAM]-(:TransactionEvent)-[:ADDS|DROPS]->(p:Player)
                     <-[:ADDS|DROPS]-(:TransactionEvent)-[:BY_TEAM]->(tb:FantasyTeam)
               WHERE ta <> tb
               RETURN id(ta) AS source, id(tb) AS target, count(DISTINCT p) AS weight')
            """
        )
        rows = s.run(
            """
            CALL gds.louvain.stream('txn', {relationshipWeightProperty: null}) YIELD nodeId, communityId
            WITH gds.util.asNode(nodeId) AS n, communityId
            WHERE n:FantasyTeam
            RETURN communityId AS c, collect(n.cbs_name) AS teams
            """
        ).data()
        s.run("CALL gds.graph.drop('txn', false)")
    return rows


if __name__ == "__main__":
    print(write_features())
    print(knn())
    print("Kwan-like FAs:", similar_free_agents("steven kwan"))
    print("sniping:", waiver_sniping()[:6])
    print("communities:", manager_communities())


def stamp_signal_fa() -> int:
    """Single source of truth for Signal.fa: unrostered = no open stint.

    Generators must not each own this flag (velocity/mix signals shipped
    without it and fa-filtered queries silently missed them). Run after any
    signal generation pass.
    """
    with session() as s:
        return s.run(
            """
            MATCH (sig:Signal)-[:ABOUT]->(p:Player)
            SET sig.fa = NOT EXISTS {
                MATCH (st:RosterStint)-[:OF_PLAYER]->(p) WHERE st.to_date IS NULL
            }
            RETURN count(sig) AS n
            """
        ).single()["n"]
