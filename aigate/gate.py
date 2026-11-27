"""The gate decision from the required reviewers and the commit's check runs.

A reviewer passes only when its latest check run on the head commit concluded
``success``. For Cursor Bugbot, ``neutral`` means it found issues and does not
pass. Outcomes:

* ``success``: every required reviewer passed.
* ``pending``: nothing failed, but a reviewer has not completed yet.
* ``failure``: a required reviewer completed with a non-success result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from .reviewers import REVIEWERS, Reviewer

Outcome = Literal["success", "pending", "failure"]
SUCCESS: Outcome = "success"
PENDING: Outcome = "pending"
FAILURE: Outcome = "failure"


@dataclass(frozen=True)
class Signal:
    """One reviewer check run on the head commit.

    ``conclusion`` is ``success``, ``pending`` (not yet completed), or the raw
    non-success conclusion. ``app_slug`` is the posting GitHub App, which is
    what the gate trusts.
    """

    app_slug: str | None
    name: str
    conclusion: str | None
    completed_at: str | None = None
    started_at: str | None = None
    # The posting App's numeric id, where the source reports it. A reviewer
    # that pins ``app_id`` never matches a signal without one.
    app_id: int | None = None
    # The run's ``output.summary``, where Cursor Bugbot states how many issues
    # it found (a cancelled run says so there).
    summary: str | None = None


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    title: str
    summary: str
    # Whether a caller waiting in-run should keep polling, because the missing
    # signal (a review or an approval) could still arrive. Defaults to
    # outcome == PENDING; escalation sets it for a failure that only a pending
    # approval would clear.
    retryable: bool | None = None
    # True when a GitHub read failed, rather than the pull request or its
    # config, so a consumer can retry instead of treating it as a verdict.
    transient: bool = False
    # False when re-running cannot change the verdict, so no re-run link is shown.
    rerun_helps: bool = True
    # True when SUCCESS came from a qualifying human approval standing in for a
    # missing or failed AI review, so the pull request comment can say so.
    via_human_fallback: bool = False
    # True when the only thing missing is GitHub's commit search catching up
    # with a new head: a consumer can retry in a few minutes.
    not_indexed: bool = False
    # True when a required reviewer reported findings (``neutral``) in any of
    # its runs on this commit, even if a later run on the same commit passed:
    # a re-run without a push must not make the findings go away.
    findings_on_head: bool = False
    # True for a FAILURE whose only cause may be reported findings: every
    # failed reviewer concluded ``neutral`` or ``failure`` and none is still
    # running. Accepted findings on the commit's threads can then green it
    # (``cli``); with no thread to judge it stays red.
    findings_only: bool = False

    def __post_init__(self) -> None:
        if self.retryable is None:
            object.__setattr__(self, "retryable", self.outcome == PENDING)

    @property
    def ok(self) -> bool:
        return self.outcome == SUCCESS


def _latest(signals: list[Signal]) -> Signal:
    # A reviewer may post several runs on one commit. The most recently
    # completed one wins; a run still in progress (no completed_at) only counts
    # when nothing has completed yet.
    return max(signals, key=lambda s: s.completed_at or "")


# Non-success check conclusions in plain words (for Bugbot, ``neutral`` means
# it found issues).
_CONCLUSION_MEANING = {
    "neutral": "found issues that are not resolved",
    "failure": "reported a failure",
    "cancelled": "was cancelled before it finished",
    "timed_out": "timed out before it finished",
    "skipped": "skipped this commit",
    "action_required": "needs action in its own app before it can review",
    "stale": "went stale before it finished",
}

# Top-level pull request comment (not a thread reply) that re-runs Bugbot.
BUGBOT_RERUN_COMMENT = "bugbot run"

FIX_FINDINGS_STEP = (
    "Fix the findings and push. The reviewer re-reviews the new commit and the "
    "gate re-runs by itself."
)


# A run Bugbot cancelled before it reviewed anything ends neutral with a
# "**Result:**" line instead, e.g. (live):
#   "**Result:** Bugbot run cancelled because the base branch changed and the
#    reviewed diff is no longer current"
#   "**Result:** Bugbot run cancelled because the review request was cancelled"
# Such a run reports no findings, like an errored run. Only these exact lines
# count; any other wording stays not understood.
_CANCELLED = re.compile(
    r"^\s*\*\*Result:\*\*\s*Bugbot run cancelled because (?:the base branch "
    r"changed and the reviewed diff is no longer current|the review request "
    r"was cancelled)\.?\s*$",
    re.MULTILINE,
)


def is_cancelled(signal: Signal) -> bool:
    """Whether the run is one Bugbot cancelled before reviewing: no review.

    Only a run whose summary is known (the full-history read fills it) can be
    recognised; any other run counts as what its conclusion says.
    """
    return bool(signal.summary) and bool(_CANCELLED.search(signal.summary or ""))


def is_reviewer_run(signal: Signal, reviewer: Reviewer) -> bool:
    """Return True if ``signal`` is ``reviewer``'s review check.

    The posting app is the identity. The name then picks the review check out of
    the app's checks: Cursor's app also posts ``Cursor Bugbot Autofix``, which
    reports ``success`` without reviewing anything.
    """
    return (
        signal.app_slug == reviewer.app_slug
        and signal.name == reviewer.check_name
        and (reviewer.app_id is None or signal.app_id == reviewer.app_id)
    )


def all_required_green(
    required: list[str], signals: list[Signal]
) -> tuple[bool, list[str]]:
    """Return ``(every required reviewer is green, what is still waiting)``.

    The same rule ``decide`` applies, as a pure yes/no for callers that must
    not spend anything until the gate COULD be green (the approver's proof
    step and its backstop): for each required reviewer, its runs on the head
    are picked by :func:`is_reviewer_run`, a run still in progress means
    waiting, and otherwise the most recently completed run decides. The
    waiting list names each reviewer that is not green with why, e.g.
    ``Cursor Bugbot (pending)`` or ``Polyglot (failure)``; a reviewer with no
    run at all is ``(missing)``. Empty when the first element is True.
    """
    waiting: list[str] = []
    for key in required:
        rv = REVIEWERS[key]
        # Like ``decide``: a run cancelled before it reviewed is no review.
        mine = [s for s in signals if is_reviewer_run(s, rv) and not is_cancelled(s)]
        if not mine:
            waiting.append(f"{rv.label} (missing)")
        elif any(s.conclusion == PENDING for s in mine):
            waiting.append(f"{rv.label} (pending)")
        elif (latest := _latest(mine)).conclusion != SUCCESS:
            waiting.append(f"{rv.label} ({latest.conclusion or 'unknown'})")
    return not waiting, waiting


def decide(
    required: list[str],
    signals: list[Signal],
    *,
    earlier: list[Signal] | tuple[Signal, ...] = (),
) -> Decision:
    """Decide the gate for ``required`` reviewers given the commit's ``signals``.

    A signal counts only if :func:`is_reviewer_run` matches it. ``earlier`` are
    reviewer runs on this commit that started before the pull request was
    opened, so they reviewed some other pull request; they never count, but
    are named in the summary. Every non-green summary ends with what the author
    can do next.
    """
    passed: list[str] = []
    waiting: list[str] = []
    failed: list[str] = []
    failed_labels: list[str] = []
    found_issues = False
    findings_on_head = False
    only_findings = True

    for key in required:
        rv = REVIEWERS[key]
        # A run cancelled before it reviewed is no review at all.
        mine = [s for s in signals if is_reviewer_run(s, rv) and not is_cancelled(s)]
        if not mine:
            if any(is_reviewer_run(s, rv) for s in earlier):
                waiting.append(
                    f"{rv.label}'s review on this commit is from before this pull "
                    f"request was opened or its base last changed, so it reviewed a "
                    f"different change."
                )
            else:
                waiting.append(
                    f"{rv.label} has not posted a review on this commit yet."
                )
            continue
        latest = _latest(mine)
        # Findings are threads: only a reviewer that posts them has any. A
        # conclusion-only reviewer's red is its verdict, nothing to accept.
        findings_on_head = findings_on_head or (
            rv.has_threads and any(s.conclusion in ("neutral", "failure") for s in mine)
        )
        if any(s.conclusion == PENDING for s in mine):
            # A review still running on this commit decides; an earlier
            # result must not pass in the meantime.
            waiting.append(f"{rv.label} is still reviewing this commit.")
        elif latest.conclusion == SUCCESS:
            passed.append(rv.label)
        else:
            raw = latest.conclusion or "unknown"
            meaning = _CONCLUSION_MEANING.get(raw, "did not give a clean pass")
            found_issues = found_issues or raw == "neutral"
            # Bugbot reports findings as ``neutral``, or as ``failure`` where it
            # is set to fail on unresolved findings.
            only_findings = (
                only_findings and rv.has_threads and raw in ("neutral", "failure")
            )
            failed.append(f"{rv.label} {meaning} (check concluded '{raw}').")
            failed_labels.append(rv.label)

    if failed:
        # ``failure`` is Bugbot's fail-on-unresolved findings verdict, so it
        # gets the findings guidance too, never "ask for a fresh review" (a
        # re-run on the same commit does not clear findings).
        if found_issues or only_findings:
            steps = [FIX_FINDINGS_STEP]
        else:
            steps = [
                f"Ask for a fresh review: post `{BUGBOT_RERUN_COMMENT}` as a new "
                "top-level PR comment (not a reply), or push a new commit.",
            ]
        return Decision(
            outcome=FAILURE,
            title=(
                f"{failed_labels[0]} found issues"
                if found_issues and len(failed_labels) == 1
                else "AI review not clean"
            ),
            summary=_summary(failed, passed, steps),
            findings_on_head=findings_on_head,
            findings_only=only_findings and not waiting,
        )
    if waiting:
        return Decision(
            outcome=PENDING,
            title="Waiting for AI review",
            summary=_summary(
                waiting,
                passed,
                [
                    "Nothing to do yet if the review is running. The gate turns "
                    "green when it finishes clean.",
                    f"No review showing up? Post `{BUGBOT_RERUN_COMMENT}` as a new "
                    "top-level PR comment. If that does nothing, Bugbot is probably "
                    "not enabled for this repository: ask a repository administrator.",
                ],
            ),
            findings_on_head=findings_on_head,
        )
    return Decision(
        outcome=SUCCESS,
        title="AI review passed",
        summary="Clean review from: " + ", ".join(passed) + ".",
        findings_on_head=findings_on_head,
    )


def _summary(problems: list[str], passed: list[str], steps: list[str]) -> str:
    lines = [*(f"- {p}" for p in problems)]
    if passed:
        lines += ["", "Already passed: " + ", ".join(passed) + "."]
    lines += ["", "**What to do:**", *(f"- {s}" for s in steps)]
    return "\n".join(lines)
