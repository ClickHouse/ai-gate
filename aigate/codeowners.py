"""Parse CODEOWNERS and decide whether a thread's resolver is an eligible owner.

Pure: the caller fetches CODEOWNERS from the base branch and resolves team
memberships.

Supports the common gitignore-style subset: a leading ``/`` anchors to the
root, a trailing ``/`` matches a directory and its contents, ``*`` matches
within a segment, ``**`` across segments, and a pattern without a slash matches
in any directory. The last matching rule wins. Character classes are not
supported.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache


@dataclass(frozen=True)
class Rule:
    pattern: str
    owners: tuple[str, ...]  # e.g. ("@alice", "@org/team")


def parse(text: str) -> list[Rule]:
    """Parse CODEOWNERS text into rules, in file order."""
    rules: list[Rule] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        pattern, owners = parts[0], tuple(p for p in parts[1:] if p.startswith("@"))
        if owners:
            rules.append(Rule(pattern=pattern, owners=owners))
    return rules


@cache
def _compile(pattern: str) -> re.Pattern[str]:
    p = pattern
    anchored = p.startswith("/")
    if anchored:
        p = p[1:]
    dir_only = p.endswith("/")
    if dir_only:
        p = p[:-1]

    out: list[str] = []
    i, n = 0, len(p)
    while i < n:
        if p[i : i + 2] == "**":
            out.append(".*")
            i += 2
        elif p[i] == "*":
            out.append("[^/]*")
            i += 1
        elif p[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(p[i]))
            i += 1
    body = "".join(out)

    # A pattern with no internal slash matches in any directory.
    prefix = "" if anchored or "/" in p else "(?:.*/)?"
    # A directory pattern (or any match) also matches everything beneath it.
    suffix = "(?:/.*)?"
    return re.compile("^" + prefix + body + suffix + "$")


def _matches(pattern: str, path: str) -> bool:
    return _compile(pattern).match(path) is not None


def owners_for_path(rules: list[Rule], path: str) -> tuple[str, ...]:
    """Return the owners of ``path`` under GitHub's last-matching-rule-wins semantics."""
    winner: tuple[str, ...] = ()
    for rule in rules:
        if _matches(rule.pattern, path):
            winner = rule.owners
    return winner


def is_eligible_resolver(
    owners: tuple[str, ...],
    resolver_login: str,
    resolver_team_slugs: frozenset[str],
) -> bool:
    """Return True if ``resolver_login`` is one of ``owners``, directly or via a team.

    ``resolver_team_slugs`` is the set of ``org/team`` slugs the resolver
    belongs to.
    """
    for owner in owners:
        handle = owner.lstrip("@")
        if "/" in handle:
            if handle.lower() in {t.lower() for t in resolver_team_slugs}:
                return True
        elif handle.lower() == resolver_login.lower():
            return True
    return False
