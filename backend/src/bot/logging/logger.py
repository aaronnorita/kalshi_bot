"""
Structured logging setup using structlog.

Usage:
    from bot.logging.logger import get_logger
    log = get_logger(__name__)
    log.info("snapshot_received", ticker="BTC-X", yes_mid=63)
    log.warning("signal_rejected", reason="max_position_reached", ticker="BTC-X")

Binding context (e.g. bind once per strategy instance):
    log = get_logger(__name__).bind(strategy_id="my_strat", ticker="BTC-X")
    log.info("signal_generated", action="buy_yes", quantity=5)
    # → {"event": "signal_generated", "strategy_id": "my_strat", "ticker": "BTC-X", ...}

Output format:
    - "json"    → one JSON object per line (use in production / log aggregation)
    - "console" → human-readable colored output (use during development)
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog


def configure_logging(level: str = "INFO", format: str = "json", log_file: str | None = None) -> None:
    """
    Initialize structlog. Call once at application startup (in runner.py).

    Args:
        level:    Logging level string ("DEBUG", "INFO", "WARNING", "ERROR")
        format:   "json" for production, "console" for development
        log_file: Optional path to write logs to (in addition to stdout)
    """
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    if format == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=shared_processors,
    )

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    for handler in handlers:
        handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers = handlers
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """
    Get a named structured logger. Call at module level:
        log = get_logger(__name__)

    The returned logger supports:
        log.debug(event, **kwargs)
        log.info(event, **kwargs)
        log.warning(event, **kwargs)
        log.error(event, **kwargs)
        log.exception(event, **kwargs)   # includes traceback
        log.bind(key=value)              # returns new logger with bound context
    """
    return structlog.get_logger(name)
