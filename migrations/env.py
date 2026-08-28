from logging.config import fileConfig

from alembic import context

from app import models as application_models  # noqa: F401
from app.database import (
    Base,
    create_database_engine,
    resolve_database_url,
)


config = context.config

if config.config_file_name is not None:
    fileConfig(
        config.config_file_name,
        disable_existing_loggers=False,
    )

target_metadata = Base.metadata


def get_migration_database_url() -> str:
    configured_url = config.attributes.get("database_url")
    if configured_url is not None:
        return configured_url
    return resolve_database_url()


def configure_context(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=config.attributes.get(
            "compare_server_default",
            False,
        ),
        render_as_batch=(
            connection.dialect.name == "sqlite"
        ),
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    """Generate migration SQL without opening a database connection."""
    database_url = get_migration_database_url()
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=config.attributes.get(
            "compare_server_default",
            False,
        ),
        render_as_batch=database_url.startswith("sqlite:"),
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations through the application's configured database URL."""
    provided_connection = config.attributes.get("connection")
    if provided_connection is not None:
        configure_context(provided_connection)
        return

    connectable = create_database_engine(
        get_migration_database_url()
    )
    try:
        with connectable.connect() as connection:
            configure_context(connection)
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
