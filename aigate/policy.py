"""The optional per-repository policy: who must sign off on what.

A repository opts in with a ``policy:`` block in ``.github/ai-gate.yml``, read
from the base branch so a pull request cannot loosen its own rules. Without the
block a clean AI review is enough; with it, ``escalation`` applies.

::

    escalation_paths:
      - paths: ["auth/**", "infra/**", "**/*secret*"]
        reviewers: "@org/security"
    security_dismissal_reviewers: "@org/security"

* ``escalation_paths``: a changed file or reviewer finding (fixed or not) on a
  matching path needs an approving review from a non-author member of
  ``reviewers``.
* ``security_dismissal_reviewers``: a dismissed finding (resolved but not
  fixed) needs an approving review from a non-author member.

A handle is a team (``@org/team``) or a user (``@login``). A malformed block
fails closed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import yaml

from .tiers import path_matches

# Named in error messages: the file the author edits.
POLICY_PATH = ".github/ai-gate.yml"

# @org/team or @login. Team slugs may also contain underscores and dots.
_HANDLE = re.compile(r"^@[A-Za-z0-9-]+(?:/[A-Za-z0-9_.-]+)?$")

_KNOWN_KEYS = frozenset({"escalation_paths", "security_dismissal_reviewers"})


class PolicyError(ValueError):
    """The ``policy:`` block of the repository's ai-gate.yml is malformed."""


@dataclass(frozen=True)
class EscalationRule:
    paths: tuple[str, ...]
    """Globs over repo-relative paths (same syntax as the sensitivity map)."""

    reviewers: str
    """The required approver: ``@org/team`` or ``@login``."""

    def matches(self, path: str) -> bool:
        return any(path_matches(path, glob) for glob in self.paths)


@dataclass(frozen=True)
class GatePolicy:
    escalation_paths: tuple[EscalationRule, ...] = ()
    security_dismissal_reviewers: str | None = None

    def escalation_reviewers_for(self, path: str) -> tuple[str, ...]:
        """Return the deduplicated approver handles required for ``path``."""
        return tuple(
            dict.fromkeys(
                rule.reviewers for rule in self.escalation_paths if rule.matches(path)
            )
        )

    def team_handles(self) -> frozenset[str]:
        """Return the ``org/team`` slugs this policy names (for membership resolution)."""
        handles = [r.reviewers for r in self.escalation_paths]
        if self.security_dismissal_reviewers:
            handles.append(self.security_dismissal_reviewers)
        return frozenset(h.lstrip("@") for h in handles if "/" in h)


def load_policy(text: str | None) -> GatePolicy | None:
    """Parse the base-branch policy; return ``None`` when it is absent.

    Anything present must validate fully. An empty or malformed policy is an
    error rather than no policy, so a team never believes a rule is in force
    when it is not.
    """
    if text is None:
        return None

    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PolicyError(f"{POLICY_PATH} is not valid YAML: {exc}") from exc

    if doc is None:
        raise PolicyError(
            f"{POLICY_PATH} is empty. Remove the file to have no escalation "
            f"policy, or declare one."
        )
    if not isinstance(doc, dict):
        raise PolicyError(f"{POLICY_PATH} must be a mapping.")

    unknown = sorted(set(doc) - _KNOWN_KEYS)
    if unknown:
        raise PolicyError(
            f"{POLICY_PATH}: unknown key(s) {', '.join(map(repr, unknown))}. "
            f"Known keys: {', '.join(sorted(_KNOWN_KEYS))}."
        )

    rules: list[EscalationRule] = []
    entries = doc.get("escalation_paths", [])
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise PolicyError(f"{POLICY_PATH}: 'escalation_paths' must be a list.")
    for i, entry in enumerate(entries):
        rules.append(_parse_rule(entry, i))

    security = doc.get("security_dismissal_reviewers")
    if security is not None:
        _check_handle(security, "security_dismissal_reviewers")

    return GatePolicy(
        escalation_paths=tuple(rules), security_dismissal_reviewers=security
    )


def _parse_rule(entry: object, index: int) -> EscalationRule:
    where = f"{POLICY_PATH}: escalation_paths[{index}]"
    if not isinstance(entry, dict):
        raise PolicyError(f"{where} must be a mapping with 'paths' and 'reviewers'.")
    unknown = sorted(set(entry) - {"paths", "reviewers"})
    if unknown:
        raise PolicyError(f"{where}: unknown key(s) {', '.join(map(repr, unknown))}.")

    paths = entry.get("paths")
    if not isinstance(paths, list) or not paths:
        raise PolicyError(f"{where}: 'paths' must be a non-empty list of globs.")
    for p in paths:
        if not isinstance(p, str) or not p.strip():
            raise PolicyError(f"{where}: path globs must be non-empty strings.")

    reviewers = entry.get("reviewers")
    _check_handle(reviewers, f"escalation_paths[{index}].reviewers")
    assert isinstance(reviewers, str)
    return EscalationRule(paths=tuple(p.strip() for p in paths), reviewers=reviewers)


def _check_handle(value: object, field: str) -> None:
    if not isinstance(value, str) or not _HANDLE.match(value):
        raise PolicyError(
            f"{POLICY_PATH}: '{field}' must be one handle, '@org/team' or "
            f"'@login', got {value!r}."
        )
