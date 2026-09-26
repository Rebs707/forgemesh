# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Engine

from semantic_mapper.config.settings import Settings
from semantic_mapper.core.exceptions import MappingConfigError
from semantic_mapper.silver.writer.base import SilverWriter
from semantic_mapper.silver.writer.mysql import MySQLSilverWriter

if TYPE_CHECKING:
    pass

_REGISTRY: dict[str, type[SilverWriter]] = {
    "mysql": MySQLSilverWriter,
    # "snowflake": SnowflakeSilverWriter,
    # "datavault": DataVaultSilverWriter,
}


def get_writer(settings: Settings, engine: Engine | None = None) -> SilverWriter:
    """
    Resolve and instantiate the configured SilverWriter.

    Raises MappingConfigError for unknown SILVER_WRITER values.
    """
    writer_key = settings.SILVER_WRITER.lower()
    writer_class = _REGISTRY.get(writer_key)
    if writer_class is None:
        known = ", ".join(sorted(_REGISTRY.keys()))
        raise MappingConfigError(
            f"Unknown SILVER_WRITER '{settings.SILVER_WRITER}'. "
            f"Registered writers: [{known}]"
        )

    if writer_class is MySQLSilverWriter:
        if engine is None:
            from sqlalchemy import create_engine

            engine = create_engine(settings.SILVER_DATABASE_URL)
        return MySQLSilverWriter(engine=engine, settings=settings)

    # Generic fallback for future writers that accept (settings,)
    return writer_class(settings=settings)  # type: ignore[call-arg]
