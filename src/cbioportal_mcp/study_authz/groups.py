"""cBioPortal's study-access matching rules, as a Python reference.

The SQL in ``templates/0-user-study.sql.tmpl`` implements the same rules; the
live tests check the two against each other. ``normalize_authority`` is not
used by the SQL yet: it is the rule the future per-email (JWT) source must
apply to IdP claims before writing grant keys (docs/study-authz.md).

Rules, from cBioPortal's ``CancerStudyPermissionEvaluator``:

- ``cancer_study.groups`` is a ';'-separated list. Empty entries are dropped;
  each remaining entry is compared whole and case-insensitively. Nothing is
  trimmed (``' PUBLIC'`` is not ``'PUBLIC'``) and there is no substring/LIKE
  matching.
- A study is public iff one of its group entries equals the deployment's
  public group (cBioPortal's ``always_show_study_group``).
- An authority has a leading ``ROLE_`` stripped. With
  ``filter_groups_by_appname`` (cBioPortal's default) only ``<app>:<X>``
  authorities count (app name compared case-insensitively) and ``X`` is the
  grant key; without it the whole stripped authority is the key.
- A grant key (upper-cased) grants a study when it equals the study id, equals
  one of its group entries, is ``ALL`` (every study), ``ALL_TCGA`` (studies
  ending in ``_TCGA``) or ``ALL_NCI_TARGET`` (studies ending in ``_TARGET`` plus
  ``ALL_TARGET_PHASE1`` / ``ALL_TARGET_PHASE2``).
"""

from __future__ import annotations

import re

# Group / app names end up inside generated SQL string literals. Always use
# fullmatch: with .match(), `$` would accept a trailing newline.
NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,128}")
# Template placeholder tokens (sqlgen.render); never allowed inside a value.
PLACEHOLDER_RE = re.compile(r"__[A-Z_]+__")


def validate_name(kind: str, value: str) -> str:
    if not isinstance(value, str) or not NAME_RE.fullmatch(value):
        raise ValueError(f"{kind} must be non-empty and match {NAME_RE.pattern}, got {value!r}")
    if PLACEHOLDER_RE.search(value):
        raise ValueError(f"{kind} must not contain a template placeholder, got {value!r}")
    return value


def group_entries(groups: str | None) -> list[str]:
    """Upper-cased, non-empty ';' entries of cancer_study.groups (no trimming)."""
    return [entry.upper() for entry in (groups or "").split(";") if entry != ""]


def is_public(groups: str | None, public_group: str) -> bool:
    return public_group.upper() in group_entries(groups)


def normalize_authority(
    authority: str, app_name: str, filter_groups_by_appname: bool = True
) -> str | None:
    """The upper-cased grant key an authority yields, or None if it yields none."""
    stripped = authority[len("ROLE_") :] if authority.startswith("ROLE_") else authority
    if filter_groups_by_appname:
        prefix = app_name.upper() + ":"
        if not stripped.upper().startswith(prefix):
            return None
        stripped = stripped[len(prefix) :]
    key = stripped.upper()
    return key or None


def grant_key_matches(grant_key: str, study_id: str, groups: str | None) -> bool:
    key = grant_key.upper()
    study = study_id.upper()
    if key == "":
        return False
    if key in (study, "ALL") or key in group_entries(groups):
        return True
    if key == "ALL_TCGA":
        return study.endswith("_TCGA")
    if key == "ALL_NCI_TARGET":
        return study.endswith("_TARGET") or study in ("ALL_TARGET_PHASE1", "ALL_TARGET_PHASE2")
    return False
