from __future__ import annotations

from collections.abc import Mapping
from typing import Any

LEVELS = ("everyone", "admin")
DEFAULT_PERMISSIONS = {
    "add": "everyone",
    "lai": "everyone",
    "del": "everyone",
    "alias": "admin",
    "tags": "everyone",
}


def _config_value(config: Mapping[str, Any] | None, key: str, default: Any) -> Any:
    if config is None:
        return default
    value = config.get(key, default)
    return default if value is None else value


def _event_is_admin(event: Any) -> bool:
    checker = getattr(event, "is_admin", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            pass
    role = getattr(event, "role", "")
    return str(role).lower() == "admin"


def is_admin(event: Any) -> bool:
    """Return whether AstrBot identifies the event sender as an admin."""

    return _event_is_admin(event)


def check(event: Any, config: Mapping[str, Any] | None, command: str) -> bool:
    """Check the runtime permission configured for one command."""

    permissions = _config_value(config, "perm", {})
    if not isinstance(permissions, Mapping):
        permissions = {}
    required = (
        str(
            permissions.get(command, DEFAULT_PERMISSIONS.get(command, "everyone")),
        )
        .strip()
        .lower()
    )
    if required == "admin":
        return is_admin(event)
    return True


def _event_group_id(event: Any) -> str:
    getter = getattr(event, "get_group_id", None)
    if callable(getter):
        try:
            value = getter()
        except Exception:
            value = ""
    else:
        value = ""
    return str(value or "").strip()


def _string_set(value: Any) -> set[str]:
    if isinstance(value, str):
        values = value.replace(",", " ").split()
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = []
    return {str(item).strip() for item in values if str(item).strip()}


def group_allowed(
    event: Any,
    config: Mapping[str, Any] | None,
    command: str,
) -> bool:
    """Apply private-chat and configured group whitelist rules."""

    group_id = _event_group_id(event)
    if not group_id:
        return bool(_config_value(config, "allow_private", False))

    groups = _string_set(_config_value(config, "enabled_groups", []))
    if not groups:
        return True
    applies_to = _string_set(
        _config_value(config, "whitelist_applies_to", ["lai", "add", "del"]),
    )
    if command not in applies_to:
        return True
    return group_id in groups


def allowed(
    event: Any,
    config: Mapping[str, Any] | None,
    command: str,
) -> bool:
    return check(event, config, command) and group_allowed(event, config, command)


__all__ = ["LEVELS", "allowed", "check", "group_allowed", "is_admin"]
