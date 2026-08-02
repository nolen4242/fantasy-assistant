"""Static-ish reference data for the 2026 Bob Uecker league season.

Period math: season starts Wed 2026-03-25; period 1 is the short Wed–Sun
period (3/25–3/29); periods 2..27 start Mondays (period 2 = 3/30). Anchor
verified against CBS: period 19 = Mon 7/27 – Sun 8/2, period 20 = 8/3–8/9.
"""
from __future__ import annotations

from datetime import date, timedelta

from fantasy_assistant.graph.client import session

LEAGUE_UID = "league:buecker"
SEASON_2026 = "season:buecker:2026"

CATEGORIES = [
    # code, kind, direction, side, components (for recomputation)
    ("HR", "counting", "higher", "bat", ["hr"]),
    ("OBP", "rate", "higher", "bat", ["h", "bb", "hbp", "ab", "sf"]),
    ("R", "counting", "higher", "bat", ["r"]),
    ("RBI", "counting", "higher", "bat", ["rbi"]),
    ("SB", "counting", "higher", "bat", ["sb"]),
    ("ERA", "rate", "lower", "pit", ["er", "outs"]),
    ("K", "counting", "higher", "pit", ["k"]),
    ("S", "counting", "higher", "pit", ["sv"]),
    ("WHIP", "rate", "lower", "pit", ["ha", "bbi", "outs"]),
    ("WQS", "counting", "higher", "pit", ["w", "qs"]),
]

# cbs_name -> (abbrev, manager)  [managers per league constitution/site]
TEAMS = {
    "Big Sticks": ("BST", "Drew Sticht"),
    "Dawg": ("DWG", None),
    "Gashouse Gang": ("GHG", "Rick Barnes"),
    "Guillotine": ("GTN", "Joe Rieken"),
    "Like a Nightmare": ("LAN", "Bobby McCann"),
    "Long Balls": ("LB", "Justin Palmer"),
    "Maga Doge": ("MD", None),
    "Magnum GI": ("MGI", "Gary Ihms"),
    "Rieken Havoc": ("RHV", "Erik Rieken"),
    "Runtime Terror": ("RTT", "Nolen Smith"),
    "Simba's Dublin Green Sox": ("SIM", "Jerry Balmer"),
    "Trex": ("TRX", "Justin Trexler"),
    "Young Guns": ("YGN", "Neil Rieken"),
}

OUR_TEAM = "Runtime Terror"


def team_uid(cbs_name: str) -> str:
    return "team:buecker:" + cbs_name.lower().replace(" ", "_").replace("'", "")


def period_dates(number: int) -> tuple[date, date]:
    if number == 1:
        return date(2026, 3, 25), date(2026, 3, 29)
    start = date(2026, 3, 30) + timedelta(days=7 * (number - 2))
    return start, start + timedelta(days=6)


def period_for_date(d: date) -> int:
    if d <= date(2026, 3, 29):
        return 1
    return 2 + (d - date(2026, 3, 30)).days // 7


def load_refdata() -> None:
    with session() as s:
        s.run(
            "MERGE (l:League {uid:$uid}) SET l.name=$name, l.cbs_slug='buecker', l.platform='cbs'",
            uid=LEAGUE_UID, name="Bob Uecker Imaginary Baseball League",
        )
        s.run(
            """
            MATCH (l:League {uid:$luid})
            MERGE (se:Season {uid:$uid})
            SET se.year=2026, se.start_date=date('2026-03-25'),
                se.draft_date=date('2026-03-18'), se.draft_rounds=23,
                se.trade_deadline=date('2026-08-24'),
                se.ip_min=1000, se.ip_max=1300,
                se.fee_add=2.50, se.fee_trade=2.50
            MERGE (se)-[:OF_LEAGUE]->(l)
            """,
            luid=LEAGUE_UID, uid=SEASON_2026,
        )
        for n in range(1, 28):
            start, end = period_dates(n)
            s.run(
                """
                MATCH (se:Season {uid:$suid})
                MERGE (p:ScoringPeriod {uid:$uid})
                SET p.number=$n, p.start_date=date($start), p.end_date=date($end),
                    p.is_final=($n=27)
                MERGE (p)-[:IN_SEASON]->(se)
                """,
                suid=SEASON_2026, uid=f"period:2026:{n}", n=n,
                start=start.isoformat(), end=end.isoformat(),
            )
        for code, kind, direction, side, components in CATEGORIES:
            s.run(
                """
                MERGE (c:Category {uid:$uid})
                SET c.code=$code, c.kind=$kind, c.direction=$direction,
                    c.side=$side, c.components=$components
                """,
                uid=f"category:{code}", code=code, kind=kind,
                direction=direction, side=side, components=components,
            )
        for name, (abbrev, manager) in TEAMS.items():
            s.run(
                """
                MATCH (se:Season {uid:$suid})
                MERGE (t:FantasyTeam {uid:$uid})
                SET t.cbs_name=$name, t.abbrev=$abbrev, t.is_us=$is_us
                MERGE (t)-[:COMPETES_IN]->(se)
                """,
                suid=SEASON_2026, uid=team_uid(name), name=name,
                abbrev=abbrev, is_us=(name == OUR_TEAM),
            )
            if manager:
                s.run(
                    """
                    MATCH (t:FantasyTeam {uid:$tuid})
                    MERGE (m:Manager {uid:$uid}) SET m.name=$name
                    MERGE (m)-[:MANAGES]->(t)
                    """,
                    tuid=team_uid(name),
                    uid="manager:" + manager.lower().replace(" ", "_"), name=manager,
                )
    print("refdata loaded: league, season 2026, 27 periods, 10 categories, 13 teams")


if __name__ == "__main__":
    load_refdata()
