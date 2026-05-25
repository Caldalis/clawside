from __future__ import annotations

import logging
import sys

import structlog

_configured = False


def _configure() -> None:
    global _configured
    if _configured:
        return

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=logging.INFO,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
    _configured = True


_configure()


class _Log:

    def __init__(self) -> None:
        self._inner = structlog.get_logger()

    def debug(self, event: str, **kw) -> None:
        self._inner.debug(event, **kw)

    def info(self, event: str, **kw) -> None:
        self._inner.info(event, **kw)

    def warn(self, event: str, **kw) -> None:
        self._inner.warning(event, **kw)

    def error(self, event: str, **kw) -> None:
        self._inner.error(event, **kw)


log = _Log()
