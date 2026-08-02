"""The bus: an always-on poller that turns pull sources into push events.

MLB has no public websocket, so this daemon IS our subscription: every
POLL_SECS it pulls the MLB transactions feed, diffs against seen state, and
for new alertable events touching our player universe it (a) writes Alert
nodes, (b) fires a macOS notification. Every SLOW_EVERY cycles it refreshes
probables and yesterday's velocity + signals. Rotowire news joins the fast
loop automatically once ROTOWIRE_API_KEY is set (see capture/rotowire.py).

Run ad hoc:  .venv/bin/python -m fantasy_assistant.capture.bus
Install:     see docs/adr/0004-realtime-bus.md (launchd agent)
"""
from __future__ import annotations

import json
import subprocess
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from fantasy_assistant.capture import mlb_status, rotowire, velocity
from fantasy_assistant.graph.client import session

POLL_SECS = 180
SLOW_EVERY = 10          # every ~30 min
STATE = Path.home() / ".fantasy-assistant" / "bus-state.json"


def _notify(title: str, text: str) -> None:
    try:
        subprocess.run(["osascript", "-e",
                        f'display notification "{text[:180]}" with title "{title}"'],
                       timeout=10, capture_output=True)
    except Exception:
        pass


def _load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"seen": []}


def _save_state(st: dict) -> None:
    STATE.parent.mkdir(exist_ok=True)
    st["seen"] = st["seen"][-5000:]
    STATE.write_text(json.dumps(st))


def _alert(row: dict, source: str) -> None:
    with session() as s:
        s.run(
            """
            MERGE (a:Alert {uid: 'alert:' + $uid})
            SET a.raised_at = datetime($ts), a.urgency = 'act_today',
                a.source = $src, a.text = $text
            """,
            uid=row["uid"], ts=datetime.now().isoformat(timespec="seconds"),
            src=source, text=row.get("description", row.get("headline", "")),
        )


def cycle(st: dict, n: int) -> None:
    today = date.today().isoformat()
    yday = (date.today() - timedelta(days=1)).isoformat()
    r = mlb_status.fetch_status_events(yday, today)
    fresh = [a for a in r["alertable"] if a["uid"] not in st["seen"]]
    for a in fresh:
        st["seen"].append(a["uid"])
        _alert(a, "mlb_statsapi")
        _notify(f"MLB: {a['type_desc']}", a["description"])
        print(f"[{datetime.now():%H:%M}] ALERT {a['type_desc']}: {a['description'][:100]}", flush=True)
    for item in rotowire.poll(st):
        _alert(item, "rotowire")
        _notify("RotoWire", item.get("headline", ""))
        print(f"[{datetime.now():%H:%M}] NEWS {item.get('headline','')[:100]}", flush=True)
    if n % SLOW_EVERY == 0:
        import asyncio
        from fantasy_assistant.capture import eligibility, runner
        from fantasy_assistant.graph import ingest
        p = mlb_status.fetch_probables()
        asyncio.run(velocity.collect(2))
        sigs = velocity.velocity_signals()
        el = asyncio.run(eligibility.collect())
        from fantasy_assistant.analytics import hotstreak, schedule
        from fantasy_assistant.capture import batted
        asyncio.run(batted.collect(2))
        batted.contact_signals()
        heat = hotstreak.run()
        n_heat = sum(len(v) for v in heat.values())
        if n % (SLOW_EVERY * 4) == 0:
            schedule.refresh()
        news = {"news": 0, "our_roster_fresh": []}
        if runner.capture_news(quiet=True):  # None if CBS profile is busy
            news = ingest.ingest_news(runner.RAW_ROOT / date.today().isoformat())
            for name, headline in news.get("our_roster_fresh", []):
                _alert({"uid": "cbsnews:" + name + ":" + headline[:40],
                        "description": f"{name}: {headline}"}, "cbs_rotowire")
                _notify("Roster news", f"{name}: {headline}")
        print(f"[{datetime.now():%H:%M}] slow lane: {p['probables']} probables, "
              f"{len(sigs)} velo signals, {len(el['windows_opening'])} eligibility "
              f"windows, {news.get('fresh', 0)} fresh news, {n_heat} heat signals", flush=True)


def main() -> None:
    st = _load_state()
    print(f"bus up — polling every {POLL_SECS}s", flush=True)
    n = 0
    while True:
        try:
            cycle(st, n)
            _save_state(st)
            (STATE.parent / "bus.heartbeat").touch()
        except Exception as exc:
            print(f"[{datetime.now():%H:%M}] cycle error: {exc}", flush=True)
        n += 1
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
