"""Neo4j driver wrapper. Reads connection settings from environment / .env.

One module-level driver reused across sessions (building and tearing down a
driver per session() call was ~all connection overhead at our scale), and a
read_session() whose access mode the SERVER enforces — the ask-box's
LLM-generated Cypher runs there, so "read-only" is not just a regex.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv
from neo4j import READ_ACCESS, Driver, GraphDatabase

# anchor to the repo so launchd/cron entrypoints find it regardless of cwd
load_dotenv(Path(__file__).resolve().parents[3] / ".env")

_driver: Driver | None = None


def get_driver() -> Driver:
    global _driver
    if _driver is None:
        uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
        auth = os.environ.get("NEO4J_AUTH")
        if not auth:
            raise RuntimeError(
                "NEO4J_AUTH not set — copy .env.example to .env (no password "
                "defaults live in code)")
        user, _, password = auth.partition("/")
        _driver = GraphDatabase.driver(uri, auth=(user, password))
    return _driver


@contextmanager
def session():
    with get_driver().session() as s:
        yield s


@contextmanager
def read_session():
    """Server-enforced read-only session for untrusted (LLM-generated) Cypher."""
    with get_driver().session(default_access_mode=READ_ACCESS) as s:
        yield s
