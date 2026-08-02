"""RotoWire news adapter (dormant until ROTOWIRE_API_KEY is set in .env).

RotoWire sells JSON news feeds (rotowire.com/apis — 'Fantasy News API',
MLB package). Once Nolen subscribes and drops the key in .env, the bus
polls it in the fast lane and news becomes NewsItem nodes + alerts.
The endpoint path/params below follow their documented v4 shape; adjust to
the exact contract in the welcome email if it differs.
"""
from __future__ import annotations

import os
from datetime import datetime

import httpx
from dotenv import load_dotenv

from fantasy_assistant.capture.parsers import normalize_name
from fantasy_assistant.graph.client import session

load_dotenv()
KEY = os.environ.get("ROTOWIRE_API_KEY")
URL = os.environ.get("ROTOWIRE_URL", "https://api.rotowire.com/News/MLB.php")


def poll(state: dict) -> list[dict]:
    """Fetch latest news items; ingest unseen ones; return alertables."""
    if not KEY:
        return []
    try:
        r = httpx.get(URL, params={"key": KEY, "format": "json"}, timeout=20)
        items = r.json() if r.status_code == 200 else []
        if isinstance(items, dict):
            items = items.get("Updates") or items.get("news") or []
    except Exception:
        return []
    out = []
    seen = state.setdefault("rw_seen", [])
    with session() as s:
        for it in items:
            nid = str(it.get("Id") or it.get("id") or hash(str(it)))
            uid = f"rotowire:{nid}"
            if uid in seen:
                continue
            seen.append(uid)
            name = (it.get("Player") or {}).get("Name") if isinstance(it.get("Player"), dict) \
                else it.get("player") or ""
            headline = it.get("Headline") or it.get("headline") or ""
            body = it.get("Notes") or it.get("notes") or ""
            s.run(
                """
                MERGE (n:NewsItem {uid:$uid})
                SET n.published_at=datetime($ts), n.source='rotowire',
                    n.headline=$h, n.body=$b
                WITH n
                OPTIONAL MATCH (p:Player {name_normalized:$norm})
                FOREACH (_ IN CASE WHEN p IS NULL THEN [] ELSE [1] END |
                         MERGE (n)-[:ABOUT]->(p))
                """,
                uid=uid, ts=datetime.now().isoformat(timespec="seconds"),
                h=headline, b=body[:2000], norm=normalize_name(name or ""),
            )
            out.append({"uid": uid, "headline": f"{name}: {headline}"})
    state["rw_seen"] = seen[-3000:]
    return out
