# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
import json
import logging
from datetime import datetime, timezone

_STANDARD_FIELDS = frozenset({
    "name", "msg", "args", "created", "filename", "funcName", "levelname",
    "levelno", "lineno", "module", "msecs", "message", "pathname",
    "process", "processName", "relativeCreated", "stack_info", "thread",
    "threadName", "exc_info", "exc_text", "taskName",
    "request_id", "query_id",
})


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        data: dict = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.message,
        }
        if getattr(record, "request_id", None):
            data["request_id"] = record.request_id
        if getattr(record, "query_id", None):
            data["query_id"] = record.query_id
        for key, val in record.__dict__.items():
            if key not in _STANDARD_FIELDS:
                try:
                    json.dumps(val)
                    data[key] = val
                except (TypeError, ValueError):
                    data[key] = str(val)
        if record.exc_info:
            data["traceback"] = self.formatException(record.exc_info)
        return json.dumps(data)


class HumanFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        level = f"[{record.levelname:<7}]"

        ctx_parts = []
        if getattr(record, "request_id", None):
            ctx_parts.append(f"req={record.request_id}")
        if getattr(record, "query_id", None):
            ctx_parts.append(f"q={record.query_id}")
        ctx = f" [{' '.join(ctx_parts)}]" if ctx_parts else ""

        extras = {
            k: v for k, v in record.__dict__.items()
            if k not in _STANDARD_FIELDS
        }
        extra_str = " ".join(f"{k}={v}" for k, v in extras.items()) if extras else ""

        line = f"{level} {ts} {record.name}{ctx} {record.getMessage()}"
        if extra_str:
            line = f"{line} {extra_str}"
        if record.exc_info:
            line = line + "\n" + self.formatException(record.exc_info)
        return line
