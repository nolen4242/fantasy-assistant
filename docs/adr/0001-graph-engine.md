# ADR-0001: Graph engine — Neo4j Community (Docker)

Date: 2026-08-02 · Status: accepted

## Decision

Neo4j 5.x Community Edition, run locally via Docker Compose, with APOC.

## Context

Schema (SCHEMA.md) is a property graph with heavy interval/versioning patterns
and JSON blob properties. Candidates: Neo4j, Memgraph, FalkorDB.

## Rationale

- Best-in-class Cypher tooling, browser, docs; the schema is Cypher-native.
- Single-user local workload — Community Edition limits (no clustering, single
  DB) are irrelevant here.
- Memgraph's streaming strengths aren't needed: our "real-time" is
  minutes-latency scraping, not Kafka-scale.
- Ecosystem maturity matters most for the R&D loop (GDS-style analytics, driver
  stability from Python).

## Consequences

- Python driver: `neo4j`. Ingestion must use idempotent `MERGE` on `uid`.
- If write volume ever hurts (it shouldn't at ~10⁴ events/day), revisit.
- Constraints/indexes bootstrapped by `graph/bootstrap.py` (to be written):
  uniqueness on `uid` per label, range indexes on date properties.
