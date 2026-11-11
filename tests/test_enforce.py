"""End-to-end tests of the enforcing ``gate`` command with the GitHub layer mocked.

Each test stubs the read-only ``github`` helpers the command calls and asserts
the exit code and the self-posted ``ai-review-gate`` check. Nothing shells out.
"""

from __future__ import annotations

import pytest

from aigate import cli, github
from aigate.gate import SUCCESS, Signal
from aigate.gateconfig import GATE_CONFIG_PATH
from aigate.reviewers import REVIEWERS

# When every test pull request was opened: before any reviewer run.
OPENED = "2000-01-01T00:00:00Z"

BUGBOT = REVIEWERS["bugbot"].app_slug
REVIEWER = cli.DEFAULT_REVIEWER_LOGIN
HEAD = "deadbeef"
AUTHOR = "alice"

# Full .github/ai-gate.yml documents (the `policy=` kwarg below is the whole file).
POLICY = """
policy:
  escalation_paths:
    - paths: ["src/payments/**"]
      reviewers: "@example-org/payments-owners"
  security_dismissal_reviewers: "@example-org/prodsec"
"""
POLICY_NO_SECURITY = """
policy:
  escalation_paths: []
"""
TEAMS = {
    ("example-org", "prodsec"): frozenset({"carol"}),
    ("example-org", "payments-owners"): frozenset({"dave"}),
    ("example-org", "security"): frozenset({"erin"}),
}
CODEOWNERS = "*  @example-org/security\n"


def _thread(
    path: str,
    *,
    resolved: bool,
    outdated: bool = False,
    by: str | None = None,
    author: str = REVIEWER,
) -> dict:
    return {
        "id": f"T_{path}",
        "isResolved": resolved,
        "isOutdated": outdated,
        "path": path,
        "resolvedBy": {"login": by} if by else None,
        "comments": {"nodes": [{"author": {"login": author}}]},
    }


def _threads_payload(threads: list[dict], author: str | None = AUTHOR) -> dict:
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "author": {"login": author} if author else None,
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False},
                        "nodes": threads,
                    },
                }
            }
        }
    }


def _approval(login: str, *, commit: str = HEAD, state: str = "APPROVED") -> dict:
    return {
        "id": 1,
        "user": {"login": login, "type": "User"},
        "state": state,
        "commit_id": commit,
        "submitted_at": "2026-10-19T10:00:00Z",
    }


def _setup(
    mocker,
    *,
    policy: str | None,
    paths: list[str],
    threads: list[dict] | None = None,
    reviews: list[dict] | None = None,
    codeowners: str | None = CODEOWNERS,
    pr_author: str | None = AUTHOR,
    truncated: bool = False,
    roles: dict[str, str] | None = None,
):
    """Stub every GitHub read the gate performs; return the post_check_run stub."""
    roles = roles or {}
    mocker.patch.object(github, "repo_slug", return_value="example-org/r")
    mocker.patch.object(
        github,
        "read_event",
        return_value={
            "pull_request": {
                "number": 1,
                "head": {"sha": HEAD},
                "base": {"ref": "main"},
                "created_at": OPENED,
            }
        },
    )
    # Everyone who approves has write access unless a test says otherwise.
    mocker.patch.object(
        github, "repo_role", side_effect=lambda slug, login: roles.get(login, "write")
    )

    def read_config(slug: str, base_ref: str, path: str) -> str | None:
        assert base_ref == "main"  # everything is read from the base branch
        if path == GATE_CONFIG_PATH:
            return policy  # the whole ai-gate.yml (reviewers default to bugbot)
        if path in github.CODEOWNERS_PATHS:
            return codeowners if path == ".github/CODEOWNERS" else None
        raise AssertionError(f"unexpected config read {path}")

    mocker.patch.object(github, "read_config_text", side_effect=read_config)
    mocker.patch.object(
        github,
        "signals_for",
        return_value=[
            Signal(app_slug=BUGBOT, name="Cursor Bugbot", conclusion=SUCCESS)
        ],
    )
    mocker.patch.object(github, "changed_paths", return_value=paths)
    # One pull request on this head SHA by default; a same-head test overrides it.
    mocker.patch.object(
        github,
        "open_pull_refs_for_sha",
        return_value=[github.PullRef(1, HEAD, "main", OPENED)],
    )
    payload = _threads_payload(threads or [], author=pr_author)
    if truncated:
        payload["data"]["repository"]["pullRequest"]["reviewThreads"]["pageInfo"] = {
            "hasNextPage": True
        }
    mocker.patch.object(github, "review_threads", return_value=payload)
    mocker.patch.object(github, "pull_reviews", return_value=reviews or [])
    # The live PR (re-runs replay the original event) and the gate comment's
    # I/O, so no test shells out to gh.
    mocker.patch.object(
        github, "pull_detail", return_value={"head": {"sha": HEAD}, "draft": False}
    )
    mocker.patch.object(github, "list_issue_comments", return_value=[])
    mocker.patch.object(github, "create_issue_comment")
    mocker.patch.object(github, "update_issue_comment")
    mocker.patch.object(github, "current_actor_login", return_value=None)
    mocker.patch.object(github, "pull_author", return_value=pr_author)

    def members(org: str, team: str) -> frozenset[str]:
        try:
            return TEAMS[(org, team)]
        except KeyError as exc:
            raise github.GitHubError(
                f"gh api orgs/{org}/teams/{team}/members failed"
            ) from exc

    mocker.patch.object(github, "team_members", side_effect=members)
    return mocker.patch.object(github, "post_check_run")


def _posted(post) -> tuple[str, str | None]:
    assert post.called
    return post.call_args.args[3], post.call_args.args[4]


# --- outdated threads, dismissals and sign-offs -------------------------------


def test_outdated_alone_is_not_fixed_so_it_waits(mocker):
    # Outdated only means the anchored code moved, which the author controls
    # (a whitespace edit or rebase). It is not a fix, so the gate waits.
    post = _setup(
        mocker,
        policy=POLICY,
        paths=["src/app.py"],
        threads=[_thread("src/app.py", resolved=False, outdated=True)],
    )
    assert cli.main(["gate"]) == 1
    assert _posted(post) == ("in_progress", None)


def test_outdated_and_author_resolved_still_needs_signoff(mocker):
    # Outdated and author-resolved is a dismissal, not a fix, so it needs the
    # configured security sign-off.
    post = _setup(
        mocker,
        policy=POLICY,
        paths=["src/app.py"],
        threads=[_thread("src/app.py", resolved=True, outdated=True, by=AUTHOR)],
    )
    assert cli.main(["gate"]) == 1
    assert _posted(post) == ("completed", "failure")
    assert "@example-org/prodsec" in post.call_args.args[6]


def test_dismissed_by_author_is_red(mocker):
    # The author's own resolution never clears a dismissal, with or without
    # security_dismissal_reviewers.
    for policy in (POLICY, POLICY_NO_SECURITY):
        post = _setup(
            mocker,
            policy=policy,
            paths=["src/app.py"],
            threads=[_thread("src/app.py", resolved=True, by=AUTHOR)],
        )
        assert cli.main(["gate"]) == 1
        assert _posted(post) == ("completed", "failure")


def test_security_dismissal_without_prodsec_approval_is_red(mocker):
    # bob is a non-author but not in the team the policy names.
    post = _setup(
        mocker,
        policy=POLICY,
        paths=["src/app.py"],
        threads=[_thread("src/app.py", resolved=True, by=AUTHOR)],
        reviews=[_approval("bob")],
    )
    assert cli.main(["gate"]) == 1
    assert _posted(post) == ("completed", "failure")
    assert "@example-org/prodsec" in post.call_args.args[6]


def test_security_dismissal_with_non_author_prodsec_approval_is_green(mocker):
    post = _setup(
        mocker,
        policy=POLICY,
        paths=["src/app.py"],
        threads=[_thread("src/app.py", resolved=True, by=AUTHOR)],
        reviews=[_approval("carol")],
    )
    assert cli.main(["gate"]) == 0
    assert _posted(post) == ("completed", "success")


def test_escalation_path_finding_without_team_review_is_red(mocker):
    # carol clears the dismissal, but the escalation path still needs the
    # payments owners: the two requirements are independent.
    post = _setup(
        mocker,
        policy=POLICY,
        paths=["src/payments/charge.py"],
        threads=[_thread("src/payments/charge.py", resolved=True)],
        reviews=[_approval("carol")],  # a security owner, not payments-owners
    )
    assert cli.main(["gate"]) == 1
    assert _posted(post) == ("completed", "failure")
    assert "@example-org/payments-owners" in post.call_args.args[6]


def test_escalation_path_finding_with_both_reviews_is_green(mocker):
    # Green with both sign-offs: carol for the dismissal, dave for the path.
    post = _setup(
        mocker,
        policy=POLICY,
        paths=["src/payments/charge.py"],
        threads=[_thread("src/payments/charge.py", resolved=True)],
        reviews=[_approval("carol"), _approval("dave")],
    )
    assert cli.main(["gate"]) == 0
    assert _posted(post) == ("completed", "success")


def test_high_risk_tier_with_clean_bugbot_but_no_human_is_red(mocker):
    post = _setup(mocker, policy=POLICY, paths=["infra/aws/main.tf"])
    assert cli.main(["gate"]) == 1
    assert _posted(post) == ("completed", "failure")
    assert "always needs a human" in post.call_args.args[6]


def test_high_risk_tier_with_any_non_author_human_approval_is_green(mocker):
    post = _setup(
        mocker, policy=POLICY, paths=["infra/aws/main.tf"], reviews=[_approval("bob")]
    )
    assert cli.main(["gate"]) == 0
    assert _posted(post) == ("completed", "success")


def test_high_risk_tier_approval_without_write_access_does_not_count(mocker):
    # On a public repository any account can leave an approving review.
    post = _setup(
        mocker,
        policy=POLICY,
        paths=["infra/aws/main.tf"],
        reviews=[_approval("stranger")],
        roles={"stranger": "read"},
    )
    assert cli.main(["gate"]) == 1
    assert _posted(post) == ("completed", "failure")


def test_an_unreadable_role_does_not_count_but_others_still_do(mocker):
    # A failed role lookup for one approver must not abort the policy check:
    # that approval simply does not count, and a writer's approval still does.
    post = _setup(
        mocker,
        policy=POLICY,
        paths=["infra/aws/main.tf"],
        reviews=[_approval("outsider"), _approval("bob")],
    )

    def role(slug, login):
        if login == "outsider":
            raise github.GitHubError("HTTP 404: Not Found")
        return "write"

    mocker.patch.object(github, "repo_role", side_effect=role)
    assert cli.main(["gate"]) == 0
    assert _posted(post) == ("completed", "success")


def test_docs_tier_clean_is_green_without_anyone(mocker):
    post = _setup(mocker, policy=POLICY, paths=["docs/guide.md"])
    assert cli.main(["gate"]) == 0
    assert _posted(post) == ("completed", "success")


def test_no_policy_and_clean_bugbot_is_green_back_compat(mocker):
    # Without a policy, threads, reviews and tiers are not consulted, so even a
    # tier-3 path with an author-dismissed finding passes on a clean review.
    post = _setup(
        mocker,
        policy=None,
        paths=["infra/aws/main.tf"],
        threads=[_thread("infra/aws/main.tf", resolved=True, by=AUTHOR)],
    )
    assert cli.main(["gate"]) == 0
    assert _posted(post) == ("completed", "success")
    assert not github.review_threads.called  # type: ignore[attr-defined]
    assert not github.pull_reviews.called  # type: ignore[attr-defined]


def test_broken_config_fails_closed(mocker):
    # A broken .github/ai-gate.yml: invalid YAML, an empty file, and an unknown
    # top-level key (a policy key written outside the `policy:` block).
    for broken in ("policy: [unclosed\n", "", "escalation_paths: 1\n"):
        post = _setup(mocker, policy=broken, paths=["docs/guide.md"])
        assert cli.main(["gate"]) == 1
        assert _posted(post) == ("completed", "failure")
        assert not github.signals_for.called  # type: ignore[attr-defined]


def test_authors_own_approval_never_satisfies(mocker):
    # The author's approval never counts, even as a member of the named team.
    mocker.patch.dict(TEAMS, {("example-org", "prodsec"): frozenset({"carol", AUTHOR})})
    post = _setup(
        mocker,
        policy=POLICY,
        paths=["infra/aws/main.tf"],
        threads=[_thread("infra/aws/main.tf", resolved=True, by=AUTHOR)],
        reviews=[_approval(AUTHOR)],
    )
    assert cli.main(["gate"]) == 1
    assert _posted(post) == ("completed", "failure")


# --- fail-closed edges ----------------------------------------------------------


def test_stale_approval_on_a_previous_commit_is_red(mocker):
    post = _setup(
        mocker,
        policy=POLICY,
        paths=["infra/aws/main.tf"],
        reviews=[_approval("bob", commit="0ld")],
    )
    assert cli.main(["gate"]) == 1
    assert _posted(post) == ("completed", "failure")


def test_unresolvable_team_fails_closed(mocker):
    # The token cannot read the team (or it does not exist): never treat the
    # team as empty-and-satisfied or as unneeded.
    post = _setup(
        mocker,
        policy="policy:\n  security_dismissal_reviewers: '@example-org/nope'\n",
        paths=["src/app.py"],
        threads=[_thread("src/app.py", resolved=True, by=AUTHOR)],
        reviews=[_approval("bob")],
    )
    assert cli.main(["gate"]) == 1
    assert _posted(post) == ("completed", "failure")
    assert "could not evaluate" in post.call_args.args[6]


def test_truncated_file_list_fails_closed(mocker):
    # >= 3000 changed files means GitHub may have truncated the list, so the
    # risk tier cannot be trusted; fail closed rather than tier it as TIER_0.
    post = _setup(mocker, policy=POLICY, paths=["docs/guide.md"])
    mocker.patch.object(
        github,
        "changed_paths",
        side_effect=github.GitHubError("3000+ files, failing closed"),
    )
    assert cli.main(["gate"]) == 1
    assert _posted(post) == ("completed", "failure")


def test_truncated_thread_list_fails_closed(mocker):
    post = _setup(mocker, policy=POLICY, paths=["docs/guide.md"], truncated=True)
    assert cli.main(["gate"]) == 1
    assert _posted(post) == ("completed", "failure")
    assert "more than 100" in post.call_args.args[6]


def test_unknown_pr_author_fails_closed(mocker):
    post = _setup(mocker, policy=POLICY, paths=["docs/guide.md"], pr_author=None)
    assert cli.main(["gate"]) == 1
    assert _posted(post) == ("completed", "failure")


def test_unresolved_finding_is_pending_not_failure(mocker):
    # Bugbot's check is (stubbed) clean but a finding is still open: wait.
    post = _setup(
        mocker,
        policy=POLICY,
        paths=["src/app.py"],
        threads=[_thread("src/app.py", resolved=False)],
    )
    assert cli.main(["gate"]) == 1
    assert _posted(post) == ("in_progress", None)


def test_bugbot_not_clean_is_decided_before_the_policy(mocker):
    _setup(mocker, policy=POLICY, paths=["docs/guide.md"])
    mocker.patch.object(
        github,
        "signals_for",
        return_value=[
            Signal(app_slug=BUGBOT, name="Cursor Bugbot", conclusion="neutral")
        ],
    )
    enforce = mocker.spy(cli, "_enforce")
    assert cli.main(["gate"]) == 1
    assert not enforce.called


# --- the ledger's CODEOWNERS rule when no security reviewer is configured -------


def test_without_security_reviewers_an_eligible_non_author_owner_can_accept(mocker):
    post = _setup(
        mocker,
        policy=POLICY_NO_SECURITY,
        paths=["src/app.py"],
        threads=[
            _thread("src/app.py", resolved=True, by="erin")
        ],  # an eligible code owner
    )
    assert cli.main(["gate"]) == 0
    assert _posted(post) == ("completed", "success")


def test_without_security_reviewers_a_non_owner_resolver_is_red(mocker):
    post = _setup(
        mocker,
        policy=POLICY_NO_SECURITY,
        paths=["src/app.py"],
        threads=[_thread("src/app.py", resolved=True, by="mallory")],
    )
    assert cli.main(["gate"]) == 1
    assert _posted(post) == ("completed", "failure")


def test_without_security_reviewers_and_no_codeowners_a_dismissal_is_red(mocker):
    post = _setup(
        mocker,
        policy=POLICY_NO_SECURITY,
        paths=["src/app.py"],
        threads=[_thread("src/app.py", resolved=True, by="erin")],
        codeowners=None,
    )
    assert cli.main(["gate"]) == 1
    assert _posted(post) == ("completed", "failure")


def test_graphql_cursor_login_is_recognised_end_to_end(mocker):
    # Live GraphQL returns the reviewer's login as "cursor" (no [bot] suffix).
    # Its thread must still count as a finding, so an author-resolved dismissal
    # without the security sign-off stays red.
    post = _setup(
        mocker,
        policy=POLICY,
        paths=["src/app.py"],
        threads=[_thread("src/app.py", resolved=True, by=AUTHOR, author="cursor")],
    )
    assert cli.main(["gate"]) == 1
    assert _posted(post) == ("completed", "failure")
    assert "@example-org/prodsec" in post.call_args.args[6]


def test_threads_from_other_authors_are_not_findings(mocker):
    post = _setup(
        mocker,
        policy=POLICY,
        paths=["src/app.py"],
        threads=[_thread("src/app.py", resolved=True, by=AUTHOR, author="bob")],
    )
    assert cli.main(["gate"]) == 0
    assert _posted(post) == ("completed", "success")


# --- observe keeps working ----------------------------------------------------------


def test_observe_reports_and_never_fails(mocker, capsys):
    _setup(
        mocker,
        policy=POLICY,
        paths=["infra/aws/main.tf"],
        threads=[_thread("infra/aws/main.tf", resolved=True, by=AUTHOR)],
    )
    mocker.patch.object(
        github, "pull_ref", return_value=github.PullRef(1, HEAD, "main", OPENED)
    )
    assert cli.main(["observe", "--repo", "example-org/r", "--pr", "1"]) == 0
    out = capsys.readouterr().out
    assert "would-be outcome: FAILURE" in out
    assert not github.post_check_run.called  # type: ignore[attr-defined]


def test_observe_with_broken_policy_reports_and_exits_zero(mocker, capsys):
    _setup(mocker, policy="nope: 1\n", paths=["docs/x.md"])
    mocker.patch.object(
        github, "pull_ref", return_value=github.PullRef(1, HEAD, "main", OPENED)
    )
    assert cli.main(["observe", "--repo", "example-org/r", "--pr", "1"]) == 0
    out = capsys.readouterr().out
    assert "configuration error" in out and "would-be outcome: FAILURE" in out


@pytest.mark.parametrize("flag", [[], ["--reviewer-login", "other[bot]"]])
def test_reviewer_login_selects_which_threads_count(mocker, flag):
    post = _setup(
        mocker,
        policy=POLICY,
        paths=["src/app.py"],
        threads=[_thread("src/app.py", resolved=True, by=AUTHOR, author="other[bot]")],
    )
    rc = cli.main(["gate", *flag])
    if flag:
        assert rc == 1 and _posted(post) == ("completed", "failure")
    else:
        assert rc == 0 and _posted(post) == ("completed", "success")


# --- a config the gate cannot READ fails closed with a red check ---------------------


def _config_read_fails_on(mocker, failing_path: str) -> None:
    real = github.read_config_text.side_effect  # type: ignore[attr-defined]

    def read_config(slug: str, base_ref: str, path: str) -> str | None:
        if path == failing_path:
            raise github.GitHubError(f"reading {path} from main failed: HTTP 502")
        return real(slug, base_ref, path)

    mocker.patch.object(github, "read_config_text", side_effect=read_config)


def test_config_read_error_posts_a_red_check(mocker):
    failing = GATE_CONFIG_PATH
    # A transient read error (5xx, rate limit) on any config probe must post a
    # red ai-review-gate, not just exit: a bare exit would leave a prior green
    # check on this head SHA standing as the required status.
    post = _setup(mocker, policy=None, paths=["docs/guide.md"])
    _config_read_fails_on(mocker, failing)
    assert cli.main(["gate"]) == 1
    assert _posted(post) == ("completed", "failure")
    assert "could not read the gate configuration" in post.call_args.args[6]
    assert not github.signals_for.called  # type: ignore[attr-defined]


# --- observe reports the SAME verdict as the enforcing gate --------------------------


def _observe(mocker, capsys) -> str:
    mocker.patch.object(
        github, "pull_ref", return_value=github.PullRef(1, HEAD, "main", OPENED)
    )
    assert cli.main(["observe", "--repo", "example-org/r", "--pr", "1"]) == 0
    assert not github.post_check_run.called  # type: ignore[attr-defined]
    return capsys.readouterr().out


def test_observe_checks_the_ai_review_itself_not_only_the_policy(mocker, capsys):
    # Observe must check the AI review too, not only the policy.
    _setup(mocker, policy=None, paths=["docs/guide.md"])
    mocker.patch.object(
        github,
        "signals_for",
        return_value=[
            Signal(app_slug=BUGBOT, name="Cursor Bugbot", conclusion="neutral")
        ],
    )
    out = _observe(mocker, capsys)
    assert "would-be outcome: FAILURE" in out


def test_observe_without_a_policy_is_the_simple_gate(mocker, capsys):
    # No policy => the simple gate, exactly as enforced: not an empty policy.
    _setup(
        mocker,
        policy=None,
        paths=["infra/aws/main.tf"],
        threads=[_thread("infra/aws/main.tf", resolved=True, by=AUTHOR)],
    )
    out = _observe(mocker, capsys)
    assert "would-be outcome: SUCCESS" in out
    assert not github.review_threads.called  # type: ignore[attr-defined]


def test_observe_read_errors_report_and_exit_zero(mocker, capsys):
    _setup(mocker, policy=None, paths=["docs/guide.md"])
    _config_read_fails_on(mocker, GATE_CONFIG_PATH)
    out = _observe(mocker, capsys)
    assert "would-be outcome: FAILURE" in out


def test_observe_unknown_pull_request_exits_zero(mocker, capsys):
    mocker.patch.object(github, "pull_ref", side_effect=github.GitHubError("404"))
    assert cli.main(["observe", "--repo", "example-org/r", "--pr", "999"]) == 0
    assert "could not look up" in capsys.readouterr().out


# --- shadow mode: same evaluation, never enforces ------------------------------------


def test_shadow_check_can_never_satisfy_the_required_check():
    # GitHub counts `neutral` as passing a required check, so the shadow check
    # must never carry the enforcing name.
    assert cli.SHADOW_CHECK_RUN_NAME != cli.CHECK_RUN_NAME


SCENARIOS = {
    "clean": {"policy": None, "paths": ["docs/guide.md"]},
    "tier3_no_human": {
        "policy": POLICY,
        "paths": ["infra/aws/main.tf"],
        "threads": [_thread("infra/aws/main.tf", resolved=True, by=AUTHOR)],
    },
    "unresolved_finding": {
        "policy": POLICY,
        "paths": ["src/app.py"],
        "threads": [_thread("src/app.py", resolved=False)],
    },
    "broken_config": {"policy": "nope: 1\n", "paths": ["docs/guide.md"]},
}
EXPECTED = {
    "clean": "success",
    "tier3_no_human": "failure",
    "unresolved_finding": "pending",
    "broken_config": "failure",
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_shadow_reports_exactly_the_enforcing_verdict(mocker, name):
    post = _setup(mocker, **SCENARIOS[name])
    enforce_rc = cli.main(["gate"])
    enforce_name, enforce_status, enforce_conclusion = (
        post.call_args.args[2],
        *_posted(post),
    )
    assert enforce_name == cli.CHECK_RUN_NAME

    post = _setup(mocker, **SCENARIOS[name])
    assert cli.main(["gate", "--shadow"]) == 0  # shadow never fails the job
    assert post.call_count == 1
    _slug, sha, check, status, conclusion, title, summary = post.call_args.args
    assert (sha, check) == (HEAD, cli.SHADOW_CHECK_RUN_NAME)
    assert (status, conclusion) == ("completed", "neutral")  # never red, never green
    assert "enforces nothing" in summary

    # Same verdict as enforcing, by construction.
    verdict = EXPECTED[name]
    assert f"would be {verdict.upper()}" in title
    assert enforce_rc == (0 if verdict == "success" else 1)
    expected_check = {
        "success": ("completed", "success"),
        "failure": ("completed", "failure"),
        "pending": ("in_progress", None),
    }[verdict]
    assert (enforce_status, enforce_conclusion) == expected_check


def test_shadow_survives_every_github_failure(mocker):
    post = _setup(mocker, policy=None, paths=["docs/guide.md"])
    _config_read_fails_on(mocker, GATE_CONFIG_PATH)
    post.side_effect = github.GitHubError("checks: write missing")
    assert cli.main(["gate", "--shadow"]) == 0

    mocker.patch.object(github, "resolve_pull", side_effect=github.GitHubError("x"))
    assert cli.main(["gate", "--shadow"]) == 0


# --- escalation path fires on a changed file, not only on a finding -----------


def test_escalation_path_fires_on_a_changed_file_with_no_finding(mocker):
    # A clean AI review does not waive the sign-off an escalation path demands.
    post = _setup(
        mocker,
        policy=POLICY,
        paths=["src/payments/charge.py"],
        threads=[],  # clean review, no findings at all
    )
    assert cli.main(["gate"]) == 1
    assert _posted(post) == ("completed", "failure")
    assert "@example-org/payments-owners" in post.call_args.args[6]
    assert "escalation path" in post.call_args.args[6]


def test_escalation_path_changed_file_passes_with_owner_approval(mocker):
    post = _setup(
        mocker,
        policy=POLICY,
        paths=["src/payments/charge.py"],
        threads=[],
        reviews=[_approval("dave")],  # @example-org/payments-owners
    )
    assert cli.main(["gate"]) == 0
    assert _posted(post) == ("completed", "success")


def test_escalation_path_not_fired_when_no_sensitive_file_changed(mocker):
    # The changed-file escalation must not fire on an unrelated path.
    post = _setup(mocker, policy=POLICY, paths=["src/app.py"], threads=[])
    assert cli.main(["gate"]) == 0
    assert _posted(post) == ("completed", "success")


# --- an owner's acceptance is bound to the current code -----------------------


def test_without_security_reviewers_an_outdated_owner_resolution_is_red(mocker):
    # The owner resolved the thread before its code moved, so the acceptance
    # covers an earlier state and no longer counts. The non-outdated case above
    # is green.
    post = _setup(
        mocker,
        policy=POLICY_NO_SECURITY,
        paths=["src/app.py"],
        threads=[_thread("src/app.py", resolved=True, outdated=True, by="erin")],
    )
    assert cli.main(["gate"]) == 1
    assert _posted(post) == ("completed", "failure")
