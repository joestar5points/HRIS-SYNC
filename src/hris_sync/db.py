import os
from datetime import datetime, timezone

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DEFAULT_DATABASE_URL = "sqlite:///hris_sync.db"


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def make_engine(url: str | None = None) -> Engine:
    """Create a database engine. Tests use "sqlite://" for a fresh in-memory database."""
    url = url or os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)
    engine = create_engine(url)

    if engine.dialect.name == "sqlite":
        # SQLite ignores foreign keys unless asked to enforce them per connection.
        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def init_db(engine: Engine) -> None:
    """Create all tables that don't exist yet."""
    # Imported for their side effect of registering tables on Base.
    from hris_sync import models, state  # noqa: F401
    from hris_sync.mocks import models as mock_models  # noqa: F401

    Base.metadata.create_all(engine)
