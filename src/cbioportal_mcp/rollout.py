"""Auth rollout switches, parsed once at startup and frozen thereafter.

No request handling reads these yet; later PRs call `auth_checks_apply` and
`public_db_check_enforced`. Each switch is temporary: see `EXPIRES`.
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


_SETTINGS: Optional[RolloutSettings] = None


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
    if filtering == "on" and (enforcement, public_db_check) != ("enforce", "enforce"):
        raise ValueError(
            f"CBIOPORTAL_MCP_STUDY_FILTERING=on requires {AUTH_ENFORCEMENT_ENV}=enforce "
            f"and {PUBLIC_DB_CHECK_ENV}=enforce"
        )
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
    """Validate on every startup check; the first successful parse is kept."""
    global _SETTINGS
    settings = parse(filtering, local_dev)
    if _SETTINGS is None:
        _SETTINGS = settings
    return _SETTINGS


def get() -> RolloutSettings:
    if _SETTINGS is None:
        raise RuntimeError("rollout settings are read before startup validation")
    return _SETTINGS


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
