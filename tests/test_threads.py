"""Tests for the pure review-thread parser and fact mapping."""

from __future__ import annotations

from aigate.threads import parse_review_threads, to_facts

REVIEWER = "cursor[bot]"


def _payload(threads: list[dict], author: str = "alice") -> dict:
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "author": {"login": author},
                    "reviewThreads": {"nodes": threads},
                }
            }
        }
    }


def _thread(
    *, author: str, resolved: bool, resolved_by: str | None, path: str = "app/x.py"
) -> dict:
    return {
        "id": f"T_{author}_{path}",
        "isResolved": resolved,
        "isOutdated": False,
        "path": path,
        "resolvedBy": {"login": resolved_by} if resolved_by else None,
        "comments": {"nodes": [{"author": {"login": author}}]},
    }


def test_only_reviewer_threads_are_kept() -> None:
    data = _payload(
        [
            _thread(author=REVIEWER, resolved=False, resolved_by=None),
            _thread(author="alice", resolved=True, resolved_by="alice"),  # human
            _thread(author="other[bot]", resolved=False, resolved_by=None),  # other bot
        ]
    )
    pr_author, findings = parse_review_threads(data, REVIEWER)
    assert pr_author == "alice"
    assert len(findings) == 1
    assert findings[0].author_login == REVIEWER


def test_graphql_bot_login_without_suffix_still_matches() -> None:
    # GraphQL returns Bot.login as "cursor"; REST spells it "cursor[bot]".
    data = _payload([_thread(author="cursor", resolved=False, resolved_by=None)])
    _, findings = parse_review_threads(data, "cursor[bot]")
    assert len(findings) == 1
    assert findings[0].author_login == "cursor"


def test_reviewer_configured_without_suffix_also_matches_rest_spelling() -> None:
    # The reverse spelling also matches.
    data = _payload([_thread(author="cursor[bot]", resolved=False, resolved_by=None)])
    _, findings = parse_review_threads(data, "cursor")
    assert len(findings) == 1


def test_other_bots_still_excluded_after_suffix_normalisation() -> None:
    # Normalising the [bot] suffix must not collapse two different bots together.
    data = _payload([_thread(author="other[bot]", resolved=False, resolved_by=None)])
    _, findings = parse_review_threads(data, "cursor[bot]")
    assert findings == []


def test_resolution_and_resolver_are_extracted() -> None:
    data = _payload(
        [_thread(author=REVIEWER, resolved=True, resolved_by="bob")], author="alice"
    )
    _, findings = parse_review_threads(data, REVIEWER)
    assert findings[0].is_resolved is True
    assert findings[0].resolved_by_login == "bob"


def test_missing_resolved_by_is_none() -> None:
    data = _payload([_thread(author=REVIEWER, resolved=False, resolved_by=None)])
    _, findings = parse_review_threads(data, REVIEWER)
    assert findings[0].resolved_by_login is None


def test_empty_or_malformed_payload_is_safe() -> None:
    assert parse_review_threads({}, REVIEWER) == (None, [])
    assert parse_review_threads({"data": {"repository": None}}, REVIEWER) == (None, [])


def test_to_facts_flags_author_self_resolution() -> None:
    data = _payload(
        [_thread(author=REVIEWER, resolved=True, resolved_by="alice")], author="alice"
    )
    pr_author, findings = parse_review_threads(data, REVIEWER)
    facts = to_facts(findings[0], pr_author_login=pr_author)
    assert facts.resolver_is_author is True
    assert facts.has_machine_evidence is False
    assert facts.resolver_is_eligible_owner is False


def test_to_facts_non_author_resolver_is_not_flagged_as_author() -> None:
    data = _payload(
        [_thread(author=REVIEWER, resolved=True, resolved_by="bob")], author="alice"
    )
    pr_author, findings = parse_review_threads(data, REVIEWER)
    facts = to_facts(findings[0], pr_author_login=pr_author)
    assert facts.resolver_is_author is False
