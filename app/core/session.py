from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from nicegui import app


def get_user_storage() -> dict[str, Any] | None:
    try:
        return app.storage.user
    except AssertionError:
        return None


def get_user_value(key: str, default: Any = None) -> Any:
    storage = get_user_storage()
    if storage is None:
        return default
    return storage.get(key, default)


def set_user_value(key: str, value: Any) -> bool:
    storage = get_user_storage()
    if storage is None:
        return False
    storage[key] = value
    return True


def update_user_session(data: Mapping[str, Any]) -> bool:
    storage = get_user_storage()
    if storage is None:
        return False
    storage.update(dict(data))
    return True


def clear_user_session() -> bool:
    storage = get_user_storage()
    if storage is None:
        return False
    storage.clear()
    return True


def is_authenticated() -> bool:
    return bool(get_user_value("authenticated", False))
