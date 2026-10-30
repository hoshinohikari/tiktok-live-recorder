import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from utils.logger_manager import LoggerManager


def test_application_log_rotates_at_twenty_megabytes_and_keeps_ten_backups():
    handlers = [
        handler
        for handler in LoggerManager().logger.handlers
        if isinstance(handler, RotatingFileHandler)
    ]

    assert len(handlers) == 1
    assert handlers[0].maxBytes == 20 * 1024 * 1024
    assert handlers[0].backupCount == 10


def test_application_logger_redacts_signed_urls_in_messages_and_tracebacks(caplog):
    logger = LoggerManager().logger
    caplog.set_level("DEBUG", logger="logger")
    logger.warning("FFmpeg rejected %s", "https://cdn.example/live.flv?sign=secret")

    try:
        raise RuntimeError("request failed: https://cdn.example/live.flv?token=secret")
    except RuntimeError:
        logger.exception("FFmpeg stream request failed")

    assert "secret" not in caplog.text
    assert "%5BREDACTED%5D" in caplog.text
