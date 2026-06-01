from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    """Provide a transactional scope around a series of operations."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# FR-CR-05-241 — SEPARATE read-only engine for the pgvector catalog DB
# (CATALOG_DATABASE_URL). Kept fully isolated from the primary engine so the
# prod transactional DB is never touched by vector reads.
_catalog_engine: Engine | None = None
_CatalogSessionLocal: sessionmaker[Session] | None = None


def get_catalog_session_factory() -> sessionmaker[Session] | None:
    """Sessionmaker bound to the separate pgvector catalog DB, or None when
    CATALOG_DATABASE_URL is unset (callers then fall back to the primary DB,
    e.g. the ops sidecar where catalog + app share one database)."""
    global _catalog_engine, _CatalogSessionLocal
    dsn = getattr(get_settings(), "catalog_database_url", None)
    if not dsn:
        return None
    if _catalog_engine is None:
        _catalog_engine = create_engine(dsn, pool_pre_ping=True, future=True)
        _CatalogSessionLocal = sessionmaker(
            bind=_catalog_engine, autoflush=False, expire_on_commit=False
        )
    return _CatalogSessionLocal


@contextmanager
def catalog_session_scope() -> Iterator[Session]:
    """Read-only scope on the catalog DB (always rolls back — no writes)."""
    factory = get_catalog_session_factory()
    if factory is None:
        raise RuntimeError("CATALOG_DATABASE_URL not configured")
    session = factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


# FR-TV — SEPARATE engine for the task/team vector DB (kind='task'/'team_member'
# /'employee'). DSN precedence: TASK_VECTOR_DATABASE_URL → CATALOG_DATABASE_URL →
# None (caller falls back to the primary DB). Isolated from the primary engine.
_task_vector_engine: Engine | None = None
_TaskVectorSessionLocal: sessionmaker[Session] | None = None


def get_task_vector_session_factory() -> sessionmaker[Session] | None:
    """Sessionmaker bound to the task vector DB, or None when neither
    TASK_VECTOR_DATABASE_URL nor CATALOG_DATABASE_URL is set (callers then use
    the primary DB)."""
    global _task_vector_engine, _TaskVectorSessionLocal
    s = get_settings()
    dsn = getattr(s, "task_vector_database_url", None) or getattr(
        s, "catalog_database_url", None
    )
    if not dsn:
        return None
    if _task_vector_engine is None:
        _task_vector_engine = create_engine(dsn, pool_pre_ping=True, future=True)
        _TaskVectorSessionLocal = sessionmaker(
            bind=_task_vector_engine, autoflush=False, expire_on_commit=False
        )
    return _TaskVectorSessionLocal
