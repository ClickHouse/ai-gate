"""A maintainer's reply-and-resolve accepts a finding; a re-run does not.

Thread payloads follow the GraphQL ``reviewThreads`` shape the gate queries
(``Bot.login`` comes back without ``[bot]``: Bugbot is ``cursor``). Roles follow
REST ``collaborators/{login}/permission``, where a maintainer reads
``"permission": "write", "role_name": "maintain"``.
"""

from __future__ import annotations

import pytest
from test_cli import _gate_comment, _patch_event

from aigate import cli, github, threads
from aigate.gate import SUCCESS, Signal, decide
from aigate.ledger import Disposition, FindingFacts, dispose
from aigate.reviewers import REVIEWERS

BUGBOT = REVIEWERS["bugbot"].app_slug

PERMISSION = {
    # repos/{slug}/collaborators/{login}/permission, trimmed.
    "maintainer": {"permission": "write", "role_name": "maintain"},
    "owner": {"permission": "admin", "role_name": "admin"},
    "dev": {"permission": "write", "role_name": "write"},
}


# Cursor Bugbot's check summaries (output.summary), trimmed from a live run.
FOUND_ONE = (
    "Bugbot Analysis Progress (8m 5s elapsed)\n\n"
    "**Final Result:** Bugbot completed review and found 1 potential issue.\n"
)
FOUND_NONE = "**Final Result:** Bugbot completed review - no issues found! ✅\n"


def _run(conclusion, at, summary=None):
    if summary is None:
        summary = FOUND_ONE if conclusion == "neutral" else FOUND_NONE
    return Signal(
        app_slug=BUGBOT,
        name="Cursor Bugbot",
        conclusion=conclusion,
        started_at=at,
        completed_at=at,
        summary=summary,
    )


def _thread(
    *,
    resolved_by=None,
    replies=(),
    outdated=False,
    path=".github/workflows/x.yml",
    more_comments=False,
    commit="abc",
):
    comments = [
        {
            "url": "https://x/1",
            "author": {"login": "cursor"},
            "originalCommit": {"oid": commit},
        }
    ]
    comments += [{"url": "https://x/r", "author": {"login": who}} for who in replies]
    return {
        "id": "PRRT_1",
        "isResolved": resolved_by is not None,
        "isOutdated": outdated,
        "path": path,
        "resolvedBy": {"login": resolved_by} if resolved_by else None,
        "comments": {"pageInfo": {"hasNextPage": more_comments}, "nodes": comments},
    }


def _payload(*nodes, author="maintainer"):
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "author": {"login": author},
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False},
                        "nodes": list(nodes),
                    },
                }
            }
        }
    }


def _setup(mocker, signals, *nodes, author="maintainer", role_error=False):
    post = _patch_event(mocker)
    mocker.patch.object(github, "read_config_text", return_value=None)
    mocker.patch.object(github, "signals_for", return_value=signals)
    mocker.patch.object(
        github, "review_threads", return_value=_payload(*nodes, author=author)
    )

    def role(slug, login):
        if role_error:
            raise github.GitHubError("HTTP 502")
        return PERMISSION.get(login, {}).get("role_name")

    mocker.patch.object(github, "repo_role_name", side_effect=role)
    return post


NEUTRAL = [_run("neutral", "2026-11-27T01:13:00Z")]
NEUTRAL_THEN_CLEAN = [
    _run("neutral", "2026-11-27T01:13:00Z"),
    _run(SUCCESS, "2026-11-27T05:46:00Z"),
]


# --- findings reported by the latest run --------------------------------------


@pytest.mark.parametrize("login", ["maintainer", "owner"])
def test_a_maintainer_reply_and_resolve_greens_reported_findings(mocker, login):
    post = _setup(mocker, NEUTRAL, _thread(resolved_by=login, replies=[login]))
    assert cli.main(["gate"]) == 0
    assert post.call_args.args[4] == "success"
    assert post.call_args.args[5] == "AI review findings accepted by a maintainer"


def test_a_maintainer_resolve_without_a_reply_stays_red(mocker):
    _setup(mocker, NEUTRAL, _thread(resolved_by="maintainer"))
    assert cli.main(["gate"]) == 1
    assert "resolved by @maintainer, which does not accept it" in _gate_comment()


def test_an_author_with_write_only_stays_red_with_a_reply(mocker):
    _setup(mocker, NEUTRAL, _thread(resolved_by="dev", replies=["dev"]), author="dev")
    assert cli.main(["gate"]) == 1
    body = _gate_comment()
    assert "AI review findings not accepted" in body
    assert "a maintainer of the repository" in body


def test_a_reply_from_someone_else_does_not_count_for_the_resolver(mocker):
    _setup(mocker, NEUTRAL, _thread(resolved_by="maintainer", replies=["dev"]))
    assert cli.main(["gate"]) == 1


def test_an_unreadable_role_stays_red(mocker):
    _setup(
        mocker,
        NEUTRAL,
        _thread(resolved_by="maintainer", replies=["maintainer"]),
        role_error=True,
    )
    assert cli.main(["gate"]) == 1


def test_a_truncated_comment_list_does_not_count_a_reply(mocker):
    _setup(
        mocker,
        NEUTRAL,
        _thread(resolved_by="maintainer", replies=["maintainer"], more_comments=True),
    )
    assert cli.main(["gate"]) == 1


def test_an_open_finding_beside_an_accepted_one_stays_red(mocker):
    _setup(
        mocker,
        NEUTRAL,
        _thread(resolved_by="maintainer", replies=["maintainer"]),
        _thread(path="src/a.py"),
    )
    assert cli.main(["gate"]) == 1
    assert "Unresolved findings (1):" in _gate_comment()


def test_open_findings_only_keep_the_reviewer_message_and_name_the_way_out(mocker):
    _setup(mocker, NEUTRAL, _thread())
    assert cli.main(["gate"]) == 1
    body = _gate_comment()
    assert "Cursor Bugbot found issues" in body
    assert cli._accept_step() in body


def test_accepted_findings_still_need_the_commit_to_be_this_pull_requests(mocker):
    _setup(mocker, NEUTRAL, _thread(resolved_by="maintainer", replies=["maintainer"]))
    mocker.patch.object(github, "other_pulls_containing", return_value=[7])
    mocker.patch.object(github, "shared_commit_reasons", return_value={})
    assert cli.main(["gate"]) == 1


def test_a_human_approval_still_stands_in(mocker):
    from test_cli import _approval

    _setup(mocker, NEUTRAL, _thread(resolved_by="dev", replies=["dev"]), author="dev")
    mocker.patch.object(github, "pull_reviews", return_value=[_approval("reviewer")])
    assert cli.main(["gate"]) == 0


# --- a later clean run on the same commit (re-run without a push) -------------


def test_a_clean_rerun_does_not_clear_an_authors_own_dismissal(mocker):
    # Findings, the author resolved them, `bugbot run`, clean on the same
    # commit.
    post = _setup(
        mocker,
        NEUTRAL_THEN_CLEAN,
        _thread(resolved_by="dev", replies=["dev"]),
        author="dev",
    )
    assert cli.main(["gate"]) == 1
    assert post.call_args.args[4] == "failure"
    assert "A later clean run on the same commit does not clear them" in (
        _gate_comment()
    )


def test_a_clean_rerun_does_not_clear_a_finding_bugbot_resolved_itself(mocker):
    _setup(mocker, NEUTRAL_THEN_CLEAN, _thread(resolved_by="cursor"))
    assert cli.main(["gate"]) == 1


def test_a_clean_rerun_does_not_clear_an_open_finding(mocker):
    _setup(mocker, NEUTRAL_THEN_CLEAN, _thread())
    assert cli.main(["gate"]) == 1


def test_a_clean_rerun_after_a_maintainer_accepted_is_green(mocker):
    _setup(
        mocker,
        NEUTRAL_THEN_CLEAN,
        _thread(resolved_by="maintainer", replies=["maintainer"]),
    )
    assert cli.main(["gate"]) == 0


def test_a_new_push_starts_fresh(mocker):
    # The head's only run is clean: earlier commits' findings do not stick.
    post = _setup(
        mocker,
        [_run(SUCCESS, "2026-11-27T05:46:00Z")],
        _thread(resolved_by="dev", replies=["dev"]),
        author="dev",
    )
    assert cli.main(["gate"]) == 0
    assert post.call_args.args[5] == "AI review passed"


def test_an_outdated_resolved_thread_is_not_judged_on_a_new_head(mocker):
    _setup(
        mocker,
        [_run(SUCCESS, "2026-11-27T05:46:00Z")],
        _thread(resolved_by="dev", outdated=True),
    )
    assert cli.main(["gate"]) == 0


def test_only_outdated_threads_after_findings_on_this_commit_stay_red(mocker):
    # On one commit a thread goes outdated only when the base changed: nothing
    # on the current code shows the findings were accepted.
    _setup(mocker, NEUTRAL_THEN_CLEAN, _thread(resolved_by="dev", outdated=True))
    assert cli.main(["gate"]) == 1


# --- the pure parts ---------------------------------------------------------


def test_decide_flags_findings_from_an_earlier_run_on_the_head():
    d = decide(["bugbot"], NEUTRAL_THEN_CLEAN)
    assert d.outcome == SUCCESS and d.findings_on_head and not d.findings_only
    d = decide(["bugbot"], NEUTRAL)
    assert d.findings_on_head and d.findings_only
    d = decide(["bugbot"], [_run("cancelled", "2026-11-27T01:13:00Z")])
    assert not d.findings_on_head and not d.findings_only


def test_threads_record_repliers_without_the_bot_suffix():
    _, [f] = threads.parse_review_threads(
        _payload(_thread(resolved_by="Maintainer", replies=["MAINTAINER", "x[bot]"])),
        "cursor[bot]",
    )
    assert f.replied_by("maintainer") and f.replied_by("x")
    _, [g] = threads.parse_review_threads(
        _payload(_thread(replies=["maintainer"], more_comments=True)), "cursor[bot]"
    )
    assert not g.replied_by("maintainer")


def test_the_ledger_accepts_a_maintainers_reply_and_resolve():
    facts = {
        "finding_id": "t",
        "resolved": True,
        "has_machine_evidence": False,
        "resolver_is_eligible_owner": False,
        "resolver_is_author": True,
    }
    assert dispose(FindingFacts(**facts)).disposition is Disposition.HAND_WAVED
    accepted = dispose(FindingFacts(**facts, resolver_is_maintainer_with_reply=True))
    assert accepted.disposition is Disposition.OWNER_ACCEPTED
    assert accepted.is_green


# --- under a repository policy ----------------------------------------------


def _policy_setup(mocker, signals, node, *, policy, roles):
    import test_enforce as te

    post = te._setup(mocker, policy=policy, paths=["src/a.py"], threads=[node])
    mocker.patch.object(github, "signals_for", return_value=signals)
    mocker.patch.object(
        github,
        "repo_role_name",
        side_effect=lambda slug, login: roles.get(login),
    )
    return post


def _policy_thread(resolved_by, replies):
    import test_enforce as te

    node = _thread(
        resolved_by=resolved_by, replies=replies, path="src/a.py", commit=te.HEAD
    )
    node["comments"]["nodes"][0]["author"] = {"login": "cursor"}
    return node


def test_under_a_policy_a_maintainer_author_accepts_their_own_finding(mocker):
    import test_enforce as te

    post = _policy_setup(
        mocker,
        NEUTRAL,
        _policy_thread(te.AUTHOR, [te.AUTHOR]),
        policy=te.POLICY_NO_SECURITY,
        roles={te.AUTHOR: "maintain"},
    )
    assert cli.main(["gate"]) == 0
    assert post.call_args.args[4] == "success"


def test_under_a_policy_a_write_author_still_needs_an_owner(mocker):
    import test_enforce as te

    _policy_setup(
        mocker,
        NEUTRAL,
        _policy_thread(te.AUTHOR, [te.AUTHOR]),
        policy=te.POLICY_NO_SECURITY,
        roles={te.AUTHOR: "write"},
    )
    assert cli.main(["gate"]) == 1


def test_under_a_policy_dismissal_reviewers_are_still_required(mocker):
    # security_dismissal_reviewers is stricter than a maintainer's acceptance.
    import test_enforce as te

    _policy_setup(
        mocker,
        NEUTRAL,
        _policy_thread(te.AUTHOR, [te.AUTHOR]),
        policy=te.POLICY,
        roles={te.AUTHOR: "maintain"},
    )
    assert cli.main(["gate"]) == 1


def test_under_a_policy_a_clean_rerun_keeps_an_authors_dismissal_red(mocker):
    import test_enforce as te

    _policy_setup(
        mocker,
        NEUTRAL_THEN_CLEAN,
        _policy_thread(te.AUTHOR, [te.AUTHOR]),
        policy=te.POLICY_NO_SECURITY,
        roles={te.AUTHOR: "write"},
    )
    assert cli.main(["gate"]) == 1


# --- ways around the sticky rule ---------------------------------------------


def test_a_superseded_run_github_hides_by_default_still_sticks(mocker):
    # GitHub lists only each check's latest run unless asked for filter=all.
    _setup(mocker, [], _thread(resolved_by="dev", replies=["dev"]), author="dev")
    latest = [_run(SUCCESS, "2026-11-27T05:46:00Z")]
    mocker.patch.object(
        github,
        "signals_for",
        side_effect=lambda slug, sha, all_runs=False: (
            NEUTRAL_THEN_CLEAN if all_runs else latest
        ),
    )
    assert cli.main(["gate"]) == 1
    assert "does not clear them" in _gate_comment()


def test_retargeting_does_not_reset_the_sticky_rule(mocker):
    # Findings, retarget away and back, a clean re-run after the base change.
    _setup(
        mocker,
        NEUTRAL_THEN_CLEAN,
        _thread(resolved_by="dev", replies=["dev"]),
        author="dev",
    )
    mocker.patch.object(github, "last_base_change", return_value="2026-11-27T02:00:00Z")
    assert cli.main(["gate"]) == 1


def test_deleted_findings_do_not_pass_a_clean_rerun(mocker):
    _setup(mocker, NEUTRAL_THEN_CLEAN, author="dev")
    assert cli.main(["gate"]) == 1
    assert "AI review findings cannot be found" in _gate_comment()


def test_the_full_history_is_asked_for(mocker):
    gh = mocker.patch.object(github, "gh_json", return_value=[{"check_runs": []}])
    github.signals_for("o/r", "a" * 40, all_runs=True)
    assert gh.call_args.args[0][0].endswith("/check-runs?filter=all")
    github.signals_for("o/r", "a" * 40)
    assert gh.call_args.args[0][0].endswith("/check-runs")


def test_under_a_policy_findings_with_no_thread_left_stay_red(mocker):
    import test_enforce as te

    post = te._setup(mocker, policy=te.POLICY_NO_SECURITY, paths=["src/a.py"])
    mocker.patch.object(github, "signals_for", return_value=NEUTRAL)
    assert cli.main(["gate"]) == 1
    assert post.call_args.args[4] == "failure"


def test_under_a_policy_deleted_findings_do_not_pass_a_clean_rerun(mocker):
    import test_enforce as te

    te._setup(mocker, policy=te.POLICY_NO_SECURITY, paths=["src/a.py"])
    mocker.patch.object(github, "signals_for", return_value=NEUTRAL_THEN_CLEAN)
    assert cli.main(["gate"]) == 1


def test_a_deleted_finding_is_missed_even_beside_an_accepted_one(mocker):
    # Bugbot found two; a maintainer accepted one; the other was deleted.
    two = [
        _run(
            "neutral",
            "2026-11-27T01:13:00Z",
            FOUND_ONE.replace("1 potential issue", "2 potential issues"),
        )
    ]
    _setup(mocker, two, _thread(resolved_by="maintainer", replies=["maintainer"]))
    assert cli.main(["gate"]) == 1
    assert "reported 2 finding(s) on this commit but only 1" in _gate_comment()


def test_a_deleted_finding_is_missed_after_a_clean_rerun(mocker):
    runs = [
        _run(
            "neutral",
            "2026-11-27T01:13:00Z",
            FOUND_ONE.replace("1 potential issue", "2 potential issues"),
        ),
        _run(SUCCESS, "2026-11-27T05:46:00Z"),
    ]
    _setup(mocker, runs, _thread(resolved_by="maintainer", replies=["maintainer"]))
    assert cli.main(["gate"]) == 1


def test_a_thread_from_another_commit_does_not_count_for_this_one(mocker):
    _setup(
        mocker,
        NEUTRAL,
        _thread(resolved_by="maintainer", replies=["maintainer"], commit="0ld"),
    )
    assert cli.main(["gate"]) == 1


def test_a_summary_in_an_unknown_wording_fails_closed(mocker):
    # A red run whose summary the gate cannot read: neither a later clean run
    # nor a maintainer's acceptance can green it.
    runs = [
        _run("neutral", "2026-11-27T01:13:00Z", "Bugbot Review"),
        _run(SUCCESS, "2026-11-27T05:46:00Z"),
    ]
    _setup(mocker, runs, _thread(resolved_by="maintainer", replies=["maintainer"]))
    assert cli.main(["gate"]) == 1
    assert "AI review history cannot be trusted" in _gate_comment()
    _setup(
        mocker,
        [_run("neutral", "2026-11-27T01:13:00Z", "Bugbot timed out.")],
        _thread(resolved_by="maintainer", replies=["maintainer"]),
    )
    assert cli.main(["gate"]) == 1


# Live summaries of Cursor Bugbot check runs (output.summary, "Final Result"
# line), captured from ClickHouse repositories on 2026-11-27.
LIVE_SUMMARIES = {
    "**Final Result:** Bugbot completed review and found 1 potential issue.": 1,
    "**Final Result:** Bugbot completed review and found 2 potential issues.": 2,
    "**Final Result:** Bugbot completed review and found 1 potential issue. "
    "3 previously reported issues remain unresolved.": 1,
    "**Final Result:** Bugbot completed review - no issues found. "
    "1 previously reported issue remain unresolved.": 0,
    "**Final Result:** Bugbot completed review - no issues found. "
    "3 previously reported issues remain unresolved.": 0,
}


@pytest.mark.parametrize("summary,count", list(LIVE_SUMMARIES.items()))
def test_the_live_wordings_are_understood(summary, count):
    got = cli._reported_findings(["bugbot"], [_run("neutral", "t", summary)])
    assert (got.count, got.unknown) == (count, False)


def test_a_clean_run_is_not_read_for_wording():
    got = cli._reported_findings(["bugbot"], [_run(SUCCESS, "t", "anything")])
    assert (got.count, got.unknown, got.runs) == (0, False, 1)


def test_findings_reported_as_failure_stick_too(mocker):
    # Bugbot set to fail on unresolved findings concludes failure.
    _setup(
        mocker,
        [
            _run("failure", "2026-11-27T01:13:00Z", FOUND_ONE),
            _run(SUCCESS, "2026-11-27T05:46:00Z"),
        ],
        _thread(resolved_by="dev", replies=["dev"]),
        author="dev",
    )
    assert cli.main(["gate"]) == 1


def test_an_outdated_thread_on_this_commit_is_still_judged(mocker):
    # Retargeting makes a thread outdated on the same commit.
    two = FOUND_ONE.replace("1 potential issue", "2 potential issues")
    runs = [
        _run("neutral", "2026-11-27T01:13:00Z", two),
        _run(SUCCESS, "2026-11-27T05:46:00Z"),
    ]
    accepted = _thread(resolved_by="maintainer", replies=["maintainer"])
    dismissed = dict(
        _thread(resolved_by="dev", replies=["dev"], outdated=True), id="PRRT_2"
    )
    _setup(mocker, runs, accepted, dismissed)
    assert cli.main(["gate"]) == 1
    # A maintainer's acceptance of it counts, outdated or not: same code.
    accepted_too = dict(
        _thread(resolved_by="maintainer", replies=["maintainer"], outdated=True),
        id="PRRT_2",
    )
    _setup(mocker, runs, accepted, accepted_too)
    assert cli.main(["gate"]) == 0


def test_new_issues_of_later_runs_add_up(mocker):
    runs = [
        _run("neutral", "2026-11-27T01:13:00Z"),
        _run("neutral", "2026-11-27T02:13:00Z"),
    ]
    one = _thread(resolved_by="maintainer", replies=["maintainer"])
    _setup(mocker, runs, one)
    assert cli.main(["gate"]) == 1  # 2 reported, 1 thread
    two = dict(one, id="PRRT_2")
    _setup(mocker, runs, one, two)
    assert cli.main(["gate"]) == 0


def test_a_run_that_started_before_the_pull_request_still_sticks(mocker):
    # The same code was reviewed for another pull request first.
    _setup(
        mocker,
        [
            _run("neutral", "1999-12-31T23:59:00Z"),
            _run(SUCCESS, "2026-11-27T05:46:00Z"),
        ],
        _thread(resolved_by="dev", replies=["dev"]),
        author="dev",
    )
    assert cli.main(["gate"]) == 1


def test_the_full_history_carries_the_summary(mocker):
    run = {
        "app": {"slug": "cursor"},
        "name": "Cursor Bugbot",
        "status": "completed",
        "conclusion": "neutral",
        "output": {"title": "Bugbot Review", "summary": FOUND_ONE},
    }
    mocker.patch.object(github, "gh_json", return_value=[{"check_runs": [run]}])
    [sig] = github.signals_for("o/r", "a" * 40, all_runs=True)
    assert sig.summary == FOUND_ONE
    # The latest-runs read carries it too: a cancelled run is recognised there.
    [sig] = github.signals_for("o/r", "a" * 40)
    assert sig.summary == FOUND_ONE


def test_findings_reported_as_failure_can_be_accepted(mocker):
    # Bugbot set to fail on unresolved findings.
    _setup(
        mocker,
        [_run("failure", "2026-11-27T01:13:00Z", FOUND_ONE)],
        _thread(resolved_by="maintainer", replies=["maintainer"]),
    )
    assert cli.main(["gate"]) == 0


def test_an_errored_red_review_is_not_greened_by_older_threads(mocker):
    # No count, no thread on this commit: an error, not findings. A thread a
    # maintainer accepted on an earlier commit does not green it.
    _setup(
        mocker,
        [_run("failure", "2026-11-27T01:13:00Z", "Bugbot failed to run.")],
        _thread(resolved_by="maintainer", replies=["maintainer"], commit="0ld"),
    )
    assert cli.main(["gate"]) == 1


def test_under_a_policy_the_findings_and_the_policy_judge_one_snapshot(mocker):
    # A thread deleted between two reads cannot pass one check and miss the
    # other: review threads are read once.
    import test_enforce as te

    _policy_setup(
        mocker,
        NEUTRAL_THEN_CLEAN,
        _policy_thread(te.AUTHOR, [te.AUTHOR]),
        policy=te.POLICY_NO_SECURITY,
        roles={te.AUTHOR: "write"},
    )
    assert cli.main(["gate"]) == 1
    assert github.review_threads.call_count == 1  # type: ignore[attr-defined]


# --- the review must be this pull request's, and its history trustworthy ----


def test_findings_accepted_from_before_a_retarget_stay_red(mocker):
    # Opened against a side branch, Bugbot flags the small delta; retargeted to
    # main; a maintainer accepts. The review saw a different change.
    _setup(
        mocker,
        [_run("neutral", "2026-11-27T01:13:00Z")],
        _thread(resolved_by="maintainer", replies=["maintainer"]),
    )
    mocker.patch.object(github, "last_base_change", return_value="2026-11-27T03:00:00Z")
    assert cli.main(["gate"]) == 1
    assert "AI review is from before the base changed" in _gate_comment()


def test_findings_accepted_from_a_review_after_the_retarget_are_green(mocker):
    _setup(
        mocker,
        [_run("neutral", "2026-11-27T04:13:00Z")],
        _thread(resolved_by="maintainer", replies=["maintainer"]),
    )
    mocker.patch.object(github, "last_base_change", return_value="2026-11-27T03:00:00Z")
    assert cli.main(["gate"]) == 0


@pytest.mark.parametrize("runs", [cli.RUN_RETENTION_GUARD, cli.RUN_RETENTION_CAP])
def test_a_history_near_githubs_retention_cap_fails_closed(mocker, runs):
    # GitHub keeps at most 1,000 runs of one check per suite: near that, the
    # run that reported findings may already be gone.
    history = [_run(SUCCESS, "2026-11-27T05:46:00Z")] * runs
    _setup(mocker, history)
    assert cli.main(["gate"]) == 1
    assert "near the 1000 GitHub keeps per check" in _gate_comment()


def test_a_history_below_the_cap_is_trusted(mocker):
    _setup(mocker, [_run(SUCCESS, "2026-11-27T05:46:00Z")] * 10)
    assert cli.main(["gate"]) == 0


def test_an_open_outdated_thread_from_an_earlier_commit_does_not_block(mocker):
    # A new head starts fresh: Bugbot's review of this head reported one
    # finding, which a maintainer accepted; an earlier head's finding, open
    # and since outdated, is that commit's business.
    _setup(
        mocker,
        NEUTRAL,
        _thread(resolved_by="maintainer", replies=["maintainer"]),
        dict(_thread(outdated=True, commit="0ld"), id="PRRT_old"),
    )
    assert cli.main(["gate"]) == 0


def test_an_open_thread_on_this_commit_blocks_even_when_outdated(mocker):
    _setup(
        mocker,
        [
            _run(
                "neutral",
                "t1",
                FOUND_ONE.replace("1 potential issue", "2 potential issues"),
            )
        ],
        _thread(resolved_by="maintainer", replies=["maintainer"]),
        dict(_thread(outdated=True), id="PRRT_2"),
    )
    assert cli.main(["gate"]) == 1


# --- Bugbot cancelled a run before reviewing ---------------------------------

# Live "**Result:**" lines of cancelled Bugbot runs (neutral, no Final Result).
CANCELLED = [
    "Bugbot Analysis Progress (6m 51s elapsed)\n\n"
    "✅ Gathered PR context (16s)\n✅ ❌ Run was cancelled (1s)\n\n"
    "**Result:** Bugbot run cancelled because the base branch changed and the "
    "reviewed diff is no longer current\n\nRequest ID: 105194cd\n",
    "**Result:** Bugbot run cancelled because the review request was cancelled\n",
]


@pytest.mark.parametrize("summary", CANCELLED)
def test_a_cancelled_run_reports_no_findings(summary):
    got = cli._reported_findings(["bugbot"], [_run("neutral", "t", summary)])
    assert (got.count, got.unknown) == (0, False)


@pytest.mark.parametrize("summary", CANCELLED)
def test_a_cancelled_run_then_a_clean_rerun_is_green(mocker, summary):
    _setup(
        mocker,
        [
            _run("neutral", "2026-11-27T01:13:00Z", summary),
            _run(SUCCESS, "2026-11-27T05:46:00Z"),
        ],
    )
    assert cli.main(["gate"]) == 0


@pytest.mark.parametrize("summary", CANCELLED)
def test_a_cancelled_run_then_findings_are_judged_normally(mocker, summary):
    runs = [
        _run("neutral", "2026-11-27T01:13:00Z", summary),
        _run("neutral", "2026-11-27T02:13:00Z"),
    ]
    _setup(mocker, runs, _thread(resolved_by="dev", replies=["dev"]), author="dev")
    assert cli.main(["gate"]) == 1  # the author cannot accept their own
    _setup(mocker, runs, _thread(resolved_by="maintainer", replies=["maintainer"]))
    assert cli.main(["gate"]) == 0


@pytest.mark.parametrize(
    "summary",
    [
        # Cancelled, but for a reason not known: stays not understood.
        "**Result:** Bugbot run cancelled because something else happened\n",
        # The cancel wording, but not on the Result line.
        "Bugbot run cancelled because the review request was cancelled\n",
    ],
)
def test_other_cancellation_wordings_stay_fail_closed(summary):
    assert cli._reported_findings(["bugbot"], [_run("neutral", "t", summary)]).unknown


@pytest.mark.parametrize("summary", CANCELLED)
def test_a_run_cancelled_after_a_retarget_is_not_a_current_review(mocker, summary):
    # Findings on the old base, a maintainer accepted them, the PR was
    # retargeted, and Bugbot's next run was cancelled: it reviewed nothing,
    # so it does not make the old findings current.
    _setup(
        mocker,
        [
            _run("neutral", "2026-11-27T01:13:00Z"),
            _run("neutral", "2026-11-27T04:00:00Z", summary),
        ],
        _thread(resolved_by="maintainer", replies=["maintainer"]),
    )
    mocker.patch.object(github, "last_base_change", return_value="2026-11-27T03:00:00Z")
    assert cli.main(["gate"]) == 1
    assert "AI review is from before the base changed" in _gate_comment()


def test_a_review_after_a_retarget_then_a_cancel_is_current(mocker):
    # A real review of the new base, then a cancelled run: the review counts.
    _setup(
        mocker,
        [
            _run("neutral", "2026-11-27T04:00:00Z"),
            _run("neutral", "2026-11-27T05:00:00Z", CANCELLED[1]),
        ],
        _thread(resolved_by="maintainer", replies=["maintainer"]),
    )
    mocker.patch.object(github, "last_base_change", return_value="2026-11-27T03:00:00Z")
    assert cli.main(["gate"]) == 0


def _latest_and_history(mocker, latest, history):
    mocker.patch.object(
        github,
        "signals_for",
        side_effect=lambda slug, sha, all_runs=False: history if all_runs else latest,
    )


@pytest.mark.parametrize("summary", CANCELLED)
def test_a_cancel_listed_as_the_latest_run_defers_to_the_review_before_it(
    mocker, summary
):
    # GitHub's default listing shows only the cancelled run; the clean review
    # before it is what counts.
    _setup(mocker, [])
    cancel = _run("neutral", "2026-11-27T05:46:00Z", summary)
    clean = _run(SUCCESS, "2026-11-27T05:00:00Z")
    _latest_and_history(mocker, [cancel], [clean, cancel])
    assert cli.main(["gate"]) == 0


@pytest.mark.parametrize("summary", CANCELLED)
def test_only_a_cancelled_run_is_no_review_yet(mocker, summary):
    post = _setup(mocker, [])
    cancel = _run("neutral", "2026-11-27T05:46:00Z", summary)
    _latest_and_history(mocker, [cancel], [cancel])
    assert cli.main(["gate"]) == 1
    assert post.call_args.args[4] != "success"


def test_findings_accepted_then_a_retarget_and_a_cancel_stay_red_until_reviewed(
    mocker,
):
    # Findings before the retarget, a maintainer accepted them, Bugbot's next
    # run was cancelled by the base change: red. A fresh findings run after
    # the change, accepted: green.
    accepted = _thread(resolved_by="maintainer", replies=["maintainer"])
    before = _run("neutral", "2026-11-27T01:13:00Z")
    cancel = _run("neutral", "2026-11-27T04:00:00Z", CANCELLED[0])
    _setup(mocker, [before, cancel], accepted)
    mocker.patch.object(github, "last_base_change", return_value="2026-11-27T03:00:00Z")
    assert cli.main(["gate"]) == 1
    # Bugbot's review of the new base: nothing new, the accepted one is still
    # there (live wording).
    after = _run(
        "neutral",
        "2026-11-27T04:30:00Z",
        "**Final Result:** Bugbot completed review - no issues found. "
        "1 previously reported issue remain unresolved.",
    )
    _setup(mocker, [before, cancel, after], accepted)
    mocker.patch.object(github, "last_base_change", return_value="2026-11-27T03:00:00Z")
    assert cli.main(["gate"]) == 0


@pytest.mark.parametrize("summary", CANCELLED)
def test_all_required_green_skips_a_cancelled_run_like_decide(summary):
    # The approver's proof uses all_required_green: a clean review, then a
    # cancel, is green there as it is for the gate.
    from aigate.gate import all_required_green

    clean = _run(SUCCESS, "2026-11-27T05:00:00Z")
    cancel = _run("neutral", "2026-11-27T05:46:00Z", summary)
    assert all_required_green(["bugbot"], [clean, cancel]) == (True, [])
    assert all_required_green(["bugbot"], [cancel])[0] is False


def test_review_signals_reads_every_run_only_behind_a_cancel(mocker):
    clean = _run(SUCCESS, "2026-11-27T05:00:00Z")
    cancel = _run("neutral", "2026-11-27T05:46:00Z", CANCELLED[0])
    calls = []

    def read(slug, sha, all_runs=False):
        calls.append(all_runs)
        return [clean, cancel] if all_runs else [cancel]

    mocker.patch.object(github, "signals_for", side_effect=read)
    assert github.review_signals("o/r", "a" * 40) == [clean, cancel]
    assert calls == [False, True]
    calls.clear()
    mocker.patch.object(
        github,
        "signals_for",
        side_effect=lambda slug, sha, all_runs=False: calls.append(all_runs) or [clean],
    )
    assert github.review_signals("o/r", "a" * 40) == [clean]
    assert calls == [False]
