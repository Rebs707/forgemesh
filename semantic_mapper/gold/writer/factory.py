# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Engine

from semantic_mapper.config.settings import Settings
from semantic_mapper.core.exceptions import MappingConfigError
from semantic_mapper.gold.writer.base import GoldWriter
from semantic_mapper.gold.writer.mysql import MySQLGoldWriter

if TYPE_CHECKING:
    pass

_REGISTRY: dict[str, type[GoldWriter]] = {
    "mysql": MySQLGoldWriter,
    # "snowflake": SnowflakeGoldWriter,
    # "datavault": DataVaultGoldWriter,
}


def get_gold_writer(settings: Settings, engine: Engine | None = None) -> GoldWriter:
    """
    Resolve and instantiate the configured GoldWriter.

    Raises MappingConfigError for unknown GOLD_WRITER values.
    """
    writer_key = settings.GOLD_WRITER.lower()
    writer_class = _REGISTRY.get(writer_key)
    if writer_class is None:
        known = ", ".join(sorted(_REGISTRY.keys()))
        raise MappingConfigError(
            f"Unknown GOLD_WRITER '{settings.GOLD_WRITER}'. "
            f"Registered writers: [{known}]"
        )

    if writer_class is MySQLGoldWriter:
        if engine is None:
            from sqlalchemy import create_engine

            engine = create_engine(settings.GOLD_DATABASE_URL)
        return MySQLGoldWriter(engine=engine, settings=settings)

    # Generic fallback for future writers that accept (settings,)
    return writer_class(settings=settings)  # type: ignore[call-arg]
