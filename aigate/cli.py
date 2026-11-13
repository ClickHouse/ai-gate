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
import os
import sys
import time

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
    decide,
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


# Next step when a GitHub read failed: not the author's fault, usually transient.
_TRANSIENT_STEP = (
    "This is a GitHub API or permissions problem, not your change. Re-run the "
    "gate; if it keeps failing, ask a repository administrator."
)


def _failed(
    msg: str, step: str = _TRANSIENT_STEP, *, rerun_helps: bool = True
) -> Decision:
    return Decision(
        outcome=FAILURE,
        title="AI review gate could not decide",
        summary=f"- ai-gate: {msg}\n\n**What to do:**\n- {step}",
        transient=step is _TRANSIENT_STEP,
        rerun_helps=rerun_helps,
    )


# Base permissions whose approval can stand in for the AI review. GitHub folds
# maintain and custom roles into a base role; read and triage cannot merge, so
# they do not count.
HUMAN_FALLBACK_ROLES = frozenset({"admin", "write"})


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
                f"this exact commit, then re-run the gate. Their approval stands "
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


def _with_open_findings(
    slug: str, pull: github.PullRef, decision: Decision, reviewer_login: str
) -> Decision:
    """List the reviewer's unresolved findings, linked, above "What to do".

    Best effort: a read failure leaves the verdict unchanged.
    """
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
    try:
        others = github.other_pulls_containing(slug, pull.head_sha, pull.number)
    except github.NotIndexedYetError as exc:
        return Decision(
            outcome=PENDING,
            title="Checking the AI review belongs to this pull request",
            summary=(
                f"- {exc}. GitHub indexes new commits within a few minutes.\n\n"
                f"**What to do:**\n- Nothing yet. If this persists, re-run the gate."
            ),
        )
    if not others:
        return decision
    listed = ", ".join(f"#{n}" for n in others)
    return Decision(
        outcome=FAILURE,
        title="AI review cannot be tied to this pull request",
        summary=(
            f"- This commit is also part of {listed}. The AI review is "
            f"attached to the commit, so it may have reviewed that pull "
            f"request's change instead of this one.\n\n**What to do:**\n"
            f"- Close the other pull request if it is not needed, then push a "
            f"new commit here so the reviewer reviews this pull request.\n"
            f"- Or ask someone with write access (not the author) to approve "
            f"this commit, then re-run the gate."
        ),
        # A human approval can still arrive while the gate waits.
        retryable=True,
    )


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
        required, policy = load_gate_config(gate_text)
    except (ConfigError, PolicyError) as exc:
        return _failed(
            f"configuration error — {exc}",
            f"Fix `{GATE_CONFIG_PATH}` on `{pull.base_ref}` in its own pull request "
            f"(the gate reads it from the base branch, so fixing it here does "
            f"nothing), then re-run this gate.",
        )
    except github.GitHubError as exc:
        return _failed(f"could not read the gate configuration — {exc}")

    try:
        if signals is None:
            signals = github.signals_for(slug, pull.head_sha)
        opened_at = _opened_at(slug, pull)
    except github.GitHubError as exc:
        return _failed(f"could not read the AI review — {exc}")
    current, earlier = _split_by_opening(signals, opened_at)
    decision = decide(required, current, earlier=earlier)
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
    if human_fallback and decision.outcome == FAILURE:
        decision = _with_open_findings(slug, pull, decision, reviewer_login)
    if human_fallback and decision.outcome != SUCCESS:
        decision = _with_human_fallback(slug, pull, decision)

    # Under a policy a clean AI review is necessary but not sufficient: finding
    # dispositions and required sign-offs decide the rest. Errors fail closed.
    if policy is not None and decision.outcome == SUCCESS:
        before_policy = decision
        try:
            decision = _enforce(slug, pull, policy, reviewer_login)
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
        return 1

    if not decision.ok and decision.rerun_helps:
        decision = _with_rerun_hint(decision, wait_seconds)

    stream = sys.stdout if decision.ok else sys.stderr
    print(f"ai-gate: {decision.title}", file=stream)
    print(decision.summary, file=stream)
    _step_summary(f"### AI review gate — {decision.title}\n\n{decision.summary}")
    _post_gate_check(
        slug, pull.head_sha, decision.outcome, decision.title, decision.summary
    )
    _post_gate_comment(
        slug,
        pull.number,
        pull.head_sha,
        decision.outcome,
        decision.title,
        decision.summary,
        decision.via_human_fallback,
    )
    return 0 if decision.ok else 1


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
    review, so the re-run should be one click away.
    """
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
    slug: str, pull: github.PullRef, policy: GatePolicy, reviewer_login: str
) -> Decision:
    """Gather the facts for the escalation policy and evaluate it.

    Policy, CODEOWNERS and configuration come from the base branch. Missing or
    ambiguous data raises ``GitHubError`` so the caller fails closed.
    """
    paths = github.changed_paths(slug, pull.number)
    tier_result = classify(paths)

    raw = github.review_threads(slug, pull.number)
    if threads.has_more_threads(raw):
        raise github.GitHubRefusalError(
            f"pull request #{pull.number} has more than 100 review threads; "
            f"refusing to judge a truncated list."
        )
    pr_author, findings = threads.parse_review_threads(raw, reviewer_login)
    if pr_author is None:
        raise github.GitHubRefusalError(
            f"could not determine the author of {slug}#{pull.number}; "
            f"non-author checks are impossible without it."
        )

    memberships = github.TeamMemberships()

    # CODEOWNERS decides who may dismiss a finding only when no
    # security_dismissal_reviewers is configured.
    rules: list[codeowners.Rule] = []
    if policy.security_dismissal_reviewers is None:
        rules = codeowners.parse(github.read_codeowners(slug, pull.base_ref) or "")

    verdicts = []
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


def _post_gate_check(
    slug: str, head_sha: str, outcome: Outcome, title: str, summary: str
) -> None:
    """Post the gate's verdict as a check-run on the head commit (best effort).

    A ``GitHubError`` is logged, not raised; the exit code still reflects the
    decision. ``AIGATE_POST_CHECK_RUN=false`` (the action's ``post-check-run``
    input) skips it: the org-required workflow has no ``checks: write``, its
    job status is the gate, and the post only produced a misleading "could not
    post … 403" line on every run.
    """
    if os.environ.get("AIGATE_POST_CHECK_RUN", "").strip().lower() == "false":
        return
    status, conclusion = _CHECK_STATE[outcome]
    try:
        github.post_check_run(
            slug, head_sha, CHECK_RUN_NAME, status, conclusion, title, summary
        )
    except github.GitHubError as exc:
        print(
            f"ai-gate: could not post the {CHECK_RUN_NAME} check-run — {exc}",
            file=sys.stderr,
        )


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
