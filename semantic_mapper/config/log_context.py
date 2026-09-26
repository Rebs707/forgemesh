# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
import contextvars
import logging

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
query_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("query_id", default=None)


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.query_id = query_id_var.get()
        return True
