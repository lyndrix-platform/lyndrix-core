import logging
import os
import sys
import re
from logging.handlers import RotatingFileHandler
from collections import deque

# Default log path; the actual directory is resolved from settings.LOGS_DIR at
# setup time (see setup_logging) so the configured path is honoured.
LOG_DIR = "/app/logs"
LOG_FILE = os.path.join(LOG_DIR, "lyndrix.log")

IS_DEBUG = os.getenv("LYNDRIX_DEBUG", "false").lower() == "true"
LOG_LEVEL = logging.DEBUG if IS_DEBUG else logging.INFO

# ENTERPRISE FORMATTING
# [time] | [level] | [component (25 chars)] | message
FORMAT_STR = "%(asctime)s | %(levelname)-8s | %(name)-25s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# In-memory store for the UI log viewer (the last 1000 entries).
# Queried by the UI to filter logs per plugin.
log_capture_buffer = deque(maxlen=1000)

# Keys we NEVER want to see in plaintext in logs or UI.
SENSITIVE_KEYS = {'token', 'password', 'secret', 'secret_value', 'private_key', 'key', 'auth'}


def mask_secrets(obj):
    """Recursively replace sensitive values in dicts/lists with a mask.

    Reused by both the log formatter and the event bus so payload dicts are
    redacted by key name before they are stringified for logging.
    """
    if isinstance(obj, dict):
        return {
            k: "********" if k.lower() in SENSITIVE_KEYS else mask_secrets(v)
            for k, v in obj.items()
        }
    elif isinstance(obj, list):
        return [mask_secrets(i) for i in obj]
    return obj


class EnterpriseFormatter(logging.Formatter):
    # Kept for backwards compatibility; delegates to the module-level helper.
    SENSITIVE_KEYS = SENSITIVE_KEYS

    def _mask_secrets(self, obj):
        return mask_secrets(obj)

    def format(self, record):
        # If the message is a dictionary (common in our Event Bus logs)
        if isinstance(record.msg, dict):
            record.msg = self._mask_secrets(record.msg)

        # If it's a string, we can use a Regex to catch common token patterns
        # Example: x-gitlab-token: [HIDDEN]
        elif isinstance(record.msg, str):
            for key in self.SENSITIVE_KEYS:
                # Matches "token: abc123", "token='abc123'", etc.
                pattern = rf"({key}['\" ]*[:=][ '\" ]*)([^ '\",\n]+)"
                record.msg = re.sub(pattern, r"\1********", record.msg, flags=re.IGNORECASE)

        return super().format(record)

class RingBufferHandler(logging.Handler):
    """Keeps logs in RAM for the UI log viewer."""
    def emit(self, record):
        log_entry = self.format(record)
        log_capture_buffer.append((record.name, record.levelname, log_entry))

def setup_logging():
    # Resolve the log directory from settings (config hierarchy) and create it
    # here rather than as an import-time side effect.
    log_dir = LOG_DIR
    try:
        from config import settings
        log_dir = settings.LOGS_DIR or LOG_DIR
    except Exception:
        # Settings not importable yet — fall back to the module default.
        pass
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "lyndrix.log")

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    # Formatter instance
    formatter = EnterpriseFormatter(FORMAT_STR, datefmt="%H:%M:%S")

    # 1. STREAM HANDLER (console)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(LOG_LEVEL)
    root_logger.addHandler(console_handler)

    # 2. FILE HANDLER
    file_handler = RotatingFileHandler(log_file, maxBytes=10*1024*1024, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)
    root_logger.addHandler(file_handler)

    # 3. MEMORY HANDLER (for the UI log viewer)
    memory_handler = RingBufferHandler()
    memory_handler.setFormatter(formatter)
    memory_handler.setLevel(logging.DEBUG)
    root_logger.addHandler(memory_handler)

    # Quiet down external loggers
    silent_loggers = ["uvicorn", "uvicorn.access", "sqlalchemy.engine", "hvac", "urllib3", "nicegui", "httpx"]
    for name in silent_loggers:
        lg = logging.getLogger(name)
        lg.setLevel(logging.WARNING if not IS_DEBUG else logging.DEBUG)
        lg.propagate = False
        lg.handlers = root_logger.handlers

    logging.info(f"LOGGING: Initialized with level {'DEBUG' if IS_DEBUG else 'INFO'}")

def get_logger(name: str):
    """Factory method for consistent logger names."""
    return logging.getLogger(name)