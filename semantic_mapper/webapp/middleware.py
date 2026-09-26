# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
import logging
import time
import uuid

from fastapi import Request

from semantic_mapper.config.log_context import request_id_var


async def request_logging_middleware(request: Request, call_next):
    request_id = uuid.uuid4().hex[:8]
    token = request_id_var.set(request_id)
    start = time.monotonic()
    logger = logging.getLogger("webapp.request")
    logger.info(f"{request.method} {request.url.path} received")

    try:
        response = await call_next(request)
        duration_ms = int((time.monotonic() - start) * 1000)

        if response.status_code >= 500:
            level = logging.ERROR
        elif response.status_code >= 400:
            level = logging.WARNING
        else:
            level = logging.INFO

        logger.log(
            level,
            f"{request.method} {request.url.path} {response.status_code}",
            extra={"duration_ms": duration_ms, "status_code": response.status_code},
        )
        return response
    finally:
        request_id_var.reset(token)
