import os
from collections.abc import Mapping

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import declarative_base, sessionmaker


DEFAULT_DATABASE_URL = "sqlite:///./saas_mvp.db"
DATABASE_URL_ENV_NAME = "DATABASE_URL"


def resolve_database_url(
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve the configured URL or fall back to the existing SQLite URL."""
    source = os.environ if environ is None else environ
    configured_url = source.get(DATABASE_URL_ENV_NAME, "").strip()
    return configured_url or DEFAULT_DATABASE_URL


def get_engine_kwargs(database_url: str) -> dict[str, object]:
    """Return only the engine arguments supported by the URL's backend."""
    if make_url(database_url).get_backend_name() == "sqlite":
        return {
            "connect_args": {
                "check_same_thread": False,
            }
        }
    return {}


def create_database_engine(
    database_url: str | None = None,
) -> Engine:
    resolved_url = (
        resolve_database_url()
        if database_url is None
        else resolve_database_url(
            {DATABASE_URL_ENV_NAME: database_url}
        )
    )
    return create_engine(
        resolved_url,
        **get_engine_kwargs(resolved_url),
    )


DATABASE_URL = resolve_database_url()
engine = create_database_engine(DATABASE_URL)

# 创建会话类
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# 声明基类
Base = declarative_base()
