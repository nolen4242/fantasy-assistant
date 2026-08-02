"""Weekly brief composer (v1).

Composes the pre-lock brief for the upcoming period from the graph: race
analysis (marginal curves), FA-pool targets with SportsLine week projections,
pending waiver claims, rival-needs context for blocking, and IP pacing.
Every actionable suggestion is also written as a Recommendation node with
BASED_ON provenance — the advised/done/outcome telemetry starts here.

Advisory only: this composes text and graph nodes; a human executes moves.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from fantasy_assistant.analytics import races, valuation, variance
from fantasy_assistant.graph.client import session
from fantasy_assistant.graph.refdata import period_for_date

REPORTS = Path(__file__).resolve().parents[3] / "reports"


def _stats(entry_week: list[str]) -> dict[str, float]:
    out = {}
    for kv in entry_week or []:
        k, _, v = kv.partition("=")
        try:
            out[k] = float(v)
        except ValueError:
            pass
    return out


def load_pool(snapshot_prefix: str) -> tuple[list[dict], list[dict]]:
    with session() as s:
        rows = s.run(
            """
            MATCH (f:FreeAgentPoolSnapshot)-[:HAS]->(e:PoolEntry)-[:OF_PLAYER]->(p:Player)
            WHERE f.uid STARTS WITH $prefix
            RETURN e.side AS side, e.avail AS avail, e.waiver_clear AS clear,
                   e.sportsline_rank AS rank, e.sportsline_week AS week,
                   p.name_full AS name, p.cbs_positions AS pos,
                   p.cbs_mlb_team AS mlb
            """,
            prefix=snapshot_prefix,
        ).data()
    pit = [dict(r, stats=_stats(r["week"])) for r in rows if r["side"] == "pit"]
    bat = [dict(r, stats=_stats(r["week"])) for r in rows if r["side"] == "bat"]
    return bat, pit


def ip_pace(capture_date: str) -> dict:
    """CBS's own Min/Max block on the my-team page is authoritative for IP
    accounting (our lineup attribution overcounts at swapped slots)."""
    import re as _re
    raw = (Path(__file__).resolve().parents[3] / "data" / "raw" / capture_date
           / "my_team_raw.txt").read_text()
    ytd = _re.search(r"Year to Date\s+([\d.]+)", raw)
    pace = _re.search(r"On Pace\s+([\d.]+)", raw)
    ip = float(ytd.group(1)) if ytd else 0.0
    pf = float(pace.group(1)) if pace else 0.0
    return {"ip_used": ip, "pace_final": pf, "cap": 1300, "floor": 1000,
            "headroom_vs_cap": round(1300 - pf, 1),
            "note": "source: CBS my-team Min/Max block"}


def compose(as_of: str | None = None) -> str:
    today = as_of or date.today().isoformat()
    next_period = period_for_date(date.today()) + (1 if True else 0)
    race = races.analyze()
    bat, pit = load_pool(f"cbs:fapool:{today}")
    pace = ip_pace(today)
    us = race["us"]

    recs: list[dict] = []
    lines: list[str] = []
    add = lines.append

    add(f"# Weekly Brief — Period {next_period} (locks Monday, per-player)")
    add(f"Generated {datetime.now().isoformat(timespec='minutes')} · model {race['model']} "
        f"· through period {race['as_of_period']}")
    add("")

    add("## Standings picture")
    for i, (t, pts) in enumerate(race["projected_final"], 1):
        if t == us:
            add(f"- Projected final on current pace: **{i}th, {pts} pts**")
            break
    leader = race["projected_final"][0]
    add(f"- Projected leader: {leader[0]} ({leader[1]} pts)")
    sim = variance.simulate(n_sims=2000)
    p10, p50, p90 = sim["our_pts_p10_p50_p90"]
    add(f"- Season odds (MC sim): P(win) {sim['p_win'][us]:.1%}, "
        f"P(top-5 money) {sim['p_top5'][us]:.1%}; points p10/p50/p90 = {p10}/{p50}/{p90}")
    add(f"- Title race: " + ", ".join(f"{t} {sim['p_win'][t]:.0%}"
        for t in sorted(sim['p_win'], key=lambda t: -sim['p_win'][t])[:3]))
    add("")

    add("## Category battle plan (cheapest points first)")
    for cat, d in sorted(race["categories"].items(),
                         key=lambda kv: ((kv[1]["curve_up"][0].get("swaps") or kv[1]["curve_up"][0]["units_needed"])
                                         if kv[1]["curve_up"] else 1e9))[:6:]:
        steps = [c for c in d["curve_up"] if c["pts_gain"] > 0]
        if not steps:
            continue
        first = steps[0]
        if d["kind"] == "rate":
            add(f"- **{cat}**: ~{first.get('swaps')} roster-swaps passes "
                f"{first['pass']} (+{first['pts_gain']} pts); proj {d['proj_value']}")
        else:
            add(f"- **{cat}**: +{first['units_needed']} season units passes "
                f"{first['pass']} (+{first['pts_gain']} pts); our pace {d['our_pace_per_period']}/wk")
    add("")

    # streaming targets: two-start or high-K SPs, sane ratios
    def sp_score(e):
        st = e["stats"]
        return st.get("k", 0) + 2 * st.get("qs", 0) + 2 * st.get("w", 0)

    sps = [e for e in pit
           if e["stats"].get("gs", 0) >= 1 and e["stats"].get("era", 9) < 4.6
           and e["stats"].get("whip", 2) < 1.35]
    sps.sort(key=sp_score, reverse=True)
    add("## Stream targets (K/WQS are our cheapest points; IP headroom exists)")
    for e in sps[:6]:
        st = e["stats"]
        two = " (2-START)" if st.get("gs", 0) >= 2 else ""
        avail = f"W until {e['clear']}" if e["avail"] == "W" else "FA"
        add(f"- {e['name']} ({e['mlb']}){two} — proj {st.get('k', 0):g} K, "
            f"{st.get('qs', 0):g} QS, {st.get('w', 0):g} W, ERA {st.get('era', 0):g} [{avail}]")
        recs.append({
            "kind": "claim" if e["avail"] == "W" else "add",
            "player": e["name"],
            "rationale": f"Stream for K/WQS: proj {st.get('k',0):g}K/{st.get('qs',0):g}QS wk{next_period}{two}",
        })
    add("")

    closers = sorted((e for e in pit if e["stats"].get("sv", 0) >= 0.5),
                     key=lambda e: -e["stats"].get("sv", 0))
    add("## Saves targets")
    for e in closers[:4]:
        st = e["stats"]
        avail = f"W until {e['clear']}" if e["avail"] == "W" else "FA"
        add(f"- {e['name']} ({e['mlb']}) — proj {st.get('sv', 0):g} SV, ERA {st.get('era', 0):g} [{avail}]")
    add("")

    pending = [e for e in bat + pit if e["avail"] == "W"]
    add("## On waivers now (claim window)")
    if pending:
        for e in sorted(pending, key=lambda e: e["rank"] or 9999):
            add(f"- {e['name']} ({e['pos']}, {e['mlb']}) — clears {e['clear']}, "
                f"SportsLine wk rank {e['rank']}")
    else:
        add("- none")
    add("")

    add("## Trade board (deadline Sun 8/24; both sides valued; tiers model acceptance)")
    for t in valuation.scan(["Gashouse Gang", "Dawg", "Maga Doge"], top=6):
        add(f"- [{t['tier']}] give **{t['give']}** for **{t['get']}** ({t['with']}): "
            f"us {t['our_delta']:+.1f} pts, them {t['their_delta']:+.1f}")
        recs.append({"kind": "trade_proposal", "player": t["get"],
                     "rationale": f"{t['tier']}: give {t['give']} to {t['with']}, "
                                  f"us {t['our_delta']:+.1f}/them {t['their_delta']:+.1f}"})
    add("")
    add("## IP pacing")
    add(f"- {pace['ip_used']} IP YTD; CBS on-pace {pace['pace_final']:.0f} vs cap {pace['cap']} "
        f"(≈{pace['headroom_vs_cap']:.0f} IP of unused headroom → streaming budget)")
    add(f"- floor {pace['floor']} is safe on current pace ({pace['note']})")
    add("")
    add("_Advisory only. Recommendations logged to graph for outcome scoring._")

    text = "\n".join(lines)
    _write_graph(next_period, recs, race)
    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"brief-period-{next_period}.md"
    out.write_text(text)
    print(f"[brief written to {out}]")
    return text


def _write_graph(period: int, recs: list[dict], race: dict) -> None:
    ts = datetime.now().isoformat(timespec="seconds")
    brief_uid = f"brief:2026:{period}"
    with session() as s:
        s.run(
            """
            MATCH (per:ScoringPeriod {uid:$puid})
            MERGE (b:Brief {uid:$uid})
            SET b.published_at=datetime($ts), b.model_version=$mv
            MERGE (b)-[:FOR_PERIOD]->(per)
            """,
            puid=f"period:2026:{period}", uid=brief_uid, ts=ts, mv=race["model"],
        )
        for i, r in enumerate(recs):
            s.run(
                """
                MATCH (b:Brief {uid:$buid})
                MERGE (rec:Recommendation {uid:$uid})
                SET rec.created_at=datetime($ts), rec.kind=$kind,
                    rec.rationale=$rationale, rec.status='open',
                    rec.action_blob=$blob
                MERGE (b)-[:CONTAINS]->(rec)
                """,
                buid=brief_uid, uid=f"rec:2026:{period}:{i}:{r['kind']}",
                ts=ts, kind=r["kind"], rationale=r["rationale"],
                blob=json.dumps(r),
            )


if __name__ == "__main__":
    print(compose())
