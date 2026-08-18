"""
Structured logging setup.

Hard rule (decisions.md §7, §9, §15): logs never contain a raw phone number,
message text, or media captions. Callers pass a `phone_hash` (see
core.security.hash_phone_number) and structured fields only — never the
original text. This module doesn't enforce that by inspecting content (that
would be unreliable); it enforces it by *shape*: log calls in this codebase
take keyword fields, and nothing in this app ever assigns a raw phone number
or message body to a field that reaches a logger. Reviewers checking new code
should grep for `logger.` calls and confirm no `text_body`/`caption`/raw
`from_number` is ever passed.
"""

import logging
import sys
from typing import Any


class _RedactionSafeFormatter(logging.Formatter):
    """Plain, structured single-line formatter. No content fields are special-
    cased here because none should ever be passed in — see module docstring."""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extra_parts = []
        for key, value in getattr(record, "extra_fields", {}).items():
            extra_parts.append(f"{key}={value!r}")
        if extra_parts:
            base = f"{base} | " + " ".join(extra_parts)
        return base


def configure_logging(level: str = "info") -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    root.handlers.clear()

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(
        _RedactionSafeFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(handler)

    # Quiet noisy third-party loggers at INFO by default.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    """Log with structured fields, e.g.:
    log_event(logger, logging.INFO, "message received", phone_hash=h[:8], wamid=w, message_type="text")
    Never pass a `text_body`, `caption`, or raw phone number as a field.
    """
    logger.log(level, message, extra={"extra_fields": fields})
