import sys
from pathlib import Path
from loguru import logger

from app.core.config import settings
from app.core.observability import current_span, mask_key, mask_uuid


def _span_patcher(record):
    """Inject span attributes into every log record's ``extra``.

    Any log call made during a traced request automatically carries
    ``request_id``, ``model``, ``account_id`` and ``client_key`` (both
    masked) so operators can grep by request across the whole app.
    """
    span = current_span()
    extra = record["extra"]
    if span is None:
        extra.setdefault("request_id", "-")
        return
    extra.setdefault("request_id", span.request_id)
    if span.model is not None:
        extra.setdefault("model", span.model)
    if span.account_id is not None:
        extra.setdefault("account_id", mask_uuid(span.account_id))
    if span.client_key is not None:
        extra.setdefault("client_key", mask_key(span.client_key))


def _is_request_complete(record) -> bool:
    return record["extra"].get("event") == "request.complete"


def configure_logger():
    """Initialize the logger with console, optional file output, and access log."""
    logger.remove()
    logger.configure(patcher=_span_patcher)

    stdout_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>[{extra[request_id]}]</cyan> "
        "<level>{message}</level>"
    )

    logger.add(
        sys.stdout,
        level=settings.log_level.upper(),
        colorize=True,
        format=stdout_format,
        filter=lambda r: not _is_request_complete(r),
    )

    if settings.log_to_file:
        log_file = Path(settings.log_file_path)
        log_file.parent.mkdir(parents=True, exist_ok=True)

        logger.add(
            settings.log_file_path,
            level=settings.log_level.upper(),
            rotation=settings.log_file_rotation,
            retention=settings.log_file_retention,
            compression=settings.log_file_compression,
            enqueue=True,
            encoding="utf-8",
            filter=lambda r: not _is_request_complete(r),
        )

    if settings.access_log_enabled:
        access_file = Path(settings.access_log_path)
        access_file.parent.mkdir(parents=True, exist_ok=True)

        logger.add(
            settings.access_log_path,
            level="INFO",
            rotation=settings.access_log_rotation,
            retention=settings.access_log_retention,
            serialize=True,
            enqueue=True,
            encoding="utf-8",
            filter=_is_request_complete,
        )
