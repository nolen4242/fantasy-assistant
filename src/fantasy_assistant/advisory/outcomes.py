"""Recommendation lifecycle: decision matching + outcome scoring.

Closes the advised -> done -> outcome loop from PROBLEM.md:
- match_decisions(): pair open Recommendations with the TransactionEvents
  that executed them (DecisionRecord, rec status -> adopted), and expire
  recommendations whose action window passed untouched.
- score_outcomes(): once a recommendation's period has closed, write an
  OutcomeReview comparing what the recommended player actually produced
  against the projection that justified the advice.

Run daily from the routine; both functions are idempotent.
"""
from __future__ import annotations

import json
from datetime import datetime

from fantasy_assistant.graph.client import session


def match_decisions() -> dict:
    ts = datetime.now().isoformat(timespec="seconds")
    with session() as s:
        # adds/claims: our transaction ADDS the recommended player after the rec
        adopted = s.run(
            """
            MATCH (rec:Recommendation {status:'open'})
            WHERE rec.kind IN ['add', 'claim']
            MATCH (rec)<-[:CONTAINS]-(b:Brief)-[:FOR_PERIOD]->(per:ScoringPeriod)
            MATCH (e:TransactionEvent)-[:BY_TEAM]->(:FantasyTeam {is_us:true}),
                  (e)-[:ADDS]->(p:Player)
            WHERE date(e.posted_at) >= per.start_date - duration('P2D')
              AND date(e.posted_at) <= per.end_date
              AND p.name_full = apoc.convert.fromJsonMap(rec.action_blob)['player']
            MERGE (d:DecisionRecord {uid: 'decision:' + rec.uid})
            SET d.decided_at = e.posted_at, d.matches_recommendation = 'full',
                d.recorded_at = datetime($ts)
            MERGE (d)-[:RESPONDS_TO]->(rec)
            MERGE (d)-[:EXECUTED_AS]->(e)
            SET rec.status = 'adopted'
            RETURN rec.uid AS rec, p.name_full AS player
            """,
            ts=ts,
        ).data()
        # expire: brief-period recs whose period has started (lock passed)
        expired = s.run(
            """
            MATCH (rec:Recommendation {status:'open'})<-[:CONTAINS]-(b:Brief)
                  -[:FOR_PERIOD]->(per:ScoringPeriod)
            WHERE per.start_date < date() AND rec.kind IN ['add', 'claim']
            SET rec.status = 'expired'
            RETURN count(rec) AS n
            """
        ).single()["n"]
    return {"adopted": [(a["rec"], a["player"]) for a in adopted],
            "expired": expired}


def score_outcomes() -> dict:
    ts = datetime.now().isoformat(timespec="seconds")
    with session() as s:
        latest_closed = s.run(
            "MATCH (st:StandingsSnapshot {scope:'period'})-[:FOR_PERIOD]->(p) "
            "RETURN max(p.number) AS n"
        ).single()["n"]
        rows = s.run(
            """
            MATCH (rec:Recommendation)<-[:CONTAINS]-(b:Brief)-[:FOR_PERIOD]->(per:ScoringPeriod)
            WHERE rec.status IN ['adopted', 'expired', 'declined']
              AND per.number <= $closed
              AND NOT (rec)<-[:EVALUATES]-(:OutcomeReview)
              AND rec.kind IN ['add', 'claim']
            MATCH (p:Player {name_full: apoc.convert.fromJsonMap(rec.action_blob)['player']})
            OPTIONAL MATCH (d:PlayerDayLine)-[:OF_PLAYER]->(p)
            WHERE d.date >= per.start_date AND d.date <= per.end_date
            WITH rec, per, p,
                 sum(coalesce(d.k, 0)) AS k, sum(coalesce(d.qs, 0)) AS qs,
                 sum(coalesce(d.w, 0)) AS w, sum(coalesce(d.sv, 0)) AS sv,
                 sum(coalesce(d.outs, 0)) AS outs, sum(coalesce(d.er, 0)) AS er,
                 sum(coalesce(d.hr, 0)) AS hr, sum(coalesce(d.r, 0)) AS r,
                 sum(coalesce(d.rbi, 0)) AS rbi, sum(coalesce(d.sb, 0)) AS sb
            RETURN rec.uid AS rec_uid, rec.status AS status,
                   rec.rationale AS rationale, per.number AS period,
                   p.name_full AS player, k, qs, w, sv, outs, er, hr, r, rbi, sb
            """,
            closed=latest_closed,
        ).data()
        reviews = []
        for r in rows:
            realized = {k: r[k] for k in
                        ("k", "qs", "w", "sv", "outs", "er", "hr", "r", "rbi", "sb")}
            # crude v1 score: did the followed advice produce? (pitcher lens:
            # K + 2*(QS+W) + 2*SV, ~one good 2-start week ≈ 12+)
            produced = (r["k"] + 2 * (r["qs"] + r["w"]) + 2 * r["sv"]
                        + 0.5 * (r["hr"] + r["sb"]))
            score = round(min(1.0, produced / 12.0) * (1 if r["status"] == "adopted" else 0)
                          - (0.3 if r["status"] == "adopted" and produced < 3 else 0), 2)
            s.run(
                """
                MATCH (rec:Recommendation {uid:$uid})
                MERGE (o:OutcomeReview {uid: 'outcome:' + $uid})
                SET o.reviewed_at = datetime($ts), o.horizon = '1p',
                    o.realized_blob = $blob, o.score = $score,
                    o.followed = $followed
                MERGE (o)-[:EVALUATES]->(rec)
                """,
                uid=r["rec_uid"], ts=ts, blob=json.dumps(realized),
                score=score, followed=r["status"] == "adopted",
            )
            reviews.append((r["player"], r["period"], r["status"], score, realized))
    return {"latest_closed_period": latest_closed, "reviews": reviews}


if __name__ == "__main__":
    print("decisions:", match_decisions())
    print("outcomes:", score_outcomes())
