"""Neo4j driver wrapper for the identity graph."""
from __future__ import annotations

from typing import Optional

from neo4j import Driver, GraphDatabase

from api.config import get_settings

_driver: Optional[Driver] = None


def get_driver() -> Driver:
    """Lazily create and cache the Neo4j driver."""
    global _driver
    if _driver is None:
        settings = get_settings()
        _driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=("neo4j", settings.neo4j_password),
        )
    return _driver


def close_driver() -> None:
    """Close the cached driver (used on application shutdown)."""
    global _driver
    if _driver is not None:
        _driver.close()
        _driver = None


def ping() -> bool:
    """Return True if Neo4j answers a trivial query."""
    try:
        with get_driver().session() as session:
            session.run("RETURN 1").single()
        return True
    except Exception:
        return False
