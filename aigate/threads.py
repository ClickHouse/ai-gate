"""Parse a pull request's review threads into ledger facts.

A thread belongs to a reviewer when its first comment was posted by the
reviewer's bot login. The caller supplies two facts, both defaulting to
``False``:

* ``has_machine_evidence``: a signal that the fix works, such as a test tied to
  the finding. ``isOutdated`` is not evidence: it only means the code moved,
  which the author controls.
* ``resolver_is_eligible_owner``: the resolver owns the path per CODEOWNERS on
  the base branch.

GitHub returns at most 100 threads per page; ``has_more_threads`` lets the
caller fail closed on a truncated list.
"""

from __future__ import annotations

from dataclasses import dataclass

from .ledger import FindingFacts


def _same_actor(a: str, b: str) -> bool:
    """Compare two logins, ignoring case and a ``[bot]`` suffix.

    GraphQL ``Bot.login`` drops the ``[bot]`` suffix that REST keeps, so
    ``cursor[bot]`` comes back as ``cursor`` in the thread query.
    """
    return a.casefold().removesuffix("[bot]") == b.casefold().removesuffix("[bot]")


@dataclass(frozen=True)
class ObservedFinding:
    """One review thread started by the reviewer."""

    thread_id: str
    path: str | None
    is_resolved: bool
    is_outdated: bool
    resolved_by_login: str | None
    author_login: str  # the bot that posted the finding
    url: str | None = None  # the finding's first comment, for linking it


def parse_review_threads(
    data: dict, reviewer_login: str
) -> tuple[str | None, list[ObservedFinding]]:
    """Extract ``(pr_author_login, findings)`` from a reviewThreads payload.

    Keep only threads whose first comment was posted by ``reviewer_login``.
    """
    pr = (((data or {}).get("data") or {}).get("repository") or {}).get(
        "pullRequest"
    ) or {}
    pr_author = ((pr.get("author") or {}).get("login")) or None

    findings: list[ObservedFinding] = []
    nodes = ((pr.get("reviewThreads") or {}).get("nodes")) or []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        comments = ((node.get("comments") or {}).get("nodes")) or []
        first = comments[0] if comments else {}
        author_login = ((first.get("author") or {}).get("login")) or ""
        if not author_login or not _same_actor(author_login, reviewer_login):
            continue
        resolved_by = node.get("resolvedBy") or None
        findings.append(
            ObservedFinding(
                thread_id=node.get("id", ""),
                path=node.get("path"),
                is_resolved=bool(node.get("isResolved")),
                is_outdated=bool(node.get("isOutdated")),
                resolved_by_login=(resolved_by.get("login") if resolved_by else None),
                author_login=author_login,
                url=first.get("url") or None,
            )
        )
    return pr_author, findings


def has_more_threads(data: dict) -> bool:
    """Return True when the ``reviewThreads`` page is not the last one.

    A missing ``pageInfo`` reads as not truncated.
    """
    pr = (((data or {}).get("data") or {}).get("repository") or {}).get(
        "pullRequest"
    ) or {}
    page_info = ((pr.get("reviewThreads") or {}).get("pageInfo")) or {}
    return bool(page_info.get("hasNextPage"))


def to_facts(
    finding: ObservedFinding,
    *,
    pr_author_login: str | None,
    has_machine_evidence: bool = False,
    resolver_is_eligible_owner: bool = False,
) -> FindingFacts:
    """Map an observed finding to ledger facts."""
    resolver_is_author = (
        finding.resolved_by_login is not None
        and pr_author_login is not None
        and finding.resolved_by_login == pr_author_login
    )
    return FindingFacts(
        finding_id=finding.thread_id,
        resolved=finding.is_resolved,
        has_machine_evidence=has_machine_evidence,
        resolver_is_eligible_owner=resolver_is_eligible_owner,
        resolver_is_author=resolver_is_author,
    )
