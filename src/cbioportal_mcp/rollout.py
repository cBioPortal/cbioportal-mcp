"""Auth rollout switches, parsed once at startup and frozen thereafter.

Authentication uses `auth_checks_apply`. `public_db_check_enforced` is reserved
for the public-database check (P10), which is not built yet: today
PUBLIC_DB_CHECK=enforce is only validated, logged and required for study
filtering `on`. Each switch is temporary: see `EXPIRES`.
"""

import logging
import os
import re
from dataclasses import dataclass
from datetime import date
from typing import Optional

from cbioportal_mcp.env import _required_env, required_enum

logger = logging.getLogger(__name__)

AUTH_ENFORCEMENT_ENV = "CBIOPORTAL_MCP_AUTH_ENFORCEMENT"
AUTH_TEST_USERS_ENV = "CBIOPORTAL_MCP_AUTH_TEST_USERS"
PUBLIC_DB_CHECK_ENV = "CBIOPORTAL_MCP_PUBLIC_DB_CHECK"
MODES = ("off", "enforce")

# Remove the "off" path and the tester list once these dates pass.
EXPIRES = {
    "AUTH_ENFORCEMENT": date(2027, 6, 30),
    "AUTH_TEST_USERS": date(2027, 6, 30),
    "PUBLIC_DB_CHECK": date(2027, 6, 30),
}

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s.]+$")


@dataclass(frozen=True)
class RolloutSettings:
    auth_enforcement: str
    auth_test_users: frozenset[str]
    public_db_check: str


@dataclass(frozen=True)
class _Pin:
    """Settings plus the context and raw env they were parsed from."""

    filtering: str
    local_dev: bool
    raw_env: tuple[Optional[str], ...]
    settings: RolloutSettings


_SWITCH_VARS = (AUTH_ENFORCEMENT_ENV, AUTH_TEST_USERS_ENV, PUBLIC_DB_CHECK_ENV)
_PIN: Optional[_Pin] = None


def _context(local_dev: bool) -> str:
    return "local stdio (defaults allowed)" if local_dev else "http/sse (no defaults)"


def _mode(name: str, local_dev: bool) -> str:
    if local_dev and os.getenv(name) is None:
        return "off"
    return required_enum(name, MODES)


def _test_users(local_dev: bool) -> frozenset[str]:
    if local_dev and os.getenv(AUTH_TEST_USERS_ENV) is None:
        return frozenset()
    raw = _required_env(AUTH_TEST_USERS_ENV)
    users = [item.strip().lower() for item in raw.split(",")]
    users = [user for user in users if user]
    for position, user in enumerate(users, start=1):
        if not _EMAIL.match(user):
            # Name the position, not the value: entries are personal data.
            raise ValueError(
                f"{AUTH_TEST_USERS_ENV} entry {position} is not a plausible email address"
            )
    return frozenset(users)


def parse(filtering: str, local_dev: bool) -> RolloutSettings:
    """Parse and cross-check the switches. Defaults exist only for local stdio."""
    enforcement = _mode(AUTH_ENFORCEMENT_ENV, local_dev)
    users = _test_users(local_dev)
    public_db_check = _mode(PUBLIC_DB_CHECK_ENV, local_dev)
    if enforcement == "enforce" and users:
        logger.warning(
            "%s is ignored (%d entries) because %s=enforce",
            AUTH_TEST_USERS_ENV,
            len(users),
            AUTH_ENFORCEMENT_ENV,
        )
        users = frozenset()
    return RolloutSettings(enforcement, users, public_db_check)


def initialize(filtering: str, local_dev: bool) -> RolloutSettings:
    """Parse and pin once per process; later startup checks must match the pin.

    A repeat in the same context with unchanged env is a no-op (no reparse).
    A different context (e.g. local-stdio defaults then an HTTP launch) or
    changed switch env raises rather than silently keeping stale values.
    """
    global _PIN
    raw_env = tuple(os.getenv(name) for name in _SWITCH_VARS)
    if _PIN is None:
        _PIN = _Pin(filtering, local_dev, raw_env, parse(filtering, local_dev))
        return _PIN.settings
    if _PIN.local_dev != local_dev:
        raise ValueError(
            f"rollout switches were pinned for {_context(_PIN.local_dev)} but this "
            f"launch is {_context(local_dev)}; restart with a consistent transport "
            "(CLICKHOUSE_MCP_SERVER_TRANSPORT)"
        )
    if _PIN.filtering != filtering:
        raise ValueError(
            "CBIOPORTAL_MCP_STUDY_FILTERING changed after rollout switches were pinned; restart"
        )
    pairs = zip(_SWITCH_VARS, _PIN.raw_env, raw_env, strict=True)
    changed = [name for name, old, new in pairs if old != new]
    if changed:
        # Name the variables only: values may contain tester emails.
        raise ValueError(f"{', '.join(changed)} changed after startup pinned them; restart")
    return _PIN.settings


def _reset_for_tests() -> None:
    """Forget the pin. Tests only; a real process pins exactly once."""
    global _PIN
    _PIN = None


def get() -> RolloutSettings:
    if _PIN is None:
        raise RuntimeError("rollout settings are read before startup validation")
    return _PIN.settings


def auth_checks_apply(identity_email: Optional[str]) -> bool:
    """Whether auth checks apply to a caller the caller already identified."""
    settings = get()
    if settings.auth_enforcement == "enforce":
        return True
    if not identity_email:
        return False
    return identity_email.strip().lower() in settings.auth_test_users


def public_db_check_enforced() -> bool:
    return get().public_db_check == "enforce"


def summary(filtering: str) -> str:
    """Startup log text: switch values and the tester count, never emails."""
    settings = get()
    return (
        f"study_filtering={filtering} auth_enforcement={settings.auth_enforcement} "
        f"auth_test_users_count={len(settings.auth_test_users)} "
        f"public_db_check={settings.public_db_check}"
    )
