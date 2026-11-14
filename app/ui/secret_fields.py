"""Rules for secret inputs on NiceGUI pages (SC-08).

A stored secret is never sent to the browser: ``password=True`` only masks the
rendering, the value still travels over the websocket and sits in the DOM. The
pages therefore render an EMPTY input plus a "configured / not set" hint, and a
blank input on save means "keep the stored value" — the same contract the REST
API uses (``configured`` flag, blank = keep). Removing a secret is an explicit,
separate action.

Pure helpers, no NiceGUI import, so the rules are unit-testable.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

CONFIGURED_HINT = "Configured — leave empty to keep the stored value."
NOT_SET_HINT = "Not set."


def is_configured(stored: Any) -> bool:
    """Reduce a stored secret to the only thing a page may know about it."""
    return bool(stored and str(stored).strip())


def secret_hint(configured: bool) -> str:
    return CONFIGURED_HINT if configured else NOT_SET_HINT


def entered_secret(value: Any) -> str | None:
    """The secret the user typed, or ``None`` when the input was left blank (= keep)."""
    text = "" if value is None else str(value).strip()
    return text or None


def field_prefill(field: Any) -> str:
    """Initial input value for a provider config field: never a secret."""
    if getattr(field, "sensitive", False):
        return ""
    return getattr(field, "current_value", None) or ""


def values_to_save(fields: Iterable[Any], entered: Mapping[str, Any]) -> dict[str, str]:
    """Which ``{key: value}`` pairs a provider form really submits.

    A blank sensitive field is left out (keep the stored secret); every other
    field is saved as entered, including an intentionally emptied one.
    """
    result: dict[str, str] = {}
    for field in fields:
        raw = entered.get(field.key)
        value = "" if raw is None else str(raw)
        if getattr(field, "sensitive", False) and not value.strip():
            continue
        result[field.key] = value
    return result
