"""Published composite of the deployed ontology.

The ask-box agent needs the REAL schema — labels, properties, relationship
triples, rel properties — not a hand-maintained hint that drifts every time
ingestion changes (published_at vs first_seen, invented BATTING edges, etc.).

build() introspects the live graph via apoc.meta and db.schema, merges the
curated semantic notes below (things introspection cannot know: formulas,
direction conventions, scope meanings), and publishes:
  - docs/ONTOLOGY.md            human-readable, committed with the repo
  - ~/.fantasy-assistant/ontology-prompt.txt   compact block for the agent

The UI reloads the prompt block by mtime, so re-running build() after any
schema-shaped change updates the agent with no restart. Regenerated daily by
the routine.
"""
from __future__ import annotations

from pathlib import Path

from fantasy_assistant.graph.client import session

REPO = Path(__file__).resolve().parents[3]
PROMPT_PATH = Path.home() / ".fantasy-assistant" / "ontology-prompt.txt"

# semantics introspection cannot derive — keep these terse and factual
CURATED = """SEMANTICS (curated, verified):
- ALL data edges point INTO Player: (x)-[:OF_PLAYER|ABOUT|SELECTED|ADDS|DROPS|MOVES]->(p:Player).
- rostered = open RosterStint (st.to_date IS NULL) with ANY status (active/il/minors).
  Free agent = NO open stint. Every Signal carries sig.fa (true = free agent).
- StandingsSnapshot scopes: 'period' (one period, FOR_PERIOD), 'cumulative'
  (season THROUGH a period, THROUGH_PERIOD), 'ytd' (current, latest only).
  Points live ON the OVERALL relationship: o.total, o.batting, o.pitching, o.rank.
- Formulas from PlayerDayLine sums: ERA=er*27/outs, WHIP=(ha+bbi)*3/outs, IP=outs/3,
  OBP=(h+bb+hbp)/(ab+bb+hbp+sf). Pitchers: PlayerDayLine.side='pit'.
- e.kinds on TransactionEvent is a LIST -> use 'trade' IN e.kinds.
- Trades are logged ONE-SIDED per team: each side is its own event with ONLY
  [:ADDS] edges (r.action='trade', r.trade_from = the counterparty team name).
  There is NO DROPS edge on a trade. List trades:
  MATCH (e:TransactionEvent)-[:BY_TEAM]->(t), (e)-[r:ADDS]->(p)
  WHERE 'trade' IN e.kinds RETURN e.posted_at, t.cbs_name, p.name_full, r.trade_from.
- NewsItem time = n.first_seen (datetime). No published_at.
- Eligibility windows: PositionGameCount g.games >= 15 AND g.position NOT IN
  split(p.cbs_positions, ',').
- Signal kinds: hot_bat cold_bat hot_arm cold_arm contact_hot contact_cold
  velocity_up velocity_down csw_up csw_down mix_change buy_low sell_high
  speed_decline. sig.strength, sig.as_of, sig.rationale, sig.results_based.
- Rel props: OVERALL{total,batting,pitching,rank} SIMILAR_TO{score} SNIPED_BY{n}
  ADDS/DROPS/MOVES{action,via_waivers,trade_from}.
- Teams: Runtime Terror (is_us:true), Rieken Havoc, Young Guns, Big Sticks,
  Maga Doge, Like a Nightmare, Dawg, Guillotine, Long Balls, Gashouse Gang,
  Magnum GI, Simba's Dublin Green Sox, Trex."""


def _introspect() -> tuple[dict, list[tuple[str, str, str]], dict]:
    """-> (label -> {prop: type}, [(src, rel, dst)], rel -> {prop: type})."""
    with session() as s:
        node_props: dict[str, dict[str, str]] = {}
        rel_props: dict[str, dict[str, str]] = {}
        for r in s.run(
            "CALL apoc.meta.data() YIELD label, property, type, elementType, other "
            "RETURN label, property, type, elementType, other"
        ).data():
            if r["type"] == "RELATIONSHIP":
                continue  # covered by db.schema.visualization
            tgt = node_props if r["elementType"] == "node" else rel_props
            tgt.setdefault(r["label"], {})[r["property"]] = r["type"]
        rec = s.run("CALL db.schema.visualization()").single()
        by_id = {n.element_id: list(n.labels)[0] for n in rec["nodes"]}
        triples = sorted({
            (by_id[e.start_node.element_id], e.type, by_id[e.end_node.element_id])
            for e in rec["relationships"]})
        counts = {r["l"]: r["c"] for r in s.run(
            "MATCH (n) UNWIND labels(n) AS l RETURN l AS l, count(*) AS c").data()}
    for lbl in node_props:
        node_props[lbl]["__count__"] = counts.get(lbl, 0)
    return node_props, triples, rel_props


def build() -> dict:
    node_props, triples, rel_props = _introspect()

    md = ["# Deployed ontology (generated — do not edit)\n",
          "Regenerate: `.venv/bin/python -m fantasy_assistant.graph.ontology`\n"]
    md.append("\n## Node labels\n")
    for lbl in sorted(node_props):
        props = node_props[lbl]
        n = props.pop("__count__", 0)
        plist = ", ".join(f"{k}:{v}" for k, v in sorted(props.items()))
        md.append(f"- **{lbl}** ({n:,}) — {plist}\n")
    md.append("\n## Relationships (src -> dst)\n")
    for src, rel, dst in triples:
        extra = ""
        if rel in rel_props:
            extra = " {" + ", ".join(f"{k}:{v}" for k, v in sorted(rel_props[rel].items())) + "}"
        md.append(f"- ({src})-[:{rel}]->({dst}){extra}\n")
    md.append("\n## Semantics\n" + CURATED + "\n")
    (REPO / "docs" / "ONTOLOGY.md").write_text("".join(md))

    # compact prompt block: triples + per-label props + curated semantics
    lines = ["SCHEMA (generated from the live graph):",
             "Triples: " + "; ".join(f"({s})-[:{r}]->({d})" for s, r, d in triples)]
    for lbl in sorted(node_props):
        props = {k: v for k, v in node_props[lbl].items() if k != "__count__"}
        lines.append(f"{lbl}: " + ", ".join(sorted(props)))
    for rel in sorted(rel_props):
        lines.append(f"rel {rel}: " + ", ".join(sorted(rel_props[rel])))
    lines.append(CURATED)
    PROMPT_PATH.parent.mkdir(exist_ok=True)
    PROMPT_PATH.write_text("\n".join(lines))
    return {"labels": len(node_props), "triples": len(triples),
            "prompt_chars": PROMPT_PATH.stat().st_size}


if __name__ == "__main__":
    print(build())
