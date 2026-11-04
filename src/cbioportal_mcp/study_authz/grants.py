"""Structured audit of all held grants, including dormant and nested roles.

system.grants omits prefix-wildcard flags in 25.8 and 26.6. SHOW GRANTS FINAL
is used only to reject wildcard names; ownership, scope, options and role
traversal come from the structured tables. No CHECK GRANT (readonly hides
write privileges), no SHOW GRANTS FOR (nested roles need SHOW ROLES).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from cbioportal_mcp.study_authz.sqlgen import validate_identifier

_TOKEN_RE = re.compile(
    r"\s*(?:(?P<quoted>`(?:[^`\\]|\\.|``)*`)|(?P<word>[A-Za-z0-9_*]+)|(?P<punct>[,.()]))"
)


class GrantParseError(ValueError):
    pass


def _unquote(token: str) -> str:
    body = token[1:-1]
    return re.sub(r"``|\\(.)", lambda m: m.group(1) if m.group(1) else "`", body)


def tokenize(line: str) -> list[tuple[str, str]]:
    """(kind, text) tokens; kind is 'word', 'name' (back-quoted) or 'punct'."""
    out, pos = [], 0
    line = line.rstrip()
    while pos < len(line):
        m = _TOKEN_RE.match(line, pos)
        if not m or m.end() == pos:
            raise GrantParseError(f"unexpected character at {pos}")
        if m.group("quoted"):
            out.append(("name", _unquote(m.group("quoted"))))
        elif m.group("word"):
            out.append(("word", m.group("word")))
        else:
            out.append(("punct", m.group("punct")))
        pos = m.end()
    return out


@dataclass(frozen=True)
class Grant:
    privilege: str
    database: str | None
    table: str | None
    column: str | None
    partial_revoke: bool
    grant_option: bool
    access_object: str = ""

    @property
    def description(self) -> str:
        return f"{self.privilege} ON {self.database or '*'}.{self.table or '*'}" + (
            f" ({self.access_object})" if self.access_object else ""
        )


@dataclass
class HeldGrants:
    grants: list[tuple[str, bool, Grant]]  # principal, is_user, grant
    roles: set[str]
    admin_roles: list[str]
    final: list[str]


def collect_grants(session, user: str) -> HeldGrants:
    """Read only this user's reachable graph, never silently skip unreadable rows."""
    out = HeldGrants([], set(), [], [])
    queue = [(user, True)]
    seen = set()
    while queue:
        name, is_user = queue.pop()
        if (name, is_user) in seen:
            continue
        seen.add((name, is_user))
        field = "user_name" if is_user else "role_name"
        params = {"principal": name}
        for row in session.rows(
            "SELECT access_type, database, table, column, is_partial_revoke, "
            "grant_option, access_object "
            f"FROM system.grants WHERE {field} = {{principal:String}}",
            params,
        ):
            out.grants.append((name, is_user, Grant(*row)))
        for role, admin in session.rows(
            "SELECT granted_role_name, with_admin_option FROM system.role_grants "
            f"WHERE {field} = {{principal:String}}",
            params,
        ):
            out.roles.add(role)
            if admin:
                out.admin_roles.append(role)
            queue.append((role, False))
    out.final = [r[0] for r in session.rows("SHOW GRANTS FINAL")]
    return out


def audit_grants(
    held: HeldGrants, user: str, target: str, target_is_user: bool, database: str
) -> list[str]:
    reasons = [f"role {r} is granted WITH ADMIN OPTION" for r in held.admin_roles]
    # A separate pass is essential: structured rows omit the wildcard suffix.
    for line in held.final:
        try:
            tokens = tokenize(line)
        except GrantParseError:
            reasons.append("unrecognized SHOW GRANTS FINAL output")
            continue
        for i, (kind, value) in enumerate(tokens):
            suffix = value == "*" and i > 0 and tokens[i - 1][0] in {"name", "word"}
            if "*" in value and (value != "*" or kind == "name" or suffix):
                reasons.append("wildcard database/table name in held grants")
    for principal, is_user, grant in held.grants:
        desc = grant.description
        who = "direct grant" if is_user else f"role {principal}: user holds"
        if grant.grant_option:
            reasons.append(f"{who} {desc} WITH GRANT OPTION")
        if grant.partial_revoke:
            reasons.append(f"{who} {desc}: partial revoke")
        if not is_user and (target_is_user or principal != target):
            reasons.append(
                f"role {principal} grants {desc} without {target}; "
                "only the policy target may carry grants"
            )
            continue
        if is_user and not target_is_user:
            reasons.append(
                f"direct {grant.privilege} grant ON {grant.database or '*'}.{grant.table or '*'} "
                f"bypasses role {target}"
            )
            continue
        if grant.database is None or grant.access_object:
            reasons.append(f"{who} {desc} is not allowed")
            continue
        try:
            validate_identifier("grant database", grant.database)
            if grant.table is not None:
                validate_identifier("grant table", grant.table)
        except ValueError as e:
            reasons.append(f"{who} {desc}: {e}")
            continue
        allowed = {"SELECT", "SHOW ROW POLICIES"} if grant.database == database else {"SELECT"}
        if grant.privilege not in allowed or (
            grant.database == database and (grant.table is not None or grant.column is not None)
        ):
            reasons.append(f"{who} {desc} is not allowed")
    return reasons
