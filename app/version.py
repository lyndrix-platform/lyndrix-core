"""Lyndrix Core — Application Version.

Follows Semantic Versioning (https://semver.org): MAJOR.MINOR.PATCH

Usage::

    from version import __version__, __codename__, get_uptime
"""

from datetime import datetime

__version__: str = "0.5.0"
__version_info__: tuple[int, int, int] = (0, 5, 0)
__release_date__: str = "2026-07-02"
__codename__: str = "First Light Alpha"

# Captured at module import time → effectively tracks application start
_start_time: datetime = datetime.now()


def get_version() -> str:
    """Return the canonical version string."""
    return __version__


def get_uptime() -> str:
    """Return a human-readable uptime string since application start."""
    delta = datetime.now() - _start_time
    total_seconds = int(delta.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"
