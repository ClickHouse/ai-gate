"""Tests for the pure enforcing decision (findings + tier + policy + approvals)."""

from __future__ import annotations

from aigate.escalation import (
    Approval,
    Requirement,
    State,
    approving_logins,
    evaluate,
    state_of,
)
from aigate.gate import FAILURE, PENDING, SUCCESS
from aigate.ledger import Disposition, FindingVerdict
from aigate.policy import EscalationRule, GatePolicy
from aigate.threads import ObservedFinding
from aigate.tiers import TIER_0, TIER_1, TIER_3, TierResult

HEAD = "head000"
PRODSEC = "@example-org/prodsec"
POLICY = GatePolicy(
    escalation_paths=(EscalationRule(paths=("auth/**",), reviewers=PRODSEC),),
    security_dismissal_reviewers=PRODSEC,
)
DOCS = TierResult(tier=TIER_0, reasons=("documentation",))
CODE = TierResult(tier=TIER_1, reasons=("ordinary application code",))
INFRA = TierResult(tier=TIER_3, reasons=("infrastructure as code",))


def _finding(path: str, *, resolved: bool, by: str | None = None) -> ObservedFinding:
    return ObservedFinding(
        thread_id=f"T_{path}",
        path=path,
        is_resolved=resolved,
        is_outdated=False,
        resolved_by_login=by,
        author_login="cursor[bot]",
    )


def _verdict(path: str, disposition: Disposition) -> FindingVerdict:
    return FindingVerdict(f"T_{path}", disposition, disposition.value)


def _review(
    login: str,
    state: str,
    *,
    commit: str = HEAD,
    at: str = "2026-10-19T10:00:00Z",
    rid: int = 1,
    kind: str = "User",
) -> dict:
    return {
        "id": rid,
        "user": {"login": login, "type": kind},
        "state": state,
        "commit_id": commit,
        "submitted_at": at,
    }


# --- approving_logins ------------------------------------------------------


def test_approval_on_head_by_non_author_human_counts() -> None:
    assert approving_logins(
        [_review("bob", "APPROVED")], head_sha=HEAD, pr_author="alice"
    ) == ["bob"]


def test_author_approval_never_counts() -> None:
    assert (
        approving_logins(
            [_review("alice", "APPROVED")], head_sha=HEAD, pr_author="alice"
        )
        == []
    )
    # Case-insensitively, as GitHub logins are.
    assert (
        approving_logins(
            [_review("Alice", "APPROVED")], head_sha=HEAD, pr_author="alice"
        )
        == []
    )


def test_bot_approval_never_counts() -> None:
    assert (
        approving_logins(
            [_review("some[bot]", "APPROVED", kind="Bot")], head_sha=HEAD, pr_author="a"
        )
        == []
    )


def test_stale_approval_on_an_earlier_commit_does_not_count() -> None:
    # The approval predates the code (and any dismissals) on the head commit.
    assert (
        approving_logins(
            [_review("bob", "APPROVED", commit="old111")], head_sha=HEAD, pr_author="a"
        )
        == []
    )


def test_latest_stance_wins_over_an_earlier_approval() -> None:
    reviews = [
        _review("bob", "APPROVED", at="2026-10-19T10:00:00Z", rid=1),
        _review("bob", "CHANGES_REQUESTED", at="2026-10-19T11:00:00Z", rid=2),
    ]
    assert approving_logins(reviews, head_sha=HEAD, pr_author="a") == []
    # ... and the reverse order of submission approves.
    reviews = [
        _review("bob", "CHANGES_REQUESTED", at="2026-10-19T10:00:00Z", rid=1),
        _review("bob", "APPROVED", at="2026-10-19T11:00:00Z", rid=2),
    ]
    assert approving_logins(reviews, head_sha=HEAD, pr_author="a") == ["bob"]


def test_dismissed_approval_does_not_count() -> None:
    # GitHub flips a dismissed review's state to DISMISSED in place.
    assert (
        approving_logins([_review("bob", "DISMISSED")], head_sha=HEAD, pr_author="a")
        == []
    )


def test_a_later_comment_does_not_revoke_an_approval() -> None:
    reviews = [
        _review("bob", "APPROVED", at="2026-10-19T10:00:00Z", rid=1),
        _review("bob", "COMMENTED", at="2026-10-19T11:00:00Z", rid=2),
    ]
    assert approving_logins(reviews, head_sha=HEAD, pr_author="a") == ["bob"]


def test_malformed_review_entries_are_ignored() -> None:
    reviews: list = [None, {}, {"user": None, "state": "APPROVED"}]
    assert approving_logins(reviews, head_sha=HEAD, pr_author="a") == []


# --- Requirement.satisfied_by -------------------------------------------------


def test_team_requirement_needs_membership() -> None:
    req = Requirement(PRODSEC, "r")
    assert req.satisfied_by([Approval("bob", frozenset())]) is None
    assert req.satisfied_by([Approval("bob", frozenset({"example-org/prodsec"}))])


def test_user_requirement_matches_login_case_insensitively() -> None:
    req = Requirement("@Alice", "r")
    assert req.satisfied_by([Approval("alice", frozenset())])
    assert req.satisfied_by([Approval("bob", frozenset())]) is None


def test_unnamed_requirement_is_met_only_by_someone_with_write_access() -> None:
    req = Requirement(None, "r")
    assert req.satisfied_by([]) is None
    assert req.satisfied_by([Approval("bob", frozenset(), can_write=True)])
    # On a public repository anyone can approve; that must not count.
    assert req.satisfied_by([Approval("stranger", frozenset())]) is None


# --- state_of / evaluate -------------------------------------------------------


def test_state_mapping_from_ledger_dispositions() -> None:
    assert state_of(_verdict("p", Disposition.ADDRESSED)) is State.FIXED
    assert state_of(_verdict("p", Disposition.UNRESOLVED)) is State.UNRESOLVED
    assert state_of(_verdict("p", Disposition.HAND_WAVED)) is State.DISMISSED
    assert state_of(_verdict("p", Disposition.OWNER_ACCEPTED)) is State.DISMISSED


def test_all_fixed_on_low_tier_is_green_with_no_approvals() -> None:
    f = [_finding("src/x.py", resolved=False)]
    v = [_verdict("src/x.py", Disposition.ADDRESSED)]
    d = evaluate(policy=POLICY, tier_result=CODE, findings=f, verdicts=v, approvals=[])
    assert d.outcome == SUCCESS


def test_dismissed_security_finding_without_prodsec_approval_is_red() -> None:
    f = [_finding("src/x.py", resolved=True, by="alice")]
    v = [_verdict("src/x.py", Disposition.HAND_WAVED)]
    # A non-author outside the named team is not enough.
    approvals = [Approval("bob", frozenset())]
    d = evaluate(
        policy=POLICY, tier_result=CODE, findings=f, verdicts=v, approvals=approvals
    )
    assert d.outcome == FAILURE
    assert PRODSEC in d.summary and "MISSING" in d.summary


def test_dismissed_security_finding_with_prodsec_approval_is_green() -> None:
    f = [_finding("src/x.py", resolved=True, by="alice")]
    v = [_verdict("src/x.py", Disposition.HAND_WAVED)]
    approvals = [Approval("carol", frozenset({"example-org/prodsec"}))]
    d = evaluate(
        policy=POLICY, tier_result=CODE, findings=f, verdicts=v, approvals=approvals
    )
    assert d.outcome == SUCCESS
    assert "approved by carol" in d.summary


def test_without_security_reviewers_the_ledger_verdict_decides_a_dismissal() -> None:
    policy = GatePolicy()  # opted in, nothing configured
    f = [_finding("src/x.py", resolved=True, by="bob")]
    red = evaluate(
        policy=policy,
        tier_result=CODE,
        findings=f,
        verdicts=[_verdict("src/x.py", Disposition.HAND_WAVED)],
        approvals=[Approval("bob", frozenset())],
    )
    assert red.outcome == FAILURE
    green = evaluate(
        policy=policy,
        tier_result=CODE,
        findings=f,
        verdicts=[_verdict("src/x.py", Disposition.OWNER_ACCEPTED)],
        approvals=[],
    )
    assert green.outcome == SUCCESS


def test_escalation_path_applies_even_to_a_fixed_finding() -> None:
    f = [_finding("auth/login.py", resolved=False)]
    v = [_verdict("auth/login.py", Disposition.ADDRESSED)]
    red = evaluate(
        policy=POLICY, tier_result=CODE, findings=f, verdicts=v, approvals=[]
    )
    assert red.outcome == FAILURE
    assert "escalation path" in red.summary
    green = evaluate(
        policy=POLICY,
        tier_result=CODE,
        findings=f,
        verdicts=v,
        approvals=[Approval("carol", frozenset({"example-org/prodsec"}))],
    )
    assert green.outcome == SUCCESS


def test_tier_3_needs_a_human_even_with_no_findings() -> None:
    red = evaluate(
        policy=POLICY, tier_result=INFRA, findings=[], verdicts=[], approvals=[]
    )
    assert red.outcome == FAILURE
    assert "always needs a human" in red.summary
    green = evaluate(
        policy=POLICY,
        tier_result=INFRA,
        findings=[],
        verdicts=[],
        approvals=[Approval("bob", frozenset(), can_write=True)],
    )
    assert green.outcome == SUCCESS


def test_docs_tier_with_no_findings_needs_nobody() -> None:
    d = evaluate(
        policy=POLICY, tier_result=DOCS, findings=[], verdicts=[], approvals=[]
    )
    assert d.outcome == SUCCESS
    assert d.title == "AI review passed"


def test_unresolved_finding_is_pending_unless_something_is_red() -> None:
    f = [_finding("src/x.py", resolved=False)]
    v = [_verdict("src/x.py", Disposition.UNRESOLVED)]
    d = evaluate(policy=POLICY, tier_result=CODE, findings=f, verdicts=v, approvals=[])
    assert d.outcome == PENDING
    # A missing required approval outranks "waiting", as in gate.decide.
    d = evaluate(policy=POLICY, tier_result=INFRA, findings=f, verdicts=v, approvals=[])
    assert d.outcome == FAILURE


def test_one_approval_can_satisfy_several_requirements() -> None:
    f = [
        _finding("auth/a.py", resolved=True, by="alice"),  # dismissed + escalation
        _finding("auth/b.py", resolved=False),  # fixed + escalation
    ]
    v = [
        _verdict("auth/a.py", Disposition.HAND_WAVED),
        _verdict("auth/b.py", Disposition.ADDRESSED),
    ]
    d = evaluate(
        policy=POLICY,
        tier_result=INFRA,
        findings=f,
        verdicts=v,
        approvals=[
            Approval("carol", frozenset({"example-org/prodsec"}), can_write=True)
        ],
    )
    assert d.outcome == SUCCESS


def test_a_missing_approval_is_retryable_so_the_wait_can_green_it() -> None:
    # Missing approval is red but retryable: one may arrive while the
    # workflow waits.
    red = evaluate(
        policy=POLICY, tier_result=INFRA, findings=[], verdicts=[], approvals=[]
    )
    assert red.outcome == FAILURE and red.retryable is True


def test_an_unacceptable_dismissal_is_terminal_not_retryable() -> None:
    # A dismissal no approver can accept needs a code change: not retryable.
    policy = GatePolicy()  # nothing configured; the ledger verdict decides
    f = [_finding("src/x.py", resolved=True, by="bob")]
    red = evaluate(
        policy=policy,
        tier_result=CODE,
        findings=f,
        verdicts=[_verdict("src/x.py", Disposition.HAND_WAVED)],
        approvals=[Approval("bob", frozenset())],
    )
    assert red.outcome == FAILURE and red.retryable is False
