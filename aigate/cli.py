"""Command line entry points for the AI review gate.

``gate``      evaluate the gate for the triggering pull request; exit 0 if green,
              1 otherwise. ``gate --shadow`` posts the same verdict as a neutral
              ``ai-review-gate (shadow)`` check and always exits 0.
``identify``  print the app behind each check run on a pull request, to confirm
              a reviewer's real ``app_slug``.
``observe``   print what the gate would decide for a pull request. Posts
              nothing and always exits 0.

All modes decide through ``evaluate``. ``gate`` (enforcing) also waits for the
reviewer, handles drafts and takes the strictest verdict across every open pull
request on the head commit; shadow and observe evaluate once.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time
from dataclasses import dataclass

from . import codeowners, escalation, github, reviewers, threads
from .gate import (
    BUGBOT_RERUN_COMMENT,
    FAILURE,
    FIX_FINDINGS_STEP,
    PENDING,
    SUCCESS,
    Decision,
    Outcome,
    Signal,
    bugbot_result,
    decide,
    is_cancelled,
    is_reviewer_run,
)
from .gateconfig import GATE_CONFIG_PATH, load_gate_config
from .ledger import dispose
from .policy import GatePolicy, PolicyError
from .render import safe_inline
from .reviewers import ConfigError
from .tiers import classify

# The bot login that posts Bugbot findings as review threads (not the check-run
# app_slug). REST reports ``cursor[bot]`` and GraphQL reports ``cursor``;
# threads.parse_review_threads ignores the ``[bot]`` suffix, so either matches.
DEFAULT_REVIEWER_LOGIN = "cursor[bot]"

# The check-run repositories make required. The gate posts it itself, rather
# than relying on the job status, so the verdict lands on the head SHA and can
# turn green once the review arrives. Needs ``checks: write``.
CHECK_RUN_NAME = "ai-review-gate"

# Hidden marker for the single PR comment the gate edits in place on each
# re-evaluation.
GATE_COMMENT_MARKER = "<!-- ai-gate:required-reason -->"


# Shadow mode concludes ``neutral``, which GitHub counts as passing a required
# check. It must use a different name, or a pull request could switch its
# caller to shadow mode and satisfy a required ``ai-review-gate``.
SHADOW_CHECK_RUN_NAME = "ai-review-gate (shadow)"

# Gate outcome -> check-run (status, conclusion). PENDING stays in_progress so a
# required check reads "not satisfied yet"; only SUCCESS is green.
_CHECK_STATE = {
    SUCCESS: ("completed", "success"),
    FAILURE: ("completed", "failure"),
    PENDING: ("in_progress", None),
}


def _step_summary(text: str) -> None:
    # Surface the decision on the workflow run's summary page when in Actions.
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except OSError:
            pass


# How the verdict text tells someone to get a fresh evaluation. The default
# suits the gate run as a workflow; a caller that evaluates the gate some other
# way (``AIGATE_RERUN_INSTRUCTION``, e.g. "comment `/ai-gate check`") says
# its own thing instead.
_DEFAULT_RERUN = "re-run the gate"


def _rerun(default: str = _DEFAULT_RERUN, *, capital: bool = False) -> str:
    text = os.environ.get("AIGATE_RERUN_INSTRUCTION", "").strip() or default
    return text[:1].upper() + text[1:] if capital else text


def _transient_step() -> str:
    """Next step when a GitHub read failed: not the author's fault, usually transient."""
    return (
        f"This is a GitHub API or permissions problem, not your change. "
        f"{_rerun(capital=True)}; if it keeps failing, ask a repository "
        f"administrator."
    )


# The title of every verdict the gate could not reach (``_failed``).
COULD_NOT_DECIDE_TITLE = "AI review gate could not decide"


def _failed(msg: str, step: str | None = None, *, rerun_helps: bool = True) -> Decision:
    return Decision(
        outcome=FAILURE,
        title=COULD_NOT_DECIDE_TITLE,
        summary=f"- ai-gate: {msg}\n\n**What to do:**\n- {step or _transient_step()}",
        transient=step is None,
        rerun_helps=rerun_helps,
    )


# Base permissions whose approval can stand in for the AI review. GitHub folds
# maintain and custom roles into a base role; read and triage cannot merge, so
# they do not count.
HUMAN_FALLBACK_ROLES = frozenset({"admin", "write"})
# Roles (``role_name``) whose reply-and-resolve accepts a reviewer finding.
MAINTAINER_ROLES = frozenset({"admin", "maintain"})


def _can_write(slug: str, login: str) -> bool:
    """Return True if ``login`` has write access; an unreadable role does not."""
    try:
        return github.repo_role(slug, login) in HUMAN_FALLBACK_ROLES
    except github.GitHubError:
        return False


def _human_fallback_approvers(slug: str, pull: github.PullRef) -> list[str]:
    """Non-author humans with write access whose latest review approves the head.

    Fails closed: an unreadable author means nobody qualifies (else the author's
    own approval could count), and an unreadable role does not count. Raises
    ``GitHubError`` only if the reviews cannot be listed.
    """
    reviews = github.pull_reviews(slug, pull.number)
    try:
        author = github.pull_author(slug, pull.number)
    except github.GitHubError:
        return []
    if not author:
        return []
    approvers = []
    for login in escalation.approving_logins(
        reviews, head_sha=pull.head_sha, pr_author=author
    ):
        try:
            role = github.repo_role(slug, login)
        except github.GitHubError:
            continue
        if role in HUMAN_FALLBACK_ROLES:
            approvers.append(login)
    return approvers


def _with_human_fallback(
    slug: str, pull: github.PullRef, decision: Decision
) -> Decision:
    """Green a non-clean AI verdict when a qualifying human approved the head.

    This is the fail-safe for a reviewer that is down, missing or wrong. A read
    failure keeps the AI verdict.
    """
    try:
        approvers = _human_fallback_approvers(slug, pull)
    except github.GitHubError:
        return decision
    if not approvers:
        return dataclasses.replace(
            decision,
            summary=(
                f"{decision.summary}\n- Finding wrong, or reviewer stuck? Ask a "
                f"human with write access who is not the PR author to approve "
                f"this exact commit, then {_rerun()}. Their approval stands "
                f"in for the AI review (a new push needs a new approval)."
            ),
        )
    return Decision(
        outcome=SUCCESS,
        title="Human approval in place of AI review",
        summary=(
            f"Approved on the head commit by {', '.join(sorted(approvers))} "
            f"(write access, not the author), standing in for the AI review:"
            f"\n\n{decision.summary}"
        ),
        via_human_fallback=True,
    )


def _maintainer_accepted(
    slug: str,
    finding: threads.ObservedFinding,
    roles: dict[str, str | None],
    head_sha: str | None = None,
) -> bool:
    """Return True if a maintainer replied in ``finding``'s thread and resolved it.

    The resolver must have the maintain or admin role (the pull request author
    counts too) and have replied in the thread: a bare resolve says nothing
    about why the finding is wrong. A thread resolved before the code moved
    (outdated) accepted an earlier state and does not count, unless it was
    posted on ``head_sha`` itself (only the base moved: the same code). An
    unreadable role does not count. ``roles`` caches logins already looked up.
    """
    login = finding.resolved_by_login
    if not finding.is_resolved or not login:
        return False
    if finding.is_outdated and not _on_head(finding, head_sha):
        return False
    if not finding.replied_by(login):
        return False
    key = login.casefold()
    if key not in roles:
        try:
            roles[key] = github.repo_role_name(slug, login)
        except github.GitHubError:
            roles[key] = None
    return roles[key] in MAINTAINER_ROLES


def _accept_step() -> str:
    """How a finding the author thinks is wrong gets accepted."""
    return (
        "A finding you think is wrong: a maintainer of the repository (maintain "
        "or admin role, the author included) replies in its thread with why and "
        f"resolves it, then {_rerun()}."
    )


def _on_head(finding: threads.ObservedFinding, head_sha: str | None) -> bool:
    return bool(head_sha) and finding.commit_oid == str(head_sha).lower()


# What a Bugbot run found is read from its last result line, matched whole
# (``aigate.gate.bugbot_result``).
# Conclusions a reviewer reports findings with: Bugbot concludes ``neutral``,
# or ``failure`` where it is set to fail on unresolved findings.
_REPORTING = frozenset({"neutral", "failure"})
# GitHub keeps at most this many runs of one check name in a check suite and
# deletes older ones: near the cap, the run that reported findings may be
# gone, so the history cannot be trusted.
RUN_RETENTION_CAP = 1000
RUN_RETENTION_GUARD = RUN_RETENTION_CAP - 50


@dataclass(frozen=True)
class Reported:
    """What a reviewer's runs on one commit said they found.

    ``count`` adds up the new findings the runs' summaries state. ``unknown``
    is True when a red (``neutral``/``failure``) run's summary is in no known
    wording. ``runs`` is how many runs of the reviewer the history holds.
    """

    count: int = 0
    unknown: bool = False
    runs: int = 0

    @property
    def any(self) -> bool:
        return self.count > 0

    @property
    def near_cap(self) -> bool:
        return self.runs >= RUN_RETENTION_GUARD


def _reported_findings(required: list[str], history: list[Signal]) -> Reported:
    """Read every run of a required reviewer on this commit, whenever it ran:
    before the pull request was opened or the base last changed, it reviewed
    the same code."""
    count, unknown, runs = 0, False, 0
    for s in history:
        if not any(is_reviewer_run(s, reviewers.REVIEWERS[key]) for key in required):
            continue
        runs += 1
        if s.conclusion not in _REPORTING:
            continue
        result = bugbot_result(s.summary)
        if result.kind == "found":
            count += result.count
        elif result.kind == "unknown":
            unknown = True
        # "none" (nothing new) and "cancelled" (no review) report no findings.
    return Reported(count, unknown, runs)


def _history_untrusted(reported: Reported) -> Decision:
    if reported.near_cap:
        why = (
            f"the reviewer has {reported.runs} runs on this commit, near the "
            f"{RUN_RETENTION_CAP} GitHub keeps per check: older runs, possibly "
            "the one that reported findings, may be gone"
        )
    else:
        why = (
            "a run of the reviewer on this commit ended red with a summary in a "
            "wording the gate does not know, so it cannot tell what it found"
        )
    return Decision(
        outcome=FAILURE,
        title="AI review history cannot be trusted",
        summary=(
            f"- {why[:1].upper()}{why[1:]}.\n\n**What to do:**\n- Push a "
            "commit: the reviewer reviews it fresh."
        ),
        findings_on_head=True,
    )


def _missing_findings(
    findings: list[threads.ObservedFinding], head_sha: str, reported: Reported
) -> str | None:
    """Why the reviewer's threads cannot show that every finding it reported on
    this commit was dealt with, or ``None``.

    Anyone with write access can delete a comment, and a deleted finding has no
    thread to judge: the threads posted on this commit must be at least as many
    as its runs said they found.
    """
    on_head = sum(1 for f in findings if _on_head(f, head_sha))
    if on_head < reported.count:
        return (
            f"the reviewer reported {reported.count} finding(s) on this commit but "
            f"only {on_head} of its threads are left (deleted, or posted elsewhere)"
        )
    return None


def _missing_decision(reason: str) -> Decision:
    return Decision(
        outcome=FAILURE,
        title="AI review findings cannot be found",
        summary=(
            f"- {reason[:1].upper()}{reason[1:]}. A later clean run on the same "
            "commit does not clear them.\n\n**What to do:**\n- Push a fix: the "
            "reviewer re-reviews the new commit."
        ),
        findings_on_head=True,
    )


def _with_accepted_findings(
    slug: str,
    pull: github.PullRef,
    decision: Decision,
    reviewer_login: str,
    policy: GatePolicy | None,
    reported: Reported | None = None,
    snapshot: dict | None = None,
) -> Decision:
    """Judge the reviewer's findings on this commit by who resolved them.

    Runs when a required reviewer reported findings on the head commit: as the
    latest result (``findings_only``), or in an earlier run on the same commit
    that a later clean run followed. A re-run without a push is the same code,
    so the findings stand until each is accepted; a new push starts fresh.

    Without a policy, every open thread keeps the gate red, and every thread
    resolved on the current code must be a maintainer's reply-and-resolve
    (``_maintainer_accepted``). Under a policy the ledger in ``_enforce``
    judges the resolutions, so a reported-findings FAILURE with no open thread
    is handed to it. Read failures keep the decision red.
    """
    sticky = decision.outcome == SUCCESS
    try:
        data = github.review_threads(slug, pull.number)
    except github.GitHubError as exc:
        return _failed(f"could not read the AI reviewer's findings — {exc}")
    if threads.has_more_threads(data):
        return _failed(
            f"pull request #{pull.number} has more than 100 review threads; "
            "refusing to judge a truncated list.",
            rerun_helps=False,
        )
    if snapshot is not None:
        snapshot["threads"] = data
    _, findings = threads.parse_review_threads(data, reviewer_login)
    reported = reported or Reported()
    head = pull.head_sha
    # Open on the current code. A thread an earlier commit's review left open
    # and that has since gone outdated does not block: a new head starts
    # fresh, and its own review says whether the problem is still there.
    open_ = [
        f
        for f in findings
        if not f.is_resolved and (not f.is_outdated or _on_head(f, head))
    ]
    # Resolved on the current code; on this commit itself, outdated too (only
    # the base moved), so retargeting cannot take a finding out of judging.
    current = [
        f
        for f in findings
        if f.is_resolved and (not f.is_outdated or _on_head(f, head))
    ]
    if reported.unknown or reported.near_cap:
        # Cannot tell what was reported: never green from here.
        return decision if not sticky else _history_untrusted(reported)
    posted_here = any(_on_head(f, head) for f in findings)
    if not reported.count and not posted_here:
        # The run ended neutral/failure without saying it found anything and
        # left no thread on this commit: an error or timeout, not findings.
        # A later clean run stands; the reviewer's own red stays red (threads
        # of earlier commits cannot green it).
        return decision

    if sticky and not open_ and not current:
        # Findings were reported on this very commit, yet none of the
        # reviewer's threads is left on it: deleted (anyone with write access
        # can delete a comment) or moved. Nothing shows they were accepted.
        return _missing_decision(
            "the reviewer reported findings on this commit in an earlier run, "
            "but none of its comment threads is on the current code (deleted, "
            "or the code moved)"
        )
    # Checked before anything goes green: a deleted finding cannot be judged.
    missing = _missing_findings(findings, pull.head_sha, reported)

    if policy is not None:
        if missing and (sticky or (not open_ and current)):
            return _missing_decision(missing)
        if sticky or open_ or not current:
            # Sticky: _enforce runs the ledger on a SUCCESS anyway. Otherwise
            # the reported-findings verdict stands until nothing is open and
            # something on the current code shows how they were resolved.
            return decision
        return Decision(
            outcome=SUCCESS,
            title="AI review findings resolved",
            summary=(
                "The reviewer reported findings on this commit; none is open. "
                "The escalation policy decides how they were resolved."
            ),
        )

    if not sticky and not open_ and not current:
        # Nothing on the current code to judge: keep the reviewer's verdict.
        return decision
    roles: dict[str, str | None] = {}
    not_accepted = [
        f for f in current if not _maintainer_accepted(slug, f, roles, head)
    ]
    if not open_ and not not_accepted:
        if missing:
            return _missing_decision(missing)
        return dataclasses.replace(
            decision,
            outcome=SUCCESS,
            title="AI review findings accepted by a maintainer",
            summary=(
                f"The reviewer reported findings on this commit. Each of the "
                f"{len(current)} on the current code was answered and resolved "
                f"by a maintainer: "
                + ", ".join(sorted({f"@{f.resolved_by_login}" for f in current}))
                + "."
            ),
            findings_only=False,
        )

    if not sticky and not not_accepted:
        # Only open findings: the reviewer's own verdict says it best; add how
        # a maintainer can accept one.
        return dataclasses.replace(
            decision,
            summary=decision.summary.replace(
                f"- {FIX_FINDINGS_STEP}",
                f"- {FIX_FINDINGS_STEP}\n- {_accept_step()}",
                1,
            ),
        )
    lines: list[str] = []
    if sticky:
        lines.append(
            "- The reviewer reported findings on this commit in an earlier run. "
            "A later clean run on the same commit does not clear them: the code "
            "has not changed."
        )
    else:
        lines.append("- " + decision.summary.splitlines()[0].lstrip("- "))
    for f in not_accepted[:10]:
        where = safe_inline(f.path) if f.path else "comment"
        shown = f"[{where}]({f.url})" if f.url else where
        who = f"@{f.resolved_by_login}" if f.resolved_by_login else "someone"
        lines.append(
            f"- {shown}: resolved by {who}, which does not accept it (needs a "
            f"maintainer's reply and resolve)."
        )
    if len(not_accepted) > 10:
        lines.append(f"- …and {len(not_accepted) - 10} more not accepted")
    steps = [
        "Fix the findings and push: the reviewer re-reviews the new commit.",
        _accept_step(),
    ]
    summary = "\n".join([*lines, "", "**What to do:**", *(f"- {x}" for x in steps)])
    return Decision(
        outcome=FAILURE,
        title=(
            "AI review findings not accepted"
            if not open_
            else (decision.title if not sticky else "AI review findings still open")
        ),
        summary=summary,
        findings_on_head=True,
    )


def _with_open_findings(
    slug: str, pull: github.PullRef, decision: Decision, reviewer_login: str
) -> Decision:
    """List the reviewer's unresolved findings, linked, above "What to do".

    Best effort: a read failure leaves the verdict unchanged. A conclusion-only
    reviewer set (empty ``reviewer_login``) has no findings to list.
    """
    if not reviewer_login:
        return decision
    try:
        data = github.review_threads(slug, pull.number)
    except github.GitHubError:
        return decision
    _, findings = threads.parse_review_threads(data, reviewer_login)
    open_ = [f for f in findings if not f.is_resolved]
    marker = "**What to do:**"
    if not open_:
        if not findings:
            return decision
        # All threads resolved but the check still reports issues: replace the
        # "fix the findings" step rather than contradict it.
        step = (
            "All of the reviewer's comment threads are resolved, but its check on "
            f"this commit still reports issues. Post `{BUGBOT_RERUN_COMMENT}` as a "
            "new top-level PR comment for a fresh review, or push a commit."
        )
        return dataclasses.replace(
            decision, summary=decision.summary.replace(FIX_FINDINGS_STEP, step, 1)
        )
    # The path is the pull request's (a fork's, under pull_request_target):
    # rendered through aigate.render so it cannot close the link or the
    # comment's Markdown.
    shown = [
        (
            f"- [{safe_inline(f.path) if f.path else 'comment'}]({f.url})"
            if f.url
            else f"- {safe_inline(f.path) if f.path else 'comment'}"
        )
        for f in open_[:10]
    ]
    if len(open_) > 10:
        shown.append(f"- …and {len(open_) - 10} more")
    block = "\n".join([f"Unresolved findings ({len(open_)}):", *shown, ""])
    return dataclasses.replace(
        decision, summary=decision.summary.replace(marker, f"{block}\n{marker}", 1)
    )


def _opened_at(slug: str, pull: github.PullRef) -> str:
    """Return when ``pull`` was opened, looking it up if the event lacked it."""
    if pull.created_at:
        return pull.created_at
    opened = (github.pull_detail(slug, pull.number) or {}).get("created_at")
    if not isinstance(opened, str) or not opened:
        raise github.GitHubError(
            f"GitHub did not report when #{pull.number} was opened"
        )
    return opened


def _split_by_opening(
    signals: list[Signal], opened_at: str
) -> tuple[list[Signal], list[Signal]]:
    """Split ``signals`` into runs for this pull request and earlier ones.

    A check run belongs to a commit, not a pull request. A review that started
    before this pull request was opened reviewed another pull request on the
    same commit, possibly against a different base, so it must not count here.
    GitHub's timestamps are all UTC ISO 8601, so they compare as strings.
    """
    current: list[Signal] = []
    earlier: list[Signal] = []
    for s in signals:
        if s.started_at is not None and s.started_at < opened_at:
            earlier.append(s)
        else:
            current.append(s)
    return current, earlier


def _tied_to_this_pull(
    slug: str,
    pull: github.PullRef,
    required: list[str],
    signals: list[Signal],
    opened_at: str,
    decision: Decision,
) -> Decision:
    """Keep a passing AI verdict only if the review is this pull request's.

    A clean review leaves nothing on the pull request, so the only evidence is
    the check run on the commit. It counts only if it started after the base
    last changed, and only if no other pull request contains this commit.
    """
    changed = github.last_base_change(slug, pull.number)
    if changed is not None and changed > opened_at:
        current, earlier = _split_by_opening(signals, changed)
        decision = decide(required, current, earlier=earlier)
        if decision.outcome != SUCCESS:
            return decision
    return _sole_pull(slug, pull, decision)


def _reported_on(s: Signal) -> bool:
    """Whether a red run reviewed the code and states its final result (how
    many new findings, or none new with earlier ones still unresolved),
    rather than being cancelled or failing first."""
    return s.conclusion in _REPORTING and bugbot_result(s.summary).kind in (
        "found",
        "none",
    )


def _accepted_review_is_current(
    slug: str,
    pull: github.PullRef,
    required: list[str],
    signals: list[Signal],
    opened_at: str,
    decision: Decision,
    history: list[Signal] | None = None,
) -> Decision:
    """Keep a green from accepted findings only if a review that reported
    findings started after the pull request's base last changed.

    Retargeting changes the change under review: findings on a small diff
    against a side branch say nothing about the diff against the new base. A
    run Bugbot cancelled after the change (it cancels on a base change) ends
    red too but reviewed nothing, so it does not count: ``history`` (every run
    on the head, with summaries) must hold a run that started strictly after
    the change and states how many findings it found.
    """
    changed = github.last_base_change(slug, pull.number)
    if changed is None or changed <= opened_at:
        return decision
    current, earlier = _split_by_opening(signals, changed)
    # Strictly after the change, with a known start and a stated count: a
    # cancelled run (Bugbot cancels on a base change) reviewed nothing.
    reviewed_since = any(
        s.started_at is not None
        and s.started_at > changed
        and _reported_on(s)
        and any(is_reviewer_run(s, reviewers.REVIEWERS[key]) for key in required)
        for s in history or ()
    )
    if decide(required, current, earlier=earlier).findings_only and reviewed_since:
        return decision
    return Decision(
        outcome=FAILURE,
        title="AI review is from before the base changed",
        summary=(
            "- The review whose findings were accepted ran before this pull "
            "request's base last changed, so it reviewed a different change.\n\n"
            f"**What to do:**\n- Post `{BUGBOT_RERUN_COMMENT}` as a new top-level "
            "PR comment for a fresh review, or push a commit."
        ),
    )


def _sole_pull(slug: str, pull: github.PullRef, decision: Decision) -> Decision:
    """Keep ``decision`` only if no other pull request contains the head commit."""
    try:
        others = github.other_pulls_containing(slug, pull.head_sha, pull.number)
    except github.NotIndexedYetError as exc:
        return Decision(
            outcome=PENDING,
            title="Checking the AI review belongs to this pull request",
            summary=(
                f"- {exc}. GitHub indexes new commits within a few minutes.\n\n"
                f"**What to do:**\n- Nothing yet. If this persists, {_rerun()}."
            ),
            not_indexed=True,
        )
    if not others:
        return decision
    return Decision(
        outcome=FAILURE,
        title="AI review cannot be tied to this pull request",
        summary=_shared_commit_summary(
            others, github.shared_commit_reasons(slug, pull.head_sha, pull.number)
        ),
        # A human approval can still arrive while the gate waits.
        retryable=True,
    )


def _shared_commit_summary(others: list[int], reasons: dict[int, str]) -> str:
    """Why this head commit's AI review is ambiguous, per other PR, and what to do.

    The review is a check run on the commit, not on a pull request, so a
    commit another pull request has (or had) as its head may carry that pull
    request's review. A pull request stacked ON TOP of this one does not count
    (its base ends here; it never had this commit as its head).
    """
    same = [n for n in others if reasons.get(n) == github.SAME_HEAD]
    contains = [n for n in others if reasons.get(n) == github.CONTAINS]
    unknown = [n for n in others if n not in same and n not in contains]
    lines: list[str] = []
    todo: list[str] = []
    if same:
        lines.append(
            f"- {_refs(same)} {_has_have(same)} this exact commit as head. The AI "
            "review is attached to the commit, not to a pull request, so it "
            "cannot be tied to this one."
        )
        todo.append(
            f"- If {_refs(same)} duplicates this pull request, close it. Otherwise "
            "push a new commit here so the reviewer reviews this pull request's "
            "own head."
        )
    if contains:
        lines.append(
            f"- {_refs(contains)} also {_contain(contains)} this commit: it was "
            f"{'their' if len(contains) > 1 else 'its'} head at some point (before "
            "a later push, or a branch reused for another pull request), so the "
            "review on it may have been for that pull request."
        )
        todo.append(
            "- Push a new commit here (an empty one is enough) so the reviewer "
            "reviews this pull request's head."
        )
    if unknown:
        lines.append(
            f"- This commit is also part of {_refs(unknown)}. The AI review is "
            "attached to the commit, so it may have reviewed that pull "
            "request's change instead of this one."
        )
        todo.append(
            "- Push a new commit here so the reviewer reviews this pull request."
        )
    todo.append(
        "- Or ask someone with write access (not the author) to approve this "
        f"commit, then {_rerun()}. A pull request stacked on top of this one is "
        "not a reason for this message: nothing needs closing for a stack."
    )
    return "\n".join(lines) + "\n\n**What to do:**\n" + "\n".join(todo)


def _refs(numbers: list[int]) -> str:
    return ", ".join(f"#{n}" for n in numbers)


def _has_have(numbers: list[int]) -> str:
    return "have" if len(numbers) > 1 else "has"


def _contain(numbers: list[int]) -> str:
    return "contain" if len(numbers) > 1 else "contains"


class _Unread:
    """Marker for "``evaluate`` reads the config itself" (``None`` means absent)."""


UNREAD = _Unread()


def evaluate(
    slug: str,
    pull: github.PullRef,
    reviewer_login: str,
    *,
    human_fallback: bool = True,
    gate_text: str | _Unread | None = UNREAD,
    signals: list[Signal] | None = None,
) -> Decision:
    """Decide the gate for ``pull``: the one verdict every mode reports.

    ``human_fallback`` lets a qualifying human approval stand in for a non-clean
    AI review. A consumer that must act on a clean AI review only can turn it
    off.

    ``gate_text`` is ``.github/ai-gate.yml`` as already read from the BASE
    branch (``None`` if absent), for a caller that needs the same file for its
    own checks and should not read it twice. Left unset, it is read here.
    ``signals`` are the head's check runs as already read by such a caller
    (the approver's proof step); left unset, they are read here. Either way
    the same rule decides.

    Configuration and GitHub read failures become a FAILURE decision instead of
    raising, so the enforcing caller always posts a red check. A bare exit
    would leave any earlier green check on this head SHA in place.
    """
    # A malformed or unreadable config fails closed.
    try:
        if isinstance(gate_text, _Unread):
            gate_text = github.read_config_text(slug, pull.base_ref, GATE_CONFIG_PATH)
        required, policy = load_gate_config(gate_text, slug)
    except (ConfigError, PolicyError) as exc:
        return _failed(
            f"configuration error — {exc}",
            f"Fix `{GATE_CONFIG_PATH}` on `{pull.base_ref}` in its own pull request "
            f"(the gate reads it from the base branch, so fixing it here does "
            f"nothing), then {_rerun('re-run this gate')}.",
        )
    except github.GitHubError as exc:
        return _failed(f"could not read the gate configuration — {exc}")

    try:
        if signals is None:
            signals = github.review_signals(slug, pull.head_sha)
        opened_at = _opened_at(slug, pull)
    except github.GitHubError as exc:
        return _failed(f"could not read the AI review — {exc}")
    if any(is_cancelled(s) for s in signals):
        # A caller's runs (or GitHub's latest-only listing) show a cancelled
        # run, which is no review: decide on every run instead, so the review
        # before it is the one that counts (``decide`` skips cancelled runs).
        try:
            signals = github.signals_for(slug, pull.head_sha, all_runs=True)
        except github.GitHubError as exc:
            return _failed(f"could not read the AI review's earlier runs — {exc}")
    current, earlier = _split_by_opening(signals, opened_at)
    decision = decide(required, current, earlier=earlier)
    # Findings are review threads, and only a reviewer that posts threads has
    # them. When every required reviewer is conclusion-only, no thread is a
    # finding: its verdict is its check, cleared by a re-review or a human.
    if reviewers.thread_reviewer_login(required) is None:
        reviewer_login = ""
    if decision.outcome == SUCCESS:
        # Only a passing review needs proving to be this pull request's, which
        # keeps the extra lookups (one of them a rate-limited search) rare.
        try:
            decision = _tied_to_this_pull(
                slug, pull, required, signals, opened_at, decision
            )
        except github.GitHubError as exc:
            return _failed(
                f"could not check that the AI review is for this pull request — {exc}"
            )
    reported = Reported()
    history: list[Signal] = []
    # The review threads the findings were judged on, for the policy to judge
    # the same snapshot.
    snapshot: dict = {}
    # Reviewers whose findings are threads; a conclusion-only reviewer's runs
    # report nothing to judge here.
    threaded = [k for k in required if reviewers.REVIEWERS[k].has_threads]
    if (
        reviewer_login
        and threaded
        and (decision.outcome == SUCCESS or decision.findings_only)
    ):
        # GitHub lists only each check's latest run by default, so a clean
        # re-run hides the run that reported findings: read every run on the
        # commit, whenever it ran, so neither a re-run nor retargeting clears
        # them. Its summaries also say how many findings there were.
        try:
            history = github.signals_for(slug, pull.head_sha, all_runs=True)
        except github.GitHubError as exc:
            return _failed(f"could not read the AI review's earlier runs — {exc}")
        reported = _reported_findings(threaded, history)
        if reported.near_cap or (decision.outcome == SUCCESS and reported.unknown):
            decision = _history_untrusted(reported)
        elif reported.any and not decision.findings_on_head:
            decision = dataclasses.replace(decision, findings_on_head=True)
    if (
        reviewer_login
        and decision.findings_on_head
        and (decision.outcome == SUCCESS or decision.findings_only)
    ):
        was_red = decision.outcome == FAILURE
        decision = _with_accepted_findings(
            slug, pull, decision, reviewer_login, policy, reported, snapshot
        )
        if was_red and decision.outcome == SUCCESS:
            # Greened by accepted findings: the review whose findings were
            # accepted must be this pull request's, by the rule a clean pass
            # meets (``_tied_to_this_pull``): started after the base last
            # changed, and no other pull request containing the commit.
            try:
                decision = _accepted_review_is_current(
                    slug, pull, required, signals, opened_at, decision, history
                )
                if decision.outcome == SUCCESS:
                    decision = _sole_pull(slug, pull, decision)
            except github.GitHubError as exc:
                return _failed(
                    f"could not check that the AI review is for this pull request — {exc}"
                )
    if human_fallback and decision.outcome == FAILURE:
        decision = _with_open_findings(slug, pull, decision, reviewer_login)
    if human_fallback and decision.outcome != SUCCESS:
        decision = _with_human_fallback(slug, pull, decision)

    # Under a policy a clean AI review is necessary but not sufficient: finding
    # dispositions and required sign-offs decide the rest. Errors fail closed.
    if policy is not None and decision.outcome == SUCCESS:
        before_policy = decision
        try:
            decision = _enforce(
                slug, pull, policy, reviewer_login, snapshot.get("threads")
            )
        except github.GitHubRefusalError as exc:
            # A re-run hits the same refusal, and the generic human fallback
            # must not override the policy's named reviewers.
            return _failed(
                f"the escalation policy cannot be judged automatically — {exc}",
                "No approval or re-run clears this on its own. Ask a repository "
                "administrator to review the pull request and the repository's "
                "escalation policy.",
                rerun_helps=False,
            )
        except github.GitHubError as exc:
            return _failed(f"could not evaluate the escalation policy — {exc}")
        # The policy decision knows nothing about the human fallback and its
        # title says "AI review passed". Restore the fallback's title, flag and
        # explanation so the result does not claim a clean AI review.
        if before_policy.via_human_fallback and decision.outcome == SUCCESS:
            decision = dataclasses.replace(
                decision,
                title="Human approval in place of AI review (escalation policy also satisfied)",
                summary=f"{before_policy.summary}\n\n{decision.summary}",
                via_human_fallback=True,
            )
    return decision


# Fold order for same-head pull requests: a red beats a pending beats a green.
_STRICTNESS = {FAILURE: 2, PENDING: 1, SUCCESS: 0}


def _evaluate_head(slug: str, pull: github.PullRef, reviewer_login: str) -> Decision:
    """Decide the verdict for a head SHA across every open pull request on it.

    The check is posted on the commit, and GitHub shows it on every pull request
    with that head. Taking the strictest verdict stops a second pull request
    into a base with a weaker config from greening the real one. Each pull
    request is judged against its own base config. Listing failures fail closed.
    """
    try:
        refs = github.open_pull_refs_for_sha(slug, pull.head_sha)
    except github.GitHubError as exc:
        return _failed(f"could not list pull requests for {pull.head_sha} — {exc}")

    # Always include the triggering pull request, even if the listing lagged.
    by_number = {p.number: p for p in refs}
    by_number[pull.number] = pull

    decisions = [evaluate(slug, p, reviewer_login) for p in by_number.values()]
    worst = max(decisions, key=lambda d: _STRICTNESS[d.outcome])
    if len(decisions) > 1:
        note = (
            f"Judged across {len(decisions)} open pull requests that share this "
            f"commit as their head; showing the strictest. A green here requires "
            f"every one of them to pass, so a second pull request from this commit "
            f"into a weaker base cannot green the others."
        )
        worst = dataclasses.replace(worst, summary=f"{note}\n\n{worst.summary}")
    return worst


def _env_int(name: str, default: int) -> int:
    """Read a non-negative integer from the environment, or ``default``.

    A missing, blank or unparseable value yields the default rather than
    crashing the gate.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def _wait_for_terminal(
    slug: str,
    pull: github.PullRef,
    reviewer_login: str,
    initial: Decision,
    wait_seconds: int,
) -> Decision:
    """Re-evaluate in-run until the verdict is final or ``wait_seconds`` elapse.

    A ruleset-required workflow runs only on ``pull_request`` events; GitHub
    ignores ``check_run`` and ``pull_request_review`` for it. So the gate cannot
    be re-triggered when the reviewer finishes and must wait within this run,
    polling every ``AIGATE_POLL_SECONDS``. On timeout the last decision stands
    and the job fails closed.
    """
    poll = max(1, _env_int("AIGATE_POLL_SECONDS", 20))
    decision = initial
    elapsed = 0
    # Poll while the verdict is retryable, not just PENDING, so a late required
    # approval can also turn the gate green.
    while decision.retryable and elapsed < wait_seconds:
        nap = min(poll, wait_seconds - elapsed)
        time.sleep(nap)
        elapsed += nap
        if _pushed_past(slug, pull):
            # A newer push has its own run queued behind this one; stop waiting.
            break
        decision = _evaluate_head(slug, pull, reviewer_login)
    return decision


def _live_pull(slug: str, number: int) -> dict:
    """Return the pull request's current state, or ``{}`` if unreadable.

    A re-run replays the original event payload, so its ``draft`` flag and head
    can be stale.
    """
    try:
        return github.pull_detail(slug, number) or {}
    except github.GitHubError:
        return {}


def _pull_identity(slug: str, pull: github.PullRef) -> dict | None:
    """What the verdict is for: state, base, last retarget, head, default branch.

    ``None`` when it cannot be read (then nothing is posted).
    """
    try:
        live = github.pull_detail(slug, pull.number)
        retargeted = github.last_base_change(slug, pull.number)
    except github.GitHubError:
        return None
    if not isinstance(live, dict):
        return None
    base = live.get("base") or {}
    return {
        "state": live.get("state"),
        "base": base.get("ref"),
        "retargeted_at": retargeted,
        "head": (live.get("head") or {}).get("sha"),
        "default_branch": (base.get("repo") or {}).get("default_branch"),
    }


def _still_the_same(slug: str, pull: github.PullRef, before: dict | None) -> str | None:
    """``None`` when the pull request is still the one the verdict was reached for.

    Otherwise what changed. Compared right before posting: open, the same
    base as the event and as at the start, no retarget in between (the
    timeline's last base change, so away-and-back counts), the same head,
    and with ``AIGATE_DEFAULT_BRANCH_ONLY`` a base that is the default branch.
    Unreadable now or at the start: changed.
    """
    now = _pull_identity(slug, pull)
    if before is None or now is None:
        return "unreadable"
    if now["state"] != "open":
        return f"state {now['state']}"
    if now["base"] != pull.base_ref or now["base"] != before["base"]:
        return f"base {before['base']} -> {now['base']}"
    if now["retargeted_at"] != before["retargeted_at"]:
        return "retargeted"
    if now["head"] != pull.head_sha or now["head"] != before["head"]:
        return "head moved"
    if _env_flag("AIGATE_DEFAULT_BRANCH_ONLY", default=False) and (
        not now["default_branch"] or now["base"] != now["default_branch"]
    ):
        return f"base {now['base']} is not the default branch"
    return None


def _pushed_past(slug: str, pull: github.PullRef) -> bool:
    """Return True only when the live head is readable and is not ours."""
    live = (_live_pull(slug, pull.number).get("head") or {}).get("sha")
    return bool(live) and live != pull.head_sha


def cmd_gate(args: argparse.Namespace) -> int:
    slug = github.repo_slug()
    event = github.read_event()
    if args.shadow:
        return _gate_shadow(slug, event, args.reviewer_login)

    pull = github.resolve_pull(event, slug)
    # With AIGATE_RECHECK_BEFORE_POST the pull request's identity is taken now
    # and must still hold right before the verdict is posted (``_still_the_same``).
    recheck = _env_flag("AIGATE_RECHECK_BEFORE_POST", default=False)
    before = _pull_identity(slug, pull) if recheck else None
    decision = _evaluate_head(slug, pull, args.reviewer_login)

    # A ruleset-required workflow is not re-triggered when the reviewer finishes,
    # so optionally wait for it here.
    wait_seconds = _env_int("AIGATE_WAIT_SECONDS", 0)
    if (
        decision.outcome == PENDING
        and _live_pull(slug, pull.number).get("draft")
        and not _reviewer_has_posted(slug, pull)
    ):
        # Bugbot does not review drafts, so do not wait for a review that is not
        # coming. Still fail closed rather than skip: a skipped required workflow
        # counts as success. A review already running on a draft is waited for.
        decision = _draft_decision()
        wait_seconds = 0
    elif wait_seconds > 0 and decision.retryable:
        decision = _wait_for_terminal(
            slug, pull, args.reviewer_login, decision, wait_seconds
        )

    if _pushed_past(slug, pull):
        # This commit is no longer the head; its verdict must not overwrite the
        # current head's check or comment.
        print(
            f"ai-gate: {pull.head_sha[:8]} is no longer the head of #{pull.number}; "
            f"the newer push has its own gate run. Nothing posted.",
            file=sys.stderr,
        )
        _result_line(pull, decision, check_run_id=None, pushed_past=True)
        return 1

    if not decision.ok and decision.rerun_helps:
        decision = _with_rerun_hint(decision, wait_seconds)
    note = os.environ.get("AIGATE_SUMMARY_NOTE", "").strip()
    if note:
        # A caller's one line about how this verdict was reached (for example
        # where its configuration came from), at the end of the summary.
        decision = dataclasses.replace(
            decision, summary=f"{decision.summary}\n\n{note}"
        )

    stream = sys.stdout if decision.ok else sys.stderr
    print(f"ai-gate: {decision.title}", file=stream)
    print(decision.summary, file=stream)
    _step_summary(f"### AI review gate — {decision.title}\n\n{decision.summary}")
    if recheck:
        changed = _still_the_same(slug, pull, before)
        if changed is not None:
            # The verdict was reached for a pull request that is not this one
            # any more (retargeted, closed, a new base): never posted.
            print(
                f"ai-gate: #{pull.number} changed during the evaluation ({changed}); "
                f"nothing posted.",
                file=sys.stderr,
            )
            _result_line(
                pull, decision, check_run_id=None, pushed_past=False, stale=changed
            )
            return 1
    check_run_id, reused = _publish_gate_check(
        slug, pull.head_sha, decision.outcome, decision.title, decision.summary
    )
    if _env_flag("AIGATE_POST_COMMENT", default=True):
        _post_gate_comment(
            slug,
            pull.number,
            pull.head_sha,
            decision.outcome,
            decision.title,
            decision.summary,
            decision.via_human_fallback,
        )
    _result_line(
        pull, decision, check_run_id=check_run_id, pushed_past=False, reused=reused
    )
    return 0 if decision.ok else 1


def _env_flag(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw not in ("false", "0", "no")


# The one machine-readable line ``gate`` prints when ``AIGATE_RESULT_JSON`` is
# true, for a caller that runs the gate as a program rather than reading the
# job status: what it decided, and whether and as which check run it published
# that. The human-readable output is unchanged either way.
RESULT_EVENT = "ai-gate-result"


def _result_line(
    pull: github.PullRef,
    decision: Decision,
    *,
    check_run_id: int | None,
    pushed_past: bool,
    reused: bool = False,
    stale: str | None = None,
) -> None:
    if not _env_flag("AIGATE_RESULT_JSON", default=False):
        return
    print(
        json.dumps(
            {
                "event": RESULT_EVENT,
                "pr": pull.number,
                "head_sha": pull.head_sha,
                "outcome": decision.outcome,
                "posted": check_run_id is not None,
                "check_run_id": check_run_id,
                "reused": reused,
                "stale": stale,
                "pushed_past": pushed_past,
                "transient": decision.transient,
                "not_indexed": decision.not_indexed,
                "title": decision.title,
                "summary": decision.summary,
            },
            sort_keys=True,
        )
    )


def _reviewer_has_posted(slug: str, pull: github.PullRef) -> bool:
    """Return True if a known AI reviewer has reviewed (any state) this pull request.

    A read failure returns True, so the gate waits as usual rather than abort a
    review that may be running.
    """
    try:
        current, _ = _split_by_opening(
            github.signals_for(slug, pull.head_sha), _opened_at(slug, pull)
        )
    except github.GitHubError:
        return True
    return any(
        is_reviewer_run(s, r) for s in current for r in reviewers.REVIEWERS.values()
    )


# How a stopped gate run gets another go without a push: the approver
# re-runs it once the review lands (ai-gate-approver, gate_rerun.py), and the
# bot command asks it to look now.
AUTO_RERUN_NOTE = (
    "The bot re-runs this gate automatically when you mark the pull request "
    "ready or the review lands; or comment `/ai-gate check`."
)


def _draft_decision() -> Decision:
    return Decision(
        outcome=PENDING,
        title="Draft pull request — AI review not started",
        summary=(
            "- Cursor Bugbot does not review draft pull requests, so the gate "
            "cannot pass yet.\n\n**What to do:**\n"
            "- Mark the pull request **Ready for review**. Bugbot then reviews it.\n"
            f"- {AUTO_RERUN_NOTE}"
        ),
    )


def _with_rerun_hint(decision: Decision, wait_seconds: int) -> Decision:
    """Say the run has stopped, and link the button that re-runs it.

    A ruleset-required gate re-runs only on a push, not on an approval or a late
    review, so the re-run should be one click away. A caller that replaced the
    re-run instruction (``AIGATE_RERUN_INSTRUCTION``) is not a workflow run, so
    nothing here applies to it, whatever the environment says.
    """
    if os.environ.get("AIGATE_RERUN_INSTRUCTION", "").strip():
        return decision
    notes = []
    if decision.outcome == PENDING and wait_seconds > 0:
        notes.append(
            f"The gate stopped waiting after {wait_seconds // 60} minutes. "
            f"{AUTO_RERUN_NOTE}"
        )
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo, run = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_RUN_ID")
    if repo and run:
        notes.append(
            f"[Re-run the gate]({server}/{repo}/actions/runs/{run}) with "
            f"“Re-run jobs” after an approval or a finished review. A push "
            f"re-runs it automatically."
        )
    if not notes:
        return decision
    return dataclasses.replace(
        decision, summary=decision.summary + "\n\n" + "\n".join(notes)
    )


def _gate_shadow(slug: str, event: dict, reviewer_login: str) -> int:
    """Evaluate as enforcing would, report the verdict, and never block.

    The verdict goes to the job log, the step summary and a neutral shadow
    check on the head commit. Always returns 0.
    """
    try:
        pull = github.resolve_pull(event, slug)
    except github.GitHubError as exc:
        print(f"ai-gate SHADOW: could not resolve the pull request — {exc}")
        return 0

    decision = evaluate(slug, pull, reviewer_login)
    verdict = decision.outcome.upper()
    title = f"Shadow — would be {verdict}: {decision.title}"
    summary = (
        "**Shadow mode: this check enforces nothing.** It reports what the "
        f"enforcing `{CHECK_RUN_NAME}` check would decide for this commit.\n\n"
        f"{decision.summary}"
    )
    print(
        f"ai-gate SHADOW — {slug}#{pull.number} would be {verdict} (enforces nothing)"
    )
    print(decision.summary)
    _step_summary(f"### AI review gate (shadow) — would be {verdict}\n\n{summary}")
    try:
        github.post_check_run(
            slug,
            pull.head_sha,
            SHADOW_CHECK_RUN_NAME,
            "completed",
            "neutral",
            title,
            summary,
        )
    except github.GitHubError as exc:
        print(f"ai-gate: could not post the {SHADOW_CHECK_RUN_NAME} check-run — {exc}")
    return 0


def _enforce(
    slug: str,
    pull: github.PullRef,
    policy: GatePolicy,
    reviewer_login: str,
    raw: dict | None = None,
) -> Decision:
    """Gather the facts for the escalation policy and evaluate it.

    Policy, CODEOWNERS and configuration come from the base branch. Missing or
    ambiguous data raises ``GitHubError`` so the caller fails closed. ``raw``
    is the review-thread payload already judged for reported findings
    (``_with_accepted_findings``): the policy judges that same snapshot, so a
    thread deleted in between cannot pass one check and miss the other.
    """
    paths = github.changed_paths(slug, pull.number)
    tier_result = classify(paths)

    if reviewer_login:
        if raw is None:
            raw = github.review_threads(slug, pull.number)
        if threads.has_more_threads(raw):
            raise github.GitHubRefusalError(
                f"pull request #{pull.number} has more than 100 review threads; "
                f"refusing to judge a truncated list."
            )
        pr_author, findings = threads.parse_review_threads(raw, reviewer_login)
    else:
        # Every required reviewer is conclusion-only: no thread is a finding,
        # so the threads are not read (nor CODEOWNERS for dismissals below).
        pr_author, findings = github.pull_author(slug, pull.number), []
    if pr_author is None:
        raise github.GitHubRefusalError(
            f"could not determine the author of {slug}#{pull.number}; "
            f"non-author checks are impossible without it."
        )

    memberships = github.TeamMemberships()

    # CODEOWNERS decides who may dismiss a finding only when no
    # security_dismissal_reviewers is configured.
    rules: list[codeowners.Rule] = []
    if findings and policy.security_dismissal_reviewers is None:
        rules = codeowners.parse(github.read_codeowners(slug, pull.base_ref) or "")

    verdicts = []
    roles: dict[str, str | None] = {}
    for finding in findings:
        eligible = False
        if (
            rules
            and finding.is_resolved
            # An outdated thread's code has moved since it was resolved, so the
            # owner accepted an earlier state. Such a resolution needs sign-off,
            # just as approvals are bound to the head commit.
            and not finding.is_outdated
            and finding.resolved_by_login
            and finding.resolved_by_login != pr_author
            # Like a maintainer: a bare resolve says nothing about why.
            and finding.replied_by(finding.resolved_by_login)
            and finding.path
        ):
            owners = codeowners.owners_for_path(rules, finding.path)
            owner_teams = frozenset(o.lstrip("@") for o in owners if "/" in o)
            eligible = codeowners.is_eligible_resolver(
                owners,
                finding.resolved_by_login,
                memberships.teams_of(finding.resolved_by_login, owner_teams),
            )
        facts = threads.to_facts(
            finding,
            pr_author_login=pr_author,
            # No machine-evidence source exists yet. Do not use
            # ``finding.is_outdated`` here: any edit near the anchored code
            # marks a thread outdated, which the author controls and which does
            # not show the problem was fixed.
            has_machine_evidence=False,
            resolver_is_eligible_owner=eligible,
            resolver_is_maintainer_with_reply=_maintainer_accepted(
                slug, finding, roles, pull.head_sha
            ),
        )
        verdicts.append(dispose(facts))

    reviews = github.pull_reviews(slug, pull.number)
    logins = escalation.approving_logins(
        reviews, head_sha=pull.head_sha, pr_author=pr_author
    )
    approvals = [
        escalation.Approval(
            login,
            memberships.teams_of(login, policy.team_handles()),
            can_write=_can_write(slug, login),
        )
        for login in logins
    ]

    return escalation.evaluate(
        policy=policy,
        tier_result=tier_result,
        findings=findings,
        verdicts=verdicts,
        approvals=approvals,
        changed_paths=paths,
    )


def _unchanged_run(
    head_sha: str, outcome: Outcome, title: str, summary: str
) -> int | None:
    """The id of a check run that already shows exactly this verdict, if any.

    ``AIGATE_UNCHANGED_CHECK_RUN`` is a JSON object a caller may pass: the
    newest ``ai-review-gate`` run it knows of on this head, as ``id``,
    ``head_sha``, ``status``, ``conclusion``, ``title`` and ``summary``. When
    this evaluation would post the same verdict (same head, state, title and
    summary), posting again only adds a run, and a head keeps a bounded number
    of them; that run is reported instead. Anything malformed means "post".
    """
    raw = os.environ.get("AIGATE_UNCHANGED_CHECK_RUN", "").strip()
    if not raw:
        return None
    try:
        known = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(known, dict):
        return None
    run_id = known.get("id")
    if not isinstance(run_id, int) or isinstance(run_id, bool):
        return None
    status, conclusion = _CHECK_STATE[outcome]
    same = (
        known.get("head_sha") == head_sha
        and known.get("status") == status
        and known.get("conclusion") == conclusion
        and known.get("title") == title
        and known.get("summary") == summary[:60000]
    )
    return run_id if same else None


def _publish_gate_check(
    slug: str, head_sha: str, outcome: Outcome, title: str, summary: str
) -> tuple[int | None, bool]:
    """Post the verdict, or reuse an identical run; ``(run id, reused)``."""
    if os.environ.get("AIGATE_POST_CHECK_RUN", "").strip().lower() != "false":
        unchanged = _unchanged_run(head_sha, outcome, title, summary)
        if unchanged is not None:
            print(
                f"ai-gate: the {CHECK_RUN_NAME} check-run {unchanged} already shows "
                f"this verdict; nothing posted.",
                file=sys.stderr,
            )
            return unchanged, True
    return _post_gate_check(slug, head_sha, outcome, title, summary), False


def _post_gate_check(
    slug: str, head_sha: str, outcome: Outcome, title: str, summary: str
) -> int | None:
    """Post the gate's verdict as a check-run on the head commit (best effort).

    Returns the new check run's id, or ``None`` when nothing was posted. A
    ``GitHubError`` is logged, not raised; the exit code still reflects the
    decision. ``AIGATE_POST_CHECK_RUN=false`` (the action's ``post-check-run``
    input) skips it: the org-required workflow has no ``checks: write``, its
    job status is the gate, and the post only produced a misleading "could not
    post … 403" line on every run.
    """
    if os.environ.get("AIGATE_POST_CHECK_RUN", "").strip().lower() == "false":
        return None
    status, conclusion = _CHECK_STATE[outcome]
    try:
        run_id = github.post_check_run(
            slug, head_sha, CHECK_RUN_NAME, status, conclusion, title, summary
        )
    except github.GitHubError as exc:
        print(
            f"ai-gate: could not post the {CHECK_RUN_NAME} check-run — {exc}",
            file=sys.stderr,
        )
        return None
    return run_id if isinstance(run_id, int) and not isinstance(run_id, bool) else None


def _post_gate_comment(
    slug: str,
    pr_number: int,
    head_sha: str,
    outcome: Outcome,
    title: str,
    summary: str,
    via_human_fallback: bool = False,
) -> None:
    """Create or update the single PR comment that explains the verdict.

    The check-run is easy to miss and some workflows cannot post one, so the
    reason also goes on the conversation. The comment is edited in place on
    each re-evaluation. A clean pass with no prior comment posts nothing.

    A human-fallback pass always posts, so it is not mistaken for a clean AI
    review. That comment names its commit and warns it can go stale, since
    nothing re-runs the gate when the AI review concludes later. Best effort:
    failures are logged, not raised.
    """
    try:
        comments = github.list_issue_comments(slug, pr_number)
    except github.GitHubError as exc:
        print(f"ai-gate: could not list comments — {exc}", file=sys.stderr)
        return
    # Only our own comment counts, or anyone could plant the marker and hijack
    # which comment gets edited. A PAT posts as a User, so match its login.
    # ``github.token`` and App tokens cannot read /user, so match any Bot.
    my_login = github.current_actor_login()
    existing_id = next(
        (
            c.get("id")
            for c in comments
            if GATE_COMMENT_MARKER in (c.get("body") or "")
            and (
                (c.get("user") or {}).get("login") == my_login
                if my_login is not None
                else (c.get("user") or {}).get("type") == "Bot"
            )
        ),
        None,
    )
    if outcome == SUCCESS and not via_human_fallback and existing_id is None:
        return
    body = f"{GATE_COMMENT_MARKER}\n### AI review gate — {title}\n\n{summary}"
    if via_human_fallback:
        body += (
            f"\n\n_Evaluated at `{head_sha[:8]}`. This required workflow does "
            f"not re-run on its own once the AI review later concludes — check "
            f"the **Cursor Bugbot** check on this commit directly for its "
            f"current state._"
        )
    try:
        if existing_id is not None:
            try:
                github.update_issue_comment(slug, existing_id, body)
                return
            except github.GitHubError as exc:
                print(
                    f"ai-gate: could not update the existing gate comment ({exc}); creating a new one",
                    file=sys.stderr,
                )
        github.create_issue_comment(slug, pr_number, body)
    except github.GitHubError as exc:
        print(f"ai-gate: could not post the gate comment — {exc}", file=sys.stderr)


def cmd_identify(args: argparse.Namespace) -> int:
    slug = args.repo or github.repo_slug()
    if args.sha:
        sha = args.sha
    else:
        data = github.gh_json([f"repos/{slug}/pulls/{args.pr}"])
        assert isinstance(data, dict)
        sha = data["head"]["sha"]

    signals = github.signals_for(slug, sha)
    if not signals:
        print(f"No check runs on {slug}@{sha[:8]}.")
        return 0
    print(f"Check runs on {slug}@{sha[:8]}:")
    width = max(len(s.name) for s in signals)
    for s in sorted(signals, key=lambda s: s.name):
        print(
            f"  {s.name:<{width}}  app.slug={s.app_slug!r:<22} conclusion={s.conclusion!r}"
        )
    return 0


def cmd_observe(args: argparse.Namespace) -> int:
    """Print the would-be gate decision for a pull request. Never blocks."""
    slug = args.repo or github.repo_slug()
    try:
        pull = github.pull_ref(slug, args.pr)
    except github.GitHubError as exc:
        print(f"ai-gate observe: could not look up {slug}#{args.pr} — {exc}")
        return 0

    print(f"ai-gate observe — {slug}#{pull.number} (enforces nothing)")
    decision = evaluate(slug, pull, args.reviewer_login)
    for line in decision.summary.splitlines():
        print(f"  {line}")
    print(f"  would-be outcome: {decision.outcome.upper()} — {decision.title}")
    return 0  # observing never fails the run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aigate",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser("gate", help="evaluate the gate for the triggering pull request")
    g.add_argument(
        "--reviewer-login",
        default=DEFAULT_REVIEWER_LOGIN,
        help=f"bot login that posts findings (default {DEFAULT_REVIEWER_LOGIN})",
    )
    g.add_argument(
        "--shadow",
        action="store_true",
        help=(
            f"evaluate but enforce nothing: post a neutral {SHADOW_CHECK_RUN_NAME!r} "
            "check with the would-be verdict and always exit 0"
        ),
    )
    g.set_defaults(func=cmd_gate)

    i = sub.add_parser(
        "identify", help="print the posting app of each check run on a PR"
    )
    i.add_argument("--repo", help="owner/name; defaults to GITHUB_REPOSITORY")
    src = i.add_mutually_exclusive_group(required=True)
    src.add_argument("--pr", type=int, help="pull request number")
    src.add_argument("--sha", help="commit SHA")
    i.set_defaults(func=cmd_identify)

    o = sub.add_parser(
        "observe",
        help="SHADOW: report the would-be gate decision for a PR (never blocks)",
    )
    o.add_argument("--repo", help="owner/name; defaults to GITHUB_REPOSITORY")
    o.add_argument("--pr", type=int, required=True, help="pull request number")
    o.add_argument(
        "--reviewer-login",
        default=DEFAULT_REVIEWER_LOGIN,
        help=f"bot login that posts findings (default {DEFAULT_REVIEWER_LOGIN})",
    )
    o.set_defaults(func=cmd_observe)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except github.GitHubError as exc:
        print(f"ai-gate: {exc}", file=sys.stderr)
        return 1
