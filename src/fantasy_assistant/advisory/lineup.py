"""Recommended lineup for the next lock: which slot each player should fill.

Greedy assignment over the league's 21-slot template (C/1B/2B/3B/SS/MI/CI/
OF×4/U/P×10), specific slots before flex so scarce eligibility is spent
where it must be. Value = valuation.quality (per-week market-value proxy)
plus a bump for pitchers with probable starts in the period — the same
streaming logic the brief uses, applied to slotting.

Output is advisory: a slot map plus the delta vs the latest observed lineup
("moves you'd actually click").
"""
from __future__ import annotations

from datetime import date, timedelta

from fantasy_assistant.analytics.valuation import quality, roster_players
from fantasy_assistant.graph.client import session

SLOT_TEMPLATE = [("C", 1), ("1B", 1), ("2B", 1), ("3B", 1), ("SS", 1),
                 ("MI", 1), ("CI", 1), ("OF", 4), ("U", 1), ("P", 10)]
FLEX = {"MI": {"2B", "SS"}, "CI": {"1B", "3B"}, "U": None}  # None = any bat


def _eligible(pos_str: str | None, slot: str) -> bool:
    pos = set((pos_str or "").split(","))
    if slot == "P":
        return "P" in pos
    if "P" in pos and len(pos) == 1:
        return False
    if slot == "U":
        return True
    if slot in FLEX and FLEX[slot]:
        return bool(pos & FLEX[slot])
    return slot in pos


def recommend(team: str = "Runtime Terror") -> dict:
    players = roster_players(team)
    with session() as s:
        status = {r["p"]: r["s"] for r in s.run(
            """
            MATCH (t:FantasyTeam {cbs_name:$team})<-[:ON_TEAM]-(st:RosterStint)
                  -[:OF_PLAYER]->(p:Player)
            WHERE st.to_date IS NULL
            RETURN p.name_full AS p, st.status AS s
            """, team=team).data()}
        starts = {r["p"]: r["n"] for r in s.run(
            """
            MATCH (t:FantasyTeam {cbs_name:$team})<-[:ON_TEAM]-(st:RosterStint)
                  -[:OF_PLAYER]->(p:Player)<-[:OF_PLAYER]-(pr:ProbableStart)
            WHERE st.to_date IS NULL AND pr.date >= date($a) AND pr.date <= date($b)
            RETURN p.name_full AS p, count(pr) AS n
            """, team=team, a=date.today().isoformat(),
            b=(date.today() + timedelta(days=8)).isoformat()).data()}
        cur_rows = s.run(
            """
            MATCH (l:LineupAssignment {section:'active'})-[:IN_PERIOD]->(per:ScoringPeriod),
                  (l)-[:BY_TEAM]->(:FantasyTeam {cbs_name:$team}), (l)-[:FILLED_BY]->(p)
            WITH max(per.number) AS latest
            MATCH (l:LineupAssignment {section:'active'})-[:IN_PERIOD]->(per:ScoringPeriod {number: latest}),
                  (l)-[:BY_TEAM]->(:FantasyTeam {cbs_name:$team}), (l)-[:FILLED_BY]->(p)
            RETURN p.name_full AS p, l.slot AS slot, latest
            """, team=team).data()
    current = {r["p"]: r["slot"] for r in cur_rows}
    observed_period = cur_rows[0]["latest"] if cur_rows else None

    pool = []
    for p in players:
        if status.get(p["name"]) != "active":
            continue
        val = quality(p) + 0.6 * starts.get(p["name"], 0)
        pool.append({"name": p["name"], "pos": p["pos"], "val": round(val, 2)})

    # stickiness: a player already in this slot wins near-ties — advice that
    # shuffles equivalent players between slots is churn, not value
    STICKY = 0.75
    assigned: dict[str, list[dict]] = {}
    taken: set[str] = set()
    for slot, count in SLOT_TEMPLATE:
        cands = sorted((x for x in pool if x["name"] not in taken
                        and _eligible(x["pos"], slot)),
                       key=lambda x: -(x["val"] + (STICKY if current.get(x["name"]) == slot else 0)))
        assigned[slot] = cands[:count]
        taken.update(x["name"] for x in cands[:count])

    rows, moves = [], []
    for slot, count in SLOT_TEMPLATE:
        for x in assigned[slot]:
            was = current.get(x["name"])
            change = was != slot
            rows.append({"slot": slot, "player": x["name"], "pos": x["pos"],
                         "val": x["val"], "was": was or "bench", "change": change})
            if change:
                moves.append(f"{x['name']}: {was or 'bench'} → {slot}")
        for _ in range(count - len(assigned[slot])):
            rows.append({"slot": slot, "player": "(EMPTY)", "pos": "",
                         "val": None, "was": "", "change": True})
            moves.append(f"{slot}: no eligible player — fill via add/claim")
    bench = sorted((x for x in pool if x["name"] not in taken),
                   key=lambda x: -x["val"])
    for x in bench:
        was = current.get(x["name"])
        rows.append({"slot": "bench", "player": x["name"], "pos": x["pos"],
                     "val": x["val"], "was": was or "bench", "change": bool(was)})
        if was:
            moves.append(f"{x['name']}: {was} → bench")
    return {"rows": rows, "moves": moves, "vs_period": observed_period,
            "n_active": len(taken), "n_bench": len(bench)}


if __name__ == "__main__":
    r = recommend()
    print(f"vs observed period {r['vs_period']} — {len(r['moves'])} moves:")
    for m in r["moves"]:
        print("  ", m)
    for row in r["rows"]:
        flag = " *" if row["change"] else ""
        print(f"  {row['slot']:>5}  {row['player']:<24} {row['val'] if row['val'] is not None else '':>6}{flag}")
