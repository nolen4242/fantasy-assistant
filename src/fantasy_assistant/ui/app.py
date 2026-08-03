"""Local ops dashboard — health, bus, R&D, brief, integrity, graph visual.

Run: .venv/bin/python -m fantasy_assistant.ui.app  ->  http://localhost:8347
Installed as launchd agent com.fantasy-assistant.ui. Local only; binds
127.0.0.1; no cloud anything.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import date, datetime
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request

from fantasy_assistant.graph.client import read_session, session

app = Flask(__name__)
REPO = Path(__file__).resolve().parents[3]
BUS_LOG = Path.home() / ".fantasy-assistant" / "bus.log"


def _age_min(p: Path) -> float | None:
    return round((time.time() - p.stat().st_mtime) / 60, 1) if p.exists() else None


def _status(ok: bool, warn: bool = False) -> str:
    return "healthy" if ok else ("stale" if warn else "down")


@app.get("/static/vis-network.min.js")
def visjs():
    from flask import send_file
    return send_file(Path(__file__).parent / "vis-network.min.js")


@app.get("/api/overview")
def overview():
    out: dict = {"generated": datetime.now().isoformat(timespec="seconds")}
    # process health
    bus_age = _age_min(BUS_LOG.parent / "bus.heartbeat") or _age_min(BUS_LOG)
    raw_dirs = sorted(d.name for d in (REPO / "data/raw").iterdir() if d.is_dir())
    latest_raw = raw_dirs[-1] if raw_dirs else None
    try:
        with session() as s:
            neo = True
            caps = s.run(
                "MATCH (c:CaptureRun) RETURN c.agent AS a, max(c.capture_date) AS d "
                "ORDER BY a").data()
            recon = s.run(
                "MATCH (r:ReconciliationRun) RETURN r.uid AS uid, r.ran_at AS at, "
                "r.discrepancy_count AS n ORDER BY r.ran_at DESC LIMIT 1").data()
            alerts = s.run(
                "MATCH (a:Alert) RETURN a.raised_at AS at, a.source AS src, a.text AS t "
                "ORDER BY a.raised_at DESC LIMIT 12").data()
            evals = s.run(
                "MATCH (m:ModelEval) RETURN m.model AS model, m.stand_at AS T, "
                "m.pts_mae AS mae ORDER BY m.model, m.stand_at").data()
            srcscores = s.run(
                "MATCH (sc:SourceScore) RETURN sc.source AS s, sc.stat AS stat, "
                "sc.period AS p, sc.mae AS mae, sc.n AS n ORDER BY sc.period").data()
            recs = s.run(
                "MATCH (r:Recommendation) RETURN r.status AS s, count(*) AS c").data()
            outcomes = s.run("MATCH (o:OutcomeReview) RETURN count(o) AS n").single()["n"]
            labels = s.run(
                "MATCH (n) UNWIND labels(n) AS l RETURN l, count(*) AS c "
                "ORDER BY c DESC").data()
            orphans = {}
            for name, q in {
                "player_no_edges": "MATCH (p:Player) WHERE NOT (p)--() RETURN count(p) AS c",
                "dayline_no_player": "MATCH (d:PlayerDayLine) WHERE NOT (d)-[:OF_PLAYER]->() RETURN count(d) AS c",
                "lineup_no_player": "MATCH (l:LineupAssignment) WHERE NOT (l)-[:FILLED_BY]->() RETURN count(l) AS c",
                "news_no_player": "MATCH (n:NewsItem) WHERE NOT (n)-[:ABOUT]->() RETURN count(n) AS c",
                "open_discrepancies": "MATCH (d:Discrepancy {status:'open'}) RETURN count(d) AS c",
            }.items():
                orphans[name] = s.run(q).single()["c"]
            sigs = s.run(
                "MATCH (sig:Signal)-[:ABOUT]->(p:Player) RETURN sig.kind AS k, "
                "p.name_full AS n, sig.rationale AS r, sig.as_of AS d "
                "ORDER BY sig.as_of DESC LIMIT 10").data()
    except Exception as exc:
        neo = False
        caps, recon, alerts, evals, srcscores, recs, labels, orphans, sigs = \
            [], [], [], [], [], [], [], {"error": str(exc)}, []
        outcomes = 0
    briefs = sorted((REPO / "reports").glob("brief-period-*.md"))
    brief_text = briefs[-1].read_text() if briefs else "(no brief yet)"
    bus_tail = ""
    if BUS_LOG.exists():
        bus_tail = "\n".join(BUS_LOG.read_text().splitlines()[-25:])
    launch = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
    agents = {a: (a in launch) for a in
              ("com.fantasy-assistant.bus", "com.fantasy-assistant.ui")}
    out.update({
        "tiles": {
            "neo4j": _status(neo),
            "bus": _status(bus_age is not None and bus_age < 10,
                           bus_age is not None and bus_age < 40),
            "bus_age_min": bus_age,
            "snapshot": _status(latest_raw == date.today().isoformat(),
                                latest_raw is not None),
            "latest_snapshot": latest_raw,
            "reconcile": _status(bool(recon) and recon[0]["n"] == 0, bool(recon)),
            "reconcile_detail": ({"n": recon[0]["n"], "at": str(recon[0]["at"])} if recon else None),
            "launchd": agents,
        },
        "captures": [{"agent": c["a"], "last": str(c["d"])} for c in caps],
        "alerts": [{"at": str(a["at"]), "src": a["src"], "text": a["t"]} for a in alerts],
        "signals": [{"kind": s_["k"], "name": s_["n"], "why": s_["r"], "date": str(s_["d"])} for s_ in sigs],
        "rd": {"evals": evals, "source_scores": srcscores,
               "recommendations": {r["s"]: r["c"] for r in recs},
               "outcome_reviews": outcomes},
        "labels": labels[:18],
        "orphans": orphans,
        "brief": brief_text,
        "bus_tail": bus_tail,
    })
    return jsonify(out)


_MANAGER_CACHE: dict = {}


@app.get("/api/manager")
def manager():
    """One aggregation for the Manager view. races.analyze() dominates the
    cost, so the whole payload is cached for 10 minutes."""
    now = time.time()
    if _MANAGER_CACHE.get("at", 0) > now - 600:
        return jsonify(_MANAGER_CACHE["data"] | {"cached": True})
    from fantasy_assistant.advisory.brief import ip_pace
    from fantasy_assistant.analytics import races
    from fantasy_assistant.graph.refdata import next_open_period, period_dates

    race = races.analyze()
    us = race["us"]
    nxt = next_open_period()
    lock_date = period_dates(nxt)[0].isoformat()

    with session() as s:
        standings = s.run(
            "MATCH (st:StandingsSnapshot {scope:'ytd'})-[o:OVERALL]->(t:FantasyTeam) "
            "WITH st, o, t ORDER BY st.as_of DESC, o.total DESC "
            "WITH st, collect({team: t.cbs_name, total: o.total, rank: o.rank, "
            "  batting: o.batting, pitching: o.pitching, us: t.is_us}) AS rows "
            "RETURN rows LIMIT 1").single()["rows"]
        sim = s.run(
            "MATCH (sr:SimResult) RETURN sr.p_win AS pw, sr.p_top5 AS pt, "
            "sr.pts_p10 AS p10, sr.pts_p50 AS p50, sr.pts_p90 AS p90, "
            "toString(sr.as_of) AS d ORDER BY sr.as_of DESC LIMIT 1").data()
        alerts = s.run(
            "MATCH (a:Alert) WHERE a.raised_at >= datetime() - duration('P1D') "
            "RETURN count(a) AS n, max(a.raised_at) AS last").single()
        last_alert = s.run(
            "MATCH (a:Alert) RETURN a.text AS t ORDER BY a.raised_at DESC LIMIT 1"
        ).single()
        recs = s.run(
            """
            MATCH (r:Recommendation {status:'open'})<-[:CONTAINS]-(:Brief)
                  -[:FOR_PERIOD]->(p:ScoringPeriod {number:$n})
            RETURN r.kind AS kind,
                   apoc.convert.fromJsonMap(r.action_blob)['player'] AS player,
                   r.rationale AS why
            """, n=nxt).data()
        hazards = s.run(
            """
            MATCH (t:FantasyTeam {is_us:true})<-[:ON_TEAM]-(st:RosterStint)
                  -[:OF_PLAYER]->(p:Player)
            WHERE st.to_date IS NULL AND st.status IN ['il','minors']
            RETURN p.name_full AS player, st.status AS status
            ORDER BY st.status, p.name_full
            """).data()
        pulse = s.run(
            """
            MATCH (t:FantasyTeam {is_us:true})<-[:ON_TEAM]-(st:RosterStint)
                  -[:OF_PLAYER]->(p:Player)<-[:ABOUT]-(sig:Signal)
            WHERE st.to_date IS NULL AND sig.as_of >= date() - duration('P3D')
            WITH p, collect(DISTINCT sig.kind) AS kinds,
                 collect(DISTINCT sig.rationale)[0] AS why
            RETURN p.name_full AS player, kinds, why
            ORDER BY size(kinds) DESC LIMIT 10
            """).data()
        my_starts = s.run(
            """
            MATCH (t:FantasyTeam {is_us:true})<-[:ON_TEAM]-(st:RosterStint)
                  -[:OF_PLAYER]->(p:Player)<-[:OF_PLAYER]-(pr:ProbableStart)
            WHERE st.to_date IS NULL AND pr.date >= date()
            RETURN p.name_full AS player, collect(toString(pr.date)) AS dates
            ORDER BY player
            """).data()
        hot_fa = s.run(
            """
            MATCH (sig:Signal {fa:true})-[:ABOUT]->(p:Player)
            WHERE sig.kind IN ['hot_arm','hot_bat','contact_hot','velocity_up','csw_up']
              AND sig.as_of >= date() - duration('P2D')
            OPTIONAL MATCH (e:PoolEntry)-[:OF_PLAYER]->(p)
            WITH p, collect(DISTINCT sig.kind) AS kinds, min(e.sportsline_rank) AS rank
            WHERE rank < 400
            RETURN p.name_full AS player, kinds, rank ORDER BY rank LIMIT 10
            """).data()
        waivers = s.run(
            """
            MATCH (e:PoolEntry)-[:OF_PLAYER]->(p:Player) WHERE e.avail='W'
            RETURN p.name_full AS player, e.waiver_clear AS clears
            ORDER BY e.waiver_clear, p.name_full LIMIT 15
            """).data()
        snipers = s.run(
            "MATCH (:FantasyTeam)-[r:SNIPED_BY]->(t:FantasyTeam) "
            "RETURN t.cbs_name AS team, sum(r.n) AS n ORDER BY n DESC LIMIT 3").data()
        activity = s.run(
            """
            MATCH (e:TransactionEvent)-[:BY_TEAM]->(t:FantasyTeam)
            WHERE e.posted_at >= datetime() - duration('P2D')
            OPTIONAL MATCH (e)-[a:ADDS]->(p:Player)
            WITH t, e, collect(p.name_full) AS adds
            RETURN t.cbs_name AS team, count(e) AS moves,
                   [x IN collect(adds) WHERE size(x) > 0 | x[0]][..3] AS recent_adds
            ORDER BY moves DESC
            """).data()
        elig = s.run(
            """
            MATCH (g:PositionGameCount)-[:OF_PLAYER]->(p:Player)
            WHERE g.games >= 15
              AND NOT g.position IN split(coalesce(p.cbs_positions,''), ',')
            RETURN p.name_full AS player, g.position AS pos, g.games AS games
            ORDER BY g.games DESC LIMIT 8
            """).data()

    battle = []
    for cat, d in race["categories"].items():
        first = (d.get("curve_up") or [None])[0]
        battle.append({
            "cat": cat, "kind": d["kind"], "ytd_pts": d["ytd_pts"],
            "proj_pts": d["proj_pts"],
            "next_point": ({"pass": first["pass"],
                            "cost": (first.get("swaps") if d["kind"] == "rate"
                                     else round(first.get("units_needed") or 0, 1)),
                            "unit": "swaps" if d["kind"] == "rate" else "units",
                            "pts_gain": first["pts_gain"]} if first else None),
            "cushion": (round(d["cushion_down"], 2)
                        if d.get("cushion_down") is not None else None),
            "cushion_unit": d.get("cushion_unit"),
        })
    battle.sort(key=lambda b: (b["next_point"] is None,
                               (b["next_point"] or {}).get("cost") or 9e9))

    try:
        ip = ip_pace(sorted(p.name for p in (REPO / "data/raw").iterdir())[-1])
    except Exception as exc:
        ip = {"note": f"ip_pace failed: {exc}"}

    kinds_order = {"claim": 0, "add": 1, "drop": 2, "trade_proposal": 3}
    recs.sort(key=lambda r: kinds_order.get(r["kind"], 9))
    seen_p: set = set()
    dedup_recs = []
    for r in recs:  # one row per player, first (highest-priority) rationale
        if r["player"] in seen_p:
            continue
        seen_p.add(r["player"])
        dedup_recs.append(r)
    wire_recs = [r for r in dedup_recs if r["kind"] in ("claim", "add", "drop")]
    trade_recs = [r for r in dedup_recs if r["kind"] == "trade_proposal"]

    try:
        from fantasy_assistant.advisory.lineup import recommend as lineup_recommend
        lineup_plan = lineup_recommend(us)
    except Exception as exc:
        lineup_plan = {"rows": [], "moves": [], "error": str(exc)}

    data = {
        "as_of": datetime.now().isoformat(timespec="seconds"),
        "us": us, "next_period": nxt, "lock_date": lock_date,
        "standings": standings,
        "sim": (sim[0] if sim else None),
        "alerts": {"n_24h": alerts["n"], "latest": (last_alert or {}).get("t", "")},
        "ip": ip,
        "wire_recs": wire_recs[:12], "trade_recs": trade_recs[:8],
        "lineup": lineup_plan, "hazards": hazards,
        "battle": battle,
        "pulse": pulse, "my_starts": my_starts,
        "hot_fa": hot_fa, "waivers": waivers, "snipers": snipers,
        "activity": activity, "eligibility": elig,
        "projected_final": race["projected_final"],
    }
    _MANAGER_CACHE.update(at=now, data=data)
    return jsonify(data)


@app.get("/api/graph")
def graph_sample():
    with session() as s:
        rows = s.run(
            """
            MATCH (t:FantasyTeam {is_us:true})<-[:ON_TEAM]-(st:RosterStint)-[:OF_PLAYER]->(p:Player)
            WHERE st.to_date IS NULL
            OPTIONAL MATCH (p)<-[:ABOUT]-(sig:Signal)
            OPTIONAL MATCH (p)<-[:OF_PLAYER]-(pr:ProbableStart) WHERE pr.date >= date()
            RETURN t.cbs_name AS team, p.uid AS puid, p.name_full AS name,
                   st.status AS status, collect(DISTINCT sig.kind) AS sigs,
                   count(DISTINCT pr) AS starts
            """
        ).data()
    nodes = [{"id": "team", "label": rows[0]["team"] if rows else "us",
              "group": "team", "value": 30}]
    edges = []
    for r in rows:
        group = "il" if r["status"] in ("il", "minors") else (
            "signal" if r["sigs"] and r["sigs"][0] else "player")
        label = r["name"] + (" ⚡" + r["sigs"][0] if r["sigs"] and r["sigs"][0] else "") \
            + (f" ({r['starts']} starts)" if r["starts"] >= 2 else "")
        nodes.append({"id": r["puid"], "label": label, "group": group, "value": 10})
        edges.append({"from": "team", "to": r["puid"]})
    return jsonify({"nodes": nodes, "edges": edges})


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>fantasy-assistant ops</title>
<script src="/static/vis-network.min.js"></script>
<style>
 :root{--bg:#101418;--panel:#1a2027;--ink:#e6ebf0;--ink2:#93a1af;--muted:#5d6b78;
   --good:#3fb27f;--warn:#d9a03f;--crit:#d95f4c;--line:#2a323b}
 body{background:var(--bg);color:var(--ink);font:14px/1.45 -apple-system,system-ui,sans-serif;margin:0;padding:18px}
 h1{font-size:17px;margin:0 0 14px} h2{font-size:13px;color:var(--ink2);text-transform:uppercase;
   letter-spacing:.06em;margin:0 0 8px}
 .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(430px,1fr));gap:14px}
 .panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;overflow:auto}
 .tiles{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px}
 .tabs{display:flex;gap:2px;margin-bottom:14px}
 .tab{padding:6px 16px;border-radius:8px 8px 0 0;background:var(--panel);color:var(--ink2);
   font-size:13px;cursor:pointer;border:1px solid var(--line);border-bottom:none}
 .tab.on{color:var(--ink);border-bottom:2px solid #4f7ec2}
 .strip{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:14px}
 .stat{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
 .stat b{display:block;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:4px}
 .stat .v{font-size:22px;font-weight:600} .stat .d{font-size:12px;color:var(--ink2);margin-top:2px}
 .k{display:inline-block;padding:1px 7px;border-radius:9px;font-size:11px;margin-right:4px}
 .k.add{background:#1f3d31;color:#7fd6ac}.k.claim{background:#42391f;color:#e8c987}
 .k.hazard{background:#472a26;color:#f0a396}.k.trade_proposal{background:#3a2a47;color:#cfa3e8}
 .k.drop{background:#3d2a2a;color:#e8a3a3}
 .hot{color:var(--good)}.coldc{color:#7fb2e8}.warnc{color:var(--warn)}.critc{color:var(--crit)}
 .why{color:var(--ink2);font-size:12px}
 .ipbar{height:6px;border-radius:3px;background:#242c35;position:relative;margin-top:6px}
 .ipbar i{position:absolute;left:0;top:0;bottom:0;border-radius:3px;background:#4f7ec2}
 .tile{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 14px;min-width:130px}
 .tile b{display:block;font-size:11px;color:var(--ink2);text-transform:uppercase;letter-spacing:.05em}
 .tile span{font-size:15px}
 .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
 .healthy .dot{background:var(--good)} .stale .dot{background:var(--warn)} .down .dot{background:var(--crit)}
 pre{background:#0c1013;border:1px solid var(--line);border-radius:8px;padding:10px;font-size:12px;
   color:var(--ink2);white-space:pre-wrap;max-height:340px;overflow:auto;margin:0}
 table{border-collapse:collapse;width:100%;font-size:13px}
 td,th{padding:4px 8px;border-bottom:1px solid var(--line);text-align:left;color:var(--ink)}
 th{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.05em}
 #net{height:420px;background:#0c1013;border-radius:8px}
 .ts{color:var(--muted);font-size:12px}
</style></head><body>
<h1>fantasy-assistant <span class="ts" id="gen"></span></h1>
<div class="tabs">
 <div class="tab on" id="tab-mgr" onclick="showTab('mgr')">Manager</div>
 <div class="tab" id="tab-ops" onclick="showTab('ops')">Ops</div>
</div>
<div id="view-mgr">
 <div class="strip" id="mstrip"></div>
 <div class="grid" id="mgrid">
  <div class="panel"><h2 id="mlock">Lineup &amp; personal moves — set by the lock</h2>
    <div class="why" id="mmoves" style="margin-bottom:8px"></div>
    <table id="mlineup"></table></div>
  <div class="panel"><h2>Waiver wire — claims &amp; adds</h2><table id="mwire"></table>
    <div class="why" id="mrecnote" style="margin-top:8px"></div></div>
  <div class="panel"><h2>Trade desk</h2><table id="mtrades"></table></div>
  <div class="panel"><h2>Category battle plan — cheapest points first</h2>
    <table id="mbattle"></table><div class="why" style="margin-top:8px">races-v3 · swap = one roster spot's ROS volume at a realistic edge</div></div>
  <div class="panel"><h2>Roster pulse</h2><table id="mpulse"></table>
    <table id="mil" style="margin-top:10px"></table></div>
  <div class="panel"><h2>The wire</h2><table id="mfa"></table>
    <table id="mwaiv" style="margin-top:10px"></table>
    <div class="why" id="msnipe" style="margin-top:8px"></div></div>
  <div class="panel"><h2>League activity — last 48h</h2><table id="mact"></table></div>
  <div class="panel"><h2>Race detail — projected final</h2><table id="mrace"></table>
    <table id="melig" style="margin-top:10px"></table></div>
 </div>
</div>
<div id="view-ops" style="display:none">
<div class="tiles" id="tiles"></div>
<div class="grid">
 <div class="panel"><h2>Event bus (last alerts)</h2><table id="alerts"></table>
   <h2 style="margin-top:12px">bus.log tail</h2><pre id="buslog"></pre></div>
 <div class="panel"><h2>R&amp;D ledger</h2><table id="evals"></table>
   <h2 style="margin-top:12px">Source scoreboard · Calibration</h2><table id="srcs"></table>
   <pre id="calib" style="max-height:80px"></pre></div>
 <div class="panel"><h2>Latest brief</h2><pre id="brief" style="max-height:520px"></pre></div>
 <div class="panel"><h2>Scouting signals</h2><table id="signals"></table>
   <h2 style="margin-top:12px">Integrity (orphans / open discrepancies)</h2><table id="orphans"></table></div>
 <div class="panel"><h2>Graph
   <button onclick="net('schema')" style="background:#22303c;color:#cfd8e0;border:0;border-radius:6px;padding:3px 10px">whole schema</button>
   <button onclick="net('roster')" style="background:#22303c;color:#cfd8e0;border:0;border-radius:6px;padding:3px 10px">my roster</button></h2>
   <div id="net"></div></div>
 <div class="panel"><h2>Graph inventory · capture freshness</h2><table id="labels"></table>
   <table id="caps" style="margin-top:10px"></table></div>
</div>
</div>
<div class="grid" style="margin-top:14px">
 <div class="panel" style="grid-column:1/-1"><h2>Ask the graph
   <span class="ts">natural language → Cypher (claude) → table + graph</span></h2>
   <div style="display:flex;gap:8px;margin-bottom:8px">
     <input id="q" style="flex:1;background:#0c1013;border:1px solid var(--line);border-radius:8px;
       color:var(--ink);padding:8px" placeholder="e.g. which rival pitchers have a buy_low signal and a 2-start week coming?"/>
     <button onclick="ask()" style="background:#2a4a72;color:#fff;border:0;border-radius:8px;padding:8px 16px">Ask</button>
   </div>
   <pre id="cy" style="max-height:60px"></pre>
   <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
     <div style="overflow:auto;max-height:420px"><table id="askrows"></table></div>
     <div id="asknet" style="height:420px;background:#0c1013;border-radius:8px"></div>
   </div></div>
</div>
<script>
const esc = x => String(x==null?'':x).replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const S = s => `<span class="${s}"><span class="dot"></span>${s}</span>`;
async function refresh(){
 const d = await (await fetch('/api/overview')).json();
 document.getElementById('gen').textContent = 'updated ' + d.generated;
 const t = d.tiles;
 document.getElementById('tiles').innerHTML = [
  ['Neo4j', S(t.neo4j)],
  ['Bus', S(t.bus) + (t.bus_age_min!=null? ` <span class=ts>${t.bus_age_min}m ago</span>`:'')],
  ['Snapshot', S(t.snapshot) + ` <span class=ts>${t.latest_snapshot||''}</span>`],
  ['Reconcile', S(t.reconcile) + (t.reconcile_detail? ` <span class=ts>${t.reconcile_detail.n} diffs</span>`:'')],
  ['launchd bus', S(t.launchd['com.fantasy-assistant.bus']?'healthy':'down')],
 ].map(([k,v])=>`<div class="tile"><b>${k}</b><span>${v}</span></div>`).join('');
 const row=(cells,h)=>`<tr>${cells.map(c=>`<${h?'th':'td'}>${esc(c)}</${h?'th':'td'}>`).join('')}</tr>`;
 document.getElementById('alerts').innerHTML = row(['at','src','event'],1)+
   d.alerts.map(a=>row([a.at.slice(5,16),a.src,a.text.slice(0,90)])).join('');
 document.getElementById('buslog').textContent = d.bus_tail;
 document.getElementById('evals').innerHTML = row(['model','stand@','pts-MAE'],1)+
   d.rd.evals.map(e=>row([e.model,'p'+e.T,e.mae])).join('');
 document.getElementById('srcs').innerHTML = row(['source','stat','period','MAE','n'],1)+
   (d.rd.source_scores.length? d.rd.source_scores.map(s=>row([s.s,s.stat,'p'+s.p,s.mae,s.n])).join('')
    : row(['(first entry when a projected period closes)','','','','']));
 document.getElementById('calib').textContent =
   'recommendations: '+JSON.stringify(d.rd.recommendations)+'  outcome_reviews: '+d.rd.outcome_reviews;
 document.getElementById('brief').textContent = d.brief;
 document.getElementById('signals').innerHTML = row(['kind','player','why'],1)+
   d.signals.map(s=>row([s.kind,s.name,s.why])).join('');
 document.getElementById('orphans').innerHTML = row(['check','count'],1)+
   Object.entries(d.orphans).map(([k,v])=>row([k, v>0? `⚠ ${v}` : '0'])).join('');
 document.getElementById('labels').innerHTML = row(['label','nodes'],1)+
   d.labels.map(l=>row([l.l,l.c])).join('');
 document.getElementById('caps').innerHTML = row(['capture agent','last run'],1)+
   d.captures.map(c=>row([c.agent,c.last])).join('');
}
const OPTS = {nodes:{shape:'dot',font:{color:'#e6ebf0',size:11}},
   groups:{team:{color:'#4f7ec2'},player:{color:'#3fb27f'},meta:{color:'#4f7ec2'},
           il:{color:'#5d6b78'},signal:{color:'#d9a03f'}},
   edges:{color:{color:'#2a323b'},font:{color:'#5d6b78'}},
   physics:{stabilization:{iterations:150}}};
async function net(mode){
 const g = await (await fetch(mode==='roster' ? '/api/graph' : '/api/schema')).json();
 new vis.Network(document.getElementById('net'),
  {nodes:new vis.DataSet(g.nodes), edges:new vis.DataSet(g.edges)}, OPTS);
}
async function ask(){
 const q = document.getElementById('q').value;
 document.getElementById('cy').textContent = 'thinking…';
 const d = await (await fetch('/api/ask', {method:'POST',
   headers:{'Content-Type':'application/json'}, body:JSON.stringify({q})})).json();
 if(d.error){ document.getElementById('cy').textContent = d.error + (d.cypher? ' | '+d.cypher:''); return; }
 document.getElementById('cy').textContent = d.cypher + (d.elapsed_s? '  -- '+d.elapsed_s+'s'+(d.cached?' (cached)':''):'');
 const row=(cells,h)=>`<tr>${cells.map(c=>`<${h?'th':'td'}>${esc(c)}</${h?'th':'td'}>`).join('')}</tr>`;
 document.getElementById('askrows').innerHTML = row(d.columns,1)+
   d.rows.map(r=>row(d.columns.map(c=>r[c]))).join('');
 new vis.Network(document.getElementById('asknet'),
  {nodes:new vis.DataSet(d.graph.nodes), edges:new vis.DataSet(d.graph.edges)}, OPTS);
}
document.getElementById('q').addEventListener('keydown', e=>{if(e.key==='Enter')ask()});
function showTab(t){
 document.getElementById('view-mgr').style.display = t==='mgr'?'':'none';
 document.getElementById('view-ops').style.display = t==='ops'?'':'none';
 document.getElementById('tab-mgr').classList.toggle('on', t==='mgr');
 document.getElementById('tab-ops').classList.toggle('on', t==='ops');
}
async function manager(){
 const d = await (await fetch('/api/manager')).json();
 const rowh=(cells)=>`<tr>${cells.map(c=>`<th>${esc(c)}</th>`).join('')}</tr>`;
 const rowr=(cells)=>`<tr>${cells.map(c=>`<td>${c}</td>`).join('')}</tr>`; // pre-escaped cells only
 const us = (d.standings||[]).find(r=>r.us) || {};
 const sim = d.sim, ip = d.ip||{};
 const pct = x => x==null?'—':(100*x).toFixed(1)+'%';
 const ipw = ip.pace_final? Math.min(100, 100*ip.pace_final/1300).toFixed(0) : 0;
 document.getElementById('mstrip').innerHTML = [
  ['Standings', `<span class="v">${esc(us.total??'—')} <span style="font-size:14px;color:var(--ink2)">pts · #${esc(us.rank??'?')}</span></span>`,
    `bat ${esc(us.batting??'—')} · pit ${esc(us.pitching??'—')}`],
  ['Season odds', `<span class="v">${pct(sim?.pt)} <span style="font-size:14px;color:var(--ink2)">top-5</span></span>`,
    sim? `P(win) ${pct(sim.pw)} · pts p10/50/90: ${esc(sim.p10)}/${esc(sim.p50)}/${esc(sim.p90)}` : 'run variance.simulate_and_store()'],
  ['Next lock', `<span class="v warnc">${esc(d.lock_date)}</span>`, `period ${esc(d.next_period)} · claims process overnight`],
  ['IP pace', `<span class="v">${esc(ip.pace_final??'?')} <span style="font-size:14px;color:var(--ink2)">/ 1300</span></span><div class="ipbar"><i style="width:${ipw}%"></i></div>`,
    ip.headroom_vs_cap!=null? `${esc(ip.headroom_vs_cap)} IP streaming budget` : esc(ip.note||'')],
  ['Alerts 24h', `<span class="v">${esc(d.alerts.n_24h)}</span>`, esc((d.alerts.latest||'').slice(0,48))],
 ].map(([k,v,sub])=>`<div class="stat"><b>${k}</b>${v}<span class="d">${sub}</span></div>`).join('');

 document.getElementById('mlock').textContent = `Lineup & personal moves — set by ${d.lock_date} lock`;
 const lp = d.lineup || {rows:[],moves:[]};
 document.getElementById('mmoves').innerHTML = lp.moves.length?
   '<b>moves to click:</b> ' + lp.moves.map(esc).join('  ·  ') : 'no lineup changes recommended';
 let lastSlot = null;
 document.getElementById('mlineup').innerHTML = rowh(['slot','player','pos','val','from']) +
  lp.rows.map(r=>{
   const cls = r.player==='(EMPTY)'? ' class="critc"' : (r.change? ' style="background:#1f2733"' : '');
   const benchTag = r.slot==='bench';
   return `<tr${cls}><td>${benchTag?'<span class="why">bench</span>':`<b>${esc(r.slot)}</b>`}</td><td>${esc(r.player)}</td><td class="why">${esc(r.pos||'')}</td><td>${r.val==null?'':esc(r.val)}</td><td class="why">${r.change? esc(r.was)+' →' : ''}</td></tr>`;
  }).join('');
 const hz = d.hazards.map(h=>rowr([`<span class="k hazard">${esc(h.status)}</span>`, `<b>${esc(h.player)}</b>`, `<span class="why">IL/minors slot — check activation dates</span>`]));
 const wc = d.wire_recs.map(r=>rowr([`<span class="k ${esc(r.kind)}">${esc(r.kind)}</span>`, `<b>${esc(r.player)}</b>`, `<span class="why">${esc((r.why||'').slice(0,90))}</span>`]));
 document.getElementById('mwire').innerHTML = rowh(['','action','why']) + wc.join('') + hz.join('');
 document.getElementById('mrecnote').textContent = 'Adopt by making the move on CBS — the decision matcher confirms it automatically. Claims process overnight before the lock.';
 document.getElementById('mtrades').innerHTML = rowh(['','proposal','why']) +
  (d.trade_recs.length? d.trade_recs.map(r=>rowr([`<span class="k trade_proposal">trade</span>`, `<b>${esc(r.player)}</b>`, `<span class="why">${esc((r.why||'').slice(0,100))}</span>`])).join('')
   : rowr(['', '<span class="why">no positive trades under current projections</span>', '']));

 document.getElementById('mbattle').innerHTML = rowh(['cat','pts → proj','next point costs','cushion']) +
  d.battle.map(b=>{
   const np = b.next_point;
   const cost = np? `${np.unit==='swaps'? np.cost+' swaps' : '+'+np.cost+' '+b.cat} passes ${np.pass}` : '(leading)';
   const thin = b.cushion!=null && b.cushion_unit==='swaps' && b.cushion < 1;
   const cush = b.cushion==null? '' : (thin? `<span class="critc">⚠ ${b.cushion} ${b.cushion_unit}</span>` : `${b.cushion} ${b.cushion_unit}`);
   return rowr([`<b>${esc(b.cat)}</b>`, `${esc(b.ytd_pts)} → ${esc(b.proj_pts)}`, esc(cost), cush]);
  }).join('');

 document.getElementById('mpulse').innerHTML = rowh(['player','signals','why']) +
  d.pulse.map(x=>{
   const hotish = x.kinds.some(k=>/hot|_up|buy/.test(k)), coldish = x.kinds.some(k=>/cold|_down/.test(k));
   const cls = hotish&&!coldish? 'hot' : (coldish? 'coldc' : 'warnc');
   return rowr([esc(x.player), `<span class="${cls}">${esc(x.kinds.join(' · '))}</span>`, `<span class="why">${esc((x.why||'').slice(0,60))}</span>`]);
  }).join('');
 document.getElementById('mil').innerHTML = rowh(['IL / minors','my starts next 7d']) +
  rowr([`<span class="why">${esc(d.hazards.map(h=>h.player+(h.status==='minors'?' (min)':'')).join(', '))}</span>`,
        `<span class="why">${esc(d.my_starts.map(m=>m.player+' '+m.dates.map(x=>x.slice(5)).join(',')).join(' · '))}</span>`]);

 document.getElementById('mfa').innerHTML = rowh(['hot free agents','signals','SL rank']) +
  d.hot_fa.map(x=>rowr([`<b>${esc(x.player)}</b>`, `<span class="hot">${esc(x.kinds.join(' · '))}</span>`, esc(x.rank)])).join('');
 const bywaiv = {};
 d.waivers.forEach(w=>{(bywaiv[w.clears]=bywaiv[w.clears]||[]).push(w.player)});
 document.getElementById('mwaiv').innerHTML = rowh(['pending waivers','clears']) +
  Object.entries(bywaiv).map(([c,ps])=>rowr([esc(ps.join(' · ')), esc(c)])).join('');
 document.getElementById('msnipe').textContent = d.snipers.length?
  '⚠ snipe risk — fastest claimers: ' + d.snipers.map(x=>`${x.team} (${x.n})`).join(', ') : '';

 document.getElementById('mact').innerHTML = rowh(['team','moves','recent adds']) +
  d.activity.map(a=>rowr([esc(a.team), esc(a.moves), `<span class="why">${esc((a.recent_adds||[]).join(', '))}</span>`])).join('');

 document.getElementById('mrace').innerHTML = rowh(['#','team','proj pts']) +
  d.projected_final.map(([t,v],i)=>{
   const cls = t===d.us? ' class="warnc"' : '';
   return `<tr${cls}><td>${i+1}</td><td>${esc(t)}</td><td>${esc(Math.round(v*10)/10)}</td></tr>`;
  }).join('');
 document.getElementById('melig').innerHTML = rowh(['eligibility windows opening']) +
  rowr([`<span class="why">${esc(d.eligibility.map(e=>`${e.player} ${e.pos} (${e.games}g)`).join(' · '))}</span>`]);
}
manager();
refresh(); net('schema'); setInterval(refresh, 30000); setInterval(manager, 300000);
</script></body></html>"""


@app.get("/api/schema")
def schema_graph():
    with session() as s:
        rec = s.run("CALL db.schema.visualization()").single()
        counts = {r["l"]: r["c"] for r in s.run(
            "MATCH (n) UNWIND labels(n) AS l RETURN l, count(*) AS c").data()}
    nodes = [{"id": n.element_id, "label": f"{list(n.labels)[0]}\n{counts.get(list(n.labels)[0], 0):,}",
              "group": "meta", "value": max(8, min(40, (counts.get(list(n.labels)[0], 1)) ** 0.35))}
             for n in rec["nodes"]]
    edges = [{"from": r.start_node.element_id, "to": r.end_node.element_id,
              "label": r.type, "arrows": "to", "font": {"size": 8}}
             for r in rec["relationships"]]
    return jsonify({"nodes": nodes, "edges": edges})


SCHEMA_HINT = """NEO4J 5 SYNTAX: pattern expressions cannot introduce new variables —
WHERE NOT (p)-[:R]->(x:Label) is ILLEGAL. Use EXISTS/COUNT subqueries:
WHERE NOT EXISTS { MATCH (p)-[:R]->(x:Label) WHERE ... }.

HOT/COLD/TRENDING questions: use Signal nodes — (sig:Signal)-[:ABOUT]->(p:Player) with
sig.kind (hot_bat, cold_bat, hot_arm, cold_arm, contact_hot, contact_cold, velocity_up,
velocity_down, csw_up, csw_down, mix_change, buy_low, sell_high, speed_decline),
sig.strength, sig.as_of, and sig.fa=true meaning the player is a FREE AGENT (unrostered).
Every Signal carries sig.fa. Do not recompute hotness from raw game lines.

DATA SHAPES:
- e.kinds on TransactionEvent is a LIST — filter with 'trade' IN e.kinds (CONTAINS is
  for strings and silently matches nothing on lists).
- NewsItem has NO published_at/date: use n.first_seen (datetime), n.headline, n.body,
  n.age_at_capture (e.g. '2h' at capture time); (n:NewsItem)-[:ABOUT]->(p:Player).
- Position eligibility: (g:PositionGameCount)-[:OF_PLAYER]->(p:Player) with g.position,
  g.games, g.as_of. 'Close to eligibility' = g.games >= 15 AND g.position NOT IN
  split(p.cbs_positions, ','). No other filters needed.
- StandingsSnapshot points all live on the OVERALL relationship: o.total, o.batting,
  o.pitching, o.rank. There are NO BATTING/PITCHING relationships.

CYPHER CRAFT: matching extra edges MULTIPLIES rows — aggregating an
event/node property after such a match double-counts it. When summing properties of X,
collapse first (WITH DISTINCT x) or don't expand X's other edges. Example: fees live on
TransactionEvent: MATCH (e:TransactionEvent)-[:BY_TEAM]->(t) RETURN t.cbs_name,
sum(e.fee) — never also match (e)-[:ADDS|DROPS]->() in the same sum.

CRITICAL — relationship directions: ALL data edges point INTO Player:
(d:PlayerDayLine)-[:OF_PLAYER]->(p:Player), (v:PitcherGameVelo)-[:OF_PLAYER]->(p),
(b:BatterGameEV)-[:OF_PLAYER]->(p), (st:RosterStint)-[:OF_PLAYER]->(p),
(sig:Signal)-[:ABOUT]->(p), (e:PoolEntry)-[:OF_PLAYER]->(p), (n:NewsItem)-[:ABOUT]->(p).
When unsure of a direction, write the pattern UNDIRECTED: (a)-[:REL]-(b).
Formulas: ERA = sum(er)*27.0/sum(outs); WHIP = (sum(ha)+sum(bbi))*3.0/sum(outs);
OBP = sum(h+bb+hbp)/sum(ab+bb+hbp+sf); IP = outs/3.0.

Labels/properties: Player(name_full,name_normalized,mlbam_id,cbs_id,cbs_positions,
primary_position,woba,xwoba,luck_gap,xera,pit_luck_gap), FantasyTeam(cbs_name,abbrev,is_us),
RosterStint(status[active|il|minors],from_date,to_date NULL=current,acquired_via).
IMPORTANT: 'rostered/taken' = open stint (to_date IS NULL) with ANY status —
IL and minors players are still rostered; free agent = NO open stint at all.
Never filter status='active' when checking availability. Pitchers: primary_position='P' or cbs_positions CONTAINS 'P'.
Relationship PROPERTIES (invisible in triples): (StandingsSnapshot)-[o:OVERALL]->(FantasyTeam)
carries o.total (+ o.batting,o.pitching,o.rank on ytd/period scopes) — 'overall standings' =
MATCH snapshot-[o:OVERALL]->team RETURN team, o.total ORDER BY o.total DESC.
(Player)-[r:SIMILAR_TO]-(Player) has r.score. (FantasyTeam)-[r:SNIPED_BY]->(FantasyTeam) has r.n.
(TransactionEvent)-[r:ADDS|DROPS|MOVES]->(Player) has r.action, r.via_waivers, r.trade_from-[:ON_TEAM]->FantasyTeam,
-[:OF_PLAYER]->Player, LineupAssignment(slot,section[active|bench|injured|minors])-[:IN_PERIOD]->
ScoringPeriod(number,start_date,end_date), -[:BY_TEAM]->FantasyTeam, -[:FILLED_BY]->Player,
PlayerDayLine(date,side[bat|pit],hr,r,rbi,sb,k,sv,w,qs,outs,er,ha,bbi,ob? h,bb)-[:OF_PLAYER]->Player,
TransactionEvent(posted_at,effective_date,fee,kinds)-[:BY_TEAM]->FantasyTeam,-[:ADDS|DROPS|MOVES]->Player,
DraftPick(round,overall)-[:BY_TEAM]->,-[:SELECTED]->Player, StandingsSnapshot(scope) — THREE scopes: 'period' (one week's stats)
-[:FOR_PERIOD]->ScoringPeriod; 'cumulative' (standings THROUGH period N)
-[:THROUGH_PERIOD]->ScoringPeriod; 'ytd' (current). 'Standings after/through
period N' => scope 'cumulative' + THROUGH_PERIOD. Snapshot-[:HAS_LINE]->CategoryStandingLine(value_reported,points,rank)
-[:FOR_TEAM]->FantasyTeam,-[:IN_CATEGORY]->Category(code), PitcherGameVelo(date,ff_avg,whiff_pct,csw_pct,mix)-[:OF_PLAYER]->Player,
BatterGameEV(date,bbe,ev_mean,hardhit,barrels)-[:OF_PLAYER]->Player (per-game contact quality),
 Signal(kind,rationale,as_of,agent)-[:ABOUT]->Player — kind values:
velocity_up/velocity_down, csw_up/csw_down, mix_change, contact_hot/contact_cold,
hot_bat/cold_bat, hot_arm/cold_arm, buy_low/sell_high, speed_decline
(negative-ish: velocity_down, csw_down, contact_cold, cold_bat, cold_arm,
speed_decline, sell_high; positive-ish: the counterparts),
ProbableStart(date)-[:OF_PLAYER]->Player, Alert(raised_at,source,text), NewsItem(headline,body)
-[:ABOUT]->Player, PoolEntry(avail,sportsline_rank,side)-[:OF_PLAYER]->Player,
MlbStatusEvent(type_desc,date,description)-[:OF_PLAYER]->Player, Recommendation(kind,status,rationale),
ModelEval(model,stand_at,pts_mae), Brief-[:CONTAINS]->Recommendation."""

FORBIDDEN = re.compile(r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|LOAD|CALL\s+apoc\.(?!convert))\b", re.I)


_ASK_CACHE: dict = {}


_TRIPLES = None


def _schema_triples() -> set:
    """Authoritative (SrcLabel, REL, DstLabel) triples from the live DB —
    the ground truth the generator is held to."""
    global _TRIPLES
    if _TRIPLES is None:
        with session() as s:
            rec = s.run("CALL db.schema.visualization()").single()
            labels = {n.element_id: list(n.labels)[0] for n in rec["nodes"]}
            _TRIPLES = {(labels[r.start_node.element_id], r.type,
                         labels[r.end_node.element_id])
                        for r in rec["relationships"]}
    return _TRIPLES


_ONTOLOGY_CACHE: dict = {}


def _triples_text() -> str:
    """Published ontology composite (graph/ontology.py) — the agent's schema
    context. Reloaded by mtime; falls back to live triples if not built."""
    p = Path.home() / ".fantasy-assistant" / "ontology-prompt.txt"
    try:
        mt = p.stat().st_mtime
        if _ONTOLOGY_CACHE.get("mtime") != mt:
            _ONTOLOGY_CACHE.update(mtime=mt, text=p.read_text())
        return _ONTOLOGY_CACHE["text"]
    except OSError:
        return ("Valid relationship triples (ONLY these exist; direction matters):\n"
                + "\n".join(f"  (:{a})-[:{r}]->(:{b})"
                            for a, r, b in sorted(_schema_triples())))


_PAT = None


def _pattern_violations(cypher: str) -> list[str]:
    """Find directed patterns whose (src, rel, dst) triple doesn't exist.

    Resolves variable->label bindings across the WHOLE query first, so
    `MATCH (p:Player) ... MATCH (p)-[:X]->(q)` is checked too — invented
    relationships used to slip through whenever a node was re-used without
    its inline label.
    """
    import re as _re
    global _PAT
    if _PAT is None:
        _PAT = _re.compile(
            r"\(\s*(\w*)\s*(?::\s*(\w+))?[^)]*\)\s*(<-|-)\s*\[[^\]]*?:(\w+)[^\]]*\]\s*(->|-)\s*\(\s*(\w*)\s*(?::\s*(\w+))?[^)]*\)")
    triples = _schema_triples()
    known_rels = {t[1] for t in triples}
    # variable -> label from every labeled node anywhere in the query
    binds = dict(_re.findall(r"\(\s*(\w+)\s*:\s*(\w+)", cypher))
    out = []
    # manual scan so chained patterns (a)-[:X]->(t)-[:Y]->(s) yield BOTH hops:
    # restart the search at the shared middle node, which finditer would skip
    matches, pos = [], 0
    while True:
        m = _PAT.search(cypher, pos)
        if not m:
            break
        matches.append(m)
        pos = cypher.rfind("(", m.start(), m.end())
        if pos <= m.start():
            pos = m.end()
    for m in matches:
        va, la, larr, rel, rarr, vb, lb = m.groups()
        a = la or binds.get(va)
        b = lb or binds.get(vb)
        if rel not in known_rels:
            out.append(f"relationship :{rel} does not exist in the schema")
            continue
        if a is None or b is None:
            continue  # unresolvable endpoint — can't judge
        if larr == "<-":
            src, dst = b, a
        elif rarr == "->":
            src, dst = a, b
        else:
            continue  # undirected — always fine
        if (src, rel, dst) not in triples:
            hint = ""
            if (dst, rel, src) in triples:
                hint = f" (it points the other way: (:{dst})-[:{rel}]->(:{src}))"
            out.append(f"(:{src})-[:{rel}]->(:{dst}) is not a valid triple{hint}")
    return out


ASK_ENV = None


def _ask_env():
    global ASK_ENV
    if ASK_ENV is None:
        import os
        # extended thinking turns a 3s translation into a 45s one — cap it
        ASK_ENV = os.environ | {"MAX_THINKING_TOKENS": "0"}
    return ASK_ENV


@app.post("/api/ask")
def ask():
    import re as _re
    import subprocess as sp
    q = (request.get_json() or {}).get("q", "").strip()
    hit = _ASK_CACHE.get(q)
    if hit and time.time() - hit.get("_at", 0) < 600:  # data changes must propagate
        return jsonify({k: v for k, v in hit.items() if k != "_at"} | {"cached": True})
    t0 = time.time()
    if not q:
        return jsonify({"error": "empty question"})
    prompt = (f"Translate to ONE read-only Cypher query for Neo4j 5.\n{SCHEMA_HINT}\n"
              f"{_triples_text()}\n"
              f"Team names: Runtime Terror (is_us:true), Rieken Havoc, Young Guns, Big Sticks, "
              f"Maga Doge, Like a Nightmare, Dawg, Guillotine, Long Balls, Gashouse Gang, "
              f"Magnum GI, Simba's Dublin Green Sox, Trex.\n"
              f"Superlatives ('hottest','best','worst') should RANK from raw per-game data "
              f"(e.g. hottest bat = highest hard-hit rate over recent BatterGameEV games, "
              f"min ~15 recent BBE), NOT require a Signal — signals are sparse flags.\n"
              f"Question: {q}\nReply with ONLY the Cypher, no fences, no prose.")
    claude = None
    for cand in ("claude", str(Path.home() / ".claude/local/claude"),
                 "/opt/homebrew/bin/claude", "/usr/local/bin/claude"):
        try:
            sp.run([cand, "--version"], capture_output=True, timeout=10)
            claude = cand
            break
        except Exception:
            continue
    if not claude:
        return jsonify({"error": "claude CLI not found — install with: "
                        "npm install -g @anthropic-ai/claude-code  (then run `claude` once to sign in)"})
    def _gen(text):
        r = sp.run([claude, "-p", "--model", "claude-haiku-4-5-20251001",
                    "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}'],
                   input=text, capture_output=True, text=True, timeout=90, env=_ask_env())
        c = r.stdout.strip()
        m = _re.search(r"```(?:cypher)?\s*(.*?)```", c, _re.S)
        if m:
            c = m.group(1).strip()
        if not c or FORBIDDEN.search(c):
            return None
        if not _re.search(r"\bLIMIT\b", c, _re.I):
            c = c.rstrip("; \n") + " LIMIT 200"
        return c

    def _exec(cy):
        with read_session() as s:
            res = s.run(cy)
            keys = res.keys()
            nodes, edges, rows = {}, [], []
            for rec in res:
                row = {}
                for k in keys:
                    v = rec[k]
                    if hasattr(v, "labels"):
                        nodes[v.element_id] = {
                            "id": v.element_id,
                            "label": v.get("name_full") or v.get("cbs_name") or v.get("code")
                                     or v.get("headline") or list(v.labels)[0],
                            "group": list(v.labels)[0]}
                        row[k] = nodes[v.element_id]["label"]
                    elif hasattr(v, "type") and hasattr(v, "start_node"):
                        edges.append({"from": v.start_node.element_id,
                                      "to": v.end_node.element_id, "label": v.type})
                        row[k] = v.type
                    else:
                        row[k] = str(v) if v is not None else ""
                rows.append(row)
            return keys, rows, nodes, edges

    # ONE pipeline for every candidate: generate -> schema-validate -> execute,
    # with every defect class (violations, runtime error, duplicate rows, zero
    # rows) feeding the next generation round. All paths share the same gates.
    MAX_ROUNDS = 4
    trail, feedback = [], None
    cypher = keys = None
    rows, nodes, edges = [], {}, []
    best = None  # last executable-but-flawed result, kept as fallback
    for _round in range(MAX_ROUNDS):
        try:
            cand = _gen(feedback if feedback else prompt)
        except Exception as exc:
            return jsonify({"error": f"claude call failed: {exc}"})
        if not cand:
            feedback = (f"Your previous reply was empty or contained a write "
                        f"clause. Question: {q}\n{SCHEMA_HINT}\n{_triples_text()}\n"
                        f"Output ONLY one read-only Cypher query.")
            continue
        trail.append(cand)
        viols = _pattern_violations(cand)
        if viols:
            feedback = (f"Your Cypher has invalid relationship patterns:\n"
                        + "\n".join(f"- {v}" for v in viols)
                        + f"\n\nQuery:\n{cand}\n\nQuestion: {q}\n{_triples_text()}\n"
                        f"Rewrite it correctly. ONLY the Cypher.")
            continue
        try:
            keys, rows, nodes, edges = _exec(cand)
        except Exception as exc:
            feedback = (f"This Cypher FAILED on Neo4j 5:\n{cand}\nError: {exc}\n"
                        f"Question: {q}\n{SCHEMA_HINT}\n{_triples_text()}\n"
                        f"Fix it (Neo4j 5: use EXISTS {{...}} subqueries, never "
                        f"pattern expressions introducing variables). ONLY the Cypher.")
            continue
        cypher = cand
        distinct = {tuple(sorted(r.items())) for r in rows}
        if rows and len(distinct) < len(rows) * 0.75:
            best = best or (cand, keys, rows, nodes, edges)
            feedback = (f"This Cypher returned {len(rows)} rows but only "
                        f"{len(distinct)} distinct — the MATCH multiplies rows "
                        f"(unused clause or extra expanded edge):\n{cand}\n"
                        f"Question: {q}\n{SCHEMA_HINT}\n"
                        f"Rewrite MINIMALLY: match only what the question needs. "
                        f"ONLY the Cypher.")
            continue
        if not rows:
            best = best or (cand, keys, rows, nodes, edges)
            feedback = (f"This Cypher returned ZERO rows:\n{cand}\n"
                        f"Question: {q}\n{SCHEMA_HINT}\n"
                        f"Likely cause: over-strict filters (sparse flags, exact "
                        f"string matches) or a wrong property name. Write ONE "
                        f"broader read-only Cypher that STILL answers the question "
                        f"— do not degrade to returning some entity you already "
                        f"matched. ONLY the Cypher.")
            continue
        break  # clean result
    else:
        if best:  # rounds exhausted: fall back to last executable attempt
            cypher, keys, rows, nodes, edges = best
        elif cypher is None:
            return jsonify({"error": "could not produce a valid query",
                            "cypher": "\n-- then -->\n".join(trail)})
    if len(trail) > 1:
        cypher = "\n-- then -->\n".join(trail[:-1] + [cypher or trail[-1]])
    out = {"cypher": cypher, "columns": list(keys), "rows": rows[:200],
           "graph": {"nodes": list(nodes.values()), "edges": edges},
           "elapsed_s": round(time.time() - t0, 1)}
    _ASK_CACHE[q] = out | {"_at": time.time()}
    if len(_ASK_CACHE) > 100:
        _ASK_CACHE.pop(next(iter(_ASK_CACHE)))
    return jsonify(out)


@app.get("/")
def index():
    return render_template_string(PAGE)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8347, debug=False, threaded=True)
