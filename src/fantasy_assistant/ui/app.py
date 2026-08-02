"""Local ops dashboard — health, bus, R&D, brief, integrity, graph visual.

Run: .venv/bin/python -m fantasy_assistant.ui.app  ->  http://localhost:8347
Installed as launchd agent com.fantasy-assistant.ui. Local only; binds
127.0.0.1; no cloud anything.
"""
from __future__ import annotations

import json
import subprocess
import time
from datetime import date, datetime
from pathlib import Path

from flask import Flask, jsonify, render_template_string

from fantasy_assistant.graph.client import session

app = Flask(__name__)
REPO = Path(__file__).resolve().parents[3]
BUS_LOG = Path.home() / ".fantasy-assistant" / "bus.log"


def _age_min(p: Path) -> float | None:
    return round((time.time() - p.stat().st_mtime) / 60, 1) if p.exists() else None


def _status(ok: bool, warn: bool = False) -> str:
    return "healthy" if ok else ("stale" if warn else "down")


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
<script src="https://unpkg.com/vis-network@9.1.9/dist/vis-network.min.js"></script>
<style>
 :root{--bg:#101418;--panel:#1a2027;--ink:#e6ebf0;--ink2:#93a1af;--muted:#5d6b78;
   --good:#3fb27f;--warn:#d9a03f;--crit:#d95f4c;--line:#2a323b}
 body{background:var(--bg);color:var(--ink);font:14px/1.45 -apple-system,system-ui,sans-serif;margin:0;padding:18px}
 h1{font-size:17px;margin:0 0 14px} h2{font-size:13px;color:var(--ink2);text-transform:uppercase;
   letter-spacing:.06em;margin:0 0 8px}
 .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(430px,1fr));gap:14px}
 .panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;overflow:auto}
 .tiles{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px}
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
<h1>fantasy-assistant · ops <span class="ts" id="gen"></span></h1>
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
 <div class="panel"><h2>Roster graph (signals ⚡, IL, 2-starts)</h2><div id="net"></div></div>
 <div class="panel"><h2>Graph inventory · capture freshness</h2><table id="labels"></table>
   <table id="caps" style="margin-top:10px"></table></div>
</div>
<script>
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
 const row=(cells,h)=>`<tr>${cells.map(c=>`<${h?'th':'td'}>${c}</${h?'th':'td'}>`).join('')}</tr>`;
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
async function net(){
 const g = await (await fetch('/api/graph')).json();
 new vis.Network(document.getElementById('net'),
  {nodes:new vis.DataSet(g.nodes), edges:new vis.DataSet(g.edges)},
  {nodes:{shape:'dot',font:{color:'#e6ebf0',size:11}},
   groups:{team:{color:'#4f7ec2'},player:{color:'#3fb27f'},
           il:{color:'#5d6b78'},signal:{color:'#d9a03f'}},
   edges:{color:{color:'#2a323b'}},physics:{stabilization:{iterations:120}}});
}
refresh(); net(); setInterval(refresh, 30000);
</script></body></html>"""


@app.get("/")
def index():
    return render_template_string(PAGE)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8347, debug=False)
