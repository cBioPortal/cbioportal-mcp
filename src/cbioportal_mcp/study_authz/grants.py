"""Fail-closed audit using the runtime user's own SHOW GRANTS and FINAL.

These commands expose only held grants and need no system-table SELECT or
SHOW USERS/ROLES privilege. FINAL includes dormant and nested roles. Role
policy targets require exactly the target role in the direct grant set.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from cbioportal_mcp.study_authz.coverage import PolicyRow, _squash, view_sql_security
from cbioportal_mcp.study_authz.sqlgen import OWNED_POLICY_PREFIX, validate_identifier

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
    privileges: tuple[str, ...]
    database: str
    table: str
    columns: bool = False
    grant_option: bool = False

    @property
    def description(self) -> str:
        return f"{', '.join(self.privileges)} ON {self.database}.{self.table}"


@dataclass
class GrantSet:
    grants: list[Grant] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    admin_option: bool = False


def _split(tokens, separator):
    parts, current, depth = [], [], 0
    for token in tokens:
        if token == ("punct", "("):
            depth += 1
        if token == ("punct", ")"):
            depth -= 1
        if depth < 0:
            raise GrantParseError("unbalanced parentheses")
        if not depth and token == ("punct", separator):
            if not current:
                raise GrantParseError("empty list item")
            parts.append(current)
            current = []
        else:
            current.append(token)
    if depth or not current:
        raise GrantParseError("malformed list")
    return parts + [current]


def _name(tokens):
    if len(tokens) == 2 and tokens[1] == ("word", "*"):
        return _name(tokens[:1]) + "*"
    if len(tokens) != 1 or tokens[0][0] not in {"word", "name"}:
        raise GrantParseError("expected one identifier")
    if tokens[0][0] == "name" and "*" in tokens[0][1]:
        raise GrantParseError("wildcard database/table name")
    return tokens[0][1]


def parse_grants(lines: list[str], principal: str | None = None) -> GrantSet:
    result = GrantSet()
    for raw in lines:
        tokens = tokenize(raw)
        if not tokens or tokens[0] != ("word", "GRANT"):
            raise GrantParseError("unrecognized grant or partial revoke")
        # Keywords inside quoted names are never syntax.
        to_positions = [i for i, t in enumerate(tokens) if t == ("word", "TO")]
        if len(to_positions) != 1:
            raise GrantParseError("expected TO principal")
        to = to_positions[0]
        tail = tokens[to + 1 :]
        option = None
        if len(tail) >= 4 and tail[-3:] in (
            [("word", "WITH"), ("word", "GRANT"), ("word", "OPTION")],
            [("word", "WITH"), ("word", "ADMIN"), ("word", "OPTION")],
        ):
            option = tail[-2][1]
            tail = tail[:-3]
        grantee = _name(tail)
        if principal is not None and grantee != principal:
            raise GrantParseError("grant for another principal")
        body = tokens[1:to]
        if ("word", "ON") not in body:
            if option == "GRANT":
                raise GrantParseError("grant option on role grant")
            result.roles.extend(_name(part) for part in _split(body, ","))
            result.admin_option |= option == "ADMIN"
            continue
        if option == "ADMIN":
            raise GrantParseError("admin option on privilege grant")
        # ClickHouse may combine multiple ON scopes in one line.
        while body:
            try:
                on = body.index(("word", "ON"))
            except ValueError:
                raise GrantParseError("missing ON") from None
            privileges, columns = [], False
            for part in _split(body[:on], ","):
                if ("punct", "(") in part:
                    opening = part.index(("punct", "("))
                    if part[-1] != ("punct", ")"):
                        raise GrantParseError("malformed columns")
                    for col in _split(part[opening + 1 : -1], ","):
                        _name(col)
                    columns = True
                    part = part[:opening]
                if not part or any(k != "word" for k, _ in part):
                    raise GrantParseError("malformed privilege")
                privileges.append(" ".join(v for _, v in part))
            rest = body[on + 1 :]
            comma = next((i for i, t in enumerate(rest) if t == ("punct", ",")), len(rest))
            scope = _split(rest[:comma], ".")
            if len(scope) == 1 and _name(scope[0]) == "*":
                scope = [scope[0], scope[0]]  # global non-database privileges: ON *
            if len(scope) != 2:
                raise GrantParseError("expected database.table scope")
            result.grants.append(
                Grant(
                    tuple(privileges), _name(scope[0]), _name(scope[1]), columns, option == "GRANT"
                )
            )
            if comma < len(rest) and comma + 1 == len(rest):
                raise GrantParseError("trailing comma")
            body = rest[comma + 1 :]
    return result


@dataclass
class HeldGrants:
    own: GrantSet
    final: GrantSet

    @property
    def roles(self) -> set[str]:
        return set(self.own.roles)


def collect_grants(session, user: str) -> HeldGrants:
    own = parse_grants([r[0] for r in session.rows("SHOW GRANTS")], user)
    final = parse_grants([r[0] for r in session.rows("SHOW GRANTS FINAL")], user)
    return HeldGrants(own, final)


def audit_grants(
    held: HeldGrants, user: str, target: str, target_is_user: bool, database: str
) -> list[str]:
    reasons = []
    if held.own.admin_option or held.final.admin_option:
        reasons.append("role granted WITH ADMIN OPTION")
    if not target_is_user:
        if held.own.roles != [target]:
            reasons.append(f"only policy role {target} may be granted to the runtime user")
        for grant in held.own.grants:
            reasons.append(
                f"direct {grant.privileges[0]} grant ON {grant.database}.{grant.table} "
                f"bypasses role {target}"
            )
    for grant in held.final.grants:
        desc = grant.description
        if grant.grant_option:
            reasons.append(f"user holds {desc} WITH GRANT OPTION")
        if grant.database == "*":
            reasons.append(f"user holds {desc}: global grant is not allowed")
            continue
        if "*" in grant.database or ("*" in grant.table and grant.table != "*"):
            reasons.append(f"user holds {desc}: wildcard database/table name")
            continue
        try:
            validate_identifier("grant database", grant.database)
            if grant.table != "*":
                validate_identifier("grant table", grant.table)
        except ValueError as e:
            reasons.append(f"user holds {desc}: {e}")
            continue
        if grant.database == "system":
            reasons.append(f"user holds {desc}: grants on system are not allowed")
            continue
        allowed = {"SELECT", "SHOW ROW POLICIES"} if grant.database == database else {"SELECT"}
        if set(grant.privileges) - allowed or (
            grant.database == database and (grant.table != "*" or grant.columns)
        ):
            reasons.append(f"user holds {desc} is not allowed")
    return reasons


def selected_databases(held: HeldGrants) -> set[str]:
    return {g.database for g in held.final.grants if "SELECT" in g.privileges}


def _references(sql: str, databases: set[str]) -> bool:
    return any(
        re.search(rf"(?<![A-Za-z0-9_]){re.escape(db)}(?![A-Za-z0-9_])", sql) for db in databases
    )


def audit_other_database(
    database: str,
    objects: list[tuple[str, str, str]],
    policies: list[PolicyRow],
    principals: set[str],
    policy_databases: set[str],
    public_databases: frozenset[str],
) -> list[str]:
    reasons = []
    has_deny = any(
        row.policy.name.startswith(OWNED_POLICY_PREFIX)
        and row.policy.table == ""
        and _squash(row.policy.select_filter) == "0"
        and row.applies_to(principals)
        for row in policies
    )
    for name, engine, create in objects:
        where = f"{database}.{name}"
        if engine == "View" and view_sql_security(create) != "INVOKER":
            reasons.append(f"{where}: View must use SQL SECURITY INVOKER")
        elif engine == "Dictionary":
            reasons.append(f"{where}: Dictionary is not allowed")
        elif engine not in {
            "View",
            "Memory",
            "Null",
            "Set",
            "Join",
            "Log",
            "TinyLog",
            "StripeLog",
        } and not re.fullmatch(r"\w*MergeTree", engine):
            # Anything indirect (including new table-function engines) needs a
            # readable definition. Listing a database never overrides this.
            if not create:
                reasons.append(f"{where}: cannot read definition of {engine}")
            elif _references(create, policy_databases):
                reasons.append(f"{where}: {engine} definition names a policy database")
        if name == "cancer_study" and not has_deny and database not in public_databases:
            reasons.append(f"{database}: unfiltered study database is not declared public")
    return reasons
