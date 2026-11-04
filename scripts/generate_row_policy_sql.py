#!/usr/bin/env python3
"""Regenerate the checked-in study row-policy SQL from row_policy_catalog.py.

    uv run python scripts/generate_row_policy_sql.py          # rewrite the files
    uv run python scripts/generate_row_policy_sql.py --check  # exit 1 on drift

Writes sql/study-authz/0-row-policies.sql (acl source),
sql/study-authz/authorities/1-row-policies.sql (authorities source) and
sql/study-authz/authorities/0-user-study.sql (from user_study_sql.py). All keep
the __CLICKHOUSE_DATABASE__ / __CBIOMCP_*_SETTING__ placeholders that
scripts/apply_sql.sh substitutes at apply time.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cbioportal_mcp.authentication.row_policy_sql import (
    SOURCE_ACL,
    SOURCE_AUTHORITIES,
    render_row_policy_sql,
)
from cbioportal_mcp.authentication.user_study_sql import user_study_sql

REPO = Path(__file__).resolve().parent.parent
OUTPUTS = {
    SOURCE_ACL: REPO / "sql" / "study-authz" / "0-row-policies.sql",
    SOURCE_AUTHORITIES: REPO / "sql" / "study-authz" / "authorities" / "1-row-policies.sql",
}
USER_STUDY_OUTPUT = REPO / "sql" / "study-authz" / "authorities" / "0-user-study.sql"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail instead of writing on drift")
    args = parser.parse_args()
    drift = []
    targets = [(path, render_row_policy_sql(source)) for source, path in OUTPUTS.items()]
    targets.append((USER_STUDY_OUTPUT, user_study_sql()))
    for path, expected in targets:
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == expected:
            continue
        if args.check:
            drift.append(str(path.relative_to(REPO)))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(expected, encoding="utf-8")
        print(f"wrote {path.relative_to(REPO)}")
    if drift:
        print("out of date (run scripts/generate_row_policy_sql.py): " + ", ".join(drift))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
