"""The gate decision from the required reviewers and the commit's check runs.

A reviewer passes only when its latest check run on the head commit concluded
``success``. For Cursor Bugbot, ``neutral`` means it found issues and does not
pass. Outcomes:

* ``success``: every required reviewer passed.
* ``pending``: nothing failed, but a reviewer has not completed yet.
* ``failure``: a required reviewer completed with a non-success result.
"""

from __future__ import annotations

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


def is_reviewer_run(signal: Signal, reviewer: Reviewer) -> bool:
    """Return True if ``signal`` is ``reviewer``'s review check.

    The posting app is the identity. The name then picks the review check out of
    the app's checks: Cursor's app also posts ``Cursor Bugbot Autofix``, which
    reports ``success`` without reviewing anything.
    """
    return signal.app_slug == reviewer.app_slug and signal.name == reviewer.check_name


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

    for key in required:
        rv = REVIEWERS[key]
        mine = [s for s in signals if is_reviewer_run(s, rv)]
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
            failed.append(f"{rv.label} {meaning} (check concluded '{raw}').")
            failed_labels.append(rv.label)

    if failed:
        if found_issues:
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
        )
    return Decision(
        outcome=SUCCESS,
        title="AI review passed",
        summary="Clean review from: " + ", ".join(passed) + ".",
    )


def _summary(problems: list[str], passed: list[str], steps: list[str]) -> str:
    lines = [*(f"- {p}" for p in problems)]
    if passed:
        lines += ["", "Already passed: " + ", ".join(passed) + "."]
    lines += ["", "**What to do:**", *(f"- {s}" for s in steps)]
    return "\n".join(lines)
