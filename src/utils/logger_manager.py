import logging
import traceback
from logging.handlers import RotatingFileHandler

from utils.url_sanitizer import redact_urls_in_text


class MaxLevelFilter(logging.Filter):
    """
    Filter that only allows log records up to a specified maximum level.
    """

    def __init__(self, max_level):
        super().__init__()
        self.max_level = max_level

    def filter(self, record):
        return record.levelno <= self.max_level


class URLRedactionFilter(logging.Filter):
    """Redact URL credentials from application messages and tracebacks."""

    def filter(self, record):
        record.msg = redact_urls_in_text(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = redact_urls_in_text(
                "".join(traceback.format_exception(*record.exc_info))
            )
            record.exc_info = None
        return True


class LoggerManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.logger = None
            cls._instance.setup_logger()
        return cls._instance

    def setup_logger(self):
        if self.logger is None:
            self.logger = logging.getLogger("logger")
            self.logger.setLevel(logging.DEBUG)
            self.logger.addFilter(URLRedactionFilter())

            fmt_datefmt = "%Y-%m-%d %H:%M:%S"

            # 1) Console INFO handler (stdout)
            info_handler = logging.StreamHandler()
            info_handler.setLevel(logging.INFO)
            info_handler.setFormatter(
                logging.Formatter("[*] %(asctime)s - %(message)s", fmt_datefmt)
            )
            info_handler.addFilter(MaxLevelFilter(logging.INFO))
            self.logger.addHandler(info_handler)

            # 2) Console ERROR handler (stderr)
            error_handler = logging.StreamHandler()
            error_handler.setLevel(logging.ERROR)
            error_handler.setFormatter(
                logging.Formatter("[!] %(asctime)s - %(message)s", fmt_datefmt)
            )
            self.logger.addHandler(error_handler)

            # 3) File handler — DEBUG level, includes full stack traces
            #    Rotates at 20 MB, keeps 10 backups
            file_handler = RotatingFileHandler(
                "tiktok-recorder.log",
                maxBytes=20 * 1024 * 1024,
                backupCount=10,
                encoding="utf-8",
            )
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s [%(levelname)s] %(message)s", fmt_datefmt
                )
            )
            self.logger.addHandler(file_handler)


logger = LoggerManager().logger
