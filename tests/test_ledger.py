"""Tests for the pure finding-ledger decision model."""

from __future__ import annotations

from aigate.ledger import Disposition, FindingFacts, dispose


def _facts(**kw: object) -> FindingFacts:
    base: dict[str, object] = {
        "finding_id": "f1",
        "resolved": False,
        "has_machine_evidence": False,
        "resolver_is_eligible_owner": False,
        "resolver_is_author": False,
    }
    base.update(kw)
    return FindingFacts(**base)  # type: ignore[arg-type]


def test_machine_evidence_closes_regardless_of_thread() -> None:
    v = dispose(_facts(has_machine_evidence=True, resolved=False))
    assert v.disposition is Disposition.ADDRESSED
    assert v.is_green


def test_unresolved_with_no_evidence_is_pending() -> None:
    v = dispose(_facts(resolved=False, has_machine_evidence=False))
    assert v.disposition is Disposition.UNRESOLVED
    assert not v.is_green


def test_author_cannot_self_clear_a_security_refutation() -> None:
    v = dispose(_facts(resolved=True, resolver_is_author=True))
    assert v.disposition is Disposition.HAND_WAVED


def test_eligible_non_author_owner_can_accept_the_risk() -> None:
    v = dispose(
        _facts(
            resolved=True,
            resolver_is_author=False,
            resolver_is_eligible_owner=True,
        )
    )
    assert v.disposition is Disposition.OWNER_ACCEPTED
    assert v.is_green


def test_resolver_who_is_not_an_owner_is_hand_waved() -> None:
    v = dispose(
        _facts(
            resolved=True, resolver_is_eligible_owner=False, resolver_is_author=False
        )
    )
    assert v.disposition is Disposition.HAND_WAVED
