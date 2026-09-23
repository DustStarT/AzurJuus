from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from socket import create_connection
from typing import Iterator
from urllib.parse import urlsplit

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .sqlite_policy import journal_mode


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None
_database_label = "uninitialized"


def _sqlite_fallback_url(settings: Settings) -> str:
    path = settings.workspace_state_path.parent / "azurjuus.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+pysqlite:///{Path(path).as_posix()}"


def _can_try_tcp_endpoint(url: str, timeout: float = 0.25) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme not in {"postgresql", "postgresql+psycopg"}:
        return True
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return True

    port = parsed.port or 5432
    try:
        with create_connection((parsed.hostname, port), timeout=timeout):
            return True
    except OSError:
        return False


def initialize_database(settings: Settings, metadata) -> str:
    global _engine, _SessionLocal, _database_label

    candidates = [settings.database_url]
    sqlite_url = _sqlite_fallback_url(settings)
    if sqlite_url not in candidates:
        candidates.append(sqlite_url)

    last_error: Exception | None = None
    for candidate in candidates:
        if not _can_try_tcp_endpoint(candidate):
            last_error = ConnectionError(f"Database endpoint is unavailable: {candidate}")
            continue
        try:
            if candidate.startswith("sqlite"):
                connect_args = {"check_same_thread": False}
            elif candidate.startswith("postgresql"):
                connect_args = {"connect_timeout": 3}
            else:
                connect_args = {}
            engine = create_engine(candidate, future=True, connect_args=connect_args, pool_pre_ping=True)
            with engine.begin() as connection:
                if candidate.startswith("sqlite"):
                    connection.exec_driver_sql("PRAGMA journal_mode=" + journal_mode())
                    connection.exec_driver_sql("PRAGMA busy_timeout=15000")
                connection.execute(text("SELECT 1"))
            from . import cognition_models  # register additive business tables
            from . import social_models
            from . import mind_models
            from .migrations import backup_mind_migration
            backup_mind_migration(engine, settings)
            from .migrations import backup_social_migration
            backup_social_migration(engine, settings)
            from .migrations import backup_cognition_migration
            backup_cognition_migration(engine, settings)
            metadata.create_all(engine)
            from .migrations import migrate
            migrate(engine)
            with engine.begin() as connection:
                connection.execute(text("CREATE TABLE IF NOT EXISTS azur_schema_migrations(version INTEGER PRIMARY KEY)"))
                if not connection.execute(text("SELECT version FROM azur_schema_migrations WHERE version=2")).first():
                    connection.execute(text("INSERT INTO azur_schema_migrations VALUES(2)"))
                if not connection.execute(text("SELECT version FROM azur_schema_migrations WHERE version=3")).first():
                    connection.execute(text("INSERT INTO azur_schema_migrations VALUES(3)"))
                if not connection.execute(text("SELECT version FROM azur_schema_migrations WHERE version=4")).first():
                    connection.execute(text("INSERT INTO azur_schema_migrations VALUES(4)"))
            if _engine is not None and _engine is not engine:
                _engine.dispose()
            _engine = engine
            _SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
            _database_label = candidate
            return candidate
        except Exception as error:  # pragma: no cover - defensive init path
            last_error = error

    raise RuntimeError(f"Unable to initialize database: {last_error}")


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("Database engine has not been initialized.")
    return _engine


def get_database_label() -> str:
    return _database_label


@contextmanager
def session_scope() -> Iterator[Session]:
    if _SessionLocal is None:
        raise RuntimeError("Database session factory has not been initialized.")
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    if _SessionLocal is None:
        raise RuntimeError("Database session factory has not been initialized.")
    session = _SessionLocal()
    try:
        yield session
    finally:
        session.close()
