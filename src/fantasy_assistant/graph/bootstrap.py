"""Create uniqueness constraints and indexes per SCHEMA.md. Idempotent."""
from __future__ import annotations

from fantasy_assistant.graph.client import session

# Labels whose `uid` must be unique. One constraint per label also gives us a
# backing index for MERGE-by-uid, which every ingestion path relies on.
UID_LABELS = [
    "League", "Season", "ScoringPeriod", "Category", "FantasyTeam", "Manager",
    "Player", "MLBTeam", "MLBGame",
    "PlayerDayLine", "PlayerPeriodLine", "StatcastProfile", "ProbableStart",
    "InjuryEvent", "InjuryStint", "RoleState", "MinorsStint",
    "PositionGameCount", "EligibilityState", "NewsItem", "Signal",
    "TransactionEvent", "TradeEvent", "RosterStint", "LineupAssignment",
    "DraftPick", "WaiverOrderSnapshot", "StandingsSnapshot",
    "CategoryStandingLine", "FreeAgentPoolSnapshot", "PoolEntry",
    "AnalyticsRun", "ProjectionSet", "PlayerProjection", "TeamStateAssessment",
    "RaceAnalysis", "PortfolioPosture", "PlayerValuation",
    "StandingsSimulation", "TendencyBelief", "RivalNeedsAssessment",
    "Brief", "Alert", "Recommendation", "DecisionRecord", "OutcomeReview",
    "CaptureRun", "ReconciliationRun", "Discrepancy",
]

RANGE_INDEXES = [
    ("Player", "name_normalized"),
    ("Player", "cbs_id"),
    ("Player", "mlbam_id"),
    ("TransactionEvent", "posted_at"),
    ("TransactionEvent", "effective_date"),
    ("PlayerDayLine", "date"),
    ("RosterStint", "from_date"),
    ("RosterStint", "to_date"),
    ("StandingsSnapshot", "as_of"),
    ("FreeAgentPoolSnapshot", "as_of"),
    ("NewsItem", "published_at"),
    ("Recommendation", "created_at"),
]


def bootstrap() -> None:
    with session() as s:
        for label in UID_LABELS:
            s.run(
                f"CREATE CONSTRAINT {label.lower()}_uid IF NOT EXISTS "
                f"FOR (n:{label}) REQUIRE n.uid IS UNIQUE"
            )
        for label, prop in RANGE_INDEXES:
            s.run(
                f"CREATE INDEX {label.lower()}_{prop} IF NOT EXISTS "
                f"FOR (n:{label}) ON (n.{prop})"
            )
        count = s.run("SHOW CONSTRAINTS YIELD name RETURN count(*) AS c").single()["c"]
    print(f"bootstrap complete: {count} constraints present")


if __name__ == "__main__":
    bootstrap()
