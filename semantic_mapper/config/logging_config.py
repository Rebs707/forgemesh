# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
import logging
import logging.handlers
import os
import sys
from pathlib import Path


def setup_logging(settings=None) -> logging.Logger:
    """Configure root logger with JSON file handler and human stdout handler.

    Can be called with no arguments (uses defaults) or with a Settings object.
    LOG_LEVEL env var always overrides settings.LOG_LEVEL.
    """
    from semantic_mapper.config.log_context import ContextFilter
    from semantic_mapper.config.log_formatters import HumanFormatter, JsonFormatter

    cfg_level = getattr(settings, "LOG_LEVEL", "INFO") if settings else "INFO"
    log_level_name = os.environ.get("LOG_LEVEL", cfg_level)
    log_level = getattr(logging, log_level_name.upper(), logging.INFO)

    log_dir = getattr(settings, "LOG_DIR", "runtime/logs") if settings else "runtime/logs"
    max_bytes = getattr(settings, "LOG_FILE_MAX_BYTES", 10_000_000) if settings else 10_000_000
    backup_count = getattr(settings, "LOG_FILE_BACKUP_COUNT", 5) if settings else 5
    log_to_stdout = getattr(settings, "LOG_TO_STDOUT", True) if settings else True

    root = logging.getLogger()
    root.setLevel(log_level)
    root.handlers.clear()
    root.filters.clear()

    ctx_filter = ContextFilter()

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_path / "webapp.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.addFilter(ctx_filter)
    file_handler.setFormatter(JsonFormatter())
    root.addHandler(file_handler)

    if log_to_stdout:
        # Log messages contain non-ASCII (e.g. the '→' arrow in ontology logs).
        # On Windows the console stream defaults to cp1252, which raises
        # UnicodeEncodeError mid-log. Force the stream to UTF-8 so any glyph
        # encodes cleanly; fall back to a replacing wrapper if reconfigure is
        # unavailable (older Pythons / already-wrapped streams).
        stream = sys.stdout
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # Python 3.7+
        except (AttributeError, ValueError):
            import io
            try:
                stream = io.TextIOWrapper(
                    sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
                )
            except Exception:  # noqa: BLE001 — last resort: use stdout as-is
                stream = sys.stdout
        stdout_handler = logging.StreamHandler(stream)
        stdout_handler.addFilter(ctx_filter)
        stdout_handler.setFormatter(HumanFormatter())
        root.addHandler(stdout_handler)

    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    return root
