"""The enforcing decision under a repository policy.

Pure, like ``gate.decide``: ``cli`` gathers the facts (threads, verdicts,
approving reviews, team memberships) and ``evaluate`` folds them into one
``Decision``. It runs only when the repository declares a ``policy:`` block and
the AI review is clean; under a policy a clean review is necessary, not
sufficient.

Each finding is in one of three states, derived from its ledger verdict:

* ``FIXED``: machine evidence shows the fix works. Passes on its own. GitHub's
  ``isOutdated`` is not such evidence (the author can make any thread outdated
  by moving code), so nothing reaches this state until a real signal is wired in.
* ``DISMISSED``: resolved but not fixed. Never passes on its own, since a
  dismissal is a risk acceptance and only a named human can make it.
* ``UNRESOLVED``: still open. The gate waits.

The gate is green when nothing is open and every requirement is met:

1. A dismissed finding needs an approving review from a non-author member of
   ``security_dismissal_reviewers``. Without that key, the ledger's rule
   applies: an eligible non-author code owner must have resolved it.
2. A finding or changed file on an ``escalation_paths`` path needs an approving
   review from a non-author member of that entry's reviewers.
3. A tier-3 change needs an approving review from someone other than the author
   with write access.

The gate does not judge whether a dismissal is right, only that the required
non-author took accountability. An approval counts only if it is the
reviewer's latest stance and was given on the head commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .codeowners import is_eligible_resolver
from .gate import FAILURE, PENDING, SUCCESS, Decision
from .ledger import Disposition, FindingVerdict
from .policy import GatePolicy
from .render import safe_inline
from .threads import ObservedFinding
from .tiers import TierResult

APPROVED = "APPROVED"

# Review states that set a reviewer's stance. COMMENTED and PENDING do not, so
# they are skipped. A dismissed approval has state DISMISSED.
_STANCES = frozenset({APPROVED, "CHANGES_REQUESTED", "DISMISSED"})


class State(Enum):
    FIXED = "fixed"
    DISMISSED = "dismissed"
    UNRESOLVED = "unresolved"


def state_of(verdict: FindingVerdict) -> State:
    if verdict.disposition is Disposition.ADDRESSED:
        return State.FIXED
    if verdict.disposition is Disposition.UNRESOLVED:
        return State.UNRESOLVED
    return State.DISMISSED


def approving_logins(
    reviews: list[dict], *, head_sha: str, pr_author: str | None
) -> list[str]:
    """Return logins of non-author humans whose latest stance approves ``head_sha``.

    ``reviews`` is the REST ``pulls/{n}/reviews`` list. Bots and the pull
    request author never count.
    """
    latest: dict[str, tuple[tuple[str, int], dict]] = {}
    for review in reviews:
        if not isinstance(review, dict):
            continue
        user = review.get("user") or {}
        login = user.get("login")
        if not login or user.get("type") == "Bot":
            continue
        if pr_author is not None and login.lower() == pr_author.lower():
            continue
        if review.get("state") not in _STANCES:
            continue
        key = (review.get("submitted_at") or "", int(review.get("id") or 0))
        prev = latest.get(login.lower())
        if prev is None or key >= prev[0]:
            latest[login.lower()] = (key, review)
    return [
        r["user"]["login"]
        for _, r in latest.values()
        if r.get("state") == APPROVED and r.get("commit_id") == head_sha
    ]


@dataclass(frozen=True)
class Approval:
    """A counted approval: the login and its relevant team memberships."""

    login: str
    team_slugs: frozenset[str]  # ``org/team`` slugs, resolved by the I/O layer
    # Whether the approver has write access to the repository. Only such an
    # approval meets a requirement that names no one: on a public repository
    # anyone can leave an approving review.
    can_write: bool = False


@dataclass(frozen=True)
class Requirement:
    approver: str | None
    """``@org/team``, ``@login``, or ``None`` for anyone with write access."""

    reason: str

    def satisfied_by(self, approvals: list[Approval]) -> Approval | None:
        for a in approvals:
            if self.approver is None:
                if a.can_write:
                    return a
            elif is_eligible_resolver((self.approver,), a.login, a.team_slugs):
                return a
        return None

    @property
    def label(self) -> str:
        return self.approver or "anyone with write access other than the author"


def requirements_for(
    policy: GatePolicy,
    tier_result: TierResult,
    findings: list[ObservedFinding],
    verdicts: list[FindingVerdict],
    changed_paths: tuple[str, ...] | list[str] = (),
) -> tuple[list[Requirement], list[str], list[str]]:
    """Return ``(requirements, problems, waiting)`` before approvals are applied.

    ``problems`` are findings that cannot pass as they stand (a dismissal with
    no ``security_dismissal_reviewers`` and no eligible owner). ``waiting`` are
    findings still open. Escalation paths apply to ``changed_paths`` as well as
    to findings, so an edited sensitive file needs its sign-off even when the
    AI review found nothing there.
    """
    requirements: list[Requirement] = list(
        _escalation_requirements(policy, findings, changed_paths)
    )
    problems: list[str] = []
    waiting: list[str] = []

    def add(req: Requirement) -> None:
        if req not in requirements:
            requirements.append(req)

    for finding, verdict in zip(findings, verdicts, strict=True):
        where = finding.path or "(no path)"
        # The path is the pull request's: every sink renders it through
        # aigate.render (stdout, step summary, comment).
        shown = safe_inline(where)
        state = state_of(verdict)
        if state is State.UNRESOLVED:
            waiting.append(f"{shown}: finding still open")
        elif state is State.DISMISSED:
            req = _dismissal_requirement(policy, where)
            if req is not None:
                add(req)
            elif not verdict.is_green:
                problems.append(f"{shown}: finding dismissed — {verdict.reason}")

    if tier_result.requires_human_always:
        add(
            Requirement(
                None,
                f"risk tier {tier_result.tier} "
                f"({'; '.join(tier_result.reasons)}) always needs a human",
            )
        )
    return requirements, problems, waiting


def _escalation_requirements(
    policy: GatePolicy,
    findings: list[ObservedFinding],
    changed_paths: tuple[str, ...] | list[str],
) -> list[Requirement]:
    """Return escalation sign-offs for findings and changed files.

    Deduplicated by approver; the first matching reason is kept.
    """
    reqs: list[Requirement] = []
    seen: set[str | None] = set()

    def add_for(path: str, kind: str) -> None:
        for approver in policy.escalation_reviewers_for(path):
            if approver not in seen:
                seen.add(approver)
                reqs.append(
                    Requirement(
                        approver,
                        f"{safe_inline(path)}: {kind} on an escalation path",
                    )
                )

    for finding in findings:
        add_for(finding.path or "(no path)", "finding")
    for path in changed_paths:
        add_for(path, "change")
    return reqs


def _dismissal_requirement(policy: GatePolicy, where: str) -> Requirement | None:
    # Rule 1. Without security_dismissal_reviewers, the ledger verdict decides.
    if not policy.security_dismissal_reviewers:
        return None
    return Requirement(
        policy.security_dismissal_reviewers,
        f"{safe_inline(where)}: finding dismissed, not fixed",
    )


def evaluate(
    *,
    policy: GatePolicy,
    tier_result: TierResult,
    findings: list[ObservedFinding],
    verdicts: list[FindingVerdict],
    approvals: list[Approval],
    changed_paths: tuple[str, ...] | list[str] = (),
) -> Decision:
    """Fold findings, tier and approvals into the gate decision (fail closed)."""
    requirements, problems, waiting = requirements_for(
        policy, tier_result, findings, verdicts, changed_paths
    )

    lines = [f"Risk tier {tier_result.tier}: {'; '.join(tier_result.reasons)}"]
    lines.append(f"Reviewer findings: {len(findings)}")
    for finding, verdict in zip(findings, verdicts, strict=True):
        where = safe_inline(finding.path or "(no path)")
        who = f" by {finding.resolved_by_login}" if finding.resolved_by_login else ""
        lines.append(
            f"- [{state_of(verdict).value}] {where}: {verdict.reason}"
            f" (resolved={str(finding.is_resolved).lower()}{who})"
        )

    missing: list[str] = []
    if requirements:
        lines.append("Required approvals (non-author, on the head commit):")
    for req in requirements:
        by = req.satisfied_by(approvals)
        if by is None:
            missing.append(f"{req.label} — {req.reason}")
            lines.append(f"- {req.label} — {req.reason}: MISSING")
        else:
            lines.append(f"- {req.label} — {req.reason}: approved by {by.login}")
    if approvals:
        lines.append(
            "Approving reviews counted: " + ", ".join(a.login for a in approvals)
        )

    if problems or missing:
        header = ["Human sign-off is missing:"]
        header += [f"- {p}" for p in problems]
        header += [f"- needs approval from {m}" for m in missing]
        return Decision(
            outcome=FAILURE,
            title="Human sign-off required",
            summary="\n".join([*header, "", *lines]),
            # Only a missing approval is retryable: one can still arrive while
            # the workflow waits. A problem needs a code change, so waiting on
            # it is pointless.
            retryable=bool(missing) and not problems,
        )
    if waiting:
        return Decision(
            outcome=PENDING,
            title="Waiting for reviewer findings to be resolved",
            summary="\n".join(
                ["Still open:", *(f"- {w}" for w in waiting), "", *lines]
            ),
        )
    title = "AI review passed" + ("; human sign-off satisfied" if requirements else "")
    return Decision(outcome=SUCCESS, title=title, summary="\n".join(lines))
