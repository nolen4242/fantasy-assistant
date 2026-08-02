"""Neo4j driver wrapper. Reads connection settings from environment / .env."""
from __future__ import annotations

import os
from contextlib import contextmanager

from dotenv import load_dotenv
from neo4j import Driver, GraphDatabase

load_dotenv()


def get_driver() -> Driver:
    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    auth = os.environ.get("NEO4J_AUTH", "neo4j/fantasy-dev-password")
    user, _, password = auth.partition("/")
    return GraphDatabase.driver(uri, auth=(user, password))


@contextmanager
def session():
    driver = get_driver()
    try:
        with driver.session() as s:
            yield s
    finally:
        driver.close()
