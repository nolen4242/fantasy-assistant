# fantasy-assistant

In-season strategic intelligence system for the Bob Uecker Imaginary Baseball
League (13-team CBS roto). Mission: **win the league.**

- [PROBLEM.md](PROBLEM.md) — problem statement, scope, valuation principles
- [SCHEMA.md](SCHEMA.md) — graph schema (events / snapshots / derived beliefs)
- [docs/adr/](docs/adr/) — architecture decision records

## Layout

```
src/fantasy_assistant/
  capture/     # scouting agents: CBS league site, MLB Stats API, Savant, news
  graph/       # graph client, schema constraints, ingestion (idempotent upserts)
  analytics/   # projections blend, race analysis, valuations, standings sim
  advisory/    # weekly brief + daily alerts
data/raw/      # timestamped raw captures (see per-day MANIFEST.md)
docker-compose.yml  # Neo4j
```

## Running Neo4j

```bash
docker compose up -d neo4j
```

Browser at http://localhost:7474 (auth in `.env`, see `.env.example`).

## Status

Live (2026-08-02). Full-season graph loaded and reconciled against CBS
(replay 369/369, recompute 98.1% exact); three autonomous loops running
(daily 07:07 snapshot routine, 3-min MLB event bus, 30-min signal lane);
backtest-gated projections; weekly brief with pre-registered recommendations
scored against outcomes; local ops dashboard with NL→Cypher ask box at
http://127.0.0.1:8347. Deployed schema: [docs/ONTOLOGY.md](docs/ONTOLOGY.md).
Tests: `.venv/bin/python -m pytest tests/`.
