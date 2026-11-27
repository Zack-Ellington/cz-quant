"""Shared HTTP error diagnostics for venue clients."""

import logging

import httpx

log = logging.getLogger(__name__)


def raise_for_status(response: httpx.Response, venue: str) -> None:
    """Log bounded response evidence, without request headers, bodies or queries."""
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError:
        text = response.text
        log.error("%s HTTP error: method=%s path=%s status=%s body=%r truncated=%s",
                  venue, response.request.method, response.request.url.path,
                  response.status_code, text[:8192], len(text) > 8192)
        raise
