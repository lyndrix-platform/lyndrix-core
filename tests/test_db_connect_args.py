"""pymysql socket timeouts derived from the settings (core 1.11.1).

Run inside the lyndrix-core runtime (``PYTHONPATH=app``):
    pytest tests/test_db_connect_args.py -v
"""
import pytest

from config import settings
from core.components.database.logic.db_service import DatabaseService


@pytest.mark.parametrize(
    "cap, configured, expected",
    [
        (60, 0, 90),      # default: cap + 30 margin
        (120, 0, 150),    # auto follows a raised cap
        (0, 0, 90),       # no statement cap → floor
        (30, 0, 90),      # small cap → floor still wins
        (60, 300, 300),   # explicit value above the cap is honoured
        (60, 60, 90),     # equal to the cap → raised so the server cap fires first
        (60, 10, 90),     # below the cap → raised
        (0, 10, 10),      # no cap → any explicit value is accepted
    ],
)
def test_socket_timeout_follows_statement_cap(monkeypatch, cap, configured, expected):
    monkeypatch.setattr(settings, "DB_STATEMENT_TIMEOUT_SECONDS", cap)
    monkeypatch.setattr(settings, "DB_SOCKET_TIMEOUT_SECONDS", configured)
    assert DatabaseService.socket_timeout_seconds() == expected


def test_connect_args_carry_connect_read_and_write_timeouts(monkeypatch):
    monkeypatch.setattr(settings, "DB_STATEMENT_TIMEOUT_SECONDS", 60)
    monkeypatch.setattr(settings, "DB_SOCKET_TIMEOUT_SECONDS", 0)
    args = DatabaseService.connect_args()
    assert args == {"connect_timeout": 5, "read_timeout": 90, "write_timeout": 90}
    assert args["read_timeout"] > settings.DB_STATEMENT_TIMEOUT_SECONDS
