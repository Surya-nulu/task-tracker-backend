from datetime import datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

# Alembic can only drop or alter a constraint it can name, so every constraint
# gets a deterministic name instead of a backend-generated one.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    # Without eager_defaults the server-generated timestamps are expired after
    # flush and reloaded on next access, which the async ORM cannot do
    # implicitly. RETURNING fetches them in the same statement instead.
    @declared_attr.directive
    def __mapper_args__(cls) -> dict:
        return {"eager_defaults": True}

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    # onupdate only fires when SQLAlchemy emits the UPDATE; raw update()
    # statements must set this column explicitly.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
